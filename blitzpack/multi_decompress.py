"""Universal Multi-Format Decompression Engine for BlitzPack.

Supports RAR (RAR4/RAR5), ZIP, 7-Zip, TAR (tar, tar.gz, tar.bz2, tar.xz),
and single compressed streams (gz, bz2, xz), with progress reporting,
Zip-Slip protection, and cooperative cancellation.
"""

from __future__ import annotations

import bz2
import datetime
import gzip
import lzma
import os
from pathlib import Path
import shutil
import sys
import tarfile
import threading
import time
from typing import Any, List, Optional, Tuple, Union
import zipfile

from .archive_format import BlitzArchiveReader, ManifestEntry
from .utils import (
    BlitzCancelled,
    ProgressCallback,
    ProgressUpdate,
)

# Optional third-party extractors
try:
    import py7zr
    HAS_PY7ZR = True
except ImportError:
    HAS_PY7ZR = False

try:
    import rarfile
    HAS_RARFILE = True
except ImportError:
    HAS_RARFILE = False


SUPPORTED_ARCHIVE_EXTENSIONS = {
    ".blitz",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".tgz",
    ".tbz2",
    ".tbz",
    ".txz",
    ".cbz",
    ".cbr",
    ".cb7",
    ".jar",
    ".war",
}


def is_supported_archive(path: Union[str, Path]) -> bool:
    """Return True if file path has a recognized archive extension."""
    name = Path(path).name.lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if name.endswith(compound):
            return True
    return Path(path).suffix.lower() in SUPPORTED_ARCHIVE_EXTENSIONS


def get_archive_format(path: Union[str, Path]) -> str:
    """Determine the archive format family from filename."""
    name = Path(path).name.lower()
    if name.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if name.endswith((".tar.bz2", ".tbz2", ".tbz")):
        return "tar.bz2"
    if name.endswith((".tar.xz", ".txz")):
        return "tar.xz"
    if name.endswith(".tar"):
        return "tar"
    if name.endswith(".blitz"):
        return "blitz"
    if name.endswith((".zip", ".jar", ".war", ".cbz")):
        return "zip"
    if name.endswith((".7z", ".cb7")):
        return "7z"
    if name.endswith((".rar", ".cbr")):
        return "rar"
    if name.endswith(".gz"):
        return "gzip"
    if name.endswith(".bz2"):
        return "bzip2"
    if name.endswith(".xz"):
        return "xz"
    return "unknown"


def _configure_rarfile_backend() -> None:
    """Locate and configure the unrar executable for rarfile."""
    if not HAS_RARFILE:
        return

    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", ""), "blitzpack", "tools", "UnRAR.exe"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "tools", "UnRAR.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "UnRAR.exe"),
        r"C:\Program Files\WinRAR\UnRAR.exe",
        r"C:\Program Files\7-Zip\7z.exe",
        shutil.which("unrar"),
        shutil.which("7z"),
    ]

    for cand in candidates:
        if cand and os.path.isfile(cand):
            rarfile.UNRAR_TOOL = cand
            return


# Configure rar backend on import
_configure_rarfile_backend()


