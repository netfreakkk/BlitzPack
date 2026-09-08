"""Checksum calculation using xxHash-64 for chunk and archive integrity verification."""

from pathlib import Path
from typing import BinaryIO

import xxhash

DEFAULT_BLOCK_SIZE = 4 * 1024 * 1024


def compute_digest(data: bytes) -> int:
    """Compute the 64-bit xxHash-64 digest of an in-memory buffer."""
    return xxhash.xxh64_intdigest(data)


def compute_file_digest(file_path: Path, block_size: int = DEFAULT_BLOCK_SIZE) -> int:
    """Compute the xxHash-64 digest of an entire file using streaming reads."""
    hasher = xxhash.xxh64()
    with open(file_path, "rb") as f:
        while block := f.read(block_size):
            hasher.update(block)
    return hasher.intdigest()


def compute_stream_digest(
    stream: BinaryIO, start: int, end: int, block_size: int = DEFAULT_BLOCK_SIZE
) -> int:
    """Compute the xxHash-64 digest of the byte range [start, end) of an open stream.

    Used to verify the whole-archive digest: the writer hashes every compressed frame
    in write order, and because frames are written contiguously in chunk-index order,
    that is byte-for-byte the same as hashing this range.
    """
    if end < start:
        raise ValueError(f"Invalid range: end ({end}) < start ({start})")

    hasher = xxhash.xxh64()
    stream.seek(start)
    remaining = end - start
    while remaining > 0:
        block = stream.read(min(block_size, remaining))
        if not block:
            raise EOFError(f"Unexpected EOF with {remaining} bytes left to hash")
        hasher.update(block)
        remaining -= len(block)
    return hasher.intdigest()


class IncrementalHasher:
    """Streaming xxHash-64 hasher for tracking archive content integrity."""

    __slots__ = ("_hasher",)

    def __init__(self) -> None:
        self._hasher = xxhash.xxh64()

    def update(self, data: bytes) -> None:
        self._hasher.update(data)

    def digest(self) -> int:
        return self._hasher.intdigest()
