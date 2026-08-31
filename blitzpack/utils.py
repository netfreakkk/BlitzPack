"""Platform-specific utilities, formatting helpers, and path management."""

from dataclasses import dataclass
import os
import platform
from pathlib import Path
import sys
from typing import Callable, Optional


@dataclass(slots=True)
class ProgressUpdate:
    """Progress snapshot reported to CLI and GUI subscribers."""
    phase: str
    files_processed: int
    total_files: int
    bytes_processed: int
    total_bytes: int
    current_speed_bps: float
    message: str = ""


ProgressCallback = Callable[[ProgressUpdate], None]


def sanitize_windows_path(path: Path | str) -> str:
    """Apply the Windows extended-length prefix (\\\\?\\) if needed to avoid MAX_PATH (260) failures."""
    path_str = str(path)
    if platform.system() != "Windows":
        return path_str

    abs_path = os.path.abspath(path_str)
    if abs_path.startswith("\\\\?\\") or abs_path.startswith("\\\\.\\"):
        return abs_path

    if len(abs_path) >= 240:
        if abs_path.startswith("\\\\"):
            # UNC path: \\server\share -> \\?\UNC\server\share
            return "\\\\?\\UNC\\" + abs_path[2:]
        return "\\\\?\\" + abs_path

    return abs_path


def format_bytes(byte_count: int | float) -> str:
    """Convert raw byte count into human-readable notation."""
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(byte_count)
    for unit in units:
        if val < 1024.0 or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} PB"


def format_throughput(bytes_processed: int | float, duration_seconds: float) -> str:
    """Calculate and format throughput in MB/s."""
    if duration_seconds <= 0:
        return "inf MB/s"
    mb_per_sec = (bytes_processed / (1024 * 1024)) / duration_seconds
    return f"{mb_per_sec:.1f} MB/s"


def strip_windows_prefix(path_str: str) -> str:
    """Remove Windows \\\\?\\ extended-length prefixes for consistent relative path comparisons."""
    if path_str.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_str[8:]
    if path_str.startswith("\\\\?\\"):
        return path_str[4:]
    return path_str


def normalize_relative_path(path: Path | str, base_dir: Path | str) -> str:
    """Normalize a path relative to base directory using forward slashes for cross-platform portability."""
    p_clean = Path(strip_windows_prefix(str(path)))
    b_clean = Path(strip_windows_prefix(str(base_dir)))
    rel = p_clean.relative_to(b_clean)
    return rel.as_posix()


def can_preallocate_fast() -> bool:
    """Check if the OS/process environment supports fast file pre-allocation without zero-fill."""
    if platform.system() != "Windows":
        return hasattr(os, "posix_fallocate")
    # On Windows, true instant pre-allocation requires SeManageVolumePrivilege (SetFileValidData).
    # Return False to let callers perform standard sequential chunk writes instead of slow zero-fill.
    return False
