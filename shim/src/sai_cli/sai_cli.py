"""Verify and launch the native asset for this wheel's exact version."""

from __future__ import annotations

import errno
import gzip
import hashlib
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import zlib
from collections.abc import Generator
from contextlib import contextmanager, suppress
from http.client import HTTPException, HTTPResponse
from importlib.metadata import version
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPErrorProcessor, Request, build_opener

from ._release_source import release_source

REPO = "SaiDotFun/open-sai"
DOWNLOAD_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}
MANIFEST_LIMIT = 64 * 1024
ARCHIVE_LIMIT = 64 * 1024 * 1024
BINARY_LIMIT = 96 * 1024 * 1024
# POSIX pax -x ustar permits physical records of up to 63 logical blocks.
USTAR_RECORD_LIMIT = 63 * tarfile.BLOCKSIZE
CHUNK = 128 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:rc(0|[1-9][0-9]*))?"
)
TARGET_OS = {"Linux": "unknown-linux-gnu", "Darwin": "apple-darwin"}
TARGET_CPU = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}


class ShimError(RuntimeError):
    pass


def release_version(wheel_version: str) -> str:
    match = VERSION.fullmatch(wheel_version)
    if match is None:
        raise ShimError("The wheel version has no matching Sai release tag")
    stable = ".".join(match.groups()[:3])
    return stable if match[4] is None else f"{stable}-rc.{match[4]}"


def native_target() -> str:
    system = TARGET_OS.get(platform.system())
    machine = TARGET_CPU.get(platform.machine().lower())
    if system is None or machine is None:
        raise ShimError("Native Sai supports Linux and macOS on x86_64 and arm64")
    return f"{machine}-{system}"


def archive_name(release: str, target: str) -> str:
    return f"sai-{release}-{target}.tar.gz"


class AssetResponses(HTTPErrorProcessor):
    # Handle redirects ourselves so urllib cannot drain an unbounded redirect body.
    def http_response(self, request: Request, response: HTTPResponse) -> HTTPResponse:
        return response

    https_response = http_response


@contextmanager
def asset_response(url: str, base: str) -> Generator[HTTPResponse]:
    opener = build_opener(AssetResponses())
    for _ in range(5):
        request = Request(url, headers={"User-Agent": "sai-cli native shim"})
        with opener.open(request, timeout=10) as response:
            if response.status == 200:
                yield response
                return
            if response.status not in (301, 302, 303, 307, 308):
                raise ShimError(f"The release server returned HTTP {response.status}")
            location = response.headers.get("Location")
            if not location:
                raise ShimError("The release server redirected without a destination")
            url = urljoin(url, location)
            target = urlsplit(url)
            origin = f"{target.scheme}://{target.netloc}"
            if base == "https://github.com":
                allowed = target.scheme == "https" and target.netloc in DOWNLOAD_HOSTS
            else:
                allowed = origin == base
            if not allowed:
                raise ShimError(
                    "The release server redirected outside its download hosts"
                )
    raise ShimError("The release server redirected too many times")


def download_timeout(_signum: int, _frame: object) -> None:
    raise ShimError("The native release download timed out")


def download(url: str, base: str, output: BinaryIO, limit: int) -> str:
    previous = signal.signal(signal.SIGALRM, download_timeout)
    signal.setitimer(signal.ITIMER_REAL, 45)
    digest = hashlib.sha256()
    try:
        with asset_response(url, base) as response:
            length = response.headers.get("Content-Length")
            if length is not None and (
                not length.isascii()
                or not length.isdecimal()
                or len(length) > 12
                or int(length) > limit
            ):
                raise ShimError("The native release asset exceeds its size limit")
            size = 0
            while chunk := response.read(min(CHUNK, limit - size + 1)):
                size += len(chunk)
                if size > limit:
                    raise ShimError("The native release asset exceeds its size limit")
                output.write(chunk)
                digest.update(chunk)
            if length is not None and size != int(length):
                raise ShimError("The release server returned an incomplete asset")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return digest.hexdigest()


def release_digest(manifest: bytes, name: str) -> str:
    matches = []
    for line in manifest.decode("ascii").splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and fields[1].removeprefix("*") == name:
            matches.append(fields[0])
    if len(matches) != 1 or SHA256.fullmatch(matches[0]) is None:
        raise ShimError(
            "SHA256SUMS must contain one checksum for the exact native archive"
        )
    return matches[0]


