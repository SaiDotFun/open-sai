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


def home_mode_repairable(path: Path, info: os.stat_result) -> bool:
    """Recognize a dedicated profile without treating ownership as consent."""
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & (stat.S_ISGID | stat.S_ISVTX | 0o022)
    ):
        return False
    if Path(os.path.abspath(path)) == Path.home() / ".sai":
        return True
    managed = {
        "trader",
        "logs",
        "config.toml",
        ".config.lock",
        "cache",
        "update.json",
        "update.lock",
        "update-cache.lock",
    }
    trader = path / "trader"
    try:
        if any(entry.name not in managed for entry in path.iterdir()):
            return False
        trader_info = trader.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISDIR(trader_info.st_mode)
        or trader_info.st_uid != info.st_uid
        or trader_info.st_mode & 0o022
    ):
        return False
    for name in ("sessions.json", "session.json", "intents.sqlite3"):
        try:
            marker = (trader / name).lstat()
        except OSError:
            continue
        if stat.S_ISREG(marker.st_mode) and marker.st_uid == info.st_uid:
            return True
    return False


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
    path: Path, *, profile_root: bool = False, only_new: bool = False
) -> Path:
    """Create PATH with owner-only access and tighten existing Sai paths.

    Repair a newly created ancestor before creating anything below it.
    Existing ancestors keep their mode.
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
    if os.name == "nt" or mode == PRIVATE_MODE:
        return path
    if profile_root and not created:
        if not mode & 0o077 or not home_mode_repairable(path, info):
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
            if profile_root and current.st_mode != info.st_mode:
                return path
            target = mode & ~0o077 if profile_root and not created else PRIVATE_MODE
            os.fchmod(fd, target)
        finally:
            os.close(fd)
    return path
