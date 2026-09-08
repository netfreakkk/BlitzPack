# BlitzPack ⚡

**Intelligent, high-throughput parallel compression engine producing seekable `.blitz` archives.**

BlitzPack combines directory-order read streaming, chunked parallel compression, and solid bundling for small files. The result is a seekable archive format with end-to-end xxHash-64 integrity verification that significantly outperforms traditional archivers on real-world multi-core workloads.

---

## Features

- **Two-Tier Scheduling** — Files at or above the 4 MB chunk boundary are split into independently addressable chunks; everything smaller is packed in directory-traversal order into ~4 MB solid blocks (up to 256 files each), which compresses far better than the same files handled individually. No manual tuning required.
- **Producer-Consumer Pipeline** — Reader threads pull *read groups* — one solid bundle, or every consecutive chunk of a single large file — so each large file is streamed through one open handle instead of being scattered across threads. Compression runs in parallel via zstandard, which releases the Python GIL.
- **Seekable Archive Format** — Each chunk is independently addressable via an embedded seek table, enabling random-access extraction without decompressing the whole archive.
- **Integrity Verification** — Every chunk is checksummed with xxHash-64 at compression time and verified on extraction. `blitzpack verify` checks the whole-archive digest over the contiguous chunk-data region in a single sequential read; `--deep` additionally decompresses and checksums every chunk, including any not referenced by the manifest.
- **Pure Python** — Core library and CLI run anywhere Python 3.10+ does, with no native build step. The GUI is developed and tested on Windows (it uses DWM Mica for translucent backdrop effects, degrading gracefully elsewhere).
- **Desktop GUI** — Dual-mode file manager and archive browser with drag-and-drop, compression profiles, and live progress with speed/ETA.
- **Rich CLI** — `compress`, `decompress`, `list`, `verify`, and `analyze` with progress bars and archive inspection.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│  N Readers  │────▶│  Bounded Queue   │────▶│  M Workers   │
│ (read-group)│     │  (JobPayload)    │     │  (zstd+xxh)  │
└─────────────┘     └──────────────────┘     └──────┬───────┘
                                                    │
                                             ┌──────▼───────┐
                                             │  Main Thread │
                                             │  Sequential  │
                                             │  Archive     │
                                             │  Writer      │
                                             └──────────────┘
```

1. **Producers (Readers):** N threads pull read groups off a queue. A group is either one solid bundle or every consecutive chunk of a single large file, streamed sequentially through one open handle. Groups themselves are read concurrently to keep the device queue busy.
2. **Consumers (Workers):** M threads pull raw payloads, compress via `zstandard` (GIL released), and compute `xxHash-64` digests.
3. **Writer (Main Thread):** Reorders completed chunks by index and appends them sequentially to the archive, keeping the on-disk chunk region contiguous so the whole-archive digest stays a simple range hash.

## Installation

### Standalone Windows Executables (No Python Required)

Download the latest prebuilt releases from the [Releases page](https://github.com/netfreakkk/BlitzPack/releases/latest):

* **`blitzpack-gui.exe`** — The desktop application (no console window).
* **`blitzpack.exe`** — High-throughput parallel archiver for scripts and terminal use.

Both are built from source by `.github/workflows/release.yml` on every tagged release.

### From Source (Python)

```bash
# Core library only
pip install .

# With CLI support (rich terminal)
pip install ".[cli]"

# With GUI support
pip install ".[gui]"

# Everything (CLI + GUI + dev tools)
pip install ".[all]"
```

### Docker

```bash
# Build the container image
docker build -t blitzpack .

# Compress a directory
docker run --rm -v $(pwd):/data blitzpack compress /data/my_folder -o /data/my_folder.blitz --level balanced

# Extract an archive
docker run --rm -v $(pwd):/data blitzpack decompress /data/my_folder.blitz -o /data/extracted

# Verify integrity
docker run --rm -v $(pwd):/data blitzpack verify /data/my_folder.blitz
```

## Usage

### CLI

```bash
# Compress a directory (profiles: fast, balanced [default], high, ultra)
blitzpack compress ./my_project -o project.blitz --level balanced --workers 8

