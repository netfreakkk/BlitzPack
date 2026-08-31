"""Checksum calculation using XXH64 for chunk and archive integrity verification.

Uses blitz_core (Rust) if available for zero-copy native speed, falls back to xxhash."""

from pathlib import Path

# Tier 1: Rust native xxHash-64
try:
    from blitz_core import compute_xxh64 as _native_xxh64

    def compute_digest(data: bytes) -> int:
        """Compute 64-bit XXH64 digest (Rust native, zero-copy)."""
        return _native_xxh64(data)

except ImportError:
    import xxhash

    def compute_digest(data: bytes) -> int:
        """Compute 64-bit XXH64 digest (Python xxhash)."""
        return xxhash.xxh64_intdigest(data)


def compute_file_digest(file_path: Path, chunk_size: int = 4 * 1024 * 1024) -> int:
    """Compute XXH64 digest for an entire file using streaming reads."""
    import xxhash
    hasher = xxhash.xxh64()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.intdigest()


class IncrementalHasher:
    """Streaming XXH64 hasher for incrementally tracking archive content integrity."""

    def __init__(self) -> None:
        import xxhash
        self._hasher = xxhash.xxh64()

    def update(self, data: bytes) -> None:
        self._hasher.update(data)

    def digest(self) -> int:
        return self._hasher.intdigest()
