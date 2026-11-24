"""The Sai home directory and owner-only directories inside it."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

PRIVATE_MODE = 0o700


def sai_home(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    return Path(values.get("SAI_HOME", Path.home() / ".sai"))


def private_directory(path: Path) -> Path:
    """Create PATH with owner-only access and tighten it if already owned.

    Call this on SAI_HOME before creating a directory inside it, since
    pathlib applies the requested mode only to the last component.
    """
    try:
        path.mkdir(mode=PRIVATE_MODE, parents=True)
    except FileExistsError:
        pass
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise OSError(f"{path} is a symlink or reparse point")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"{path} is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"{path} belongs to another user")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) != PRIVATE_MODE:
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
                if not os.path.samestat(info, os.fstat(fd)):
                    raise OSError(f"{path} changed while setting its mode")
                os.fchmod(fd, PRIVATE_MODE)
            finally:
                os.close(fd)
    return path