def unpack(archive: BinaryIO, output: BinaryIO) -> str:
    digest = hashlib.sha256()
    with gzip.GzipFile(fileobj=archive) as source:
        member = tarfile.TarInfo.frombuf(
            source.read(tarfile.BLOCKSIZE), "utf-8", "strict"
        )
        if (
            member.name != "sai"
            or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
            or member.linkname
            or not 0 < member.size <= BINARY_LIMIT
        ):
            raise ShimError(
                "The native archive must contain one regular file named sai"
            )
        remaining = member.size
        while remaining:
            chunk = source.read(min(CHUNK, remaining))
            if not chunk:
                raise ShimError(
                    "The native archive ended before the binary was complete"
                )
            output.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        # The member alignment and two end blocks can cross a record boundary.
        # After the end blocks, up to one record minus one block may remain.
        minimum = -member.size % tarfile.BLOCKSIZE + 2 * tarfile.BLOCKSIZE
        limit = minimum + USTAR_RECORD_LIMIT - tarfile.BLOCKSIZE
        padding = source.read(limit + 1)
        if (
            not minimum <= len(padding) <= limit
            or (len(padding) - minimum) % tarfile.BLOCKSIZE
            or any(padding)
        ):
            raise ShimError("The native archive contains extra data after sai")
    return digest.hexdigest()


def private_directory(path: Path, *, home: bool = False, create: bool = True) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=home, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if create:
            raise
        if path.parent.is_dir() and not os.access(path.parent, os.W_OK | os.X_OK):
            raise ShimError("The Sai cache parent directory is not writable")
        return path
    unwanted = 0o022 if home else 0o077
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & unwanted
    ):
        raise ShimError(
            f"The Sai cache requires an owner-controlled directory named {path.name}"
        )
    check_directory_acl(path)
    return path


def check_acl(fd: int) -> None:
    if sys.platform == "darwin":
        from ._darwin_acl import has_mutating_acl

        if has_mutating_acl(fd):
            raise ShimError("The Sai cache path has an ACL permitting changes")


def check_directory_acl(path: Path) -> None:
    if sys.platform == "darwin":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            check_acl(fd)
        finally:
            os.close(fd)


def trusted_home(path: Path, *, create: bool = True) -> Path:
    # Canonicalize the parent once, for aliases such as /var on macOS.
    # The opened directory chain below must keep that canonical path stable.
    path = path.parent.resolve() / path.name
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        check_acl(fd)
        # User namespaces can report the host root as an unmapped UID.
        trusted_owners = {0, os.getuid(), os.fstat(fd).st_uid}
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    if not os.access(".", os.W_OK | os.X_OK, dir_fd=fd):
                        raise ShimError("The SAI_HOME parent directory is not writable")
                    return path
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            check_acl(fd)
            info = os.fstat(fd)
            mode = info.st_mode
            # In a trusted sticky directory, only the entry owner or the
            # directory owner can rename an entry. Check both owners.
            if info.st_uid not in trusted_owners or (
                mode & 0o022 and not mode & stat.S_ISVTX
            ):
                raise ShimError("SAI_HOME has an untrusted ancestor directory")
    finally:
        os.close(fd)
    return path


def is_private_file(info: os.stat_result, mode: int, limit: int) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == mode
        and info.st_nlink == 1
        and 0 < info.st_size <= limit
    )


