"""Binary specification, writer, and reader for the BlitzPack (.blitz) seekable archive format."""

from dataclasses import dataclass
import io
import os
from pathlib import Path
import struct
from typing import BinaryIO, Dict, Generator, List, Optional
import msgpack

from .checksum import IncrementalHasher

HEADER_MAGIC = b"BLTZ"
FOOTER_MAGIC = b"BLTZEND\x00"
CURRENT_VERSION = 1
CODEC_ZSTD = 1
CODEC_STORED = 0           # raw, uncompressed chunk
FLAG_STORED = 1            # bit 0 of SeekEntry.flags: chunk is stored raw

HEADER_STRUCT = struct.Struct("<4sHBIH11sQ")    # 32 bytes
SEEK_ENTRY_STRUCT = struct.Struct("<QIIQII")    # 32 bytes
FOOTER_STRUCT = struct.Struct("<QQQQII8s")      # 48 bytes


class ArchiveFormatError(Exception):
    """Raised when an archive file is corrupted, truncated, or has an invalid structure."""


@dataclass(slots=True)
class SeekEntry:
    offset: int
    compressed_size: int
    original_size: int
    digest: int
    overlap_prefix_size: int = 0
    flags: int = 0


@dataclass(slots=True)
class ManifestEntry:
    path: str
    size: int
    mtime: float
    file_type: int        # 0: file, 1: dir, 2: symlink
    symlink_target: Optional[str]
    permissions: int
    win_attrs: int
    start_chunk: int
    start_offset: int
    end_chunk: int
    end_offset: int


@dataclass(slots=True)
class ArchiveHeader:
    version: int
    codec: int
    chunk_size: int
    flags: int
    redundant_seek_offset: int


@dataclass(slots=True)
class ArchiveFooter:
    seek_table_offset: int
    manifest_offset: int
    total_original_size: int
    archive_digest: int
    file_count: int


class BlitzArchiveWriter:
    """Sequential archive writer: single buffered stream for fast OS write coalescing,
    with an order-independent archive digest computed at finalize."""

    def __init__(self, stream: BinaryIO, default_chunk_size: int = 4 * 1024 * 1024) -> None:
        self._stream = stream
        self._default_chunk_size = default_chunk_size
        self._seek_entries: Dict[int, SeekEntry] = {}
        self._archive_hasher = IncrementalHasher()

        # Reserve 32 bytes for the header (backpatched at finalize)
        self._stream.write(b"\x00" * HEADER_STRUCT.size)

    def write_chunk(
        self,
        chunk_index: int,
        compressed_bytes: bytes,
        original_size: int,
        digest: int,
        overlap_prefix_size: int = 0,
        flags: int = 0
    ) -> int:
        """Append a compressed frame sequentially — fast OS-buffered sequential write."""
        chunk_offset = self._stream.tell()
        self._stream.write(compressed_bytes)
        self._archive_hasher.update(compressed_bytes)
        self._seek_entries[chunk_index] = SeekEntry(
            offset=chunk_offset,
            compressed_size=len(compressed_bytes),
            original_size=original_size,
            digest=digest,
            overlap_prefix_size=overlap_prefix_size,
            flags=flags
        )
        return chunk_index

    def finalize(
        self,
        manifest_entries: List[ManifestEntry],
        total_original_size: int
    ) -> None:
        """Write seek table, manifest, footer; compute archive digest; backpatch header."""
        # Record where chunk data ends (= start of seek table)
        seek_table_offset = self._stream.tell()

        # 1. Write seek table: [entry_count (uint32)] + [entry_0, entry_1, ...]
        max_idx = max(self._seek_entries.keys()) if self._seek_entries else -1
        self._stream.write(struct.pack("<I", max_idx + 1))
        for i in range(max_idx + 1):
            entry = self._seek_entries.get(i)
            if not entry:
                raise ArchiveFormatError(f"Missing chunk index {i} during finalize")
            self._stream.write(SEEK_ENTRY_STRUCT.pack(
                entry.offset,
                entry.compressed_size,
                entry.original_size,
                entry.digest,
                entry.overlap_prefix_size,
                entry.flags
            ))

        # 2. Write manifest
        manifest_offset = self._stream.tell()
        packed_manifest = msgpack.packb([
            {
                "p": e.path,
                "s": e.size,
                "t": e.mtime,
                "y": e.file_type,
                "l": e.symlink_target,
                "m": e.permissions,
                "w": e.win_attrs,
                "sc": e.start_chunk,
                "so": e.start_offset,
                "ec": e.end_chunk,
                "eo": e.end_offset,
            }
            for e in manifest_entries
        ], use_bin_type=True)
        self._stream.write(packed_manifest)

        # 3. Use incrementally computed archive digest (zero disk re-read!)
        archive_digest = self._archive_hasher.digest()

        # 4. Write footer
        self._stream.write(FOOTER_STRUCT.pack(
            seek_table_offset,
            manifest_offset,
            total_original_size,
            archive_digest,
            len(manifest_entries),
            0,
            FOOTER_MAGIC
        ))

        # 5. Backpatch header
        self._stream.seek(0)
        self._stream.write(HEADER_STRUCT.pack(
            HEADER_MAGIC,
            CURRENT_VERSION,
            CODEC_ZSTD,
            self._default_chunk_size,
            0,
            b"\x00" * 11,
            seek_table_offset
        ))
        self._stream.flush()



