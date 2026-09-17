"""Shared tuning constants for classification and scheduling.

These live in their own module because both `analyzer` and `scheduler` need them
and `scheduler` already imports `analyzer`.
"""

CHUNK_SIZE = 4 * 1024 * 1024        # files >= this are split into chunk jobs
BUNDLE_TARGET = 4 * 1024 * 1024     # solid bundles are flushed once they reach this
MAX_BUNDLE_MEMBERS = 1024           # cap on files packed into a single bundle (reduced NTFS syscall overhead)

# Cold cache acceleration constants
INLINE_MAX_SIZE = 64 * 1024         # files <= this are eligible for single-pass in-flight read
MAX_PRELOAD_BUDGET = 128 * 1024 * 1024  # 128 MB RAM ceiling for preloaded small files
IO_WORKERS = 32                     # worker pool size for concurrent disk I/O prefetching

