"""BlitzPack: High-performance intelligent parallel compression engine."""

from .analyzer import FileAnalyzer, FileEntry, FileManifest
from .compressor import compress
from .decompressor import decompress
from .archive_format import ArchiveFormatError, BlitzArchiveReader, BlitzArchiveWriter
from .multi_decompress import (
    SUPPORTED_ARCHIVE_EXTENSIONS,
    extract_archive,
    get_archive_format,
    inspect_archive,
    is_supported_archive,
    test_archive,
)

__version__ = "1.0.0"
__all__ = [
    "ArchiveFormatError",
    "FileAnalyzer",
    "FileEntry",
    "FileManifest",
    "compress",
    "decompress",
    "BlitzArchiveReader",
    "BlitzArchiveWriter",
    "is_supported_archive",
    "get_archive_format",
    "inspect_archive",
    "test_archive",
    "extract_archive",
    "SUPPORTED_ARCHIVE_EXTENSIONS",
]
