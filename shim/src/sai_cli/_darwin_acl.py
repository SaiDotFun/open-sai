"""Read macOS access-control entries from an open file descriptor."""

import ctypes
import errno
import os

# Apple's sys/acl.h constants. Allow read, search, and synchronize rights.
READ_ONLY = (1 << 1) | (1 << 3) | (1 << 7) | (1 << 9) | (1 << 11) | (1 << 20)
ACL_EXTENDED_DENY = 2
ACL_FIRST_ENTRY = 0
ACL_NEXT_ENTRY = -1

libc = ctypes.CDLL(None, use_errno=True)
libc.acl_get_fd.argtypes = [ctypes.c_int]
libc.acl_get_fd.restype = ctypes.c_void_p
libc.acl_get_entry.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
]
libc.acl_get_tag_type.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
libc.acl_get_permset_mask_np.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint64),
]
libc.acl_free.argtypes = [ctypes.c_void_p]


def has_mutating_acl(fd: int) -> bool:
    acl = libc.acl_get_fd(fd)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return False
        raise OSError(error, os.strerror(error))
    try:
        entry = ctypes.c_void_p()
        tag = ctypes.c_int()
        permissions = ctypes.c_uint64()
        index = ACL_FIRST_ENTRY
        while libc.acl_get_entry(acl, index, ctypes.byref(entry)) == 0:
            if (
                libc.acl_get_tag_type(entry, ctypes.byref(tag)) != 0
                or libc.acl_get_permset_mask_np(entry, ctypes.byref(permissions)) != 0
            ):
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            if tag.value != ACL_EXTENDED_DENY and permissions.value & ~READ_ONLY:
                return True
            index = ACL_NEXT_ENTRY
        error = ctypes.get_errno()
        if error != errno.EINVAL:  # Darwin reports end of iteration as EINVAL.
            raise OSError(error, os.strerror(error))
        return False
    finally:
        libc.acl_free(acl)