@contextmanager
def cache_lock(path: Path, *, create: bool = True) -> Generator[None]:
    import fcntl

    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
    except FileNotFoundError:
        if create:
            raise
        check_cache_writable(path.parent)
        yield
        return
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ShimError("The Sai cache lock must be an owner-only regular file")
        check_acl(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def repair_digest_mode(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        check_acl(fd)
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise ShimError(
                "The native archive digest directory must belong to the current user"
            )
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def digest_directory_inaccessible(info: os.stat_result) -> bool:
    required = stat.S_IRUSR | stat.S_IXUSR
    return info.st_mode & required != required


def executable_digest(file: BinaryIO) -> str:
    # The installer can run this check with Python 3.10.
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(CHUNK), b""):
        digest.update(chunk)
    return digest.hexdigest()


def cached_binary(cache: Path, *, repair: bool = True) -> tuple[Path, BinaryIO] | None:
    receipt = cache / "verified"
    check_file_destination(receipt)
    try:
        fd = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as contents:
            if not is_private_file(os.fstat(fd), 0o600, 130):
                return None
            check_acl(fd)
            fields = contents.read(131).split()
        if len(fields) != 2 or any(
            SHA256.fullmatch(d.decode("ascii")) is None for d in fields
        ):
            return None
        archive_digest, binary_digest = (d.decode("ascii") for d in fields)
        folder = cache / archive_digest
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return None
        if digest_directory_inaccessible(info):
            return None
        if repair and stat.S_IMODE(info.st_mode) != 0o700:
            repair_digest_mode(folder)
        else:
            check_directory_acl(folder)
        binary = folder / "sai"
        check_file_destination(binary)
        fd = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        file = os.fdopen(fd, "rb")
        verified = False
        try:
            check_acl(fd)
            verified = (
                is_private_file(os.fstat(fd), 0o700, BINARY_LIMIT)
                and executable_digest(file) == binary_digest
            )
            if verified:
                return binary, file
        finally:
            if not verified:
                file.close()
    except (FileNotFoundError, UnicodeError):
        pass
    except OSError as exc:
        # O_NOFOLLOW rejected a cache entry. A verified publish replaces
        # the link itself without reading or writing its destination.
        if exc.errno != errno.ELOOP:
            raise
    return None


def publish(path: Path, source: BinaryIO, mode: int) -> None:
    os.fchmod(source.fileno(), mode)
    source.flush()
    os.fsync(source.fileno())
    os.replace(str(source.name), path)


def check_cache_writable(path: Path) -> None:
    if path.is_dir() and not os.access(path, os.W_OK | os.X_OK):
        raise ShimError("The Sai native cache directory is not writable")


def check_file_destination(path: Path) -> None:
    with suppress(FileNotFoundError):
        if stat.S_ISDIR(path.lstat().st_mode):
            raise ShimError(
                f"The Sai native cache cannot replace the {path.name} directory"
            )


def check_cache_destination(cache: Path, digest: str) -> None:
    check_cache_writable(cache)
    folder = cache / digest
    try:
        info = folder.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ShimError(
            "The native archive digest path must be an owner-controlled directory"
        )
    if digest_directory_inaccessible(info):
        raise ShimError("The native archive digest directory is inaccessible")
    check_directory_acl(folder)
    check_file_destination(folder / "sai")


def install_binary(cache: Path, url: str, name: str, base: str) -> None:
    check_file_destination(cache / "verified")
    with tempfile.TemporaryFile(dir=cache) as manifest:
        download(f"{url}/SHA256SUMS", base, manifest, MANIFEST_LIMIT)
        manifest.seek(0)
        expected = release_digest(manifest.read(), name)
    with tempfile.TemporaryFile(dir=cache) as archive:
        digest = download(f"{url}/{name}", base, archive, ARCHIVE_LIMIT)
        if digest != expected:
            raise ShimError("The native archive failed SHA-256 verification")
        archive.seek(0)
        # The staging directory keeps interrupted writes away from cache readers.
        with tempfile.TemporaryDirectory(dir=cache) as staging:
            staged = Path(staging)
            with (staged / "sai").open("wb") as file:
                binary_digest = unpack(archive, file)
                folder = cache / digest
                with suppress(FileNotFoundError):
                    info = folder.lstat()
                    if (
                        stat.S_ISDIR(info.st_mode)
                        and info.st_uid == os.getuid()
                        and digest_directory_inaccessible(info)
                    ):
                        if os.chmod in os.supports_follow_symlinks:
                            # Restore the owned directory's mode so we can
                            # open it and check its ACL before publishing.
                            os.chmod(folder, 0o700, follow_symlinks=False)
                        else:
                            # Keep the rename in the locked parent. Moving
                            # between parents can need write access to '..'.
                            with tempfile.TemporaryDirectory(dir=cache) as discarded:
                                folder.replace(discarded)
                folder.mkdir(mode=0o700, exist_ok=True)
                repair_digest_mode(folder)
                binary = folder / "sai"
                check_file_destination(binary)
                publish(binary, file, 0o700)
            with (staged / "verified").open("wb") as file:
                file.write(f"{digest} {binary_digest}\n".encode("ascii"))
                publish(cache / "verified", file, 0o600)


def parent_directory() -> Path | None:
    parent = os.getppid()
    if sys.platform == "linux":
        with suppress(OSError):
            return Path(os.readlink(f"/proc/{parent}/cwd"))
    elif sys.platform == "darwin":
        with suppress(OSError, subprocess.SubprocessError):
            result = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-a", "-p", str(parent), "-d", "cwd", "-Fn0"],
                capture_output=True,
                check=True,
                timeout=5,
            )
            for field in result.stdout.split(b"\0"):
                if field.startswith(b"n/"):
                    return Path(os.fsdecode(field[1:]))
    return None


