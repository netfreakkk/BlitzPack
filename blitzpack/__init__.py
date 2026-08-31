"""BlitzPack: High-performance intelligent parallel compression engine."""

from .analyzer import FileAnalyzer, FileEntry, FileManifest
from .compressor import compress
from .decompressor import decompress
from .archive_format import BlitzArchiveReader, BlitzArchiveWriter

__version__ = "1.0.0"
__all__ = [
    "FileAnalyzer",
    "FileEntry",
    "FileManifest",
    "compress",
    "decompress",
    "BlitzArchiveReader",
    "BlitzArchiveWriter",
]
