"""Parallel compression engine using a strict Producer-Consumer pipeline.

1. Producers (Readers): Read files from disk strictly in directory 
   order, maintaining OS prefetching/read-ahead and avoiding random seeks.
2. Consumers (Workers): N threads pull raw bytes, compress via zstandard 
   (releasing the GIL), and compute xxhash checksums.
3. Writer: Collects out-of-order completed chunks, re-orders them sequentially,
   and appends to the final archive.
"""

import queue
import threading
from collections import defaultdict
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Dict, List, Optional, Tuple
import zstandard as zstd

from .analyzer import FileAnalyzer, FileEntry, FileManifest
from .archive_format import BlitzArchiveWriter, ManifestEntry, FLAG_STORED
from .checksum import IncrementalHasher, compute_digest
from .scheduler import CompressionJob, WorkScheduler
from .utils import ProgressCallback, ProgressUpdate, sanitize_windows_path

@dataclass(slots=True)
class CompressionResult:
    archive_path: Path
    total_files: int
    original_size: int
    compressed_size: int
    duration_seconds: float
    compression_ratio: float
    throughput_mb_s: float
    chunks_created: int
    backend: str


@dataclass(slots=True)
class JobPayload:
    job_id: int
    raw_bytes: bytes


@dataclass(slots=True)
class JobResult:
    job_id: int
    compressed_bytes: bytes
    original_size: int
    digest: int
    is_stored: bool


class SequentialReader:
    """Reads jobs sequentially from disk using native C-accelerated file handles."""
    def __init__(self):
        self._current_path = None
        self._current_py_f = None

    def _open(self, path: str):
        if self._current_path == path:
            return
        self._close()
        self._current_path = path
        self._current_py_f = open(path, "rb")

    def _read(self, length: int) -> bytes:
        return self._current_py_f.read(length)

    def _close(self):
        if self._current_py_f:
            self._current_py_f.close()
            self._current_py_f = None
        self._current_path = None

    def read_job(self, job: CompressionJob) -> bytes:
        if job.job_type == "chunk":
            path = sanitize_windows_path(job.source_entry.path)
            try:
                self._open(path)
                # Seek to chunk offset for multi-chunk files
                self._current_py_f.seek(job.offset)
                return self._read(job.length)
            except OSError:
                return b""
        elif job.job_type == "bundle":
            self._close()
            parts = []
            for member in job.bundle_members:
                path = sanitize_windows_path(member.entry.path)
                try:
                    with open(path, "rb") as f:
                        parts.append(f.read())
                except OSError:
                    pass
            return b"".join(parts)
        return b""


_thread_local = threading.local()

def _worker_loop(level: int, read_queue: queue.Queue, write_queue: queue.Queue):
    """Consumer thread: pulls raw bytes, compresses, hashes, queues for writing."""
    try:
        if not hasattr(_thread_local, "compressor"):
            _thread_local.compressor = zstd.ZstdCompressor(level=level)
        compressor = _thread_local.compressor

        while True:
            payload: JobPayload = read_queue.get()
            if payload is None: # Sentinel value to shutdown
                read_queue.task_done()
                break

            raw_data = payload.raw_bytes
            digest = compute_digest(raw_data)
            compressed = compressor.compress(raw_data)

            is_stored = False
            if len(compressed) >= len(raw_data):
                compressed = raw_data
                is_stored = True

            result = JobResult(
                job_id=payload.job_id,
                compressed_bytes=compressed,
                original_size=len(raw_data),
                digest=digest,
                is_stored=is_stored
            )
            write_queue.put(result)
            read_queue.task_done()
    except Exception as e:
        write_queue.put(e) # Pass exception to main thread


