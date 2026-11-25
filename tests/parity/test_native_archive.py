"""Check the shim's ustar extraction and bounds without running archive members."""

from __future__ import annotations

import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shim.src.sai_cli.sai_cli import BINARY_LIMIT, ShimError, unpack


def sai_header(size: int) -> bytes:
    member = tarfile.TarInfo("sai")
    member.size = size
    return member.tobuf(format=tarfile.USTAR_FORMAT)


@pytest.mark.parametrize(("record_size", "member_blocks"), [(10_240, 18), (32_256, 61)])
def test_unpack_accepts_standard_record_padding(
    record_size: int, member_blocks: int
) -> None:
    # The second end block starts a new record at each blocking factor.
    block = tarfile.BLOCKSIZE
    content = b"x" * (member_blocks * block - 1)
    tar = sai_header(len(content)) + content + bytes(1 + 2 * block)
    tar += bytes(-len(tar) % record_size)
    output = io.BytesIO()

    digest = unpack(io.BytesIO(gzip.compress(tar)), output)

    assert output.getvalue() == content
    assert digest == hashlib.sha256(content).hexdigest()


def test_unpack_rejects_an_oversized_member_before_copying() -> None:
    archive = io.BytesIO(gzip.compress(sai_header(BINARY_LIMIT + 1)))
    output = io.BytesIO()

    with pytest.raises(ShimError, match="one regular file named sai"):
        unpack(archive, output)

    assert output.getvalue() == b""


@pytest.mark.parametrize(
    "trailer",
    [bytes(32_256), b"\0", b"x" + bytes(tarfile.BLOCKSIZE - 1)],
    ids=["oversized-padding", "partial-block", "nonzero-block"],
)
def test_unpack_rejects_invalid_record_padding(trailer: bytes) -> None:
    block = tarfile.BLOCKSIZE
    content = b"x" * block
    tar = sai_header(len(content)) + content + bytes(2 * block) + trailer

    with pytest.raises(ShimError, match="extra data after sai"):
        unpack(io.BytesIO(gzip.compress(tar)), io.BytesIO())
