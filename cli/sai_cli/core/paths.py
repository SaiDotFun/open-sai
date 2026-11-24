"""The Sai home directory and owner-only directories inside it."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Mapping
from pathlib import Path

PRIVATE_MODE = 0o700


def sai_home(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    return Path(values.get("SAI_HOME", Path.home() / ".sai"))


def mkdir_private(path: Path) -> None:
    """Create a directory while another first run may be repairing a parent."""
    deadline = time.monotonic() + 0.5
    while True:
        try:
            path.mkdir(mode=PRIVATE_MODE)
            return
        except PermissionError:
            # Under a restrictive umask, a concurrent creator can publish
            # a directory before it restores the owner's search permission.
            if os.name == "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def private_directory(
    path: Path,
    *,
    profile_root: bool = False,
    preserve_mode: bool = False,
    only_new: bool = False,
) -> Path:
    """Create PATH with owner-only access and tighten managed directories.

    Repair a newly created ancestor before creating anything below it.
    Existing ancestors and profile roots keep their modes.
    """
    created = False
    try:
        try:
            mkdir_private(path)
        except FileNotFoundError:
            if path.parent == path:
                raise
            private_directory(path.parent, only_new=True)
            mkdir_private(path)
        created = True
    except FileExistsError:
        if only_new:
            return path
    info = path.lstat()
    if (
        profile_root
        and os.name == "nt"
        and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        and path.is_dir()
    ):
        return path
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise OSError(f"{path} is a symlink or reparse point")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"{path} is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"{path} belongs to another user")
    mode = stat.S_IMODE(info.st_mode)
    if (
        os.name == "nt"
        or mode == PRIVATE_MODE
        or (not created and (profile_root or preserve_mode))
    ):
        return path
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except PermissionError:
        # A restrictive umask can leave a new directory unreadable even
        # to its owner. Restore the owner bits on an already private path.
        if info.st_mode & 0o077:
            raise
        path.chmod(PRIVATE_MODE, follow_symlinks=False)
    else:
        try:
            current = os.fstat(fd)
            if not os.path.samestat(info, current):
                raise OSError(f"{path} changed while setting its mode")
            os.fchmod(fd, PRIVATE_MODE)
        finally:
            os.close(fd)
    return path
