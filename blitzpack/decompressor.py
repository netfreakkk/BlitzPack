"""Parallel decompression engine reading seek tables and extracting chunks concurrently.

Uses an optimized 3-stage pipeline with cooperative cancellation, path-traversal (Zip Slip)
protection, selective random-access extraction, and non-blocking background teardown.
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import zstandard as zstd

from .archive_format import ArchiveFormatError, BlitzArchiveReader, SeekEntry, FLAG_STORED, ManifestEntry
from .checksum import compute_digest
from .utils import ProgressCallback, ProgressUpdate, sanitize_windows_path, BlitzCancelled


@dataclass(slots=True)
class DecompressionResult:
    output_dir: Path
    total_files: int
    extracted_bytes: int
    duration_seconds: float
    throughput_mb_s: float
    backend: str
    skipped_symlinks: int = 0


@dataclass(slots=True)
class ExtractTarget:
    target_path: Path
    start_offset: int
    end_offset: int
    is_multi_chunk: bool
    file_offset: int
    total_file_size: int
    total_file_chunks: int
    mtime: float


@dataclass(slots=True)
class ExtractTask:
    chunk_index: int
    seek_entry: SeekEntry
    raw_bytes: bytes
    targets: List[ExtractTarget]


_thread_local = threading.local()


def _worker_decompress_loop(
    read_queue: queue.Queue,
    write_queue: queue.Queue,
    error_queue: queue.Queue,
    stop_event: threading.Event,
) -> None:
    """Worker: decompress + checksum, cooperating with stop_event for clean shutdown."""
    if not hasattr(_thread_local, "decompressor"):
        _thread_local.decompressor = zstd.ZstdDecompressor()
    decompressor = _thread_local.decompressor

    while not stop_event.is_set():
        try:
            task: Optional[ExtractTask] = read_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if task is None:
            read_queue.task_done()
            break

        try:
            seek_entry = task.seek_entry
            raw_bytes = task.raw_bytes

            if seek_entry.flags & FLAG_STORED:
                decompressed = raw_bytes
            else:
                decompressed = decompressor.decompress(
                    raw_bytes, max_output_size=seek_entry.original_size + 65536
                )
            del raw_bytes

            if compute_digest(decompressed) != seek_entry.digest:
                raise ValueError(
                    f"Chunk {task.chunk_index} checksum mismatch! Expected {seek_entry.digest:#x}"
                )

            while not stop_event.is_set():
                try:
                    write_queue.put((task, decompressed), timeout=0.1)
                    break
                except queue.Full:
                    continue
        except Exception as ex:
            error_queue.put(ex)
            stop_event.set()
        finally:
            read_queue.task_done()


def _writer_thread_loop(
    write_queue: queue.Queue,
    result_queue: queue.Queue,
    error_queue: queue.Queue,
    total_valid_chunks: int,
    stop_event: threading.Event,
) -> None:
    """Disk writer: streams chunks to files, honoring stop_event."""
    open_handles: Dict[str, Any] = {}
    chunks_written_per_file: Dict[str, int] = {}
    completed = 0

    while completed < total_valid_chunks and not stop_event.is_set():
        if not error_queue.empty():
            break

        try:
            item = write_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        task, decompressed = item
        try:
            for target in task.targets:
                sanitized_dest = sanitize_windows_path(target.target_path)

                if target.is_multi_chunk:
                    handle = open_handles.get(sanitized_dest)
                    if handle is None:
                        handle = open(sanitized_dest, "wb+")
                        handle.truncate(target.total_file_size)
                        open_handles[sanitized_dest] = handle
                        chunks_written_per_file[sanitized_dest] = 0

                    handle.seek(target.file_offset)
                    handle.write(decompressed)
                    chunks_written_per_file[sanitized_dest] += 1

                    if chunks_written_per_file[sanitized_dest] >= target.total_file_chunks:
                        handle.close()
                        del open_handles[sanitized_dest]
                        del chunks_written_per_file[sanitized_dest]
                else:
                    slice_data = decompressed[target.start_offset : target.end_offset]
                    with open(sanitized_dest, "wb") as f_out:
                        f_out.write(slice_data)

            result_queue.put(len(decompressed))
            completed += 1
        except Exception as ex:
            error_queue.put(ex)
            stop_event.set()
            break
        finally:
            del decompressed
            write_queue.task_done()

    for h in open_handles.values():
        try:
            h.close()
        except OSError:
            pass


def _reader_thread_loop(
    archive_path: Path,
    seek_entries: List[SeekEntry],
    chunk_to_targets: Dict[int, List[ExtractTarget]],
    read_queue: queue.Queue,
    error_queue: queue.Queue,
    stop_event: threading.Event,
) -> None:
    """Reader: reads referenced chunks in offset order, honoring stop_event."""
    sanitized_archive = sanitize_windows_path(archive_path)
    ordered_entries = sorted(enumerate(seek_entries), key=lambda x: x[1].offset)

    try:
        with open(sanitized_archive, "rb") as f:
            for chunk_index, seek_entry in ordered_entries:
                if stop_event.is_set() or not error_queue.empty():
                    break
                targets = chunk_to_targets.get(chunk_index)
                if not targets:
                    continue

                f.seek(seek_entry.offset)
                raw_bytes = f.read(seek_entry.compressed_size)

                task = ExtractTask(
                    chunk_index=chunk_index,
                    seek_entry=seek_entry,
                    raw_bytes=raw_bytes,
                    targets=targets,
                )
                while not stop_event.is_set():
                    try:
                        read_queue.put(task, timeout=0.1)
                        break
                    except queue.Full:
                        continue
    except Exception as ex:
        error_queue.put(ex)
        stop_event.set()


def _select_entries(manifest: List[ManifestEntry], include: Optional[List[str]]) -> List[ManifestEntry]:
    """Filter manifest entries to those matching `include` (files or directory prefixes).

    include=None -> everything. Paths are archive-relative, forward-slash.
    """
    if not include:
        return manifest
    norm = []
    for i in include:
        i = i.strip().replace("\\", "/").strip("/")
        if i in ("", "."):
            return manifest  # explicit root selects all
        norm.append(i)
    out = []
    for e in manifest:
        p = e.path.replace("\\", "/")
        if any(p == inc or p.startswith(inc + "/") for inc in norm):
            out.append(e)
    return out


def decompress(
    archive_path: Path | str,
    output_dir: Path | str,
    workers: int = 0,
    verify_first: bool = False,
    cleanup_on_error: bool = False,
    allow_external_symlinks: bool = False,
    include: Optional[List[str]] = None,
    cancel_event: Optional[threading.Event] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> DecompressionResult:
    """Extract a .blitz archive in parallel with path traversal protection, cancellation, and selective extraction."""
    arc_p = Path(archive_path).resolve()
    out_p = Path(output_dir).resolve()
    out_root = out_p
    num_workers = workers or os.cpu_count() or 4
    created_output_root = not out_p.exists()

    def _safe_target(rel_path: str) -> Path:
        """Resolve an archive path under out_root, refusing any escape (Zip Slip)."""
        clean_rel = rel_path.lstrip("/\\")
        resolved = (out_root / clean_rel).resolve()
        if resolved != out_root and not resolved.is_relative_to(out_root):
            raise ArchiveFormatError(
                f"Refusing to extract outside destination (path traversal): {rel_path!r}"
            )
        return resolved

    start_time = time.perf_counter()

    # 1. Read Archive Structure & Optional Pre-flight Verification
    with open(sanitize_windows_path(arc_p), "rb") as f_in:
        reader = BlitzArchiveReader(f_in)
        if verify_first:
            reader.verify(deep=False)

    selected = _select_entries(reader.manifest, include)
    if include and not selected:
        raise FileNotFoundError(f"No entries in archive matched: {include}")

    # 2. Recreate Directory Hierarchy
    seen_dirs = set()
    for entry in selected:
        target_path = _safe_target(entry.path)
        sanitized_target = sanitize_windows_path(target_path)
        if entry.file_type == 1:  # Directory
            if sanitized_target not in seen_dirs:
                os.makedirs(sanitized_target, exist_ok=True)
                seen_dirs.add(sanitized_target)
        elif entry.file_type == 0:
            parent_dir = os.path.dirname(sanitized_target)
            if parent_dir not in seen_dirs:
                os.makedirs(parent_dir, exist_ok=True)
                seen_dirs.add(parent_dir)

            if entry.size == 0:
                with open(sanitized_target, "wb"):
                    pass

    # 3. Map chunk index -> targets with dynamic seek table offset accumulation
    chunk_to_targets: Dict[int, List[ExtractTarget]] = {
        i: [] for i in range(len(reader.seek_entries))
    }

    for entry in selected:
        if entry.file_type != 0 or entry.size == 0:
            continue

        target_path = _safe_target(entry.path)
        if entry.start_chunk != entry.end_chunk:
            total_chunks = entry.end_chunk - entry.start_chunk + 1
            running = 0
            for chunk_idx in range(entry.start_chunk, entry.end_chunk + 1):
                if not 0 <= chunk_idx < len(reader.seek_entries):
                    raise ArchiveFormatError(
                        f"Manifest entry {entry.path!r} references out-of-range chunk {chunk_idx}"
                    )
                chunk_to_targets[chunk_idx].append(
                    ExtractTarget(
                        target_path=target_path,
                        start_offset=0,
                        end_offset=-1,
                        is_multi_chunk=True,
                        file_offset=running,
                        total_file_size=entry.size,
                        total_file_chunks=total_chunks,
                        mtime=entry.mtime,
                    )
                )
                running += reader.seek_entries[chunk_idx].original_size
            if running != entry.size:
                raise ArchiveFormatError(
                    f"Chunk span for {entry.path!r} covers {running} bytes but the manifest records {entry.size}"
                )
        else:
            if not 0 <= entry.start_chunk < len(reader.seek_entries):
                raise ArchiveFormatError(
                    f"Manifest entry {entry.path!r} references out-of-range chunk {entry.start_chunk}"
                )
            chunk_to_targets[entry.start_chunk].append(
                ExtractTarget(
                    target_path=target_path,
                    start_offset=entry.start_offset,
                    end_offset=entry.end_offset,
                    is_multi_chunk=False,
                    file_offset=0,
                    total_file_size=entry.size,
                    total_file_chunks=1,
                    mtime=entry.mtime,
                )
            )

    total_bytes = sum(e.size for e in selected if e.file_type == 0)
    bytes_done = 0
    total_valid_chunks = sum(1 for v in chunk_to_targets.values() if v)

    # 4. Start 3-Stage Pipeline with stop_event
    read_queue: queue.Queue = queue.Queue(maxsize=max(8, num_workers * 2))
    write_queue: queue.Queue = queue.Queue(maxsize=max(8, num_workers * 2))
    result_queue: queue.Queue = queue.Queue()
    error_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    writer_thread = threading.Thread(
        target=_writer_thread_loop,
        args=(write_queue, result_queue, error_queue, total_valid_chunks, stop_event),
        daemon=True,
    )
    writer_thread.start()

    worker_threads = []
    for _ in range(num_workers):
        t = threading.Thread(
            target=_worker_decompress_loop,
            args=(read_queue, write_queue, error_queue, stop_event),
            daemon=True,
        )
        t.start()
        worker_threads.append(t)

    reader_thread = threading.Thread(
        target=_reader_thread_loop,
        args=(arc_p, reader.seek_entries, chunk_to_targets, read_queue, error_queue, stop_event),
        daemon=True,
    )
    reader_thread.start()

    completed_chunks = 0
    last_callback_time = 0.0

    try:
        while completed_chunks < total_valid_chunks:
            if cancel_event is not None and cancel_event.is_set():
                raise BlitzCancelled("extraction cancelled")
            if not error_queue.empty():
                raise error_queue.get()

            try:
                res = result_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            bytes_done += res
            completed_chunks += 1

            now = time.perf_counter()
            if progress_callback and (now - last_callback_time >= 0.1 or completed_chunks == total_valid_chunks):
                last_callback_time = now
                elapsed = now - start_time
                speed = bytes_done / elapsed if elapsed > 0 else 0
                progress_callback(
                    ProgressUpdate(
                        phase="decompressing",
                        files_processed=len(selected),
                        total_files=len(selected),
                        bytes_processed=bytes_done,
                        total_bytes=total_bytes,
                        current_speed_bps=speed,
                        message=f"Extracting ({num_workers} workers)...",
                    )
                )
    except BaseException:
        stop_event.set()
        if cleanup_on_error and created_output_root and out_p.exists():
            shutil.rmtree(out_p, ignore_errors=True)
        raise
    finally:
        stop_event.set()
        for _ in range(num_workers):
            try:
                read_queue.put_nowait(None)
            except queue.Full:
                pass
        for t in worker_threads:
            t.join(timeout=5)
        reader_thread.join(timeout=5)
        writer_thread.join(timeout=5)

    # 5. Restore Symlinks, Windows Attributes, Permissions, and Timestamps
    skipped_symlinks = 0
    for entry in selected:
        target_path = _safe_target(entry.path)
        sanitized_target = sanitize_windows_path(target_path)

        if entry.file_type == 2 and entry.symlink_target:
            link_dest = (target_path.parent / entry.symlink_target).resolve()
            if not allow_external_symlinks and not link_dest.is_relative_to(out_root):
                skipped_symlinks += 1
                continue
            try:
                if os.path.islink(sanitized_target) or os.path.exists(sanitized_target):
                    os.remove(sanitized_target)
                os.symlink(entry.symlink_target, sanitized_target)
            except OSError:
                pass
        else:
            if os.name != "nt" and entry.permissions:
                try:
                    os.chmod(sanitized_target, entry.permissions)
                except OSError:
                    pass
            elif os.name == "nt" and entry.win_attrs:
                try:
                    import ctypes
                    settable = entry.win_attrs & 0x27  # READONLY | HIDDEN | SYSTEM | ARCHIVE
                    if settable:
                        ctypes.windll.kernel32.SetFileAttributesW(str(sanitized_target), settable)
                except Exception:
                    pass

            if entry.mtime:
                try:
                    os.utime(sanitized_target, (entry.mtime, entry.mtime))
                except OSError:
                    pass

    total_duration = time.perf_counter() - start_time
    throughput = (total_bytes / (1024 * 1024)) / total_duration if total_duration > 0 else 0.0

    return DecompressionResult(
        output_dir=out_p,
        total_files=len(selected),
        extracted_bytes=total_bytes,
        duration_seconds=total_duration,
        throughput_mb_s=throughput,
        backend="py-pipeline-fast",
        skipped_symlinks=skipped_symlinks,
    )
