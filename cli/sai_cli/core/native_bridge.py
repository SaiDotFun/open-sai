"""Identify the native shim and verify it without opening the user's profile."""

from __future__ import annotations

import json
import os
import platform
import selectors
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

SHIM_MARKER = "sai_cli/_release_source.py"
MAX_REPLY = 64 << 10
INITIALIZE = (
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
    b'{"protocolVersion":"2025-11-25","capabilities":{},'
    b'"clientInfo":{"name":"sai-update","version":"1.0.0"}}}\n'
)
COPY_VERIFIED = """
import shutil
import sys
from sai_cli.sai_cli import verified_binary
with verified_binary() as (_, executable), open(sys.argv[1], "wb") as output:
    executable.seek(0)
    shutil.copyfileobj(executable, output)
"""


def is_shim(wheel: Path) -> bool:
    with zipfile.ZipFile(wheel) as archive:
        return SHIM_MARKER in archive.namelist()


def supported() -> bool:
    return platform.system() in ("Linux", "Darwin") and platform.machine().lower() in (
        "x86_64",
        "amd64",
        "aarch64",
        "arm64",
    )


def mcp_reply(process: subprocess.Popen[bytes], timeout: float) -> bool:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(INITIALIZE)
    process.stdin.flush()
    deadline = time.monotonic() + timeout
    reply = bytearray()
    with selectors.DefaultSelector() as ready:
        ready.register(process.stdout, selectors.EVENT_READ)
        while len(reply) < MAX_REPLY and b"\n" not in reply:
            if not ready.select(max(0, deadline - time.monotonic())):
                return False
            part = os.read(process.stdout.fileno(), MAX_REPLY - len(reply))
            if not part:
                return False
            reply.extend(part)
    if b"\n" not in reply:
        return False
    try:
        message = json.loads(reply.split(b"\n", 1)[0])
        result = message["result"]
        return (
            message["jsonrpc"] == "2.0"
            and message["id"] == 1
            and isinstance(result["protocolVersion"], str)
            and bool(result["protocolVersion"])
            and isinstance(result["serverInfo"]["name"], str)
            and bool(result["serverInfo"]["name"])
            and isinstance(result["capabilities"], dict)
        )
    except (ValueError, KeyError, TypeError):
        return False


def smoke(
    sai: Path, version: str, home: Path, timeout: float, *, version_only: bool = False
) -> str | None:
    env = {**os.environ, "SAI_HOME": str(home), "SAI_LOG": "false", "CI": "1"}
    for args in (["--version"], ["config", "get", "update.auto"]):
        label = f"`sai {' '.join(args)}`"
        try:
            done = subprocess.run(
                [sai, *args],
                cwd=home,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"{label} could not start or finish: {type(error).__name__}"
        if done.returncode:
            return (
                f"{label} failed: {done.stderr[:512].decode(errors='replace').strip()}"
            )
        if args == ["--version"] and done.stdout.strip() != f"sai {version}".encode():
            return f"{label} did not report {version}"
        if version_only:
            return None
    try:
        with (
            tempfile.TemporaryFile() as errors,
            subprocess.Popen(
                [sai, "mcp", "serve"],
                cwd=home,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
            ) as process,
        ):
            try:
                ready = mcp_reply(process, timeout)
                assert process.stdin is not None
                process.stdin.close()
                if ready and process.wait(timeout=timeout) == 0:
                    return None
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            errors.seek(0)
            diagnostic = errors.read(512).decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        diagnostic = type(error).__name__
    return "`sai mcp serve` did not initialize" + (
        f": {diagnostic}" if diagnostic else ""
    )


def keep_recovery(
    python: Path, version: str, home: Path, destination: Path, timeout: float
) -> str | None:
    """Copy the bytes opened and checked by the shim to a tool-local path."""
    env = {**os.environ, "SAI_HOME": str(home), "SAI_LOG": "false", "CI": "1"}
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".sai-recover-")
    path = Path(name)
    try:
        with os.fdopen(fd, "rb") as saved:
            done = subprocess.run(
                [python, "-I", "-c", COPY_VERIFIED, path],
                cwd=home,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if done.returncode != 0:
                return "could not retain the shim's verified recovery binary"
            os.fchmod(saved.fileno(), 0o700)
            os.fsync(saved.fileno())
        if problem := smoke(path, version, home, timeout, version_only=True):
            return f"the recovery binary failed verification: {problem}"
        help_text = subprocess.run(
            [path, "update", "--help"],
            cwd=home,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if help_text.returncode or b"--recover" not in help_text.stdout:
            return "the native release does not support offline uv recovery"
        path.replace(destination)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"could not prepare the recovery binary: {type(error).__name__}"
    finally:
        path.unlink(missing_ok=True)
    return None