# Tune reader threads for a spinning disk (HDD)
blitzpack compress ./my_project -o project.blitz --readers 1

# Extract an archive
blitzpack decompress project.blitz -o ./restored

# Inspect archive contents
blitzpack list project.blitz

# Check archive integrity without extracting (fast: whole-archive digest)
blitzpack verify project.blitz

# Deep integrity check: decompress and checksum every chunk
blitzpack verify project.blitz --deep

# Dry-run scheduling profile
blitzpack analyze ./my_project
```

### GUI

```bash
blitzpack-gui
# Or:
python gui.py
```

### Python API

```python
from blitzpack import compress, decompress, BlitzArchiveReader

# Compress
result = compress("./my_project", "project.blitz", level=3, workers=8)
print(f"Compressed {result.total_files} files: {result.original_size} -> {result.compressed_size} bytes")
print(f"Throughput: {result.throughput_mb_s:.1f} MB/s")

# Extract
result = decompress("project.blitz", "./restored", workers=8)
print(f"Extracted {result.total_files} files in {result.duration_seconds:.1f}s")

# Verify
with open("project.blitz", "rb") as f:
    reader = BlitzArchiveReader(f)
    reader.verify(deep=False)  # Fast range digest
```

## Archive Format (`.blitz`)

```
┌─────────────────────────────────────────────┐
│  Header (32 bytes)  struct "<4sHBIH11sQ"    │
│  ├─ Magic: "BLTZ"                (4 bytes)  │
│  ├─ Version: uint16                         │
│  ├─ Codec: uint8   (0=stored, 1=zstd)       │
│  ├─ Chunk size: uint32                      │
│  ├─ Flags: uint16                           │
│  ├─ Reserved                    (11 bytes)  │
│  └─ Redundant seek table offset: uint64     │
├─────────────────────────────────────────────┤
│  Chunk Data (contiguous, index order)       │
│  ├─ Chunk 0: [zstd frame | raw bytes]       │
│  ├─ Chunk 1: [zstd frame | raw bytes]       │
│  └─ ...                                     │
├─────────────────────────────────────────────┤
│  Seek Table                                 │
│  ├─ Entry count: uint32                     │
│  └─ Per entry (32 bytes) "<QIIQII":         │
│     offset, compressed_size, original_size, │
│     digest, overlap_prefix (reserved, 0),   │
│     flags (bit 0 = stored raw)              │
├─────────────────────────────────────────────┤
│  Manifest (msgpack)                         │
│  └─ Paths, sizes, mtimes, types, symlink    │
│     targets, permissions, chunk mappings    │
├─────────────────────────────────────────────┤
│  Footer (48 bytes)  struct "<QQQQII8s"      │
│  ├─ Seek table offset: uint64               │
│  ├─ Manifest offset: uint64                 │
│  ├─ Total original size: uint64             │
│  ├─ Archive digest: uint64                  │
│  ├─ Entry count: uint32                     │
│  ├─ Reserved: uint32                        │
│  └─ Magic: "BLTZEND\0"           (8 bytes)  │
└─────────────────────────────────────────────┘
```

The chunk-data region is contiguous and written in chunk-index order, starting at byte 32. The archive digest is the xxHash-64 of that entire region, so integrity can be checked with one sequential read and no decompression.

## Project Structure

```
blitzpack/              Core compression library
├── __init__.py         Public API exports
├── analyzer.py         File discovery, metadata, chunked/bundled classification
├── archive_format.py   Binary archive reader/writer + integrity verification
├── checksum.py         xxHash-64 digest computation & range hashing
├── compressor.py       Producer-consumer parallel compression pipeline
├── constants.py        Shared chunk/bundle tuning constants
├── decompressor.py     Producer-consumer parallel extraction pipeline
├── scheduler.py        Directory-order job scheduling with solid bundling
└── utils.py            Path sanitization, progress types, formatting

cli.py                  Rich CLI entry point (compress, decompress, list, verify, analyze)
gui.py                  macOS Fluent Tkinter GUI with performance monitoring
tests/                  Pytest roundtrip & integrity test suite
pyproject.toml          Package metadata and dependencies
```

## License

[MIT](LICENSE)
