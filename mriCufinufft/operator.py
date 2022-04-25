"""Provides Operator for MR Image processing on GPU."""

import warnings

import numpy as np
import cupy as cp

from .raw_operator import RawCufinufft
from .utils import is_host_array, is_cuda_array, sizeof_fmt, pin_memory
from .kernels import sense_adj_mono, update_density


class MRICufiNUFFT:
    """MRI Transform operator, build around cufinufft.

    This operator adds density estimation and compensation (preconditioning)
    and multicoil support.

    Parameters
    ----------
    samples: np.ndarray or GPUArray.
        The samples location of shape ``Nsamples x N_dimensions``.
    shape: tuple
        Shape of the image space.
    n_coils: int
        Number of coils.
    density: bool or array
       Density compensation support.
        - If array, use this for density compensation
        - If True, the density compensation will be automatically estimated,
          using the fixed point method.
        - If False, density compensation will not be used.
    smaps: np.ndarray or GPUArray , optional
        - If None: no Smaps wil be used.
        - If np.ndarray: Smaps will be copied on the device,
          according to `smaps_cached`.
        - If GPUArray, the smaps are already cached.
    smaps_cached: bool, default False
        - If False the smaps are copied on device and free at each iterations.
        - If True, the smaps are copied on device and stay on it.
    kwargs :
        Extra kwargs for the raw cufinufft operator


    Notes
    -----
    TODO: Add concurrency for multicoil operations.
    TODO: Add device context for multi gpu support.

    See Also
    --------
    cufinufft.raw_operator.RawCufinufft
    """

    def __init__(self, samples, shape, density=False, n_coils=1, smaps=None,
                 smaps_cached=False, verbose=False, **kwargs):
        self.shape = shape
        self.n_samples = len(samples)
        if is_host_array(samples):
            samples_d = cp.asarray(np.asfortranarray(samples))
        else:
            raise ValueError("Samples should be either a C-ordered ndarray, "
                             "or a GPUArray.")

        # density compensation support
        if density is True:
            self.density_d = MRICufiNUFFT.estimate_density(samples_d, shape)
            self.uses_density = True
        elif is_host_array(density) or is_cuda_array(density):
            if len(density) != len(samples):
                raise ValueError("Density array and samples array should "
                                 "have the same length.")
            self.uses_density = True
            self.density_d = cp.asarray(density)
        else:
            self.density_d = None
            self.uses_density = False

        self._uses_sense = False
        self.smaps_cached = False
        # Smaps support
        if n_coils < 1:
            raise ValueError("n_coils should be ≥ 1")
        self.n_coils = n_coils
        if smaps is not None:
            self._uses_sense = True
            if not(is_host_array(smaps) or is_cuda_array(smaps)):
                print(smaps.flags)
                raise ValueError("Smaps should be either a C-ordered ndarray, "
                                 "or a GPUArray.")
            if smaps_cached:
                if verbose:
                    warnings.warn(f"{sizeof_fmt(smaps.nbytes)} will be used on gpu.")
                self._smaps_d = cp.array(smaps, order='C', copy=False)
                self.smaps_cached = True
            else:
                # allocate device memory
                self._smap_d = cp.empty(shape, dtype=np.complex64)
                self._smaps_pinned = pin_memory(smaps)
                self._smaps = smaps
        else:
            self._uses_sense = False
            self._smaps = None
        # Initialise NUFFT plans
        self.raw_op = RawCufinufft(samples_d, tuple(shape), **kwargs)

        # Usefull data sizes:
        self.img_size = int(
            np.prod(self.shape) * np.dtype(np.complex64).itemsize)
        self.ksp_size = int(self.n_samples * np.dtype(np.complex64).itemsize)

    def op(self, data, ksp_d=None):
        r"""Non Cartesian MRI forward operator.

        Parameters
        ----------
        data: np.ndarray or GPUArray
        The uniform (2D or 3D) data in image space.

        Returns
        -------
        Results array on the same device as data.

        Notes
        -----
        this performs for every coil \ell:
        ..math:: \mathcal{F}\mathcal{S}_\ell x
        """
        # monocoil
        if self.n_coils == 1:
            return self._op_mono(data, ksp_d)
        # sense
        if self.uses_sense:
            return self._op_sense(data, ksp_d)
        # calibrationless, data on device
        return self._op_calibless(data, ksp_d)

    def _op_mono(self, data, ksp_d=None):
        img_d = cp.asarray(data)
        if ksp_d is None:
            ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
        self.__op(img_d, ksp_d)
        if is_cuda_array(data):
            return ksp_d
        return ksp_d.get()

    def _op_sense(self, data, ksp_d=None):
        img_d = cp.asarray(data)
        if is_host_array(data):
            ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
            ksp = np.zeros((self.n_coils, self.n_samples),
                           dtype=np.complex64)
        elif is_cuda_array and ksp_d is None:
            ksp_d = cp.empty((self.n_coils, self.n_samples),
                             dtype=np.complex64)
        coil_img_d = cp.empty(self.shape, dtype=np.complex64)
        for i in range(self.n_coils):
            cp.copyto(coil_img_d, img_d)
            if self.smaps_cached:
                coil_img_d *= self._smaps_d[i]  # sense forward
            else:
                self._smap_d.set(self._smaps[i])
                coil_img_d *= self._smap_d  # sense forward
            if is_host_array(data):
                self.__op(coil_img_d, ksp_d)
                cp.asnumpy(ksp_d, out=ksp[i])
            else:
                self.__op(coil_img_d.data.ptr, ksp_d.data.ptr + i * self.ksp_size)
        if is_cuda_array(data):
            return ksp_d
        return ksp

    def _op_calibless(self, data, ksp_d=None):
        if is_cuda_array(data):
            if ksp_d is None:
                ksp_d = cp.empty((self.n_coils, self.n_samples),
                                 dtype=np.complex64)
            for i in range(self.n_coils):
                self.__op(data.data.ptr + i * self.img_size,
                          ksp_d.data.ptr + i * self.ksp_size)
            return ksp_d
        # calibrationless, data on host
        coil_img_d = cp.empty(self.shape, dtype=np.complex64)
        ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
        ksp = np.zeros((self.n_coils, self.n_samples), dtype=np.complex64)
        for i in range(self.n_coils):
            coil_img_d.set(data[i])
            self.__op(coil_img_d.data.ptr, ksp_d.data.ptr)
            cp.asnumpy(ksp_d, out=ksp[i])
        return ksp

    def __op(self, image_d, coeffs_d):
        # ensure everything is pointers before going to raw level.
        if is_cuda_array(image_d) and is_cuda_array(coeffs_d):
            return self.raw_op.type2(coeffs_d.data.ptr, image_d.data.ptr)
        return self.raw_op.type2(coeffs_d, image_d)

    def adj_op(self, coeffs, img_d=None):
        """Non Cartesian MRI adjoint operator.

        Parameters
        ----------
        coeffs: np.array or GPUArray

        Returns
        -------
        Array in the same memory space of coeffs. (ie on cpu or gpu Memory).
        """
        if self.n_coils == 1:
            return self._adj_op_mono(coeffs, img_d)
        # sense
        if self.uses_sense:
            return self._adj_op_sense(coeffs, img_d)
        # calibrationless
        return self._adj_op_calibless(coeffs, img_d)

    def _adj_op_mono(self, coeffs, img_d=None):
        if img_d is None:
            img_d = cp.empty(self.shape, dtype=np.complex64)
        coil_ksp_d = cp.asarray(coeffs)
        if self.uses_density:
            coil_ksp_d *= self.density_d  # density preconditionning
        self.__adj_op(coil_ksp_d.data.ptr, img_d.data.ptr)
        if is_cuda_array(coeffs):
            return img_d
        return img_d.get()

    def _adj_op_sense(self, coeffs, img_d=None):
        coil_img_d = cp.empty(self.shape, dtype=np.complex64)
        if img_d is None:
            img_d = cp.empty(self.shape, dtype=np.complex64)
        if not is_cuda_array(coeffs) or self.uses_density:
            coil_ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
        for i in range(self.n_coils):
            if self.uses_density:
                if not is_cuda_array(coeffs):
                    coil_ksp_d = cp.array(coeffs[i], copy=True)
                    coil_ksp_d *= self.density_d  # density preconditionning
                else:
                    cp.copyto(coil_ksp_d, coeffs[i])
                self.__adj_op(coil_ksp_d.data.ptr, coil_img_d.data.ptr)
            else:
                if not is_cuda_array(coeffs):
                    coil_ksp_d.set(coeffs[i])
                    self.__adj_op(coil_ksp_d.data.ptr, coil_img_d.data.ptr)
                else:
                    self.__adj_op(coeffs.data.ptr + i * self.ksp_size, coil_img_d.data.ptr)
            if self.smaps_cached:
                sense_adj_mono(img_d,
                               coil_img_d,
                               self._smaps_d[i])
            else:
                self._smap_d.set(self._smaps[i])
                sense_adj_mono(img_d,
                               coil_img_d,
                               self._smap_d)
        if is_cuda_array(coeffs):
            return img_d
        return img_d.get()

    def _adj_op_calibless(self, coeffs, img_d=None):
        if is_cuda_array(coeffs):
            if img_d is None:
                img_d = cp.empty((self.n_coils, *self.shape),
                                 dtype=np.complex64)
            coil_ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
            for i in range(self.n_coils):
                if self.uses_density:
                    coil_ksp_d.set(coeffs[i])
                    coil_ksp_d *= self.density_d
                    self.__adj_op(coil_ksp_d.data.ptr,
                                  img_d.data.ptr + i * self.img_size)
                else:
                    self.__adj_op(coeffs.data.ptr + i * self.ksp_size,
                                  img_d.data.ptr + i * self.img_size)
            return img_d
        # calibrationless, data on host
        img = np.zeros((self.n_coils, *self.shape), dtype=np.complex64)
        coil_img_d = cp.empty(self.shape, dtype=np.complex64)
        coil_ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
        for i in range(self.n_coils):
            coil_ksp_d.set(coeffs[i])
            if self.uses_density:
                coil_ksp_d *= self.density_d
            self.__adj_op(coil_ksp_d.data.ptr, coil_img_d.data.ptr)
            cp.asnumpy(coil_img_d, out=img[i])
        return img

    def __adj_op(self, coeffs_d, image_d):
        if not isinstance(coeffs_d, int):
            return self.raw_op.type1(coeffs_d.data.ptr, image_d.data.ptr)
        return self.raw_op.type1(coeffs_d, image_d)

    def data_consistency(self, image_data, obs_data):
        """Compute the gradient estimation directly on gpu.

        This mixes the op and adj_op method to perform F_adj(F(x-y))
        on a per coil basis. By doing the computation coil wise,
        it uses less memory than the naive call to adj_op(op(x)-y)

        Parameters
        ----------
        image: array
            Image on which the gradient operation will be evaluated.
            N_coil x Image shape is not using sense.
        obs_data: array
            Observed data.
        """
        if self.n_coils == 1:
            return self._data_consistency_mono(image_data, obs_data)
        if self.uses_sense:
            raise NotImplementedError("Data consistency with smaps is still inconsistent.")
            return self._data_consistency_sense(image_data, obs_data)
        return self._data_consistency_calibless(image_data, obs_data)

    def _data_consistency_mono(self, image_data, obs_data):
        img_d = cp.array(image_data, copy=True)
        obs_d = cp.asarray(obs_data)
        ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
        self.__op(img_d, ksp_d)
        ksp_d -= obs_d
        self.__adj_op(ksp_d, img_d)
        if is_cuda_array(image_data):
            return img_d
        return img_d.get()

    def _data_consistency_sense(self, image_data, obs_data):
        img_d = cp.array(image_data, copy=True)
        coil_img_d = cp.empty(self.shape, dtype=np.complex64)
        coil_ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
        if not is_cuda_array(obs_data):
            coil_obs_data = cp.empty(self.n_samples, dtype=np.complex64)
            obs_data_pinned = pin_memory(obs_data)
        for i in range(self.n_coils):
            cp.copyto(coil_img_d, img_d)
            if self.smaps_cached:
                coil_img_d *= self._smaps_d[i]
            else:
                self._smap_d = cp.asarray(self._smaps[i])
