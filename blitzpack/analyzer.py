"""File discovery, metadata extraction, and size-tier classification."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from typing import Dict, List, Optional, Set
from .utils import normalize_relative_path, sanitize_windows_path

LARGE_THRESHOLD = 16 * 1024 * 1024   # > 16 MB
SMALL_THRESHOLD = 64 * 1024          # < 64 KB


@dataclass(slots=True)
class FileEntry:
    path: Path
    relative_path: str
    size: int
    mtime: float
    file_type: int        # 0: file, 1: dir, 2: symlink
    symlink_target: Optional[str] = None
    permissions: int = 0o644
    win_attrs: int = 0
    extension: str = ""


@dataclass(slots=True)
class ClassifiedFiles:
    large: List[FileEntry] = field(default_factory=list)
    medium: List[FileEntry] = field(default_factory=list)
    small: List[FileEntry] = field(default_factory=list)
    directories: List[FileEntry] = field(default_factory=list)
    symlinks: List[FileEntry] = field(default_factory=list)
    total_bytes: int = 0
    total_files: int = 0


@dataclass(slots=True)
class FileManifest:
    entries: List[FileEntry] = field(default_factory=list)
    base_dir: Path = field(default_factory=Path)
    total_bytes: int = 0


class FileAnalyzer:
    """Recursively scans directories and classifies files for optimal parallel scheduling."""

    def __init__(self, base_path: Path | str) -> None:
        self.base_path = Path(base_path).resolve()

    def scan(self) -> FileManifest:
        """Scan input path (single file or recursive directory) into a FileManifest."""
        entries: List[FileEntry] = []
        total_bytes = 0

        if self.base_path.is_file():
            entry = self._inspect_entry(self.base_path, self.base_path.parent)
            if entry:
                entries.append(entry)
                total_bytes += entry.size
            return FileManifest(entries=entries, base_dir=self.base_path.parent, total_bytes=total_bytes)

        # Directory traversal using scandir
        dirs_to_visit = [self.base_path]
        while dirs_to_visit:
            current_dir = dirs_to_visit.pop()
            sanitized = sanitize_windows_path(current_dir)

            try:
                with os.scandir(sanitized) as it:
                    has_children = False
                    for dir_entry in it:
                        has_children = True
                        try:
                            # Avoid following directory symlinks to prevent infinite loops
                            is_symlink = dir_entry.is_symlink()
                            if dir_entry.is_dir(follow_symlinks=False) and not is_symlink:
                                dirs_to_visit.append(Path(dir_entry.path))
                            else:
                                item_entry = self._inspect_entry(Path(dir_entry.path), self.base_path)
                                if item_entry:
                                    entries.append(item_entry)
                                    if item_entry.file_type == 0:
                                        total_bytes += item_entry.size
                        except (PermissionError, FileNotFoundError, OSError):
                            continue

                    # Record empty directory so directory structure is fully preserved
                    if not has_children and current_dir != self.base_path:
                        dir_item = self._inspect_entry(current_dir, self.base_path, is_dir=True)
                        if dir_item:
                            entries.append(dir_item)

            except (PermissionError, FileNotFoundError, OSError):
                continue

        return FileManifest(entries=entries, base_dir=self.base_path, total_bytes=total_bytes)

    def _inspect_entry(self, full_path: Path, base_dir: Path, is_dir: bool = False) -> Optional[FileEntry]:
        sanitized = sanitize_windows_path(full_path)
        try:
            st = os.lstat(sanitized)
        except (PermissionError, FileNotFoundError, OSError):
            return None

        rel_path = normalize_relative_path(full_path, base_dir)

        if stat.S_ISLNK(st.st_mode):
            try:
                target = os.readlink(sanitized)
            except OSError:
                target = ""
            return FileEntry(
                path=full_path,
                relative_path=rel_path,
                size=0,
                mtime=st.st_mtime,
                file_type=2,
                symlink_target=target,
                permissions=st.st_mode & 0o777,
                win_attrs=getattr(st, "st_file_attributes", 0),
                extension=full_path.suffix.lower()
            )

        if is_dir or stat.S_ISDIR(st.st_mode):
            return FileEntry(
                path=full_path,
                relative_path=rel_path,
                size=0,
                mtime=st.st_mtime,
                file_type=1,
                permissions=st.st_mode & 0o777,
                win_attrs=getattr(st, "st_file_attributes", 0)
            )

        return FileEntry(
            path=full_path,
            relative_path=rel_path,
            size=st.st_size,
            mtime=st.st_mtime,
            file_type=0,
            permissions=st.st_mode & 0o777,
            win_attrs=getattr(st, "st_file_attributes", 0),
            extension=full_path.suffix.lower()
        )

    def classify(self, manifest: FileManifest) -> ClassifiedFiles:
        """Classify manifest entries into size tiers."""
        result = ClassifiedFiles()

        for entry in manifest.entries:
            if entry.file_type == 1:
                result.directories.append(entry)
            elif entry.file_type == 2:
                result.symlinks.append(entry)
            else:
                result.total_files += 1
                result.total_bytes += entry.size
                if entry.size > LARGE_THRESHOLD:
                    result.large.append(entry)
                elif entry.size < SMALL_THRESHOLD:
                    result.small.append(entry)
                else:
                    result.medium.append(entry)

        return result
