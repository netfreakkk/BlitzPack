BlitzPack — Additional Patches (consolidated)
Everything still worth doing that is not in the two docs already produced:

excludes ROBUSTNESS_PATCH.md (path-traversal, reader DoS bounds, symlink safety, --readers, CI lint)
excludes PERF_AND_ROADMAP.md (performance optimizations, benchmarking, the large-feature roadmap)
This file is the remaining correctness fix plus the near-term functional features that make the tool robust and directly enable a good UI (cancellation, random-access extraction, clean exit codes). Apply ROBUSTNESS_PATCH.md first — a couple of items here build on it and I note where.

Sections:

A. Correctness / robustness
A1. Decompressor error-path thread leak (blocking get())
A2. Compress stall-detector guard
A3. Drop the redundant sorted()
B. Functional features (robust + promising, UI-enabling)
B1. Cancellation token in the library API — the UI needs this
B2. Selective / random-access extraction — the format's differentiator
B3. CLI exit-code taxonomy + clean errors
B4. Deterministic / reproducible archives
B5. --skip-unreadable lenient mode (design only)
C. Polish / hygiene
D. FORMAT.md — write the spec
E. Tracked but not patched here
F. Commit sequence
A1, A2, B1 all touch the same thread machinery, so they're presented together as one authoritative rewrite of the decompressor's thread functions. B1 and B2 both edit decompress(), so I give those as layered edits in one place.