def _reader_thread_func(read_jobs: queue.Queue, read_queue: queue.Queue, write_queue: queue.Queue):
    """Producer thread worker: pulls jobs, reads files, pushes to work queue."""
    try:
        reader = SequentialReader()
        while True:
            job: CompressionJob = read_jobs.get()
            if job is None:
                read_jobs.task_done()
                break
            raw_bytes = reader.read_job(job)
            read_queue.put(JobPayload(job_id=job.job_id, raw_bytes=raw_bytes))
            read_jobs.task_done()
        reader._close()
    except Exception as e:
        write_queue.put(e)


LEVEL_PROFILES: Dict[str, int] = {
    "fast": 1,
    "balanced": 3,
    "high": 9,
    "ultra": 19,
}


def compress(
    input_path: Path | str,
    output_path: Path | str,
    level: int | str = 3,
    workers: int = 0,
    progress_callback: Optional[ProgressCallback] = None,
) -> CompressionResult:
    in_p = Path(input_path).resolve()
    out_p = Path(output_path).resolve()
    num_workers = workers or os.cpu_count() or 4

    if isinstance(level, str):
        level_int = LEVEL_PROFILES.get(level.lower().strip(), 3)
    else:
        level_int = int(level)

    start_time = time.perf_counter()

    # 1. Discovery & Analysis
    analyzer = FileAnalyzer(in_p)
    manifest = analyzer.scan()

    # 2. Work Scheduling (Strictly Sequential)
    scheduler = WorkScheduler()
    jobs = scheduler.schedule(manifest)
    ordered_jobs = sorted(jobs, key=lambda j: j.job_id)

    # 3. Chunk index assignments & Manifest building
    job_chunk_index_map: Dict[int, int] = {
        j.job_id: chunk_idx for chunk_idx, j in enumerate(ordered_jobs)
    }

    manifest_entries: List[ManifestEntry] = []
    large_file_chunks: Dict[str, List[Tuple[int, CompressionJob]]] = defaultdict(list)
    
    for j in ordered_jobs:
        if j.job_type == "chunk" and j.source_entry:
            chunk_idx = job_chunk_index_map[j.job_id]
            large_file_chunks[j.source_entry.relative_path].append((chunk_idx, j))

    for entry in manifest.entries:
        if entry.file_type != 0 or entry.size == 0:
            manifest_entries.append(ManifestEntry(
                path=entry.relative_path, size=entry.size, mtime=entry.mtime,
                file_type=entry.file_type, symlink_target=entry.symlink_target,
                permissions=entry.permissions, win_attrs=entry.win_attrs,
                start_chunk=0, start_offset=0, end_chunk=0, end_offset=0,
            ))
            continue

        if entry.size >= scheduler.chunk_size:
            chunks_info = large_file_chunks.get(entry.relative_path, [])
            if chunks_info:
                start_chunk = chunks_info[0][0]
                end_chunk = chunks_info[-1][0]
                last_job = chunks_info[-1][1]
                manifest_entries.append(ManifestEntry(
                    path=entry.relative_path, size=entry.size, mtime=entry.mtime,
                    file_type=0, symlink_target=None,
                    permissions=entry.permissions, win_attrs=entry.win_attrs,
                    start_chunk=start_chunk, start_offset=0,
                    end_chunk=end_chunk, end_offset=last_job.length,
                ))
        else:
            pass # handled below for bundles

    for j in ordered_jobs:
        if j.job_type == "bundle":
            chunk_idx = job_chunk_index_map[j.job_id]
            for member in j.bundle_members:
                manifest_entries.append(ManifestEntry(
                    path=member.entry.relative_path, size=member.size,
                    mtime=member.entry.mtime, file_type=0, symlink_target=None,
                    permissions=member.entry.permissions, win_attrs=member.entry.win_attrs,
                    start_chunk=chunk_idx, start_offset=member.offset_in_bundle,
                    end_chunk=chunk_idx, end_offset=member.offset_in_bundle + member.size,
                ))

    out_p.parent.mkdir(parents=True, exist_ok=True)
    temp_archive_path = out_p.with_suffix(f"{out_p.suffix}.tmp")

    total_bytes = manifest.total_bytes
    bytes_done = 0
    backend_str = "py-pipeline"

    # 4. Pipeline Execution
    # Start Reader Threads (scaled to overlap disk handle latency across bundles)
    num_readers = max(12, num_workers * 2)
    read_queue = queue.Queue(maxsize=num_readers * 2)
    write_queue = queue.Queue()

    # Start Worker Threads
    worker_threads = []
    for _ in range(num_workers):
        t = threading.Thread(target=_worker_loop, args=(level_int, read_queue, write_queue), daemon=True)
        t.start()
        worker_threads.append(t)

    read_jobs = queue.Queue()
    for job in ordered_jobs:
        read_jobs.put(job)
    for _ in range(num_readers):
        read_jobs.put(None) # shutdown sentinels
        
    reader_threads = []
    for _ in range(num_readers):
        t = threading.Thread(target=_reader_thread_func, args=(read_jobs, read_queue, write_queue), daemon=True)
        t.start()
        reader_threads.append(t)

    # Main Thread: acts as the Writer
    pending_writes: Dict[int, JobResult] = {}
    next_write_id = 0
    total_jobs = len(ordered_jobs)

    with open(temp_archive_path, "wb") as f_out:
        writer = BlitzArchiveWriter(f_out)

        while next_write_id < total_jobs:
            res = write_queue.get()
            if isinstance(res, Exception):
                raise RuntimeError("Exception occurred in worker/reader thread") from res
            
            pending_writes[res.job_id] = res
            
            # Write out any sequentially ready chunks
            while next_write_id in pending_writes:
                ready_res = pending_writes.pop(next_write_id)
                chunk_index = job_chunk_index_map[ready_res.job_id]
                
                writer.write_chunk(
                    chunk_index=chunk_index,
                    compressed_bytes=ready_res.compressed_bytes,
                    original_size=ready_res.original_size,
                    digest=ready_res.digest,
                    overlap_prefix_size=0,
                    flags=FLAG_STORED if ready_res.is_stored else 0,
                )

                bytes_done += ready_res.original_size
                next_write_id += 1

                # Progress update
                elapsed = time.perf_counter() - start_time
                speed = bytes_done / elapsed if elapsed > 0 else 0
                if progress_callback and next_write_id % 50 == 0:
                    progress_callback(ProgressUpdate(
                        phase="compressing",
                        files_processed=len(manifest.entries),
                        total_files=len(manifest.entries),
                        bytes_processed=bytes_done,
                        total_bytes=total_bytes,
                        current_speed_bps=speed,
                        message=f"Compressing [{backend_str}] ({num_workers} workers)...",
                    ))

        # Finalize archive
        writer.finalize(manifest_entries=manifest_entries, total_original_size=total_bytes)

    # Shutdown workers
    for _ in range(num_workers):
        read_queue.put(None)
    for t in worker_threads:
        t.join()
    for t in reader_threads:
        t.join()

    # Atomic rename (retry on Windows if Defender holds a temp handle)
    import time as _time
    for _attempt in range(5):
        try:
            if out_p.exists():
                out_p.unlink()
            temp_archive_path.rename(out_p)
            break
        except PermissionError:
            _time.sleep(0.5)
    else:
        temp_archive_path.rename(out_p)

    total_duration = time.perf_counter() - start_time
    final_compressed_size = out_p.stat().st_size
    ratio = total_bytes / final_compressed_size if final_compressed_size > 0 else 1.0
    throughput = (total_bytes / (1024 * 1024)) / total_duration if total_duration > 0 else 0.0

    return CompressionResult(
        archive_path=out_p,
        total_files=len(manifest.entries),
        original_size=total_bytes,
        compressed_size=final_compressed_size,
        duration_seconds=total_duration,
        compression_ratio=ratio,
        throughput_mb_s=throughput,
        chunks_created=total_jobs,
        backend=backend_str,
    )
