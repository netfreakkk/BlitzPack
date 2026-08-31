"""End-to-end roundtrip test suite validating byte-for-byte fidelity and edge cases."""

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import pytest

from blitzpack import compress, decompress, BlitzArchiveReader
from blitzpack.checksum import compute_file_digest


def _hash_directory(root: Path) -> dict[str, str]:
    """Compute sha256 hashes of all files in a directory mapped by relative posix path."""
    hashes = {}
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            fp = Path(dirpath) / fname
            rel = fp.relative_to(root).as_posix()
            h = hashlib.sha256()
            with open(fp, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
            hashes[rel] = h.hexdigest()
    return hashes


def test_empty_files_and_directories(tmp_path: Path):
    """Test handling of 0-byte files and nested empty directories."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "empty.txt").write_bytes(b"")
    (src / "nested" / "deep").mkdir(parents=True)
    (src / "nested" / "deep" / "empty_inner.dat").write_bytes(b"")

    archive = tmp_path / "empty_test.blitz"
    dest = tmp_path / "extracted"

    compress(src, archive, level=3, workers=2)
    assert archive.exists() and archive.stat().st_size > 0

    decompress(archive, dest, workers=2)

    assert (dest / "empty.txt").exists()
    assert (dest / "empty.txt").stat().st_size == 0
    assert (dest / "nested" / "deep" / "empty_inner.dat").exists()
    assert (dest / "nested" / "deep" / "empty_inner.dat").stat().st_size == 0


def test_small_file_bundling_fidelity(tmp_path: Path):
    """Test that thousands of small files bundled by extension unpack byte-identical."""
    src = tmp_path / "bundle_src"
    src.mkdir()

    # Create 500 small files with different extensions
    for i in range(200):
        (src / f"code_{i}.py").write_text(f"def test_{i}():\n    return {i * 42}\n", encoding="utf-8")
    for i in range(150):
        (src / f"data_{i}.json").write_text(f'{{"index": {i}, "name": "item_{i}"}}\n', encoding="utf-8")
    for i in range(150):
        (src / f"doc_{i}.md").write_text(f"# Header {i}\nSample documentation paragraph.\n", encoding="utf-8")

    archive = tmp_path / "small_bundle.blitz"
    dest = tmp_path / "extracted_bundle"

    original_hashes = _hash_directory(src)

    compress(src, archive, level=3, workers=4)
    decompress(archive, dest, workers=4)

    extracted_hashes = _hash_directory(dest)
    assert original_hashes == extracted_hashes


def test_large_file_chunking_with_overlap(tmp_path: Path):
    """Test large file (>16 MB) splitting across 4MB chunks and seamless reassembly."""
    src = tmp_path / "large_src"
    src.mkdir()

    # Generate a 22 MB compressible file
    large_file = src / "large_dataset.bin"
    chunk_pattern = os.urandom(8192) * 512  # 4 MB semi-compressible pattern
    with open(large_file, "wb") as f:
        for _ in range(5):
            f.write(chunk_pattern)
        f.write(os.urandom(2 * 1024 * 1024))  # 2 MB remainder

    archive = tmp_path / "large_chunk.blitz"
    dest = tmp_path / "extracted_large"

    orig_digest = compute_file_digest(large_file)

    compress(src, archive, level=3, workers=4)
    decompress(archive, dest, workers=4)

    extracted_file = dest / "large_dataset.bin"
    assert extracted_file.exists()
    assert extracted_file.stat().st_size == large_file.stat().st_size
    assert compute_file_digest(extracted_file) == orig_digest


def test_mixed_hierarchy_with_unicode_and_spaces(tmp_path: Path):
    """Test mixed hierarchy with spaces, unicode characters, and various file types."""
    src = tmp_path / "mixed_tree"
    src.mkdir()

    (src / "Folder With Spaces").mkdir()
    (src / "Folder With Spaces" / "test space file.txt").write_bytes(b"content inside spaced path")
    (src / "unicode_测试_🚀.json").write_text('{"unicode": "🚀⚡"}', encoding="utf-8")
    (src / "normal.py").write_bytes(b"import sys\nprint('hello')\n")

    archive = tmp_path / "mixed.blitz"
    dest = tmp_path / "extracted_mixed"

    orig_hashes = _hash_directory(src)

    compress(src, archive, level=3, workers=4)
    decompress(archive, dest, workers=4)

    extracted_hashes = _hash_directory(dest)
    assert orig_hashes == extracted_hashes


def test_archive_reader_and_redundant_recovery(tmp_path: Path):
    """Test that archive reader correctly parses seek table and header redundant offset."""
    src = tmp_path / "meta_src"
    src.mkdir()
    (src / "f1.txt").write_bytes(b"content one")
    (src / "f2.txt").write_bytes(b"content two")

    archive = tmp_path / "meta.blitz"
    compress(src, archive, level=3)

    with open(archive, "rb") as f:
        reader = BlitzArchiveReader(f)
        assert reader.header.version == 1
        assert reader.header.redundant_seek_offset > 0
        assert len(reader.seek_entries) >= 1
        assert len(reader.manifest) == 2