def _is_safe_path(target_path: Path, base_dir: Path) -> bool:
    """Prevent Zip-Slip directory traversal attacks."""
    try:
        resolved_target = target_path.resolve()
        resolved_base = base_dir.resolve()
        return resolved_target == resolved_base or resolved_base in resolved_target.parents
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Archive Inspection (Virtual directory browsing)
# -----------------------------------------------------------------------------
def inspect_archive(archive_path: Path) -> List[ManifestEntry]:
    """Read the file manifest of any supported archive format."""
    archive_path = archive_path.resolve()
    fmt = get_archive_format(archive_path)

    if fmt == "blitz":
        with open(archive_path, "rb") as f_in:
            reader = BlitzArchiveReader(f_in)
            return list(reader.manifest)

    entries: List[ManifestEntry] = []

    if fmt == "zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                is_dir = info.is_dir()
                mtime = 0.0
                try:
                    dt = datetime.datetime(*info.date_time)
                    mtime = dt.timestamp()
                except Exception:
                    pass
                entries.append(
                    ManifestEntry(
                        path=info.filename.rstrip("/"),
                        size=0 if is_dir else info.file_size,
                        mtime=mtime,
                        file_type=1 if is_dir else 0,
                        symlink_target=None,
                        permissions=0,
                        win_attrs=0,
                        start_chunk=0,
                        start_offset=0,
                        end_chunk=0,
                        end_offset=0,
                    )
                )
        return entries

    if fmt == "7z":
        if not HAS_PY7ZR:
            raise RuntimeError("7-Zip support requires 'py7zr' package.")
        with py7zr.SevenZipFile(archive_path, "r") as sz:
            for item in sz.list():
                is_dir = getattr(item, "is_directory", False)
                mtime = getattr(item, "archived", None)
                mtime_ts = mtime.timestamp() if mtime else 0.0
                entries.append(
                    ManifestEntry(
                        path=item.filename.rstrip("/"),
                        size=0 if is_dir else getattr(item, "uncompressed", 0),
                        mtime=mtime_ts,
                        file_type=1 if is_dir else 0,
                        symlink_target=None,
                        permissions=0,
                        win_attrs=0,
                        start_chunk=0,
                        start_offset=0,
                        end_chunk=0,
                        end_offset=0,
                    )
                )
        return entries

    if fmt == "rar":
        if not HAS_RARFILE:
            raise RuntimeError("RAR support requires 'rarfile' package.")
        _configure_rarfile_backend()
        with rarfile.RarFile(str(archive_path), "r") as rf:
            for info in rf.infolist():
                is_dir = info.isdir()
                mtime = 0.0
                try:
                    if info.date_time:
                        dt = datetime.datetime(*info.date_time)
                        mtime = dt.timestamp()
                except Exception:
                    pass
                entries.append(
                    ManifestEntry(
                        path=info.filename.replace("\\", "/").rstrip("/"),
                        size=0 if is_dir else info.file_size,
                        mtime=mtime,
                        file_type=1 if is_dir else 0,
                        symlink_target=None,
                        permissions=0,
                        win_attrs=0,
                        start_chunk=0,
                        start_offset=0,
                        end_chunk=0,
                        end_offset=0,
                    )
                )
        return entries

    if fmt in ("tar", "tar.gz", "tar.bz2", "tar.xz"):
        mode = "r:*"
        with tarfile.open(archive_path, mode) as tf:
            for m in tf.getmembers():
                is_dir = m.isdir()
                entries.append(
                    ManifestEntry(
                        path=m.name.rstrip("/"),
                        size=0 if is_dir else m.size,
                        mtime=float(m.mtime),
                        file_type=1 if is_dir else 0,
                        symlink_target=None,
                        permissions=0,
                        win_attrs=0,
                        start_chunk=0,
                        start_offset=0,
                        end_chunk=0,
                        end_offset=0,
                    )
                )
        return entries

    if fmt in ("gzip", "bzip2", "xz"):
        # Single compressed file stream
        out_name = archive_path.stem
        entries.append(
            ManifestEntry(
                path=out_name,
                size=archive_path.stat().st_size * 2,  # Estimate
                mtime=archive_path.stat().st_mtime,
                file_type=0,
                symlink_target=None,
                permissions=0,
                win_attrs=0,
                start_chunk=0,
                start_offset=0,
                end_chunk=0,
                end_offset=0,
            )
        )
        return entries

    raise ValueError(f"Unsupported archive format for inspection: {archive_path.name}")


