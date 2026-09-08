"""Regression and integrity test suite ensuring no silent corruption occurs."""

import os
import subprocess
import sys
from pathlib import Path
import pytest

from blitzpack import compress, decompress, BlitzArchiveReader
from blitzpack.archive_format import ArchiveFormatError, CHUNK_DATA_START
from blitzpack.analyzer import FileAnalyzer
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