#                self._smap_d.set(self._smaps[i])
                coil_img_d *= self._smap_d
            self.__op(coil_img_d.data.ptr, coil_ksp_d.data.ptr + i * self.ksp_size)
            if not is_cuda_array(obs_data):
                coil_obs_data = cp.asarray(obs_data_pinned[i])
                coil_ksp_d -= coil_obs_data
            else:
                coil_ksp_d -= obs_data[i]
            if self.uses_density:
                coil_ksp_d *= self.density_d
            self.__adj_op(coil_ksp_d.data.ptr, coil_img_d.data.ptr)
            if self.smaps_cached:
                sense_adj_mono(img_d, coil_img_d, self._smaps_d[i])
            else:
                sense_adj_mono(img_d, coil_img_d, self._smap_d)

        if not is_cuda_array(obs_data):
            del obs_data_pinned
        if is_cuda_array(image_data):
            return img_d
        return img_d.get()

    def _data_consistency_calibless(self, image_data, obs_data):
        if is_cuda_array(image_data):
            img_d = cp.empty((self.n_coils, *self.shape), dtype=np.complex64)
            ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
            for i in range(self.n_coils):
                self.__op(image_data.data.ptr + i * self.img_size,
                          ksp_d.data.ptr)
                ksp_d -= obs_data[i]
                if self.uses_density:
                    ksp_d *= self.density_d
                self.__adj_op(ksp_d.data.ptr,
                              img_d.data.ptr + i * self.img_size)
            return img_d

        img_d = cp.empty(self.shape, dtype=np.complex64)
        img = np.zeros((self.n_coils, *self.shape), dtype=np.complex64)
        ksp_d = cp.empty(self.n_samples, dtype=np.complex64)
        obs_d = cp.empty(self.n_samples, dtype=np.complex64)
        for i in range(self.n_coils):
            img_d.set(image_data[i])
            obs_d.set(obs_data[i])
            self.__op(img_d.data.ptr, ksp_d.data.ptr)
            ksp_d -= obs_d
            if self.uses_density:
                ksp_d *= self.density_d
            self.__adj_op(ksp_d.data.ptr, img_d.data.ptr)
            cp.asnumpy(img_d, out=img[i])
        return img

    def get_device_memory_size(self, verbose=False):
        """Get the size in bytes of allocated device memory for this object."""
        device_mem = 0
        if verbose:
            mem_table = []
        for attr in dir(self):
            val = getattr(self, attr)
            if is_cuda_array(val):
                if verbose:
                    mem_table.append((attr, val.size * val.dtype.itemsize))
                device_mem += val.size * val.dtype.itemsize
        if verbose:
            mem_table = sorted(mem_table, key=lambda x: x[1])
            for attr in mem_table:
                print(f"{attr:15} {attr.shape}: {sizeof_fmt(val)}")
        return device_mem

    @property
    def uses_sense(self):
        """Return True if the transform uses the SENSE method, else False."""
        return self._uses_sense

    @property
    def eps(self):
        """Return the underlying precision parameter."""
        return self.raw_op.eps
    
    @classmethod
    def estimate_density(cls, samples, shape, n_iter=10, **kwargs):
        """Estimate the density compensation array."""
        oper = cls(samples, shape, density=False, **kwargs)

        density = cp.ones(len(samples), dtype=np.complex64)
        update = cp.empty_like(density)
        img = cp.empty(shape, dtype=np.complex64)
        for _ in range(n_iter):
            oper.__adj_op(density, img)
            oper.__op(img, update)
            update_density(density, update)
        return density.real

    def __repr__(self):
        """Return info about the MRICufiNUFFT Object."""
        return ("MRICufiNUFFT(\n"
                f"  shape: {self.shape}\n"
                f"  n_coils: {self.n_coils}\n"
                f"  n_samples: {self.n_samples}\n"
                f"  uses_density: {self.uses_density}\n"
                f"  uses_sense: {self.uses_sense}\n"
                f"  smaps_cached: {self.smaps_cached}\n"
                f"  reuse_plans: {self.raw_op.reuse_plans}\n"
                f"  eps:{self.raw_op.eps:.0e}\n"
                ")"
                )