def updater_holds_cache_lock(home: Path) -> bool:
    import fcntl

    try:
        fd = os.open(
            home / "update-cache.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def profile_home() -> Path:
    home = Path(os.environ.get("SAI_HOME", Path.home() / ".sai"))
    if (
        not home.is_absolute()
        and sys.argv[1:] == ["--version"]
        and Path.cwd() == Path("/")
    ):
        # The v0.1.1 updater runs its version probes from / with the
        # original environment. Resolve the profile from the updater's cwd.
        parent = parent_directory()
        if parent is not None and parent.is_absolute():
            candidate = Path(os.path.abspath(parent / home))
            if updater_holds_cache_lock(candidate):
                home = candidate
                os.environ["SAI_HOME"] = str(home)
    return Path(os.path.abspath(home))


def profile_cache(release: str, target: str, scope: str, *, create: bool) -> Path:
    home = trusted_home(profile_home(), create=create)
    cache = private_directory(home, home=True, create=create)
    for part in ("bin", scope, release, target):
        cache = private_directory(cache / part, create=create)
    return cache


def check_install_profile(release: str, digest: str) -> int:
    """Check the real profile from the verified wheel before uv replaces Sai."""
    try:
        _, scope = release_source()
        cache = profile_cache(release, native_target(), scope, create=False)
        with cache_lock(cache / ".lock", create=False):
            cached = cached_binary(cache, repair=False)
            if cached is None:
                check_cache_destination(cache, digest)
            else:
                _, executable = cached
                executable.close()
    except RuntimeError as error:
        print(error)
    except OSError as error:
        print(
            f"Could not check the Sai profile or native cache ({type(error).__name__})"
        )
    else:
        return 0
    return 1


@contextmanager
def verified_binary() -> Generator[tuple[Path, BinaryIO]]:
    release = release_version(version("sai-cli"))
    target = native_target()
    try:
        base, scope = release_source()
    except RuntimeError as exc:
        raise ShimError(str(exc)) from exc
    cache = profile_cache(release, target, scope, create=True)
    with cache_lock(cache / ".lock"):
        cached = cached_binary(cache)
        if cached is None:
            url = f"{base}/{REPO}/releases/download/sai-cli/v{release}"
            install_binary(cache, url, archive_name(release, target), base)
            cached = cached_binary(cache)
            if cached is None:
                raise ShimError("Could not reopen the verified Sai binary")
        path, executable = cached
        with executable:
            yield path, executable


def main() -> int:
    try:
        with verified_binary() as (binary, executable):
            argv = [str(binary), *sys.argv[1:]]
            env = {**os.environ, "SAI_NATIVE_UV_PREFIX": sys.prefix}
            # Match subprocess's restore_signals behavior. CPython ignores
            # these signals on startup, and exec preserves ignored handlers.
            for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
                if signum := getattr(signal, name, None):
                    signal.signal(signum, signal.SIG_DFL)
            if os.execve in os.supports_fd:
                os.execve(executable.fileno(), argv, env)
            # macOS uses the canonical path under the trusted directory
            # chain. Retain the verified inode until exec replaces us.
            if not os.path.samestat(os.fstat(executable.fileno()), binary.lstat()):
                raise ShimError("The verified Sai binary changed before launch")
            os.execve(binary, argv, env)
    except ShimError as exc:
        print(f"sai: {exc}", file=sys.stderr)
    except (
        OSError,
        ValueError,
        EOFError,
        HTTPException,
        tarfile.HeaderError,
        zlib.error,
    ) as exc:
        print(
            f"sai: could not prepare or run the verified native binary ({type(exc).__name__})",
            file=sys.stderr,
        )
    return 1
