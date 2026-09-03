"""Parallel decompression engine reading seek tables and extracting chunks concurrently.

Uses a Producer-Consumer pipeline:
1. Producer: One thread reads compressed chunks from the archive sequentially.
2. Consumers: N worker threads decompress chunks and write them to individual output files 
   using per-thread file handles and pre-allocated offsets.
"""

import queue
import threading
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Dict, List, Optional, Tuple
import zstandard as zstd

from .archive_format import BlitzArchiveReader, ManifestEntry, SeekEntry, FLAG_STORED
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
class ExtractTask:
    chunk_index: int
    seek_entry: SeekEntry
    raw_bytes: bytes
    targets: List[Tuple[Path, int, int, int, float]]


_thread_local = threading.local()

def _decompress_worker_loop(read_queue: queue.Queue, result_queue: queue.Queue):
    """Worker: decompress a chunk, validate checksum, write all target files."""
    if not hasattr(_thread_local, "decompressor"):
        _thread_local.decompressor = zstd.ZstdDecompressor()
    decompressor = _thread_local.decompressor

    while True:
        task: ExtractTask = read_queue.get()
        if task is None:
            read_queue.task_done()
            break

        seek_entry = task.seek_entry
        raw_bytes = task.raw_bytes
        chunk_index = task.chunk_index

        # 1. Decompress (or use raw bytes for stored chunks)
        if seek_entry.flags & FLAG_STORED:
            decompressed = raw_bytes
        else:
            decompressed = decompressor.decompress(
                raw_bytes,
                max_output_size=seek_entry.original_size + 65536
            )
        del raw_bytes  # free immediately

        # 2. Verify chunk checksum
        actual_digest = compute_digest(decompressed)
        if actual_digest != seek_entry.digest:
            result_queue.put(ValueError(
                f"Chunk {chunk_index} checksum mismatch! Expected {seek_entry.digest:#x}, got {actual_digest:#x}"
            ))
            read_queue.task_done()
            continue

        # 3. Write all target files from this chunk and restore timestamps in parallel
        for target_path, start_off, end_off, file_dest_offset, mtime in task.targets:
            sanitized_dest = sanitize_windows_path(target_path)

            if end_off == -1:
                # Multi-chunk slice: file is pre-allocated; write at file_dest_offset
                slice_data = decompressed
                with open(sanitized_dest, "r+b") as f_out:
                    f_out.seek(file_dest_offset)
                    f_out.write(slice_data)
            else:
                # Single file or slice from bundled batch
                slice_data = decompressed[start_off:end_off]
                with open(sanitized_dest, "wb") as f_out:
                    f_out.write(slice_data)
                try:
                    os.utime(sanitized_dest, (mtime, mtime))
                except OSError:
                    pass

        bytes_written = len(decompressed)
        del decompressed
        result_queue.put(bytes_written)
        read_queue.task_done()


def _reader_thread_func(archive_path: Path, seek_entries: List[SeekEntry], chunk_to_targets: Dict[int, list], read_queue: queue.Queue):
    """Producer: opens the archive once and reads chunks sequentially."""
    sanitized_archive = sanitize_windows_path(archive_path)
    
    # Sort seek entries by offset to ensure purely sequential reading
    # (Though they are normally written sequentially anyway)
    ordered_entries = sorted(enumerate(seek_entries), key=lambda x: x[1].offset)

    try:
        with open(sanitized_archive, "rb") as f:
            for chunk_index, seek_entry in ordered_entries:
                f.seek(seek_entry.offset)
                raw_bytes = f.read(seek_entry.compressed_size)
                
                targets = chunk_to_targets.get(chunk_index, [])
                if not targets:
                    continue
                    
                read_queue.put(ExtractTask(
                    chunk_index=chunk_index,
                    seek_entry=seek_entry,
                    raw_bytes=raw_bytes,
                    targets=targets
                ))
    except Exception as e:
        # If reader fails, push exception to queue so main thread can catch it
        read_queue.put(e)


