"""Verify and launch the native asset for this wheel's exact version."""

from __future__ import annotations

import gzip
import hashlib
import os
import platform
import re
import signal
import stat
import sys
import tarfile
import tempfile
import zlib
from collections.abc import Generator
from contextlib import contextmanager
from http.client import HTTPException, HTTPResponse
from importlib.metadata import version
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPErrorProcessor, Request, build_opener

REPO = "SaiDotFun/open-sai"
DOWNLOAD_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}
MANIFEST_LIMIT = 64 * 1024
ARCHIVE_LIMIT = 64 * 1024 * 1024
BINARY_LIMIT = 96 * 1024 * 1024
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


def release_source() -> tuple[str, str]:
    fixture = os.environ.get("SAI_NATIVE_RELEASE_BASE")
    if fixture is None:
        return "https://github.com", "shim"
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", fixture)
    if match is None or int(match[1]) > 65535 or not os.environ.get("SAI_HOME"):
        raise ShimError(
            "SAI_NATIVE_RELEASE_BASE requires a loopback origin and an explicit SAI_HOME"
        )
    # Fixture downloads cannot populate the official offline cache.
    return fixture, f"shim-loopback-{match[1]}"


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
        # Account for the file's padding and the two required end blocks.
        # A second member or an extended header would have nonzero bytes.
        padding = source.read(tarfile.RECORDSIZE + 1)
        minimum = -member.size % tarfile.BLOCKSIZE + 2 * tarfile.BLOCKSIZE
        if not minimum <= len(padding) <= tarfile.RECORDSIZE or any(padding):
            raise ShimError("The native archive contains extra data after sai")
    return digest.hexdigest()


def private_directory(path: Path, *, home: bool = False) -> Path:
    path.mkdir(mode=0o700, parents=home, exist_ok=True)
    info = path.lstat()
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


def trusted_home(path: Path) -> Path:
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
def cache_lock(path: Path) -> Generator[None]:
    import fcntl

    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
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


def cached_binary(cache: Path) -> tuple[Path, BinaryIO] | None:
    receipt = cache / "verified"
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
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            return None
        check_directory_acl(folder)
        binary = folder / "sai"
        fd = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        file = os.fdopen(fd, "rb")
        verified = False
        try:
            check_acl(fd)
            verified = (
                is_private_file(os.fstat(fd), 0o700, BINARY_LIMIT)
                and hashlib.file_digest(file, "sha256").hexdigest() == binary_digest
            )
            if verified:
                return binary, file
        finally:
            if not verified:
                file.close()
    except (FileNotFoundError, UnicodeError):
        pass
    return None


def publish(path: Path, source: BinaryIO, mode: int) -> None:
    os.fchmod(source.fileno(), mode)
    source.flush()
    os.fsync(source.fileno())
    os.replace(str(source.name), path)


def install_binary(cache: Path, url: str, name: str, base: str) -> None:
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
                binary = private_directory(cache / digest) / "sai"
                publish(binary, file, 0o700)
            with (staged / "verified").open("wb") as file:
                file.write(f"{digest} {binary_digest}\n".encode("ascii"))
                publish(cache / "verified", file, 0o600)


@contextmanager
def verified_binary() -> Generator[tuple[Path, BinaryIO]]:
    release = release_version(version("sai-cli"))
    target = native_target()
    base, scope = release_source()
    home = Path(os.environ.get("SAI_HOME", Path.home() / ".sai")).absolute()
    cache = private_directory(trusted_home(home), home=True)
    for part in ("bin", scope, release, target):
        cache = private_directory(cache / part)
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
            if os.execve in os.supports_fd:
                os.execve(executable.fileno(), argv, os.environ)
            # macOS uses the canonical path under the trusted directory
            # chain. Retain the verified inode until exec replaces us.
            if not os.path.samestat(os.fstat(executable.fileno()), binary.lstat()):
                raise ShimError("The verified Sai binary changed before launch")
            os.execv(binary, argv)
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
