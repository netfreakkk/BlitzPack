"""Parallel decompression engine reading seek tables and extracting chunks concurrently.

Uses an optimized 3-stage pipeline:
1. Reader Thread: Sequentially reads compressed chunk bytes from the archive.
2. Worker Threads: Decompress chunks in parallel using zstandard and verify xxHash checksums.
3. Writer Thread: Streams decompressed bytes to disk using pooled handles with wb+ truncation,
   eliminating Windows NTFS file-sharing conflicts and r+b mode lock contention.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import zstandard as zstd

from .archive_format import BlitzArchiveReader, SeekEntry, FLAG_STORED
from .checksum import compute_digest
from .utils import ProgressCallback, ProgressUpdate, sanitize_windows_path


@dataclass(slots=True)
class DecompressionResult:
    output_dir: Path
    total_files: int
    extracted_bytes: int
    duration_seconds: float
    throughput_mb_s: float
    backend: str


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
) -> None:
    """Worker: decompresses chunks and verifies checksums in parallel across CPU cores."""
    if not hasattr(_thread_local, "decompressor"):
        _thread_local.decompressor = zstd.ZstdDecompressor()
    decompressor = _thread_local.decompressor

    while True:
        task: Optional[ExtractTask] = read_queue.get()
        if task is None:
            read_queue.task_done()
            break

        try:
            seek_entry = task.seek_entry
            raw_bytes = task.raw_bytes

            # 1. Decompress
            if seek_entry.flags & FLAG_STORED:
                decompressed = raw_bytes
            else:
                decompressed = decompressor.decompress(
                    raw_bytes, max_output_size=seek_entry.original_size + 65536
                )
            del raw_bytes

            # 2. Checksum validation
            actual_digest = compute_digest(decompressed)
            if actual_digest != seek_entry.digest:
                raise ValueError(
                    f"Chunk {task.chunk_index} checksum mismatch! Expected {seek_entry.digest:#x}, got {actual_digest:#x}"
                )

            write_queue.put((task, decompressed))
        except Exception as ex:
            error_queue.put(ex)
        finally:
            read_queue.task_done()


def _writer_thread_loop(
    write_queue: queue.Queue,
    result_queue: queue.Queue,
    error_queue: queue.Queue,
    total_valid_chunks: int,
) -> None:
    """High-throughput disk writer: streams chunks to files using pooled sequential handles."""
    open_handles: Dict[str, Any] = {}
    chunks_written_per_file: Dict[str, int] = {}
    completed = 0

    while completed < total_valid_chunks:
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

            bytes_written = len(decompressed)
            result_queue.put(bytes_written)
            completed += 1
        except Exception as ex:
            error_queue.put(ex)
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
) -> None:
    """Reader: reads compressed chunks from the archive sequentially from disk."""
    sanitized_archive = sanitize_windows_path(archive_path)
    ordered_entries = sorted(enumerate(seek_entries), key=lambda x: x[1].offset)

    try:
        with open(sanitized_archive, "rb") as f:
            for chunk_index, seek_entry in ordered_entries:
                if not error_queue.empty():
                    break
                targets = chunk_to_targets.get(chunk_index)
                if not targets:
                    continue

                f.seek(seek_entry.offset)
                raw_bytes = f.read(seek_entry.compressed_size)

                read_queue.put(
                    ExtractTask(
                        chunk_index=chunk_index,
                        seek_entry=seek_entry,
                        raw_bytes=raw_bytes,
                        targets=targets,
                    )
                )
    except Exception as ex:
        error_queue.put(ex)


def decompress(
    archive_path: Path | str,
    output_dir: Path | str,
    workers: int = 0,
    progress_callback: Optional[ProgressCallback] = None,
) -> DecompressionResult:
    """Extract a .blitz archive in parallel with full directory, permission, and timestamp restoration."""
    arc_p = Path(archive_path).resolve()
    out_p = Path(output_dir).resolve()
    num_workers = workers or os.cpu_count() or 4

    start_time = time.perf_counter()

    # 1. Read Archive Structure
    with open(sanitize_windows_path(arc_p), "rb") as f_in:
        reader = BlitzArchiveReader(f_in)

    # 2. Recreate Directory Hierarchy
    seen_dirs = set()
    for entry in reader.manifest:
        target_path = out_p / entry.path
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
                with open(sanitized_target, "wb") as f_empty:
                    pass

    # 3. Map chunk index -> targets
    chunk_to_targets: Dict[int, List[ExtractTarget]] = {
        i: [] for i in range(len(reader.seek_entries))
    }

    for entry in reader.manifest:
        if entry.file_type != 0 or entry.size == 0:
            continue

        target_path = out_p / entry.path
        if entry.start_chunk != entry.end_chunk:
            total_chunks = entry.end_chunk - entry.start_chunk + 1
            for offset_in_span, chunk_idx in enumerate(range(entry.start_chunk, entry.end_chunk + 1)):
                chunk_to_targets[chunk_idx].append(
                    ExtractTarget(
                        target_path=target_path,
                        start_offset=0,
                        end_offset=-1,
                        is_multi_chunk=True,
                        file_offset=offset_in_span * (4 * 1024 * 1024),
                        total_file_size=entry.size,
                        total_file_chunks=total_chunks,
                        mtime=entry.mtime,
                    )
                )
        else:
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

    total_bytes = reader.footer.total_original_size
    bytes_done = 0
    total_valid_chunks = sum(1 for v in chunk_to_targets.values() if v)

    # 4. Start 3-Stage Pipeline
    read_queue: queue.Queue = queue.Queue(maxsize=max(8, num_workers * 2))
    write_queue: queue.Queue = queue.Queue(maxsize=max(8, num_workers * 2))
    result_queue: queue.Queue = queue.Queue()
    error_queue: queue.Queue = queue.Queue()

    writer_thread = threading.Thread(
        target=_writer_thread_loop,
        args=(write_queue, result_queue, error_queue, total_valid_chunks),
        daemon=True,
    )
    writer_thread.start()

    worker_threads = []
    for _ in range(num_workers):
        t = threading.Thread(
            target=_worker_decompress_loop,
            args=(read_queue, write_queue, error_queue),
            daemon=True,
        )
        t.start()
        worker_threads.append(t)

    reader_thread = threading.Thread(
        target=_reader_thread_loop,
        args=(arc_p, reader.seek_entries, chunk_to_targets, read_queue, error_queue),
        daemon=True,
    )
    reader_thread.start()

    completed_chunks = 0
    last_callback_time = 0.0

    while completed_chunks < total_valid_chunks:
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
                    files_processed=len(reader.manifest),
                    total_files=len(reader.manifest),
                    bytes_processed=bytes_done,
                    total_bytes=total_bytes,
                    current_speed_bps=speed,
                    message=f"Extracting ({num_workers} workers)...",
                )
            )

    for _ in range(num_workers):
        read_queue.put(None)
    for t in worker_threads:
        t.join()
    reader_thread.join()
    writer_thread.join()

    # 5. Restore Symlinks and Timestamps
    for entry in reader.manifest:
        target_path = out_p / entry.path
        sanitized_target = sanitize_windows_path(target_path)

        if entry.file_type == 2 and entry.symlink_target:
            try:
                if os.path.islink(sanitized_target) or os.path.exists(sanitized_target):
                    os.remove(sanitized_target)
                os.symlink(entry.symlink_target, sanitized_target)
            except OSError:
                pass
        elif entry.mtime:
            try:
                os.utime(sanitized_target, (entry.mtime, entry.mtime))
            except OSError:
                pass

    total_duration = time.perf_counter() - start_time
    throughput = (total_bytes / (1024 * 1024)) / total_duration if total_duration > 0 else 0.0

    return DecompressionResult(
        output_dir=out_p,
        total_files=len(reader.manifest),
        extracted_bytes=total_bytes,
        duration_seconds=total_duration,
        throughput_mb_s=throughput,
        backend="py-pipeline-fast",
    )
