"""Parallel compression engine built on a producer-consumer pipeline.

1. Producers (Readers): N threads pull *read groups* off a queue. A group is either
   one solid bundle or every consecutive chunk of a single large file, so each large
   file is streamed through one open handle rather than scattered across threads.
   Groups themselves are read concurrently to keep the device queue busy.
2. Consumers (Workers): N threads pull raw payloads, compress via zstandard (which
   releases the GIL), and compute xxHash-64 digests.
3. Writer (main thread): reorders completed chunks by index and appends them
   sequentially to the archive, keeping the on-disk chunk region contiguous so the
   whole-archive digest stays a simple range hash.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import zstandard as zstd

from .analyzer import FileAnalyzer
from .archive_format import BlitzArchiveWriter, FLAG_STORED, ManifestEntry
from .checksum import compute_digest
from .constants import CHUNK_SIZE
from .scheduler import CompressionJob, WorkScheduler
from .utils import ProgressCallback, ProgressUpdate, sanitize_windows_path

LEVEL_PROFILES: Dict[str, int] = {
    "fast": 1,
    "balanced": 3,
    "high": 9,
    "ultra": 19,
}

# How long the writer tolerates an idle pipeline (readers finished, queues drained)
# before declaring a stall rather than blocking forever.
_STALL_POLLS = 50
_POLL_TIMEOUT = 0.1


class SourceReadError(OSError):
    """Raised when a source file cannot be read, or changed size, during compression."""

    def __init__(self, path: Path | str, reason: str) -> None:
        super().__init__(f"Failed to read source file {path}: {reason}")
        self.path = Path(path)


@dataclass(slots=True)
class CompressionResult:
    archive_path: Path
    total_files: int
    total_entries: int
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
    """Reads job payloads, holding one file handle open across a group of chunks."""

    __slots__ = ("_path", "_fh")

    def __init__(self) -> None:
        self._path: Optional[str] = None
        self._fh = None

    def _open(self, path: str) -> None:
        if self._path == path:
            return
        self.close()
        self._fh = open(path, "rb")
        self._path = path

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
                self._path = None

    def read_job(self, job: CompressionJob) -> bytes:
        """Read a job's payload, raising SourceReadError rather than returning short data.

        Returning b"" on failure (as this used to) archives the file as empty *and*
        checksums the empty payload, so extraction reports success on a corrupt archive.
        Bundle offsets are also computed at scan time, so a file that changed size since
        then would silently misalign every later member of its bundle.
        """
        if job.job_type == "chunk":
            entry = job.source_entry
            assert entry is not None, "chunk job without a source entry"
            path = sanitize_windows_path(entry.path)
            try:
                self._open(path)
                self._fh.seek(job.offset)
                data = self._fh.read(job.length)
            except OSError as exc:
                raise SourceReadError(entry.path, str(exc)) from exc
            if len(data) != job.length:
                raise SourceReadError(
                    entry.path,
                    f"short read at offset {job.offset}: expected {job.length} bytes, "
                    f"got {len(data)} (file changed during compression?)",
                )
            return data

        if job.job_type == "bundle":
            self.close()
            parts: List[bytes] = []
            for member in job.bundle_members:
                path = sanitize_windows_path(member.entry.path)
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError as exc:
                    raise SourceReadError(member.entry.path, str(exc)) from exc
                if len(data) != member.size:
                    raise SourceReadError(
                        member.entry.path,
                        f"size changed during compression: scheduled {member.size} bytes, "
                        f"read {len(data)}",
                    )
                parts.append(data)
            return b"".join(parts)

        raise ValueError(f"Unknown job type: {job.job_type!r}")


def _build_read_groups(ordered_jobs: List[CompressionJob]) -> List[List[CompressionJob]]:
    """Group consecutive chunk jobs that belong to the same source file.

    Each group is handled by a single reader thread with one open handle, so a large
    multi-chunk file is streamed sequentially instead of scattered across threads.
    Bundle jobs are always their own group.
    """
    groups: List[List[CompressionJob]] = []
    current: List[CompressionJob] = []
    current_path: Optional[Path] = None

    for job in ordered_jobs:
        if job.job_type == "chunk" and job.source_entry is not None:
            if current and current_path == job.source_entry.path:
                current.append(job)
                continue
            if current:
                groups.append(current)
            current = [job]
            current_path = job.source_entry.path
        else:
            if current:
                groups.append(current)
                current = []
                current_path = None
            groups.append([job])

    if current:
        groups.append(current)
    return groups


def _put_until_stopped(q: queue.Queue, item, stop_event: threading.Event) -> bool:
    """Put `item` on `q`, giving up if `stop_event` is set. Returns False if abandoned."""
    while not stop_event.is_set():
        try:
            q.put(item, timeout=_POLL_TIMEOUT)
            return True
        except queue.Full:
            continue
    return False


def _reader_thread_func(
    read_groups: queue.Queue,
    read_queue: queue.Queue,
    error_box: queue.Queue,
    stop_event: threading.Event,
) -> None:
    """Producer: reads one group at a time with a single handle."""
    reader = SequentialReader()
    try:
        while not stop_event.is_set():
            try:
                group = read_groups.get(timeout=_POLL_TIMEOUT)
            except queue.Empty:
                continue
            try:
                if group is None:
                    return
                for job in group:
                    payload = JobPayload(job_id=job.job_id, raw_bytes=reader.read_job(job))
                    if not _put_until_stopped(read_queue, payload, stop_event):
                        return
            finally:
                read_groups.task_done()
    except BaseException as exc:
        error_box.put(exc)
        stop_event.set()
    finally:
        reader.close()


def _worker_loop(
    level: int,
    read_queue: queue.Queue,
    write_queue: queue.Queue,
    error_box: queue.Queue,
    stop_event: threading.Event,
) -> None:
    """Consumer: compresses and hashes payloads, then queues them for writing."""
    try:
        compressor = zstd.ZstdCompressor(level=level)
        while not stop_event.is_set():
            try:
                payload: Optional[JobPayload] = read_queue.get(timeout=_POLL_TIMEOUT)
            except queue.Empty:
                continue
            try:
                if payload is None:
                    return
                raw = payload.raw_bytes
                digest = compute_digest(raw)
                compressed = compressor.compress(raw)

                is_stored = len(compressed) >= len(raw)
                if is_stored:
                    compressed = raw

                result = JobResult(
                    job_id=payload.job_id,
                    compressed_bytes=compressed,
                    original_size=len(raw),
                    digest=digest,
                    is_stored=is_stored,
                )
                if not _put_until_stopped(write_queue, result, stop_event):
                    return
            finally:
                read_queue.task_done()
    except BaseException as exc:
        error_box.put(exc)
        stop_event.set()


def compress(
    input_path: Path | str,
    output_path: Path | str,
    level: int | str = 3,
    workers: int = 0,
    readers: int = 0,
    chunk_size: int = CHUNK_SIZE,
    progress_callback: Optional[ProgressCallback] = None,
) -> CompressionResult:
    """Compress a file or directory tree into a seekable .blitz archive."""
    in_p = Path(input_path).resolve()
    out_p = Path(output_path).resolve()
    num_workers = workers or os.cpu_count() or 4

    num_readers = readers if readers > 0 else max(2, min(8, num_workers))
    level_int = LEVEL_PROFILES.get(level.lower().strip(), 3) if isinstance(level, str) else int(level)

    start_time = time.perf_counter()

    # 1. Discovery
    manifest = FileAnalyzer(in_p).scan()

    # 2. Scheduling, in directory-traversal order
    scheduler = WorkScheduler(chunk_size=chunk_size, bundle_target=chunk_size)
    ordered_jobs = sorted(scheduler.schedule(manifest), key=lambda j: j.job_id)
    total_jobs = len(ordered_jobs)

    job_chunk_index_map: Dict[int, int] = {j.job_id: i for i, j in enumerate(ordered_jobs)}

    # 3. Manifest construction
    chunks_by_path: Dict[str, List[Tuple[int, CompressionJob]]] = defaultdict(list)
    for j in ordered_jobs:
        if j.job_type == "chunk" and j.source_entry:
            chunks_by_path[j.source_entry.relative_path].append((job_chunk_index_map[j.job_id], j))

    manifest_entries: List[ManifestEntry] = []
    total_source_files = 0

    for entry in manifest.entries:
        if entry.file_type != 0 or entry.size == 0:
            if entry.file_type == 0:
                total_source_files += 1
            manifest_entries.append(ManifestEntry(
                path=entry.relative_path, size=entry.size, mtime=entry.mtime,
                file_type=entry.file_type, symlink_target=entry.symlink_target,
                permissions=entry.permissions, win_attrs=entry.win_attrs,
                start_chunk=-1, start_offset=0, end_chunk=-1, end_offset=0,
            ))
            continue

        total_source_files += 1

        if entry.size >= scheduler.chunk_size:
            chunks_info = chunks_by_path.get(entry.relative_path)
            if not chunks_info:
                raise RuntimeError(
                    f"Internal error: no chunk jobs scheduled for {entry.relative_path!r} "
                    f"({entry.size} bytes)"
                )
            manifest_entries.append(ManifestEntry(
                path=entry.relative_path, size=entry.size, mtime=entry.mtime,
                file_type=0, symlink_target=None,
                permissions=entry.permissions, win_attrs=entry.win_attrs,
                start_chunk=chunks_info[0][0], start_offset=0,
                end_chunk=chunks_info[-1][0], end_offset=chunks_info[-1][1].length,
            ))

    for j in ordered_jobs:
        if j.job_type != "bundle":
            continue
        chunk_idx = job_chunk_index_map[j.job_id]
        for member in j.bundle_members:
            manifest_entries.append(ManifestEntry(
                path=member.entry.relative_path, size=member.size,
                mtime=member.entry.mtime, file_type=0, symlink_target=None,
                permissions=member.entry.permissions, win_attrs=member.entry.win_attrs,
                start_chunk=chunk_idx, start_offset=member.offset_in_bundle,
                end_chunk=chunk_idx, end_offset=member.offset_in_bundle + member.size,
            ))

    files_per_job: Dict[int, int] = {}
    for j in ordered_jobs:
        if j.job_type == "bundle":
            files_per_job[j.job_id] = len(j.bundle_members)
    for chunks_info in chunks_by_path.values():
        last_job_id = chunks_info[-1][1].job_id
        files_per_job[last_job_id] = files_per_job.get(last_job_id, 0) + 1

    out_p.parent.mkdir(parents=True, exist_ok=True)
    temp_archive_path = out_p.with_suffix(f"{out_p.suffix}.tmp")

    total_bytes = manifest.total_bytes
    bytes_done = 0
    files_done = 0
    backend_str = "py-pipeline"

    # 4. Pipeline
    stop_event = threading.Event()
    error_box: queue.Queue = queue.Queue()
    read_queue: queue.Queue = queue.Queue(maxsize=max(4, num_readers * 2))
    write_queue: queue.Queue = queue.Queue(maxsize=max(8, num_workers * 4))

    read_groups: queue.Queue = queue.Queue()
    for group in _build_read_groups(ordered_jobs):
        read_groups.put(group)
    for _ in range(num_readers):
        read_groups.put(None)

    worker_threads = [
        threading.Thread(
            target=_worker_loop,
            args=(level_int, read_queue, write_queue, error_box, stop_event),
            daemon=True,
        )
        for _ in range(num_workers)
    ]
    reader_threads = [
        threading.Thread(
            target=_reader_thread_func,
            args=(read_groups, read_queue, error_box, stop_event),
            daemon=True,
        )
        for _ in range(num_readers)
    ]

    try:
        for t in worker_threads:
            t.start()
        for t in reader_threads:
            t.start()

        pending: Dict[int, JobResult] = {}
        next_write_id = 0
        idle_polls = 0
        last_callback = 0.0

        with open(temp_archive_path, "wb") as f_out:
            writer = BlitzArchiveWriter(f_out, default_chunk_size=scheduler.chunk_size)

            while next_write_id < total_jobs:
                if not error_box.empty():
                    raise error_box.get()

                try:
                    res: JobResult = write_queue.get(timeout=_POLL_TIMEOUT)
                    idle_polls = 0
                except queue.Empty:
                    readers_done = not any(t.is_alive() for t in reader_threads)
                    if readers_done and read_queue.empty() and write_queue.empty():
                        idle_polls += 1
                        if idle_polls >= _STALL_POLLS:
                            if not error_box.empty():
                                raise error_box.get()
                            raise RuntimeError(
                                f"Compression pipeline stalled after "
                                f"{next_write_id}/{total_jobs} chunks"
                            )
                    continue

                pending[res.job_id] = res

                while next_write_id in pending:
                    ready = pending.pop(next_write_id)
                    writer.write_chunk(
                        chunk_index=job_chunk_index_map[ready.job_id],
                        compressed_bytes=ready.compressed_bytes,
                        original_size=ready.original_size,
                        digest=ready.digest,
                        overlap_prefix_size=0,
                        flags=FLAG_STORED if ready.is_stored else 0,
                    )

                    bytes_done += ready.original_size
                    files_done += files_per_job.get(ready.job_id, 0)
                    next_write_id += 1

                    now = time.perf_counter()
                    if progress_callback and (
                        now - last_callback >= _POLL_TIMEOUT or next_write_id == total_jobs
                    ):
                        last_callback = now
                        elapsed = now - start_time
                        progress_callback(ProgressUpdate(
                            phase="compressing",
                            files_processed=files_done,
                            total_files=total_source_files,
                            bytes_processed=bytes_done,
                            total_bytes=total_bytes,
                            current_speed_bps=bytes_done / elapsed if elapsed > 0 else 0.0,
                            message=(
                                f"Compressing [{backend_str}] "
                                f"({num_workers} workers, {num_readers} readers)..."
                            ),
                        ))

            writer.finalize(manifest_entries=manifest_entries, total_original_size=total_bytes)
    finally:
        stop_event.set()
        for _ in range(num_workers):
            try:
                read_queue.put_nowait(None)
            except queue.Full:
                pass
        for t in reader_threads:
            t.join(timeout=5)
        for t in worker_threads:
            t.join(timeout=5)

    # 5. Publish finished archive
    for _attempt in range(5):
        try:
            if out_p.exists():
                out_p.unlink()
            temp_archive_path.rename(out_p)
            break
        except PermissionError:
            time.sleep(0.5)
    else:
        temp_archive_path.rename(out_p)

    total_duration = time.perf_counter() - start_time
    final_compressed_size = out_p.stat().st_size
    ratio = total_bytes / final_compressed_size if final_compressed_size > 0 else 1.0
    throughput = (total_bytes / (1024 * 1024)) / total_duration if total_duration > 0 else 0.0

    return CompressionResult(
        archive_path=out_p,
        total_files=total_source_files,
        total_entries=len(manifest.entries),
        original_size=total_bytes,
        compressed_size=final_compressed_size,
        duration_seconds=total_duration,
        compression_ratio=ratio,
        throughput_mb_s=throughput,
        chunks_created=total_jobs,
        backend=backend_str,
    )