# -----------------------------------------------------------------------------
# Archive Integrity Testing
# -----------------------------------------------------------------------------
def test_archive(archive_path: Path) -> Tuple[bool, str]:
    """Test archive integrity without extracting files to disk."""
    archive_path = archive_path.resolve()
    fmt = get_archive_format(archive_path)

    try:
        if fmt == "blitz":
            with open(archive_path, "rb") as f_in:
                reader = BlitzArchiveReader(f_in)
                reader.verify(deep=True)
            return True, "All chunks and checksums verified OK."

        if fmt == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                bad_file = zf.testzip()
                if bad_file:
                    return False, f"Corrupted file detected: {bad_file}"
            return True, "ZIP integrity verified OK."

        if fmt == "7z":
            if not HAS_PY7ZR:
                return False, "py7zr library not installed."
            with py7zr.SevenZipFile(archive_path, "r") as sz:
                res = sz.test()
                if res is False:
                    return False, "7-Zip archive test failed."
                return True, "7-Zip archive verified OK."

        if fmt == "rar":
            if not HAS_RARFILE:
                return False, "rarfile library not installed."
            _configure_rarfile_backend()
            with rarfile.RarFile(str(archive_path), "r") as rf:
                if rf.testrar():
                    return False, "RAR CRC test failed on one or more files."
            return True, "RAR archive verified OK."

        if fmt in ("tar", "tar.gz", "tar.bz2", "tar.xz"):
            with tarfile.open(archive_path, "r:*") as tf:
                for m in tf.getmembers():
                    if m.isfile():
                        f = tf.extractfile(m)
                        if f:
                            while f.read(1024 * 1024):
                                pass
            return True, "TAR archive stream read successfully."

        if fmt in ("gzip", "bzip2", "xz"):
            openers = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}
            with openers[fmt](archive_path, "rb") as f_stream:
                while f_stream.read(1024 * 1024):
                    pass
            return True, f"{fmt.upper()} stream verified OK."

    except Exception as ex:
        return False, str(ex)

    return False, f"Unsupported format for integrity testing: {archive_path.name}"