def decompress(
    archive_path: Path | str,
    output_dir: Path | str,
    workers: int = 0,
    progress_callback: Optional[ProgressCallback] = None
) -> DecompressionResult:
    """Extract a .blitz archive in parallel with full directory, permission, and timestamp restoration."""
    arc_p = Path(archive_path).resolve()
    out_p = Path(output_dir).resolve()
    num_workers = workers or os.cpu_count() or 4
    
    start_time = time.perf_counter()

    # 1. Read Archive Structure
    with open(sanitize_windows_path(arc_p), "rb") as f_in:
        reader = BlitzArchiveReader(f_in)

    # 2 & 3. Recreate Directory Hierarchy and pre-allocate files in single pass
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
                try:
                    os.utime(sanitized_target, (entry.mtime, entry.mtime))
                except OSError:
                    pass
            elif entry.start_chunk != entry.end_chunk:
                # Pre-allocate large file so concurrent worker threads can write non-overlapping slices in r+b mode safely
                with open(sanitized_target, "wb") as f_large:
                    f_large.seek(entry.size - 1)
                    f_large.write(b"\x00")

    # 4. Map chunk index -> files/offsets to write (including mtime for parallel restoration)
    chunk_to_targets: Dict[int, List[Tuple[Path, int, int, int, float]]] = {
        i: [] for i in range(len(reader.seek_entries))
    }

    for entry in reader.manifest:
        if entry.file_type != 0 or entry.size == 0:
            continue
        if entry.start_chunk != entry.end_chunk:
            # Multi-chunk file
            target_path = out_p / entry.path
            for offset_in_span, chunk_idx in enumerate(range(entry.start_chunk, entry.end_chunk + 1)):
                file_dest_offset = offset_in_span * (4 * 1024 * 1024)
                chunk_to_targets[chunk_idx].append((target_path, 0, -1, file_dest_offset, entry.mtime))
        else:
            # Single chunk (or bundle member)
            target_path = out_p / entry.path
            chunk_to_targets[entry.start_chunk].append((
                target_path,
                entry.start_offset,
                entry.end_offset,
                0,
                entry.mtime
            ))

    total_bytes = reader.footer.total_original_size
    bytes_done = 0
    total_valid_chunks = sum(1 for v in chunk_to_targets.values() if v)

    # 5. Parallel Chunk Decompression Pipeline
    read_queue = queue.Queue(maxsize=num_workers * 2)
    result_queue = queue.Queue()

    # Start Worker Threads
    threads = []
    for _ in range(num_workers):
        t = threading.Thread(target=_decompress_worker_loop, args=(read_queue, result_queue), daemon=True)
        t.start()
        threads.append(t)

    # Start Reader Thread
    reader_thread = threading.Thread(
        target=_reader_thread_func, 
        args=(arc_p, reader.seek_entries, chunk_to_targets, read_queue), 
        daemon=True
    )
    reader_thread.start()

    # Main Thread: Consumes results
    completed_chunks = 0
    while completed_chunks < total_valid_chunks:
        res = result_queue.get()
        if isinstance(res, Exception):
            # Error encountered in a worker or reader
            raise res
            
        bytes_done += res
        completed_chunks += 1
        
        elapsed = time.perf_counter() - start_time
        speed = bytes_done / elapsed if elapsed > 0 else 0
        if progress_callback and completed_chunks % 50 == 0:
            progress_callback(ProgressUpdate(
                phase="decompressing",
                files_processed=len(reader.manifest),
                total_files=len(reader.manifest),
                bytes_processed=bytes_done,
                total_bytes=total_bytes,
                current_speed_bps=speed,
                message=f"Extracting [py-pipeline] ({num_workers} workers)..."
            ))

    # Shutdown workers
    for _ in range(num_workers):
        read_queue.put(None)
    for t in threads:
        t.join()
    reader_thread.join()

    # 6. Restore Symlinks, Directory Timestamps, and Multi-chunk File Timestamps
    for entry in reader.manifest:
        target_path = out_p / entry.path
        sanitized_target = sanitize_windows_path(target_path)

        if entry.file_type == 2:  # Symlink
            if entry.symlink_target:
                try:
                    if os.path.islink(sanitized_target) or os.path.exists(sanitized_target):
                        os.remove(sanitized_target)
                    os.symlink(entry.symlink_target, sanitized_target)
                except OSError:
                    pass

        # Only touch directory timestamps and multi-chunk files (single/bundle files were timestamped in workers)
        if entry.file_type == 1 or (entry.file_type == 0 and entry.start_chunk != entry.end_chunk):
            try:
                os.utime(sanitized_target, (entry.mtime, entry.mtime))
                if os.name != "nt" and entry.permissions:
                    os.chmod(sanitized_target, entry.permissions)
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
        backend="py-pipeline",
    )