class BlitzArchiveReader:
    """Reads header, footer, seek table, and manifest from a .blitz archive."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.header: ArchiveHeader
        self.footer: ArchiveFooter
        self.seek_entries: List[SeekEntry] = []
        self.manifest: List[ManifestEntry] = []

        self._read_structure()

    def _read_structure(self) -> None:
        self._stream.seek(0, os.SEEK_END)
        file_len = self._stream.tell()
        if file_len < HEADER_STRUCT.size + FOOTER_STRUCT.size:
            raise ArchiveFormatError(f"File too small to be a valid .blitz archive ({file_len} bytes)")

        # 1. Parse Header
        self._stream.seek(0)
        header_raw = self._stream.read(HEADER_STRUCT.size)
        magic, ver, codec, chunk_sz, flags, _, redundant_seek = HEADER_STRUCT.unpack(header_raw)
        if magic != HEADER_MAGIC:
            raise ArchiveFormatError(f"Invalid archive header magic: {magic!r}")
        if ver > CURRENT_VERSION:
            raise ArchiveFormatError(f"Unsupported archive version: {ver} (max supported: {CURRENT_VERSION})")
        if codec != CODEC_ZSTD:
            raise ArchiveFormatError(f"Unsupported compression codec: {codec}")

        self.header = ArchiveHeader(
            version=ver,
            codec=codec,
            chunk_size=chunk_sz,
            flags=flags,
            redundant_seek_offset=redundant_seek
        )

        # 2. Parse Footer
        self._stream.seek(file_len - FOOTER_STRUCT.size)
        footer_raw = self._stream.read(FOOTER_STRUCT.size)
        seek_off, man_off, orig_sz, digest, count, _, end_magic = FOOTER_STRUCT.unpack(footer_raw)

        if end_magic != FOOTER_MAGIC:
            # Fall back to redundant header offset if available
            if self.header.redundant_seek_offset > 0 and self.header.redundant_seek_offset < file_len:
                seek_off = self.header.redundant_seek_offset
                man_off = 0
                orig_sz = 0
                digest = 0
                count = 0
            else:
                raise ArchiveFormatError("Corrupt or missing archive footer")

        self.footer = ArchiveFooter(
            seek_table_offset=seek_off,
            manifest_offset=man_off,
            total_original_size=orig_sz,
            archive_digest=digest,
            file_count=count
        )

        # 3. Parse Seek Table
        self._stream.seek(self.footer.seek_table_offset)
        raw_count = self._stream.read(4)
        if len(raw_count) < 4:
            raise ArchiveFormatError("Failed to read seek table count")
        num_entries = struct.unpack("<I", raw_count)[0]

        for _ in range(num_entries):
            entry_raw = self._stream.read(SEEK_ENTRY_STRUCT.size)
            if len(entry_raw) < SEEK_ENTRY_STRUCT.size:
                raise ArchiveFormatError("Truncated seek table entry")
            off, c_sz, o_sz, c_digest, p_sz, flg = SEEK_ENTRY_STRUCT.unpack(entry_raw)
            self.seek_entries.append(SeekEntry(
                offset=off,
                compressed_size=c_sz,
                original_size=o_sz,
                digest=c_digest,
                overlap_prefix_size=p_sz,
                flags=flg
            ))

        # 4. Parse Manifest
        if self.footer.manifest_offset > 0:
            self._stream.seek(self.footer.manifest_offset)
            # Read through to the footer
            manifest_len = (file_len - FOOTER_STRUCT.size) - self.footer.manifest_offset
            manifest_data = self._stream.read(manifest_len)
            raw_manifest = msgpack.unpackb(manifest_data, raw=False)

            for d in raw_manifest:
                self.manifest.append(ManifestEntry(
                    path=d["p"],
                    size=d["s"],
                    mtime=d["t"],
                    file_type=d["y"],
                    symlink_target=d.get("l"),
                    permissions=d["m"],
                    win_attrs=d["w"],
                    start_chunk=d["sc"],
                    start_offset=d["so"],
                    end_chunk=d["ec"],
                    end_offset=d["eo"],
                ))

    def read_raw_chunk(self, chunk_index: int) -> tuple[SeekEntry, bytes]:
        """Read the exact compressed payload bytes for a specific chunk index."""
        if chunk_index < 0 or chunk_index >= len(self.seek_entries):
            raise IndexError(f"Chunk index {chunk_index} out of bounds (total: {len(self.seek_entries)})")
        entry = self.seek_entries[chunk_index]
        self._stream.seek(entry.offset)
        chunk_data = self._stream.read(entry.compressed_size)
        if len(chunk_data) != entry.compressed_size:
            raise ArchiveFormatError(f"Unexpected EOF reading chunk {chunk_index}")
        return entry, chunk_data
