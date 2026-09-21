"""Unit tests for universal multi-format archive decompression, inspection, and verification."""

from __future__ import annotations

import bz2
import gzip
import lzma
from pathlib import Path
import tarfile
import threading
import zipfile

import pytest

from blitzpack.decompressor import decompress
from blitzpack.multi_decompress import (
    extract_archive,
    get_archive_format,
    inspect_archive,
    is_supported_archive,
    test_archive as check_archive,
)
from blitzpack.utils import BlitzCancelled


def test_supported_extensions_and_recognition():
    assert is_supported_archive("project.zip")
    assert is_supported_archive("backup.rar")
    assert is_supported_archive("data.7z")
    assert is_supported_archive("release.tar.gz")
    assert is_supported_archive("dist.tar.xz")
    assert is_supported_archive("dump.sql.gz")
    assert is_supported_archive("archive.blitz")
    assert not is_supported_archive("document.pdf")
    assert not is_supported_archive("image.png")

    assert get_archive_format(Path("a.tar.gz")) == "tar.gz"
    assert get_archive_format(Path("a.tgz")) == "tar.gz"
    assert get_archive_format(Path("a.tar.bz2")) == "tar.bz2"
    assert get_archive_format(Path("a.tbz2")) == "tar.bz2"
    assert get_archive_format(Path("a.tar.xz")) == "tar.xz"
    assert get_archive_format(Path("a.txz")) == "tar.xz"
    assert get_archive_format(Path("a.zip")) == "zip"
    assert get_archive_format(Path("a.rar")) == "rar"
    assert get_archive_format(Path("a.7z")) == "7z"
    assert get_archive_format(Path("a.gz")) == "gzip"
    assert get_archive_format(Path("a.bz2")) == "bzip2"
    assert get_archive_format(Path("a.xz")) == "xz"


def test_zip_inspect_test_and_extract(tmp_path: Path):
    zip_file = tmp_path / "sample.zip"
    with zipfile.ZipFile(zip_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("folder/hello.txt", "Hello from ZIP!")
        zf.writestr("folder/data.bin", b"\x00\x01\x02\x03\x04" * 100)

    # 1. Inspect
    manifest = inspect_archive(zip_file)
    assert len(manifest) >= 2
    names = {e.path for e in manifest}
    assert "folder/hello.txt" in names
    assert "folder/data.bin" in names

    # 2. Test integrity
    ok, err = check_archive(zip_file)
    assert ok is True
    assert "OK" in err

    # 3. Extract
    out_dir = tmp_path / "zip_out"
    res = extract_archive(zip_file, out_dir)
    assert res.total_files == 2
    assert (out_dir / "folder" / "hello.txt").read_text(encoding="utf-8") == "Hello from ZIP!"
    assert len((out_dir / "folder" / "data.bin").read_bytes()) == 500


def test_tar_gz_and_tar_xz(tmp_path: Path):
    for comp in ["gz", "bz2", "xz"]:
        archive_name = f"test.tar.{comp}"
        tar_path = tmp_path / archive_name

        mode = f"w:{comp}"
        with tarfile.open(tar_path, mode) as tf:
            info = tarfile.TarInfo(name="docs/readme.txt")
            data = f"Tar with {comp} compression".encode("utf-8")
            info.size = len(data)
            import io
            tf.addfile(info, io.BytesIO(data))

        # Inspect
        manifest = inspect_archive(tar_path)
        assert any("readme.txt" in e.path for e in manifest)

        # Test
        ok, err = check_archive(tar_path)
        assert ok is True, f"Testing {comp} failed: {err}"

        # Extract
        out_dir = tmp_path / f"out_{comp}"
        res = extract_archive(tar_path, out_dir)
        assert res.total_files >= 1
        assert (out_dir / "docs" / "readme.txt").read_text(encoding="utf-8") == f"Tar with {comp} compression"


def test_single_stream_gz_bz2_xz(tmp_path: Path):
    # GZ
    gz_file = tmp_path / "single.txt.gz"
    with gzip.open(gz_file, "wb") as f:
        f.write(b"GZIP single stream content")

    manifest = inspect_archive(gz_file)
    assert len(manifest) == 1
    assert manifest[0].path == "single.txt"

    out_dir = tmp_path / "gz_out"
    extract_archive(gz_file, out_dir)
    assert (out_dir / "single.txt").read_bytes() == b"GZIP single stream content"

    # BZ2
    bz2_file = tmp_path / "single.bin.bz2"
    with bz2.open(bz2_file, "wb") as f:
        f.write(b"BZIP2 single stream content")

    out_dir_bz2 = tmp_path / "bz2_out"
    extract_archive(bz2_file, out_dir_bz2)
    assert (out_dir_bz2 / "single.bin").read_bytes() == b"BZIP2 single stream content"

    # XZ
    xz_file = tmp_path / "single.dat.xz"
    with lzma.open(xz_file, "wb") as f:
        f.write(b"XZ single stream content")

    out_dir_xz = tmp_path / "xz_out"
    extract_archive(xz_file, out_dir_xz)
    assert (out_dir_xz / "single.dat").read_bytes() == b"XZ single stream content"


def test_7z_support(tmp_path: Path):
    try:
        import py7zr
    except ImportError:
        pytest.skip("py7zr not installed")

    sevenz_file = tmp_path / "archive.7z"
    with py7zr.SevenZipFile(sevenz_file, "w") as z:
        dummy_file = tmp_path / "7z_test.txt"
        dummy_file.write_text("Testing 7z universal extraction")
        z.write(dummy_file, arcname="subfolder/7z_test.txt")

    # Inspect
    manifest = inspect_archive(sevenz_file)
    assert any("7z_test.txt" in e.path for e in manifest)

    # Test
    ok, err = check_archive(sevenz_file)
    assert ok is True

    # Extract
    out_dir = tmp_path / "7z_out"
    res = extract_archive(sevenz_file, out_dir)
    assert res.total_files >= 1
    assert (out_dir / "subfolder" / "7z_test.txt").read_text() == "Testing 7z universal extraction"


def test_universal_decompress_delegation(tmp_path: Path):
    # Ensure calling decompress() on a non-blitz format works transparently
    zip_file = tmp_path / "delegated.zip"
    with zipfile.ZipFile(zip_file, "w") as zf:
        zf.writestr("test.txt", "Automated format delegation")

    out_dir = tmp_path / "delegated_out"
    res = decompress(zip_file, out_dir)
    assert res.total_files == 1
    assert (out_dir / "test.txt").read_text() == "Automated format delegation"


def test_cancellation_during_extraction(tmp_path: Path):
    zip_file = tmp_path / "cancel_test.zip"
    with zipfile.ZipFile(zip_file, "w") as zf:
        for i in range(20):
            zf.writestr(f"file_{i}.txt", f"data_{i}")

    cancel_evt = threading.Event()
    cancel_evt.set()  # Cancel immediately

    with pytest.raises(BlitzCancelled):
        extract_archive(zip_file, tmp_path / "cancel_out", cancel_event=cancel_evt)


def test_zip_slip_protection(tmp_path: Path):
    zip_file = tmp_path / "malicious.zip"
    with zipfile.ZipFile(zip_file, "w") as zf:
        # Malicious relative path trying to escape destination directory
        zf.writestr("../../../evil.txt", "Pwned")

    out_dir = tmp_path / "safe_out"
    # Should safely skip the traversal entry without writing outside
    res = extract_archive(zip_file, out_dir)
    assert res.total_files == 0
    assert not (tmp_path / "evil.txt").exists()