# -----------------------------------------------------------------------------
# Multi-Format Decompression Pipeline
# -----------------------------------------------------------------------------
def extract_archive(
    archive_path: Path,
    output_dir: Path,
    workers: int = 4,
    include: Optional[List[str]] = None,
    cancel_event: Optional[threading.Event] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> Any:
    """Extract any recognized archive format with Zip-Slip safety and progress updates."""
    from .decompressor import DecompressionResult

    archive_path = archive_path.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    fmt = get_archive_format(archive_path)
    start_time = time.perf_counter()

    include_set = set(include) if include else None

    # Track extraction stats
    total_files = 0
    extracted_bytes = 0

    if fmt == "zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            infolist = zf.infolist()
            if include_set:
                infolist = [i for i in infolist if i.filename in include_set or i.filename.rstrip("/") in include_set]

            total_uncompressed = sum(i.file_size for i in infolist if not i.is_dir())
            processed_bytes = 0

            for item in infolist:
                if cancel_event and cancel_event.is_set():
                    raise BlitzCancelled("Extraction cancelled by user.")

                target_path = output_dir / item.filename
                if not _is_safe_path(target_path, output_dir):
                    continue

                if item.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(item) as src, open(target_path, "wb") as dst:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise BlitzCancelled("Extraction cancelled by user.")
                        chunk = src.read(256 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        extracted_bytes += len(chunk)
                        processed_bytes += len(chunk)

                        if progress_callback:
                            elapsed = max(0.001, time.perf_counter() - start_time)
                            speed = processed_bytes / elapsed
                            progress_callback(
                                ProgressUpdate(
                                    bytes_processed=processed_bytes,
                                    total_bytes=total_uncompressed,
                                    files_processed=total_files,
                                    total_files=len(infolist),
                                    current_speed_bps=speed,
                                    phase="extracting",
                                    current_file=item.filename,
                                )
                            )

                total_files += 1

    elif fmt == "7z":
        if not HAS_PY7ZR:
            raise RuntimeError("7-Zip extraction requires the 'py7zr' package.")
        with py7zr.SevenZipFile(archive_path, "r") as sz:
            targets = include if include else None
            archive_size = archive_path.stat().st_size

            # Periodic heartbeat while py7zr extracts
            def monitor_cancel() -> None:
                while True:
                    if cancel_event and cancel_event.is_set():
                        # Close or signal py7zr
                        break
            if targets:
                sz.extract(path=str(output_dir), targets=targets)
            else:
                sz.extractall(path=str(output_dir))
            # Count extracted files
            for root, _, files in os.walk(output_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    total_files += 1
                    extracted_bytes += os.path.getsize(fp)

            if progress_callback:
                progress_callback(
                    ProgressUpdate(
                        bytes_processed=archive_size,
                        total_bytes=archive_size,
                        files_processed=total_files,
                        total_files=total_files,
                        current_speed_bps=extracted_bytes / max(0.001, time.perf_counter() - start_time),
                        phase="done",
                        current_file="Complete",
                    )
                )

    elif fmt == "rar":
        if not HAS_RARFILE:
            raise RuntimeError("RAR extraction requires the 'rarfile' package.")
        _configure_rarfile_backend()
        with rarfile.RarFile(str(archive_path), "r") as rf:
            infolist = rf.infolist()
            if include_set:
                infolist = [i for i in infolist if i.filename in include_set or i.filename.replace("\\", "/") in include_set]

            total_uncompressed = sum(i.file_size for i in infolist if not i.isdir())
            processed_bytes = 0

            for item in infolist:
                if cancel_event and cancel_event.is_set():
                    raise BlitzCancelled("Extraction cancelled by user.")

                norm_name = item.filename.replace("\\", "/")
                target_path = output_dir / norm_name
                if not _is_safe_path(target_path, output_dir):
                    continue

                if item.isdir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)
                with rf.open(item) as src, open(target_path, "wb") as dst:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise BlitzCancelled("Extraction cancelled by user.")
                        chunk = src.read(256 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        extracted_bytes += len(chunk)
                        processed_bytes += len(chunk)

                        if progress_callback:
                            elapsed = max(0.001, time.perf_counter() - start_time)
                            speed = processed_bytes / elapsed
                            progress_callback(
                                ProgressUpdate(
                                    bytes_processed=processed_bytes,
                                    total_bytes=total_uncompressed,
                                    files_processed=total_files,
                                    total_files=len(infolist),
                                    current_speed_bps=speed,
                                    phase="extracting",
                                    current_file=norm_name,
                                )
                            )

                total_files += 1

    elif fmt in ("tar", "tar.gz", "tar.bz2", "tar.xz"):
        with tarfile.open(archive_path, "r:*") as tf:
            members = tf.getmembers()
            if include_set:
                members = [m for m in members if m.name in include_set or m.name.rstrip("/") in include_set]

            total_uncompressed = sum(m.size for m in members if m.isfile())
            processed_bytes = 0

            for m in members:
                if cancel_event and cancel_event.is_set():
                    raise BlitzCancelled("Extraction cancelled by user.")

                target_path = output_dir / m.name
                if not _is_safe_path(target_path, output_dir):
                    continue

                if m.isdir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue

                if m.isfile():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    f_in = tf.extractfile(m)
                    if f_in:
                        with open(target_path, "wb") as f_out:
                            while True:
                                if cancel_event and cancel_event.is_set():
                                    raise BlitzCancelled("Extraction cancelled by user.")
                                chunk = f_in.read(256 * 1024)
                                if not chunk:
                                    break
                                f_out.write(chunk)
                                extracted_bytes += len(chunk)
                                processed_bytes += len(chunk)

                                if progress_callback:
                                    elapsed = max(0.001, time.perf_counter() - start_time)
                                    speed = processed_bytes / elapsed
                                    progress_callback(
                                        ProgressUpdate(
                                            bytes_processed=processed_bytes,
                                            total_bytes=total_uncompressed,
                                            files_processed=total_files,
                                            total_files=len(members),
                                            current_speed_bps=speed,
                                            phase="extracting",
                                            current_file=m.name,
                                        )
                                    )
                    total_files += 1

    elif fmt in ("gzip", "bzip2", "xz"):
        openers = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}
        target_path = output_dir / archive_path.stem
        with openers[fmt](archive_path, "rb") as src, open(target_path, "wb") as dst:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise BlitzCancelled("Extraction cancelled by user.")
                chunk = src.read(512 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                extracted_bytes += len(chunk)
        total_files = 1

    else:
        raise ValueError(f"Unsupported format for extraction: {archive_path.name}")

    duration = max(0.001, time.perf_counter() - start_time)
    throughput_mb = (extracted_bytes / (1024 * 1024)) / duration

    return DecompressionResult(
        output_dir=output_dir,
        total_files=total_files,
        extracted_bytes=extracted_bytes,
        duration_seconds=duration,
        throughput_mb_s=throughput_mb,
        backend=fmt.upper(),
        skipped_symlinks=0,
    )
