"""Regression and integrity test suite ensuring no silent corruption occurs."""

import os
from pathlib import Path
import struct
import subprocess
import sys
import pytest

from blitzpack import BlitzArchiveReader, compress, decompress
from blitzpack.analyzer import FileAnalyzer
from blitzpack.archive_format import ArchiveFormatError, BlitzArchiveWriter, CHUNK_DATA_START, FLAG_STORED, ManifestEntry
from blitzpack.checksum import compute_digest
from blitzpack.compressor import SequentialReader, SourceReadError
from blitzpack.scheduler import WorkScheduler


def test_analyze_command_runs(tmp_path: Path):
    """Regression: `analyze` used to pass ClassifiedFiles to a FileManifest parameter."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello")

    result = subprocess.run(
        [sys.executable, "cli.py", "analyze", str(src)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_scheduler_accepts_manifest_not_classified(tmp_path: Path):
    """Unit-level guard for scheduler accepting FileManifest, without spawning a process."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x" * 100)

    manifest = FileAnalyzer(src).scan()
    jobs = WorkScheduler().schedule(manifest)
    assert len(jobs) == 1
    assert jobs[0].job_type == "bundle"


def test_unreadable_file_fails_loudly(tmp_path: Path, monkeypatch):
    """A source file that cannot be read must abort compression with SourceReadError, not archive as empty."""
    from blitzpack import compressor

    src = tmp_path / "src"
    src.mkdir()
    (src / "good.txt").write_bytes(b"fine")
    (src / "bad.txt").write_bytes(b"unreadable")

    real_open = open

    def failing_open(path, *a, **kw):
        if str(path).endswith("bad.txt"):
            raise PermissionError(13, "Access is denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(compressor, "open", failing_open, raising=False)

    with pytest.raises(Exception) as excinfo:
        compress(src, tmp_path / "out.blitz", level=1, workers=2)
    assert "bad.txt" in str(excinfo.value)


def test_size_change_during_compression_is_detected(tmp_path: Path):
    """A file that changes size between scan and read must raise SourceReadError."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "shrinking.txt"
    target.write_bytes(b"x" * 5000)

    manifest = FileAnalyzer(src).scan()
    jobs = WorkScheduler().schedule(manifest)

    target.write_bytes(b"x" * 10)  # shrink after scheduling

    with pytest.raises(SourceReadError):
        SequentialReader().read_job(jobs[0])


def test_verify_passes_on_good_archive(tmp_path: Path):
    """Whole-archive and deep chunk integrity verification succeed on untouched archive."""
    src = tmp_path / "src"
    src.mkdir()
    for i in range(20):
        (src / f"f{i}.txt").write_text(f"content {i}\n")

    archive = tmp_path / "ok.blitz"
    compress(src, archive, level=3, workers=2)

    with open(archive, "rb") as f:
        reader = BlitzArchiveReader(f)
        reader.verify(deep=False)
        reader.verify(deep=True)


def test_verify_detects_flipped_byte(tmp_path: Path):
    """Corrupting a byte in the chunk-data region must fail the whole-archive digest."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "payload.bin").write_bytes(os.urandom(100_000))

    archive = tmp_path / "corrupt.blitz"
    compress(src, archive, level=3, workers=2)

    # Flip one byte in the contiguous chunk-data region
    with open(archive, "r+b") as f:
        f.seek(CHUNK_DATA_START + 64)
        original = f.read(1)
        f.seek(CHUNK_DATA_START + 64)
        f.write(bytes([original[0] ^ 0xFF]))

    with open(archive, "rb") as f:
        reader = BlitzArchiveReader(f)
        with pytest.raises(ArchiveFormatError, match="digest mismatch"):
            reader.verify(deep=False)


def test_non_default_chunk_size_roundtrip(tmp_path: Path):
    """Regression: multi-chunk extraction must not hardcode 4MB stride."""
    src = tmp_path / "src"
    src.mkdir()
    payload = os.urandom(3_000_000)
    (src / "big.bin").write_bytes(payload)

    archive = tmp_path / "custom.blitz"
    dest = tmp_path / "out"

    # 1 MB chunk size -> the 3 MB file spans 3 chunks with a 1 MB stride
    compress(src, archive, level=1, workers=2, chunk_size=1024 * 1024)
    decompress(archive, dest, workers=2)

    assert (dest / "big.bin").read_bytes() == payload


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions chmod not applicable on Windows")
def test_permissions_restored_on_posix(tmp_path: Path):
    """Ensure file permissions are preserved on POSIX systems."""
    src = tmp_path / "src"
    src.mkdir()
    script = src / "script.sh"
    script.write_bytes(b"#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)

    archive = tmp_path / "perm.blitz"
    dest = tmp_path / "out"

    compress(src, archive)
    decompress(archive, dest)

    assert (dest / "script.sh").stat().st_mode & 0o777 == 0o755


# =============================================================================
# Security Hardening & Robustness Tests (docs/robust.md)
# =============================================================================


def _build_archive_with_manifest_path(archive_path: Path, rel_path: str):
    """Write a minimal valid archive whose single file claims `rel_path`."""
    data = b"pwned"
    with open(archive_path, "wb") as f:
        w = BlitzArchiveWriter(f)
        w.write_chunk(0, data, len(data), compute_digest(data), 0, FLAG_STORED)
        w.finalize(
            [ManifestEntry(
                path=rel_path, size=len(data), mtime=0.0, file_type=0,
                symlink_target=None, permissions=0o644, win_attrs=0,
                start_chunk=0, start_offset=0, end_chunk=0, end_offset=len(data),
            )],
            total_original_size=len(data),
        )


def test_path_traversal_is_blocked(tmp_path: Path):
    """A manifest path escaping destination via .. must be refused without writing anything."""
    archive = tmp_path / "evil.blitz"
    _build_archive_with_manifest_path(archive, "../escaped.txt")

    dest = tmp_path / "out"
    with pytest.raises(ArchiveFormatError, match="path traversal"):
        decompress(archive, dest)

    assert not (tmp_path / "escaped.txt").exists()


def test_absolute_path_is_blocked(tmp_path: Path):
    """An absolute or drive-letter path in manifest must be blocked."""
    archive = tmp_path / "evil2.blitz"
    bad = "/tmp/escaped_abs.txt" if os.name != "nt" else "C:/Windows/Temp/escaped_abs.txt"
    _build_archive_with_manifest_path(archive, bad)

    dest = tmp_path / "out2"
    with pytest.raises(ArchiveFormatError, match="path traversal"):
        decompress(archive, dest)


def test_absurd_seek_count_rejected(tmp_path: Path):
    """A corrupt seek-table count must be rejected without huge allocations."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello")
    archive = tmp_path / "a.blitz"
    compress(src, archive, level=1)

    with open(archive, "rb") as f:
        off = BlitzArchiveReader(f).footer.seek_table_offset

    with open(archive, "r+b") as f:
        f.seek(off)
        f.write(struct.pack("<I", 0xFFFFFFFF))  # claim 4 billion entries

    with open(archive, "rb") as f:
        with pytest.raises(ArchiveFormatError, match="Seek table claims"):
            BlitzArchiveReader(f)


def test_truncated_archive_rejected(tmp_path: Path):
    """A file cut off mid-stream must raise a clean format error, not unhandled crash."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x" * 5000)
    archive = tmp_path / "a.blitz"
    compress(src, archive, level=1)

    full = archive.read_bytes()
    (tmp_path / "cut.blitz").write_bytes(full[: len(full) // 2])

    with open(tmp_path / "cut.blitz", "rb") as f:
        with pytest.raises(ArchiveFormatError):
            BlitzArchiveReader(f)


# =============================================================================
# Functional Features Tests (docs/additional_patch.md)
# =============================================================================

def test_selective_extraction(tmp_path: Path):
    """Selective extraction must extract only matching files and skip unreferenced chunks."""
    src = tmp_path / "src"
    (src / "keep").mkdir(parents=True)
    (src / "drop").mkdir()
    (src / "keep" / "a.txt").write_text("A")
    (src / "keep" / "b.txt").write_text("B")
    (src / "drop" / "c.txt").write_text("C")

    archive = tmp_path / "a.blitz"
    compress(src, archive, level=1)

    dest = tmp_path / "out"
    res = decompress(archive, dest, include=["keep"])

    assert (dest / "keep" / "a.txt").read_text() == "A"
    assert (dest / "keep" / "b.txt").read_text() == "B"
    assert not (dest / "drop").exists()
    assert res.total_files == 2


def test_reproducible_archive_fidelity(tmp_path: Path):
    """Deterministic mode (-x) must produce bit-for-bit identical archives."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "z.txt").write_text("zeta")
    (src / "a.txt").write_text("alpha")
    (src / "m.txt").write_text("mu")

    arc1 = tmp_path / "rep1.blitz"
    arc2 = tmp_path / "rep2.blitz"

    compress(src, arc1, level=1, deterministic=True)
    compress(src, arc2, level=1, deterministic=True)

    assert arc1.read_bytes() == arc2.read_bytes()


def test_cancellation_stops_pipeline(tmp_path: Path):
    """Setting cancel_event must raise BlitzCancelled promptly without corrupt output."""
    import threading
    from blitzpack.utils import BlitzCancelled

    src = tmp_path / "src"
    src.mkdir()
    for i in range(50):
        (src / f"data_{i}.bin").write_bytes(os.urandom(100_000))

    archive = tmp_path / "cancelled.blitz"
    cancel = threading.Event()
    cancel.set()  # Cancel immediately

    with pytest.raises(BlitzCancelled):
        compress(src, archive, level=1, cancel_event=cancel)

    assert not archive.exists()

    # Verify decompress cancellation
    valid_archive = tmp_path / "valid.blitz"
    compress(src, valid_archive, level=1)
    cancel_decomp = threading.Event()
    cancel_decomp.set()

    dest = tmp_path / "out_cancelled"
    with pytest.raises(BlitzCancelled):
        decompress(valid_archive, dest, cancel_event=cancel_decomp)
