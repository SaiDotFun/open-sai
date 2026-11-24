"""Block socket families that can reach the host across a network namespace."""

from __future__ import annotations

import ctypes
import errno
import os
import resource
import socket
import sys

SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_KILL_PROCESS = 0x80000000
SCMP_ACT_ERRNO = 0x00050000
SCMP_CMP_MASKED_EQ = 7
INT_ARGUMENT_MASK = 0xFFFFFFFF
SOCKET_TYPE_MASK = 0xF


class ArgumentComparison(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


def block_host_sockets() -> None:
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(ArgumentComparison),
    ]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None

    ctx = lib.seccomp_init(SCMP_ACT_ALLOW)
    if not ctx:
        raise ValueError("seccomp filter allocation failed")
    try:
        socket_call = lib.seccomp_syscall_resolve_name(b"socket")
        if socket_call < 0:
            raise ValueError("socket syscall is unavailable")
        for family in (socket.AF_UNIX, socket.AF_VSOCK):
            # A socketpair remains available for intra-process notifications.
            address_family = ArgumentComparison(
                0, SCMP_CMP_MASKED_EQ, INT_ARGUMENT_MASK, family
            )
            if lib.seccomp_rule_add_array(
                ctx, SCMP_ACT_KILL_PROCESS, socket_call, 1, ctypes.byref(address_family)
            ):
                raise ValueError("seccomp socket rule could not be configured")
        # Datagram socketpairs can send to an unrelated pathname peer.
        socketpair = lib.seccomp_syscall_resolve_name(b"socketpair")
        datagram = ArgumentComparison(
            1, SCMP_CMP_MASKED_EQ, SOCKET_TYPE_MASK, socket.SOCK_DGRAM
        )
        if socketpair < 0 or lib.seccomp_rule_add_array(
            ctx, SCMP_ACT_KILL_PROCESS, socketpair, 1, ctypes.byref(datagram)
        ):
            raise ValueError("seccomp socketpair rule could not be configured")
        # Socket creation through io_uring bypasses socket(2) syscall rules.
        # ENOSYS makes clients fall back to regular syscalls.
        io_uring = lib.seccomp_syscall_resolve_name(b"io_uring_setup")
        if io_uring >= 0 and lib.seccomp_rule_add_array(
            ctx, SCMP_ACT_ERRNO | errno.ENOSYS, io_uring, 0, None
        ):
            raise ValueError("seccomp io_uring rule could not be configured")
        if lib.seccomp_load(ctx):
            raise ValueError("seccomp filter could not be loaded")
    finally:
        lib.seccomp_release(ctx)


if __name__ == "__main__":
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        block_host_sockets()
    except (OSError, ValueError):
        sys.exit("Parity could not block host sockets using libseccomp")
    os.execv(sys.argv[1], sys.argv[1:])