A. Correctness / robustness
A1. Decompressor error-path thread leak (real bug)
compress() was hardened last review with a stop_event + timeout polling. decompress() was not — its workers still call read_queue.get() with no timeout, and the shutdown finally sends sentinels via put_nowait, which silently drops them when the queue is full. On an error mid-extraction with a full read queue, a worker blocks forever (daemon thread, so it dies only at process exit — a real leak in the long-lived GUI you're about to build).

Fix: give decompress the same stop_event treatment. This also lays the groundwork for A2/B1 (cancellation), because a cancel just sets the same event.

Add BlitzCancelled to blitzpack/utils.py (used by A2 and B1 too):

class BlitzCancelled(Exception):
    """Raised when a compression or extraction is cancelled via a cancel_event."""
Replace all three thread functions in blitzpack/decompressor.py with these (they add a stop_event parameter and never block indefinitely):

def _worker_decompress_loop(read_queue, write_queue, error_queue, stop_event):
    """Worker: decompress + checksum, cooperating with stop_event for clean shutdown."""
    if not hasattr(_thread_local, "decompressor"):
        _thread_local.decompressor = zstd.ZstdDecompressor()
    decompressor = _thread_local.decompressor

    while not stop_event.is_set():
        try:
            task = read_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if task is None:
            read_queue.task_done()
            break
        try:
            seek_entry = task.seek_entry
            if seek_entry.flags & FLAG_STORED:
                decompressed = task.raw_bytes
            else:
                decompressed = decompressor.decompress(
                    task.raw_bytes, max_output_size=seek_entry.original_size + 65536
                )
            if compute_digest(decompressed) != seek_entry.digest:
                raise ValueError(
                    f"Chunk {task.chunk_index} checksum mismatch! "
                    f"Expected {seek_entry.digest:#x}"
                )
            # push, but don't block forever if the writer has stopped
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


def _writer_thread_loop(write_queue, result_queue, error_queue, total_valid_chunks, stop_event):
    """Disk writer: streams chunks to files, honoring stop_event."""
    open_handles = {}
    chunks_written_per_file = {}
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


def _reader_thread_loop(archive_path, seek_entries, chunk_to_targets, read_queue, error_queue, stop_event):
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
                    chunk_index=chunk_index, seek_entry=seek_entry,
                    raw_bytes=raw_bytes, targets=targets,
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
In decompress(), create the event and pass it to every thread. Replace the pipeline setup:

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
with:

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
And in the teardown finally, set the event first so the polling threads exit promptly:

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
A2. Compress stall-detector guard
The writer declares a stall after _STALL_POLLS * _POLL_TIMEOUT = 5 s of empty queues + dead readers. That's safe at 4 MB chunks, but if you ever expose a large chunk_size, a single slow compress() of one in-flight chunk can trip a false "pipeline stalled". Make the threshold scale with chunk size instead of being a constant. In blitzpack/compressor.py:

_POLL_TIMEOUT = 0.1
# Allow more idle time before declaring a stall when chunks are large (a single
# in-flight compress can legitimately take a while). ~5 s at 4 MB, more above that.
def _stall_polls(chunk_size: int) -> int:
    return max(50, int((chunk_size / (4 * 1024 * 1024)) * 50))
Then in the writer loop, replace the if idle_polls >= _STALL_POLLS: check with if idle_polls >= _stall_polls(scheduler.chunk_size):. Minor, but removes a latent false-positive.

A3. Drop the redundant sorted()
In compress(), scheduler.schedule() already emits jobs in job_id order, so:

    ordered_jobs = sorted(scheduler.schedule(manifest), key=lambda j: j.job_id)
is an O(n log n) no-op. Replace with:

    ordered_jobs = list(scheduler.schedule(manifest))
(If you want a belt-and-suspenders assertion instead: keep list(...) and add assert all(j.job_id == i for i, j in enumerate(ordered_jobs)) under a debug flag.)

B. Functional features
B1. Cancellation token in the library API — do this before the UI
The GUI runs compress/decompress on a background thread and currently has no clean way to stop them (only process exit). Expose the internal stop_event via an optional cancel_event the caller owns and can set. A2's decompressor rewrite already cooperates; you just wire the check.

blitzpack/compressor.py — add the parameter and the check.

Signature:

def compress(
    input_path: Path | str,
    output_path: Path | str,
    level: int | str = 3,
    workers: int = 0,
    readers: int = 0,
    chunk_size: int = CHUNK_SIZE,
    deterministic: bool = False,          # see B4
    cancel_event: "threading.Event | None" = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> CompressionResult:
Import the exception at the top: from .utils import ProgressCallback, ProgressUpdate, sanitize_windows_path, BlitzCancelled.

In the main writer loop, right after while next_write_id < total_jobs::

            while next_write_id < total_jobs:
                if cancel_event is not None and cancel_event.is_set():
                    raise BlitzCancelled("compression cancelled")
                if not error_box.empty():
                    raise error_box.get()
The existing finally already tears the pipeline down, and the partial .tmp file is never promoted to the final output on this path — good. (Optionally unlink the .tmp in an except BlitzCancelled before re-raising, to not leave it behind.)

blitzpack/decompressor.py — add cancel_event to the signature and check it in the main result loop:

    while completed_chunks < total_valid_chunks:
        if cancel_event is not None and cancel_event.is_set():
            raise BlitzCancelled("extraction cancelled")
        if not error_queue.empty():
            raise error_queue.get()
Import BlitzCancelled there too. The except BaseException: cleanup block already runs on cancel; if you set cleanup_on_error=True, a cancelled extraction into a fresh dir is also removed. That's exactly the behavior a UI "Cancel" button wants.

This is small but it's the difference between a UI that can cancel a 5 GB job and one that can't.

B2. Selective / random-access extraction — the differentiator
The whole point of the seekable format is extracting one file or subtree without decompressing everything — and no command exposes it. The pipeline already only reads chunks that have targets, so this is almost free: filter which manifest entries become targets, and the reader naturally skips every unreferenced chunk.

blitzpack/decompressor.py — add a selector helper at module level:

def _select_entries(manifest, include):
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
        p = e.path
        if any(p == inc or p.startswith(inc + "/") for inc in norm):
            out.append(e)
    return out
Add include: "list[str] | None" = None to the decompress() signature, then use a filtered list everywhere the function currently iterates reader.manifest:

    selected = _select_entries(reader.manifest, include)
Directory-creation loop: for entry in selected:
chunk_to_targets build loop: for entry in selected:
Metadata-restore loop: for entry in selected:
Progress + result: use len(selected) where it currently uses len(reader.manifest).
If include is given and nothing matched, raise so the user isn't silently handed an empty dir:

    selected = _select_entries(reader.manifest, include)
    if include and not selected:
        raise FileNotFoundError(f"No entries in archive matched: {include}")
cli.py — new extract command (keep decompress as the extract-everything command):

def handle_extract(args: argparse.Namespace) -> None:
    arc_path = Path(args.archive).resolve()
    if not arc_path.is_file():
        console.print(f"[bold red]Error:[/] Archive file does not exist: {arc_path}")
        sys.exit(1)

    out_dir = Path(args.output).resolve() if args.output else arc_path.with_suffix("")
    workers = args.workers or os.cpu_count() or 4

    console.print(Panel(
        f"[bold cyan]Archive:[/] {arc_path}\n"
        f"[bold cyan]Extracting:[/] {', '.join(args.paths)}\n"
        f"[bold cyan]Destination:[/] {out_dir}",
        title="BlitzPack Extract (selective)",
        border_style="cyan",
    ))

    res = decompress(
        archive_path=arc_path,
        output_dir=out_dir,
        workers=workers,
        include=args.paths,
    )

    console.print(Panel(
        f"[bold green]Extracted {res.total_files} entr{'y' if res.total_files == 1 else 'ies'}.[/]\n"
        f"Total Size:  {format_bytes(res.extracted_bytes)}\n"
        f"Duration:    {res.duration_seconds:.2f}s",
        title="Extraction Complete",
        border_style="green",
    ))
Register it in main():

    p_extract = subparsers.add_parser(
        "extract", help="Extract only specific files or folders (random access)"
    )
    p_extract.add_argument("archive", help="Path to .blitz archive")
    p_extract.add_argument("paths", nargs="+", help="Archive-relative path(s) to extract")
    p_extract.add_argument("-o", "--output", help="Target extraction directory")
    p_extract.add_argument("-w", "--workers", type=int, default=0, help="CPU worker count")
    p_extract.set_defaults(func=handle_extract)
Note the honesty caveat for the README: random access is at chunk granularity. Pulling one tiny file that lives in a solid bundle still decompresses that whole bundle chunk (but nothing else). Large files are extracted at true 4 MB-chunk granularity. Either way you avoid reading the rest of the archive — the real win over solid .7z/.rar.

Test (tests/test_integrity.py):

def test_selective_extraction(tmp_path: Path):
    src = tmp_path / "src"
    (src / "keep").mkdir(parents=True)
    (src / "drop").mkdir()
    (src / "keep" / "a.txt").write_text("A")
    (src / "keep" / "b.txt").write_text("B")
    (src / "drop" / "c.txt").write_text("C")

    archive = tmp_path / "a.blitz"
    compress(src, archive, level=1)

    dest = tmp_path / "out"
    decompress(archive, dest, include=["keep"])

    assert (dest / "keep" / "a.txt").read_text() == "A"
    assert (dest / "keep" / "b.txt").read_text() == "B"
    assert not (dest / "drop").exists()
B3. CLI exit-code taxonomy + clean errors
Right now an error dumps a traceback and exits 1. Give scriptable, documented codes and readable messages. In cli.py, wrap the dispatch in main():

    args = parser.parse_args()
    try:
        args.func(args)
    except BlitzCancelled:
        console.print("[yellow]Cancelled.[/]")
        sys.exit(130)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        sys.exit(130)
    except ArchiveFormatError as exc:
        console.print(f"[bold red]Archive error:[/] {exc}")
        sys.exit(2)
    except SourceReadError as exc:
        console.print(f"[bold red]Read error:[/] {exc}")
        sys.exit(3)
    except FileNotFoundError as exc:
        console.print(f"[bold red]Not found:[/] {exc}")
        sys.exit(4)
Add the imports:

from blitzpack.compressor import SourceReadError
from blitzpack.utils import ProgressUpdate, format_bytes, format_throughput, BlitzCancelled
Codes: 0 ok, 2 corrupt/invalid archive, 3 unreadable source, 4 not found, 130 cancelled/interrupted, 1 anything else. Document them in the README.

B4. Deterministic / reproducible archives
A --reproducible flag makes the same input produce byte-identical output — valued by build systems and content-addressed storage. In compress(), after the scan:

    manifest = FileAnalyzer(in_p).scan()
    if deterministic:
        manifest.entries.sort(key=lambda e: e.relative_path)
and when building manifest entries, clamp mtimes (both the top loop and the bundle-member loop):

        mtime=0.0 if deterministic else entry.mtime,
Sorting also stabilizes bundle grouping across runs (it no longer depends on filesystem enumeration order). Add -x/--reproducible (store_true) to the compress subparser and pass deterministic=args.reproducible.

B5. --skip-unreadable lenient mode (design only — needs care + a test)
The fail-fast default from the earlier review is correct. Some users want a lenient mode that drops unreadable files instead of aborting. It's more than a try/except:

The worker/reader must report which job failed (carry the failing job_id + path on a side channel), not just raise.
Those files must be removed from manifest_entries before finalize(), or the archive will claim files it didn't store.
The result should list skipped files (CompressionResult.skipped: list[str]), and the CLI should print a warning.
Do this only with a dedicated test that injects a mid-stream read failure and asserts the archive is valid and the other files roundtrip. Until then, fail-fast is the safe behavior — don't ship a half-done lenient mode.

C. Polish / hygiene
C1. pyproject.toml metadata:

authors = [
    { name = "<your name>", email = "<your email>" }
]

[project.urls]
Homepage = "https://github.com/netfreakkk/BlitzPack"
Issues = "https://github.com/netfreakkk/BlitzPack/issues"
C2. requirements.txt → single source of truth (stop duplicating the pins):

# Canonical dependency list lives in pyproject.toml.
-e .[all]
C3. Consolidate LEVEL_PROFILES. It's defined three times (compressor, cli profile_map, gui). In cli.py::handle_compress, replace the inline dict:

    from blitzpack.compressor import LEVEL_PROFILES as profile_map
and delete the local profile_map = {...}. Do the same in gui.py if you touch it.

C4. Apply Windows file attributes on extract (optional). win_attrs is stored and never applied (parallel to the permissions bug that was fixed). If you want hidden/readonly bits restored on Windows, in the metadata-restore loop:

        elif os.name == "nt" and entry.win_attrs:
            try:
                import ctypes
                # mask to safe, settable attributes (READONLY|HIDDEN|SYSTEM|ARCHIVE)
                settable = entry.win_attrs & 0x27
                if settable:
                    ctypes.windll.kernel32.SetFileAttributesW(str(target_path), settable)
            except Exception:
                pass
Low priority; only if attribute fidelity matters to you.

D. FORMAT.md — write the spec
A production archiver needs an independent format spec so the .blitz layout isn't "whatever archive_format.py happens to do." Promote the README's diagram into a real FORMAT.md covering:

Byte layout with exact struct formats: header "<4sHBIH11sQ", seek entry "<QIIQII", footer "<QQQQII8s".
Field semantics and units, magics (BLTZ, BLTZEND\0), CURRENT_VERSION, codec ids, FLAG_STORED.
Invariants a valid archive must satisfy: chunk data is contiguous starting at byte 32 and in chunk-index order; the whole-archive digest is xxHash-64 over [32, seek_table_offset); manifest paths are relative, forward-slash, no ..; empty files and manifest-only entries use start_chunk = -1.
Versioning/compatibility policy (readers reject version > CURRENT_VERSION).
This is documentation, not code, but it's a prerequisite for calling the format stable.

E. Tracked but not patched here
These are real, but they're features/infra rather than drop-in patches — they live in PERF_AND_ROADMAP.md §4 with rationale. Listed so nothing is lost:

Encryption (AES-256-GCM over chunks, password KDF) — format change; design first.
Append / update / delete entries in an existing archive.
stdin/stdout streaming (blitzpack compress - -o -) for pipelines.
File-level dedup (store identical files once, using the digests you already compute).
Fuzzing + property-based tests for the reader (atheris/hypothesis).
PyPI publish + wheels + signed releases + published SHA-256 for the .exes.
Performance work: parallel decompression writer, overlapped/parallel scan, trained dictionaries — all in PERF_AND_ROADMAP.md §2.
F. Commit sequence
fix(decompress): cooperative shutdown via stop_event, no thread leak on error — A1
fix(compress): scale stall threshold with chunk size; drop redundant sort — A2, A3
feat(api): cancel_event for cooperative cancellation of compress/decompress — B1
feat(cli): selective random-access extraction (blitzpack extract <archive> <path>) — B2
feat(cli): documented exit codes and readable error messages — B3
feat(compress): --reproducible for deterministic archives — B4
chore: metadata, requirements pointer, consolidate level profiles — C1–C3
docs: add FORMAT.md archive specification — D
After this, the CLI is compress / decompress / extract / list / verify(test) / analyze, the library supports cancellation (UI-ready), extraction is safe and cancellable, and the format is specified. That's a robust, promising base to build the UI on. --skip-unreadable (B5), Windows attrs (C4), and everything in section E come after, as demand dictates.

Note on ordering with the other patch docs
If you apply ROBUSTNESS_PATCH.md and this file to the same files: they touch decompressor.py and cli.py in different places and compose cleanly, except the decompressor's three thread functions and the decompress() signature — this file's A1/B1/B2 are the authoritative version of those. Apply ROBUSTNESS_PATCH.md first (its _safe_target and symlink edits are inside decompress()'s body, which this file preserves), then apply this file's A1/B1/B2 edits on top. Run the full test suite after each commit.
