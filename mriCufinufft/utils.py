"""Utils functions."""

import cupy as cp
import numpy as np

def sizeof_fmt(num, suffix="B"):
    """
    Return a number as a XiB format.
    Parameters
    ----------
    num: int
        The number to format
    suffix: str, default "B"
        The unit suffix

    Notes
    -----
    `https://stackoverflow.com/a/1094933`
    """
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"

def is_cuda_array(var):
    """Check if var implement the CUDA Array interface."""
    try:
        return hasattr(var, "__cuda_array_interface__")
    except Exception:
        return False


def is_host_array(var):
    """Check if var is a host contiguous np array."""
    try:
        return isinstance(var, np.ndarray) and var.flags.c_contiguous
    except Exception:
        return False

def pin_memory(array):
    """Create a copy of the array in pinned memory."""
    mem = cp.cuda.alloc_pinned_memory(array.nbytes)
    ret = np.frombuffer(mem, array.dtype, array.size).reshape(array.shape)
    ret[...] = array
    return ret

def check_error(ier, message):
    if ier != 0:
        raise RuntimeError(message)
