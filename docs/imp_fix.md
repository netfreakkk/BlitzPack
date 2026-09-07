# BlitzPack — Fact-Fixing Implementation Plan

Status: draft, authored from a read-only review of `master` (commit as of 2026-09-03).
Scope: fix real defects, close the integrity hole, and make the documentation
describe what the code actually does.

Every item below is either **[BUG]** (the code is wrong), **[CLAIM]** (the code is
fine but the docs/CLI lie about it), or **[HYGIENE]** (repo/process).

Work the phases in order — Phase 1 changes the archive-writing path, and Phase 2's
tests depend on it.

---

## Table of contents

- [Phase 0 — Repo hygiene](#phase-0--repo-hygiene)
- [Phase 1 — Correctness bugs (P0)](#phase-1--correctness-bugs-p0)
- [Phase 2 — Integrity: actually verify what we claim to verify](#phase-2--integrity-actually-verify-what-we-claim-to-verify)
- [Phase 3 — Make the "sequential read" design real](#phase-3--make-the-sequential-read-design-real)
- [Phase 4 — Minor fixes and dead code](#phase-4--minor-fixes-and-dead-code)
- [Phase 5 — Tests to add](#phase-5--tests-to-add)
- [Appendix A — Corrected README sections](#appendix-a--corrected-readme-sections)
- [Appendix B — Full replacement files](#appendix-b--full-replacement-files)

---

## Phase 0 — Repo hygiene

### 0.1 [HYGIENE] Get the 40 MB of `.exe` out of git

`bin/blitzpack.exe` (25.8 MB) and `bin/blitzpack-gui.exe` (14.9 MB) are ~99% of the
67 MB repo. They're also **redundant** — `.github/workflows/release.yml` already
builds and publishes these exact two binaries to GitHub Releases on tag push.

Stop tracking them:

```bash
git rm --cached bin/blitzpack.exe bin/blitzpack-gui.exe
```

Add to `.gitignore`:

```gitignore
# Locally built binaries — released via GitHub Releases, never committed
bin/
dist/
```

Commit:

```bash
git commit -m "Stop tracking prebuilt binaries; they ship via GitHub Releases"
```

**Optional, destructive — purge them from history.** Removing from HEAD does not
shrink clones; the blobs stay in history forever. Only do this on a personal repo
where you're fine force-pushing:

```bash
pip install git-filter-repo
git filter-repo --path bin/blitzpack.exe --path bin/blitzpack-gui.exe --invert-paths
git push --force origin master
```

Then cut a `v1.0.0` tag so the release workflow publishes the binaries properly:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

### 0.2 [HYGIENE] Add a CI workflow that runs the tests

Right now the only workflow is release-on-tag. The pytest suite never runs.

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [master]
  pull_request:
  workflow_dispatch:

jobs:
  test:
    name: Test (${{ matrix.os }} / py${{ matrix.python-version }})
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        python-version: ['3.10', '3.12']
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip

      - name: Install package and test deps
        run: |
          python -m pip install --upgrade pip
          pip install ".[cli,dev]"

      - name: Run tests
        run: pytest -v --durations=10

  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install ruff
      - run: ruff check .
```

Add a minimal ruff config to `pyproject.toml` (the first run on the existing tree
will flag unused imports — `gui.py` imports `collections`, `datetime`, `gc`,
`shutil`, `time` and `analyzer.py` imports `Dict`/`Set` that aren't all used):

```toml
[tool.ruff]
line-length = 110
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B"]
ignore = [
    "E501",  # long lines are mostly theme dicts and docstrings
]
```

If the first lint run is too noisy to fix in one sitting, change the lint step to
`ruff check . --exit-zero` and tighten it later — but do not delete the job.

### 0.3 [HYGIENE] Fill in placeholder metadata

`pyproject.toml`:

```diff
 authors = [
-    { name = "BlitzPack Contributors" }
+    { name = "<your name>", email = "<your email>" }
 ]
+[project.urls]
+Homepage = "https://github.com/netfreakkk/BlitzPack"
+Issues = "https://github.com/netfreakkk/BlitzPack/issues"
```

Same for the `Dockerfile` `LABEL maintainer=`.

---

## Phase 1 — Correctness bugs (P0)

### 1.1 [BUG] `blitzpack analyze` crashes with `AttributeError`

`cli.py::handle_analyze` passes a `ClassifiedFiles` to `WorkScheduler.schedule()`,
which iterates `manifest.entries`. `ClassifiedFiles` has no `.entries`, so the
command raises `AttributeError` every single time it's run.

The fix is one word, but while we're here the surrounding table text is also wrong
(see [1.2](#12-claim-the-size-tier-story-doesnt-match-the-scheduler)), so replace
the whole function.

### 1.2 [CLAIM] The size-tier story doesn't match the scheduler

Declared in `analyzer.py`:

| Tier   | Threshold      | README says            |
| ------ | -------------- | ---------------------- |
| large  | > 16 MB        | chunked at 4 MB        |
| medium | 64 KB – 16 MB  | compressed individually |
| small  | < 64 KB        | bundled into solid blocks |

What `WorkScheduler.schedule()` actually does — it ignores `classify()` entirely and
walks `manifest.entries` directly:

| Actual behaviour              | Threshold |
| ----------------------------- | --------- |
| split into 4 MB chunk jobs    | size >= `chunk_size` (4 MB) |
| appended to a solid bundle    | size < `chunk_size` (4 MB) |

So the "medium tier compressed individually" does not exist: a 64 KB–4 MB file gets
bundled, and a 4–16 MB file gets chunked. `classify()` is only consumed by the
(crashing) `analyze` command.

**Decision: keep the scheduler's behaviour, fix the classification and the docs.**
Bundling 64 KB–4 MB files into solid blocks gives a *better* ratio than compressing
them individually, so the code is the better design — it's the description that's
wrong. Reclassify along the real seam (chunked vs. bundled) instead of inventing a
third tier nothing implements.

`scheduler.py` currently owns `DEFAULT_CHUNK_SIZE` and `analyzer.py` can't import it
(`scheduler` already imports `analyzer` → circular). Introduce a neutral module.

**New file `blitzpack/constants.py`:**

```python
"""Shared tuning constants for classification and scheduling.

These live in their own module because both `analyzer` and `scheduler` need them
and `scheduler` already imports `analyzer`.
"""

CHUNK_SIZE = 4 * 1024 * 1024        # files >= this are split into chunk jobs
BUNDLE_TARGET = 4 * 1024 * 1024     # solid bundles are flushed once they reach this
MAX_BUNDLE_MEMBERS = 256            # cap on files packed into a single bundle
```

**`blitzpack/scheduler.py`** — re-export from the new module so nothing else breaks:

```diff
 from dataclasses import dataclass, field
 from typing import List, Optional
 from .analyzer import FileEntry, FileManifest
+from .constants import BUNDLE_TARGET, CHUNK_SIZE, MAX_BUNDLE_MEMBERS

-DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024       # 4 MB
-DEFAULT_BUNDLE_TARGET = 4 * 1024 * 1024    # 4 MB
-DEFAULT_MAX_BUNDLE_MEMBERS = 256           # Maximum files in a single solid bundle
+DEFAULT_CHUNK_SIZE = CHUNK_SIZE
+DEFAULT_BUNDLE_TARGET = BUNDLE_TARGET
+DEFAULT_MAX_BUNDLE_MEMBERS = MAX_BUNDLE_MEMBERS
```

Also fix the module docstring, which claims bundling is "by extension" in one place
and "by sequential proximity" in another (the latter is correct):

```diff
-"""Work scheduler implementing strict sequential disk-order dispatch and continuous bundling.
-
-By yielding jobs in exact manifest (directory) order, we preserve OS read-ahead cache
-locality. Small and medium files are aggregated into solid bundles purely by sequential
-proximity rather than extension, acting like a tarball for maximum disk sequentiality.
-"""
+"""Work scheduler: emits compression jobs in directory-traversal order.
+
+Jobs are yielded in exact manifest order so that readers walk the tree the same way
+the OS laid it out. Files at or above `CHUNK_SIZE` are split into independently
+addressable chunk jobs; everything smaller is appended to a solid bundle in traversal
+order (not grouped by extension), which behaves like a small tarball and compresses
+far better than the same files would individually.
+"""
```

**`blitzpack/analyzer.py`** — replace the tier constants and `ClassifiedFiles`:

```diff
 from .utils import normalize_relative_path, sanitize_windows_path
+from .constants import CHUNK_SIZE

-LARGE_THRESHOLD = 16 * 1024 * 1024   # > 16 MB
-SMALL_THRESHOLD = 64 * 1024          # < 64 KB
+# A file is chunked if the scheduler will split it, and bundled otherwise. This is
+# the only seam the pipeline actually has — keep it derived from the scheduler's
+# chunk size so the two can never drift apart again.
+CHUNKED_THRESHOLD = CHUNK_SIZE
```

```diff
 @dataclass(slots=True)
 class ClassifiedFiles:
-    large: List[FileEntry] = field(default_factory=list)
-    medium: List[FileEntry] = field(default_factory=list)
-    small: List[FileEntry] = field(default_factory=list)
+    chunked: List[FileEntry] = field(default_factory=list)
+    bundled: List[FileEntry] = field(default_factory=list)
+    empty: List[FileEntry] = field(default_factory=list)
     directories: List[FileEntry] = field(default_factory=list)
     symlinks: List[FileEntry] = field(default_factory=list)
     total_bytes: int = 0
     total_files: int = 0
```

```diff
     def classify(self, manifest: FileManifest) -> ClassifiedFiles:
-        """Classify manifest entries into size tiers."""
+        """Split manifest entries by how the scheduler will actually handle them."""
         result = ClassifiedFiles()
 
         for entry in manifest.entries:
             if entry.file_type == 1:
                 result.directories.append(entry)
             elif entry.file_type == 2:
                 result.symlinks.append(entry)
             else:
                 result.total_files += 1
                 result.total_bytes += entry.size
-                if entry.size > LARGE_THRESHOLD:
-                    result.large.append(entry)
-                elif entry.size < SMALL_THRESHOLD:
-                    result.small.append(entry)
-                else:
-                    result.medium.append(entry)
+                if entry.size == 0:
+                    result.empty.append(entry)      # manifest-only, never gets a chunk
+                elif entry.size >= CHUNKED_THRESHOLD:
+                    result.chunked.append(entry)
+                else:
+                    result.bundled.append(entry)
 
         return result
```

**`cli.py`** — replace `handle_analyze` wholesale. This fixes the crash *and* removes
two false claims: small files are **not** bundled by extension, and there is **no**
LPT (longest-processing-time) scheduling anywhere in the codebase.

```python
def handle_analyze(args: argparse.Namespace) -> None:
    in_path = Path(args.input).resolve()
    if not in_path.exists():
        console.print(f"[bold red]Error:[/] Target path does not exist: {in_path}")
        sys.exit(1)

    analyzer = FileAnalyzer(in_path)
    manifest = analyzer.scan()
    classified = analyzer.classify(manifest)
    scheduler = WorkScheduler()
    jobs = scheduler.schedule(manifest)  # takes a FileManifest, not ClassifiedFiles

    chunk_jobs = sum(1 for j in jobs if j.job_type == "chunk")
    bundle_jobs = sum(1 for j in jobs if j.job_type == "bundle")

    table = Table(title="File Profiling & Scheduling Plan", show_lines=True)
    table.add_column("Category", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Total Size", justify="right")
    table.add_column("Scheduled Strategy", style="dim")

    table.add_row(
        f"Chunked (>= {format_bytes(scheduler.chunk_size)})",
        str(len(classified.chunked)),
        format_bytes(sum(f.size for f in classified.chunked)),
        f"Split into {format_bytes(scheduler.chunk_size)} chunks, one job per chunk",
    )
    table.add_row(
        f"Bundled (< {format_bytes(scheduler.chunk_size)})",
        str(len(classified.bundled)),
        format_bytes(sum(f.size for f in classified.bundled)),
        f"Packed in traversal order into ~{format_bytes(scheduler.bundle_target)} "
        f"solid blocks (max {scheduler.max_bundle_members} files each)",
    )
    table.add_row("Empty files", str(len(classified.empty)), "-", "Manifest-only, no chunk")
    table.add_row("Directories", str(len(classified.directories)), "-", "Manifest-only")
    table.add_row("Symlinks", str(len(classified.symlinks)), "-", "Manifest-only")

    console.print()
    console.print(table)
    console.print(
        f"\n[bold]Dispatch plan:[/] {len(jobs)} jobs "
        f"([bold green]{chunk_jobs}[/] chunk, [bold green]{bundle_jobs}[/] bundle), "
        f"emitted in directory-traversal order.\n"
    )
```

### 1.3 [BUG] Silent data loss: unreadable files are archived as empty

`compressor.py::SequentialReader.read_job` swallows `OSError` and returns `b""`:

```python
except OSError:
    return b""
```

The worker then hashes those zero bytes and stores them as a legitimate chunk. A file
that can't be opened (locked by another process, permission denied, transient I/O
error) is **archived as empty**, and because the per-chunk digest is computed over the
empty payload, extraction's integrity check *passes*. The archive is silently,
verifiably-wrong.

For a tool whose headline feature is "integrity verification", this is the most
important bug in the repo.

**Fix: fail loudly.** Raise a typed error carrying the path, and let the existing
error-propagation path surface it. Full replacement `SequentialReader` (part of the
complete `compressor.py` in [Appendix B](#b2-blitzpackcompressorpy)):

```python
class SourceReadError(OSError):
    """Raised when a source file cannot be read (or changed) during compression."""

    def __init__(self, path: Path | str, reason: str) -> None:
        super().__init__(f"Failed to read source file {path}: {reason}")
        self.path = Path(path)
```

### 1.4 [BUG] Files that change size between scan and read corrupt the archive

This one is related and just as quiet. Bundle member offsets are computed at *scan*
time from `entry.size`:

```python
current_bundle.append(BundleMember(entry=entry, offset_in_bundle=current_bundle_size, size=entry.size))
current_bundle_size += entry.size
```

but the bundle payload is built at *read* time with `f.read()` — whatever the file
holds now. If any file in a bundle grew or shrank in between, every subsequent
member's `offset_in_bundle` is wrong, and extraction slices garbage out of the bundle
for all of them. No checksum catches it, because the digest is taken over the
already-misaligned payload.

Same for chunk jobs: a short read at `job.offset` silently yields a shorter chunk.

**Fix: assert the length we read is the length we scheduled.** Included in the
`read_job` replacement below — both branches now verify size and raise
`SourceReadError` on mismatch.

```python
    def read_job(self, job: CompressionJob) -> bytes:
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
                        f"size changed during compression: manifest says {member.size} bytes, "
                        f"read {len(data)}",
                    )
                parts.append(data)
            return b"".join(parts)

        raise ValueError(f"Unknown job type: {job.job_type!r}")
```

> **Follow-up worth doing later, not now:** a `--skip-unreadable` mode. It's more work
> than it looks — you can't just write an empty chunk, you have to drop the affected
> entries from `manifest_entries` before `finalize()` and report them on
> `CompressionResult`. Fail-fast first; make it lenient once there's a test for it.

### 1.5 [BUG] Multi-chunk extraction assumes a hardcoded 4 MB chunk size

`decompressor.py`:

```python
file_offset=offset_in_span * (4 * 1024 * 1024),
```

The header stores the real `chunk_size` precisely so this doesn't have to be a magic
number. Today it works only because the writer always defaults to 4 MB — change
`WorkScheduler(chunk_size=...)` or `BlitzArchiveWriter(default_chunk_size=...)` and
every large-file extraction silently corrupts.

**Fix: don't assume uniformity at all.** Derive each chunk's file offset from the
actual `original_size` values in the seek table. This is correct even for non-uniform
chunking, and it gives us a free consistency check.

```diff
     for entry in reader.manifest:
         if entry.file_type != 0 or entry.size == 0:
             continue
 
         target_path = out_p / entry.path
         if entry.start_chunk != entry.end_chunk:
             total_chunks = entry.end_chunk - entry.start_chunk + 1
-            for offset_in_span, chunk_idx in enumerate(range(entry.start_chunk, entry.end_chunk + 1)):
+            running = 0
+            for chunk_idx in range(entry.start_chunk, entry.end_chunk + 1):
+                if not 0 <= chunk_idx < len(reader.seek_entries):
+                    raise ArchiveFormatError(
+                        f"Manifest entry {entry.path!r} references out-of-range chunk {chunk_idx}"
+                    )
                 chunk_to_targets[chunk_idx].append(
                     ExtractTarget(
                         target_path=target_path,
                         start_offset=0,
                         end_offset=-1,
                         is_multi_chunk=True,
-                        file_offset=offset_in_span * (4 * 1024 * 1024),
+                        file_offset=running,
                         total_file_size=entry.size,
                         total_file_chunks=total_chunks,
                         mtime=entry.mtime,
                     )
                 )
+                running += reader.seek_entries[chunk_idx].original_size
+            if running != entry.size:
+                raise ArchiveFormatError(
+                    f"Chunk span for {entry.path!r} covers {running} bytes but the manifest "
+                    f"records {entry.size}"
+                )
```

Add the import at the top of `decompressor.py`:

```diff
-from .archive_format import BlitzArchiveReader, SeekEntry, FLAG_STORED
+from .archive_format import ArchiveFormatError, BlitzArchiveReader, SeekEntry, FLAG_STORED
```

### 1.6 [BUG] A large file whose chunks went missing is silently dropped

`compressor.py`, manifest construction:

```python
if entry.size >= scheduler.chunk_size:
    chunks_info = large_file_chunks.get(entry.relative_path, [])
    if chunks_info:
        ...append ManifestEntry...
    # <-- no else: the file vanishes from the archive with no error
```

If the lookup ever misses, the file is omitted from the manifest entirely and nothing
reports it. Make it loud:

```python
            chunks_info = large_file_chunks.get(entry.relative_path)
            if not chunks_info:
                raise RuntimeError(
                    f"Internal error: no chunk jobs were scheduled for {entry.relative_path!r} "
                    f"({entry.size} bytes)"
                )
```

### 1.7 [BUG] Worker threads leak and the pipeline can hang on error

Two problems in both `compress()` and `decompress()`:

1. When the main thread raises, shutdown sentinels are never sent. The threads only
   die because they're `daemon=True` — fine for the CLI, a real leak for library and
   GUI use (the GUI runs compression in a background thread inside a long-lived
   process, so every failed job leaks N reader + N worker threads for the life of the
   app).
2. `write_queue.get()` in `compress()` blocks forever with no timeout. If a reader
   dies before enqueuing its payload, the main thread waits indefinitely — the
   exception is on the queue but nobody ever reads it.

**Fix:** dedicated error queue, a `stop_event` every thread checks, `get(timeout=...)`
in the main loop, stall detection, and a `finally:` block that always drains and
joins. Implemented in the full `compressor.py` in
[Appendix B](#b2-blitzpackcompressorpy). Apply the same `try/finally` shutdown to
`decompress()`:

```diff
-    for _ in range(num_workers):
-        read_queue.put(None)
-    for t in worker_threads:
-        t.join()
-    reader_thread.join()
-    writer_thread.join()
+    finally:
+        # Always tear the pipeline down, including on the error path above.
+        for _ in range(num_workers):
+            try:
+                read_queue.put_nowait(None)
+            except queue.Full:
+                pass
+        for t in worker_threads:
+            t.join(timeout=5)
+        reader_thread.join(timeout=5)
+        writer_thread.join(timeout=5)
```

(wrap everything from `writer_thread.start()` through the progress loop in the
matching `try:`).

### 1.8 [BUG] Header records a chunk size the writer wasn't told about

`compress()` constructs the writer with no `default_chunk_size`, so the header always
records 4 MB regardless of what the scheduler used:

```diff
-        writer = BlitzArchiveWriter(f_out)
+        writer = BlitzArchiveWriter(f_out, default_chunk_size=scheduler.chunk_size)
```

### 1.9 [BUG] Empty files point at a chunk they don't own

Empty files get `start_chunk=0, end_chunk=0` — chunk 0 is a real chunk belonging to
some other file. It happens to work only because `decompress()` special-cases
`entry.size == 0` before building the target map. Use an unambiguous sentinel so a
future refactor can't turn this into data corruption:

In `compressor.py`, manifest-only entries:

```diff
             manifest_entries.append(ManifestEntry(
                 path=entry.relative_path, size=entry.size, mtime=entry.mtime,
                 file_type=entry.file_type, symlink_target=entry.symlink_target,
                 permissions=entry.permissions, win_attrs=entry.win_attrs,
-                start_chunk=0, start_offset=0, end_chunk=0, end_offset=0,
+                start_chunk=-1, start_offset=0, end_chunk=-1, end_offset=0,
             ))
```

`decompress()` already skips these (`if entry.file_type != 0 or entry.size == 0`), and
after 1.5 the bounds check will now catch anything that slips through. Note this is a
**format-compatible** change only in the sense that new archives use `-1`; old
archives still read fine because the entries are skipped before the index is used.

---

## Phase 2 — Integrity: actually verify what we claim to verify

### 2.1 [CLAIM/BUG] The whole-archive digest is computed and never checked

The README says:

> A whole-archive digest in the footer catches corruption anywhere in the file.

`BlitzArchiveWriter` does compute it (`IncrementalHasher` over every compressed frame
in write order), and `BlitzArchiveReader` does parse `footer.archive_digest` — and
then nothing ever compares them. Corruption in the chunk-data region is caught only
for chunks that some manifest entry happens to reference; **orphan chunks are never
verified at all** (`decompressor.py`: `if not targets: continue`).

The digest is checkable cheaply. Chunks are written back-to-back starting immediately
after the 32-byte header, in chunk-index order, so hashing the contiguous byte range
`[HEADER_STRUCT.size, footer.seek_table_offset)` reproduces the writer's incremental
hash exactly — no decompression required.

**Add a streaming range hasher to `checksum.py`** (full file in
[Appendix B](#b1-blitzpackchecksumpy)):

```python
def compute_stream_digest(
    stream: BinaryIO, start: int, end: int, block_size: int = 4 * 1024 * 1024
) -> int:
    """Compute the XXH64 digest of the byte range [start, end) of an open stream."""
    if end < start:
        raise ValueError(f"Invalid range: end ({end}) < start ({start})")
    hasher = xxhash.xxh64()
    stream.seek(start)
    remaining = end - start
    while remaining > 0:
        block = stream.read(min(block_size, remaining))
        if not block:
            raise EOFError(f"Unexpected EOF with {remaining} bytes left to hash")
        hasher.update(block)
        remaining -= len(block)
    return hasher.intdigest()
```

**Add `verify()` to `BlitzArchiveReader`** in `archive_format.py`:

```python
CHUNK_DATA_START = HEADER_STRUCT.size  # chunk payloads begin right after the header
```

```python
    def verify_layout(self) -> None:
        """Check that chunk frames are contiguous and cover the whole data region.

        The whole-archive digest is a hash of the contiguous byte range, so a gap or
        overlap in the seek table would make that digest meaningless.
        """
        expected = CHUNK_DATA_START
        for idx, entry in enumerate(self.seek_entries):
            if entry.offset != expected:
                raise ArchiveFormatError(
                    f"Chunk {idx} starts at {entry.offset} but the previous frame ends at "
                    f"{expected} (seek table is not contiguous)"
                )
            expected += entry.compressed_size
        if expected != self.footer.seek_table_offset:
            raise ArchiveFormatError(
                f"Chunk data ends at {expected} but the seek table starts at "
                f"{self.footer.seek_table_offset}"
            )

    def verify(self, deep: bool = False, progress_callback=None) -> None:
        """Verify archive integrity, raising ArchiveFormatError on any mismatch.

        Always: structural layout plus the whole-archive digest over the chunk-data
        region (one sequential read, no decompression).
        With deep=True: additionally decompress every chunk in the seek table and
        check its per-chunk digest, including chunks no manifest entry references.
        """
        if self.footer.archive_digest == 0:
            raise ArchiveFormatError(
                "Archive footer was not recoverable, so the whole-archive digest is "
                "unavailable; re-create this archive from source"
            )

        self.verify_layout()

        actual = compute_stream_digest(
            self._stream, CHUNK_DATA_START, self.footer.seek_table_offset
        )
        if actual != self.footer.archive_digest:
            raise ArchiveFormatError(
                f"Whole-archive digest mismatch: expected "
                f"{self.footer.archive_digest:#018x}, got {actual:#018x}"
            )

        if not deep:
            return

        import zstandard as zstd

        dctx = zstd.ZstdDecompressor()
        total = len(self.seek_entries)
        for idx in range(total):
            entry, raw = self.read_raw_chunk(idx)
            if entry.flags & FLAG_STORED:
                data = raw
            else:
                data = dctx.decompress(raw, max_output_size=entry.original_size + 65536)
            if len(data) != entry.original_size:
                raise ArchiveFormatError(
                    f"Chunk {idx} decompressed to {len(data)} bytes, expected "
                    f"{entry.original_size}"
                )
            if compute_digest(data) != entry.digest:
                raise ArchiveFormatError(
                    f"Chunk {idx} digest mismatch (expected {entry.digest:#018x})"
                )
            if progress_callback:
                progress_callback(idx + 1, total)
```

Update the import line in `archive_format.py`:

```diff
-from .checksum import IncrementalHasher
+from .checksum import IncrementalHasher, compute_digest, compute_stream_digest
```

### 2.2 [CLAIM] The README advertises "integrity testing" that has no command

The README claims the CLI does "archive inspection, and integrity testing". There are
only four subcommands: `compress`, `decompress`, `list`, `analyze`. Add the missing
one.

`cli.py` — new handler:

```python
def handle_verify(args: argparse.Namespace) -> None:
    arc_path = Path(args.archive).resolve()
    if not arc_path.is_file():
        console.print(f"[bold red]Error:[/] Archive file does not exist: {arc_path}")
        sys.exit(1)

    mode = "deep (every chunk decompressed)" if args.deep else "fast (whole-archive digest)"
    console.print(Panel(
        f"[bold cyan]Archive:[/] {arc_path}\n[bold cyan]Mode:[/] {mode}",
        title="BlitzPack Verify",
        border_style="cyan",
    ))

    start = time.perf_counter()
    try:
        with open(arc_path, "rb") as f:
            reader = BlitzArchiveReader(f)
            if args.deep:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                    TimeRemainingColumn(),
                    console=console,
                ) as progress:
                    task_id = progress.add_task("Verifying chunks...", total=len(reader.seek_entries))
                    reader.verify(deep=True, progress_callback=lambda done, total: progress.update(task_id, completed=done, total=total))
            else:
                reader.verify(deep=False)
            entry_count = len(reader.manifest)
            chunk_count = len(reader.seek_entries)
            original = reader.footer.total_original_size
    except (ArchiveFormatError, OSError) as exc:
        console.print(Panel(
            f"[bold red]Verification FAILED[/]\n\n{exc}",
            title="Corrupt Archive",
            border_style="red",
        ))
        sys.exit(2)

    console.print(Panel(
        f"[bold green]Archive is intact.[/]\n\n"
        f"Entries:          {entry_count}\n"
        f"Chunks verified:  {chunk_count}\n"
        f"Original Size:    {format_bytes(original)}\n"
        f"Elapsed:          {time.perf_counter() - start:.2f}s",
        title="Verification Complete",
        border_style="green",
    ))
```

Register it in `main()`:

```python
    # Verify
    p_verify = subparsers.add_parser(
        "verify", aliases=["test"], help="Check archive integrity without extracting"
    )
    p_verify.add_argument("archive", help="Path to .blitz archive")
    p_verify.add_argument(
        "--deep",
        action="store_true",
        help="Also decompress and checksum every chunk (slower, catches everything)",
    )
    p_verify.set_defaults(func=handle_verify)
```

And extend the imports:

```diff
 from blitzpack import (
     BlitzArchiveReader,
     FileAnalyzer,
     compress,
     decompress,
 )
+from blitzpack.archive_format import ArchiveFormatError
 from blitzpack.scheduler import WorkScheduler
 from blitzpack.utils import ProgressUpdate, format_bytes, format_throughput
```

Consider exporting `ArchiveFormatError` from `blitzpack/__init__.py` too:

```diff
-from .archive_format import BlitzArchiveReader, BlitzArchiveWriter
+from .archive_format import ArchiveFormatError, BlitzArchiveReader, BlitzArchiveWriter
```
```diff
 __all__ = [
+    "ArchiveFormatError",
     "FileAnalyzer",
```

### 2.3 Optional: pre-flight verification on extract

Add `verify_first: bool = False` to `decompress()` and a `--verify-first` CLI flag. It
costs one extra sequential read of the archive, so keep it opt-in — but it turns
"extract and hope" into "confirm, then extract".

```python
    with open(sanitize_windows_path(arc_p), "rb") as f_in:
        reader = BlitzArchiveReader(f_in)
        if verify_first:
            reader.verify(deep=False)
```

### 2.4 [BUG] Failed extraction leaves partial files behind

If a chunk fails its checksum mid-run, `decompress()` raises and whatever was already
written stays on disk — including `wb+`-truncated sparse files at full length, which
look complete to anything checking sizes. At minimum, say so; better, offer cleanup.

Add to `decompress()`:

```python
    created_output_root = not out_p.exists()
    ...
    except BaseException:
        if cleanup_on_error and created_output_root and out_p.exists():
            shutil.rmtree(out_p, ignore_errors=True)
        raise
```

with `cleanup_on_error: bool = False` in the signature and a `--clean-on-error` CLI
flag. Only ever remove a directory *we* created — never one the user pointed us at
that already had contents.

---

## Phase 3 — Make the "sequential read" design real

### 3.1 [CLAIM] "Strict directory-traversal order" is contradicted by the code

The README, and the `compressor.py` module docstring, stake the entire performance
thesis on this:

> Reads happen in strict directory-traversal order to preserve OS read-ahead locality.

> Producers (Readers): Read files from disk strictly in directory order, maintaining
> OS prefetching/read-ahead and avoiding random seeks.

But `compress()` does:

```python
num_readers = max(12, num_workers * 2)
```

and hands all 12+ reader threads a single shared `read_jobs` queue. Reads are
concurrent and interleaved — the exact opposite of strict sequential order. On a
spinning disk, 12 concurrent readers cause seek thrashing, which is the failure mode
the design claims to avoid.

There's a second, quieter cost: `SequentialReader` caches an open handle and reuses it
when consecutive jobs share a path, but with a flat shared queue the consecutive
chunks of one large file get grabbed by *different* threads. The cache almost never
hits, so a 4 GB file is opened and seeked once per chunk instead of streamed once.

**Fix both at once: hand out read *groups*, not individual jobs.** Group consecutive
chunk jobs of the same file so one thread streams that file start-to-finish with one
handle. Bundles stay their own group. Parallelism across groups is retained (that's
what keeps an NVMe queue busy), but within a file the reads are genuinely sequential —
which makes the claim true instead of aspirational.

```python
def _build_read_groups(ordered_jobs: List[CompressionJob]) -> List[List[CompressionJob]]:
    """Group consecutive chunk jobs that belong to the same source file.

    Each group is handled by a single reader thread with one open handle, so a large
    multi-chunk file is streamed sequentially rather than scattered across threads.
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
```

### 3.2 Make the reader count sane and configurable

`max(12, num_workers * 2)` means a minimum of 12 reader threads even for
`--workers 1`, and 16 for an 8-core box. The right number depends entirely on the
device: an NVMe drive wants high queue depth, a spinning disk wants 1–2.

```diff
-    num_readers = max(12, num_workers * 2)
+    # Reader count is a device property, not a CPU property: SSDs/NVMe benefit from a
+    # deep queue, spinning disks are destroyed by it. Default to a moderate depth and
+    # let the caller override.
+    num_readers = readers if readers > 0 else max(2, min(8, num_workers))
```

Add `readers: int = 0` to the `compress()` signature and a CLI flag:

```python
    p_comp.add_argument(
        "-r", "--readers", type=int, default=0,
        help="Disk reader threads (default: auto; use 1-2 for spinning disks)",
    )
```

then pass `readers=args.readers` through in `handle_compress`.

### 3.3 Rewrite the docstrings to match

`compressor.py` module docstring:

```python
"""Parallel compression engine built on a producer-consumer pipeline.

1. Producers (Readers): N threads pull *read groups* off a queue. A group is either
   one solid bundle or every consecutive chunk of a single large file, so each large
   file is streamed sequentially through one open handle rather than scattered across
   threads. Groups themselves are read concurrently to keep the device queue busy.
2. Consumers (Workers): N threads pull raw payloads, compress via zstandard (which
   releases the GIL), and compute xxHash-64 digests.
3. Writer (main thread): reorders completed chunks by index and appends them
   sequentially to the archive, so the on-disk layout is contiguous and the
   whole-archive digest stays a simple range hash.
"""
```

### 3.4 Fix the misleading progress reporting

Two problems in `compress()`:

- `files_processed=len(manifest.entries)` and `total_files=len(manifest.entries)` — it
  reports 100% of files done from the very first callback. The number is meaningless.
- The callback only fires on `next_write_id % 50 == 0`, so an archive with fewer than
  50 chunks never reports progress at all, and the final state is never pushed.

Fix: count files as they actually complete, and switch to time-based throttling with a
guaranteed final update (the decompressor already does it this way). Precompute how
many files each job finalizes:

```python
    # How many manifest files each job completes: every member for a bundle, and one
    # for the final chunk of a chunked file.
    files_per_job: Dict[int, int] = {}
    for j in ordered_jobs:
        if j.job_type == "bundle":
            files_per_job[j.job_id] = len(j.bundle_members)
    for chunks_info in large_file_chunks.values():
        files_per_job[chunks_info[-1][1].job_id] = files_per_job.get(chunks_info[-1][1].job_id, 0) + 1
```

Then in the write loop:

```python
                files_done += files_per_job.get(ready_res.job_id, 0)
                ...
                now = time.perf_counter()
                if progress_callback and (now - last_callback >= 0.1 or next_write_id == total_jobs):
                    last_callback = now
                    progress_callback(ProgressUpdate(
                        phase="compressing",
                        files_processed=files_done,
                        total_files=total_source_files,
                        bytes_processed=bytes_done,
                        total_bytes=total_bytes,
                        current_speed_bps=bytes_done / max(now - start_time, 1e-9),
                        message=f"Compressing ({num_workers} workers, {num_readers} readers)...",
                    ))
```

### 3.5 `CompressionResult.total_files` counts directories and symlinks

`total_files=len(manifest.entries)` includes directories and symlinks, so
`blitzpack compress` reports a "file" count that doesn't match `list`'s file rows.
Report both:

```diff
 class CompressionResult:
     archive_path: Path
     total_files: int
+    total_entries: int
     original_size: int
```

with `total_files` = count of `file_type == 0` entries and `total_entries` =
`len(manifest.entries)`. Update the CLI panel to show files and note entries.

### 3.6 [CLAIM] Benchmarks need methodology or a caveat

The README's benchmark table reports a single run on one machine ("4.75 GB React
Native Android project, 59,946 files") with no hardware spec, no repetition count, no
variance, and a comparison against WinRAR/7-Zip at unstated settings — while
BlitzPack produces a **21% larger archive** (1.25 GB vs 1.03 GB). The speed numbers
are plausible for zstd level 3 vs. WinRAR's default, but as presented they aren't
reproducible.

At minimum, add the machine spec (CPU, core count, disk type), state the exact
competitor flags used, say how many runs and whether the number is best/median, and
keep the existing "different algorithms" note. Better: commit the benchmark script
(`benchmark.py` is currently in `.gitignore`) so the numbers can be reproduced.

---

## Phase 4 — Minor fixes and dead code

### 4.1 [BUG] `format_bytes` can never print PB

`utils.py`:

```python
for unit in units:
    if val < 1024.0 or unit == "TB":
        return ...
    val /= 1024.0
return f"{val:.1f} PB"   # unreachable
```

The `unit == "TB"` guard returns on the TB iteration unconditionally, so a petabyte
prints as `"1024.0 TB"` and the final line is dead.

```python
def format_bytes(byte_count: int | float) -> str:
    """Convert a raw byte count into human-readable notation."""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    val = float(byte_count)
    for unit in units:
        if val < 1024.0 or unit == units[-1]:
            return f"{int(val)} B" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    raise AssertionError("unreachable")
```

### 4.2 [CLAIM] The Rust `blitz_core` fast path can never execute

`checksum.py` opens with:

```python
# Tier 1: Rust native xxHash-64
try:
    from blitz_core import compute_xxh64 as _native_xxh64
```

There is no Rust crate in the repository — `.gitignore` marks `blitz_core/` as
"deprecated — pure Python now". The import always fails, so the branch is dead. It
also directly contradicts the README's "Pure Python with no native dependencies".

Delete the branch (full file in [Appendix B](#b1-blitzpackchecksumpy)). If you want a
native accelerator later, add it back together with the crate and a build.

Also in the same file: `compute_file_digest` and `IncrementalHasher.__init__` each do a
function-local `import xxhash` even though the module can just import it at the top.

### 4.3 Test name promises a feature that doesn't exist

`tests/test_roundtrip.py::test_large_file_chunking_with_overlap` — there is no overlap
feature. `SeekEntry.overlap_prefix_size` exists in the format but is always written as
`0` and never read. Rename the test to
`test_large_file_chunking_roundtrip`, and either implement overlap or add a comment on
the field marking it reserved-for-future-use.

The same file's `test_small_file_bundling_fidelity` docstring says files are "bundled
by extension" — they aren't. Fix the docstring.

### 4.4 [CLAIM] GUI identity and cross-platform claims

- `gui.py`'s docstring says "macOS-Inspired Fluent Desktop GUI"; the README calls it a
  "WinRAR-Style GUI". Pick one.
- The README lists **Cross-Platform** as a feature, but `gui.py` calls
  `ctypes.windll.user32` / `ctypes.windll.dwmapi` for Windows 11 Mica. Those are
  wrapped in `try/except Exception: pass`, so the GUI probably *runs* elsewhere — but
  it's designed and themed for Windows. Say "core library and CLI are cross-platform;
  the GUI is developed and tested on Windows."

### 4.5 `gui.py` is a 63 KB single-file monolith

Not a bug, but the largest maintainability liability in the repo — theme
dictionaries, a hand-drawn Canvas toggle widget, ctypes OS calls, file-manager logic,
and compression orchestration all in one module. A reasonable split:

```
blitzpack_gui/
├── __init__.py
├── theme.py        THEMES dict, palette lookup, light/dark switching
├── widgets.py      MacOSSwitch, dropzone, the throughput graph canvas
├── platform.py     apply_windows_mica and other ctypes calls
├── icons.py        get_file_icon_and_badge
└── app.py          the main window and orchestration
```

Keep `gui.py` as a thin `from blitzpack_gui.app import main` shim so
`[project.gui-scripts]` and the PyInstaller spec keep working. Do this *after* the
correctness work — it's a big diff with no functional payoff.

### 4.6 Duplicated dependency lists

`pyproject.toml` and `requirements.txt` list the same pins independently, and
`LEVEL_PROFILES` is defined three times (`compressor.py`, `cli.py` as
`profile_map`, `gui.py`). Make `requirements.txt` a pointer:

```
# Canonical dependency list lives in pyproject.toml.
# This file exists for `pip install -r requirements.txt` convenience.
-e .[all]
```

And import the profile map from one place:

```diff
-    profile_map = {"fast": 1, "balanced": 3, "high": 9, "ultra": 19}
+    from blitzpack.compressor import LEVEL_PROFILES as profile_map
```

### 4.7 `Dockerfile` installs dependencies twice

```dockerfile
COPY pyproject.toml requirements.txt ./
RUN pip install --upgrade pip && pip install .[cli]      # <-- no source copied yet
COPY blitzpack/ ./blitzpack/
COPY cli.py gui.py ./
RUN pip install --no-deps -e .
```

The first `pip install .[cli]` runs with no package source present. With
`setuptools.build_meta` and `packages.find`, that either fails or silently builds an
empty wheel depending on the setuptools version — it's working by accident, and it
defeats the layer-caching it was written to enable. Do it explicitly:

```dockerfile
COPY pyproject.toml requirements.txt ./
# Install only third-party deps first so this layer caches independently of source.
RUN pip install --upgrade pip && \
    pip install zstandard>=0.23.0 xxhash>=3.4.0 msgpack>=1.0.0 rich>=13.0.0

COPY blitzpack/ ./blitzpack/
COPY cli.py ./
RUN pip install --no-deps -e .
```

Note `gui.py` is dropped from the image — a Tkinter GUI in a headless container is
dead weight, and `cli.py`'s `main()` imports it lazily only when argv is empty, which
never happens with an `ENTRYPOINT`. If you keep the copy, also add `tk` to the image
or the double-click fallback path will raise inside the container.

---

## Phase 5 — Tests to add

The existing five roundtrip tests are decent but they only cover the happy path. None
of the bugs in Phase 1 would have been caught. Add these to
`tests/test_roundtrip.py` (or split into `tests/test_integrity.py` and
`tests/test_cli.py`).

```python
def test_analyze_command_runs(tmp_path: Path):
    """Regression: `analyze` used to pass ClassifiedFiles to a FileManifest parameter."""
    import subprocess, sys

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello")

    result = subprocess.run(
        [sys.executable, "-m", "cli", "analyze", str(src)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_scheduler_accepts_manifest_not_classified(tmp_path: Path):
    """Unit-level guard for the same bug, without spawning a process."""
    from blitzpack.analyzer import FileAnalyzer
    from blitzpack.scheduler import WorkScheduler

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x" * 100)

    manifest = FileAnalyzer(src).scan()
    jobs = WorkScheduler().schedule(manifest)
    assert len(jobs) == 1
    assert jobs[0].job_type == "bundle"


def test_unreadable_file_fails_loudly(tmp_path: Path, monkeypatch):
    """A source file that cannot be read must abort compression, not archive as empty."""
    from blitzpack import compressor

    src = tmp_path / "src"
    src.mkdir()
    (src / "good.txt").write_bytes(b"fine")
    (src / "bad.txt").write_bytes(b"unreadable")

    real_open = open

    def failing_open(path, *a, **kw):
        if str(path).endswith("bad.txt"):
            raise PermissionError(13, "Access is denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(compressor, "open", failing_open, raising=False)

    with pytest.raises(Exception) as excinfo:
        compress(src, tmp_path / "out.blitz", level=1, workers=2)
    assert "bad.txt" in str(excinfo.value)


def test_size_change_during_compression_is_detected(tmp_path: Path):
    """A file that shrinks between scan and read must not silently misalign a bundle."""
    from blitzpack.analyzer import FileAnalyzer
    from blitzpack.compressor import SequentialReader, SourceReadError
    from blitzpack.scheduler import WorkScheduler

    src = tmp_path / "src"
    src.mkdir()
    target = src / "shrinking.txt"
    target.write_bytes(b"x" * 5000)

    manifest = FileAnalyzer(src).scan()
    jobs = WorkScheduler().schedule(manifest)

    target.write_bytes(b"x" * 10)  # shrink after scheduling

    with pytest.raises(SourceReadError):
        SequentialReader().read_job(jobs[0])


def test_verify_passes_on_good_archive(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    for i in range(50):
        (src / f"f{i}.txt").write_text(f"content {i}\n")

    archive = tmp_path / "ok.blitz"
    compress(src, archive, level=3, workers=2)

    with open(archive, "rb") as f:
        reader = BlitzArchiveReader(f)
        reader.verify(deep=False)
        reader.verify(deep=True)


def test_verify_detects_flipped_byte(tmp_path: Path):
    """Corrupting a byte in the chunk-data region must fail the whole-archive digest."""
    from blitzpack.archive_format import ArchiveFormatError, CHUNK_DATA_START

    src = tmp_path / "src"
    src.mkdir()
    (src / "payload.bin").write_bytes(os.urandom(200_000))

    archive = tmp_path / "corrupt.blitz"
    compress(src, archive, level=3, workers=2)

    with open(archive, "r+b") as f:
        f.seek(CHUNK_DATA_START + 64)
        original = f.read(1)
        f.seek(CHUNK_DATA_START + 64)
        f.write(bytes([original[0] ^ 0xFF]))

    with open(archive, "rb") as f:
        reader = BlitzArchiveReader(f)
        with pytest.raises(ArchiveFormatError, match="digest mismatch"):
            reader.verify(deep=False)


def test_non_default_chunk_size_roundtrip(tmp_path: Path):
    """Regression: the extractor used to hardcode a 4 MB chunk stride."""
    src = tmp_path / "src"
    src.mkdir()
    payload = os.urandom(3_000_000)
    (src / "big.bin").write_bytes(payload)

    archive = tmp_path / "custom.blitz"
    dest = tmp_path / "out"

    # 1 MB chunks -> the file spans 3 chunks with a non-4MB stride
    compress(src, archive, level=1, workers=2, chunk_size=1024 * 1024)
    decompress(archive, dest, workers=2)

    assert (dest / "big.bin").read_bytes() == payload
```

That last test requires threading a `chunk_size` parameter through `compress()` into
`WorkScheduler(chunk_size=...)`. Worth doing — it's a two-line change and it's the
only way to actually regression-test fix [1.5](#15-bug-multi-chunk-extraction-assumes-a-hardcoded-4-mb-chunk-size):

```diff
 def compress(
     input_path: Path | str,
     output_path: Path | str,
     level: int | str = 3,
     workers: int = 0,
+    readers: int = 0,
+    chunk_size: int = CHUNK_SIZE,
     progress_callback: Optional[ProgressCallback] = None,
 ) -> CompressionResult:
```
```diff
-    scheduler = WorkScheduler()
+    scheduler = WorkScheduler(chunk_size=chunk_size, bundle_target=chunk_size)
```

Also worth adding, cheaply:

- A symlink roundtrip test, skipped on Windows without developer mode
  (`pytest.mark.skipif`). Symlink restoration is currently in a bare
  `except OSError: pass`, so it fails silently and nothing notices.
- A permissions/mtime restoration assertion. `mtime` is restored but never asserted;
  `permissions` is stored in the manifest and **never applied at all** on extract —
  `decompress()` restores symlinks and mtimes but never calls `os.chmod`. That's
  arguably another bug: the README claims "full directory, permission, and timestamp
  restoration."

### 5.1 [BUG] Permissions are stored but never restored

Confirmed from reading `decompressor.py`: the final restoration loop handles
`file_type == 2` (symlinks) and `entry.mtime`, but there is no `os.chmod` anywhere.
`ManifestEntry.permissions` is written and then ignored.

```diff
         if entry.file_type == 2 and entry.symlink_target:
             ...
-        elif entry.mtime:
-            try:
-                os.utime(sanitized_target, (entry.mtime, entry.mtime))
-            except OSError:
-                pass
+        else:
+            # POSIX permissions are meaningless on Windows (os.chmod only toggles the
+            # read-only bit), so only apply them where they mean something.
+            if os.name != "nt" and entry.permissions:
+                try:
+                    os.chmod(sanitized_target, entry.permissions)
+                except OSError:
+                    pass
+            if entry.mtime:
+                try:
+                    os.utime(sanitized_target, (entry.mtime, entry.mtime))
+                except OSError:
+                    pass
```

Note ordering: `chmod` before `utime`, and directories must be restored after their
contents are written (which the existing loop placement already gets right, since it
runs after the pipeline joins).

---

## Appendix A — Corrected README sections

### A.1 Features — replace the misleading bullets

```markdown
- **Two-Tier Scheduling** — Files at or above the 4 MB chunk boundary are split into
  independently addressable chunks; everything smaller is packed in directory-traversal
  order into ~4 MB solid blocks (up to 256 files each), which compresses far better
  than the same files handled individually. No manual tuning required.
- **Producer-Consumer Pipeline** — Reader threads pull *read groups* — one solid bundle,
  or every consecutive chunk of a single large file — so each large file is streamed
  through one open handle instead of being scattered across threads. Compression runs in
  parallel via zstandard, which releases the Python GIL.
- **Seekable Archive Format** — Each chunk is independently addressable via an embedded
  seek table, enabling random-access extraction without decompressing the whole archive.
- **Integrity Verification** — Every chunk is checksummed with xxHash-64 at compression
  time and verified on extraction. `blitzpack verify` checks the whole-archive digest
  over the contiguous chunk-data region in a single sequential read; `--deep` additionally
  decompresses and checksums every chunk, including any not referenced by the manifest.
- **Pure Python** — Core library and CLI run anywhere Python 3.10+ does, with no native
  build step. The GUI is developed and tested on Windows (it uses DWM Mica for the
  translucent backdrop, degrading gracefully elsewhere).
- **Desktop GUI** — Dual-mode file manager and archive browser with drag-and-drop,
  compression profiles, and live progress with speed/ETA.
- **Rich CLI** — `compress`, `decompress`, `list`, `verify`, and `analyze` with progress
  bars and archive inspection.
```

Delete the "**Cross-Platform**" bullet as written and the "Size-Tiered Scheduling"
bullet's 16 MB / 64 KB thresholds — those numbers don't exist in the pipeline.

### A.2 Archive format — corrected header and footer

The current diagram is wrong in three places: it omits the codec and chunk-size
fields, states a 16-byte reserved block (it's 11 bytes), and gives the footer magic as
4-byte `"BLTZ"` (it's the 8-byte `b"BLTZEND\x00"`).

```markdown
┌─────────────────────────────────────────────┐
│  Header (32 bytes)  struct "<4sHBIH11sQ"    │
│  ├─ Magic: "BLTZ"                (4 bytes)  │
│  ├─ Version: uint16                         │
│  ├─ Codec: uint8   (0=stored, 1=zstd)       │
│  ├─ Chunk size: uint32                      │
│  ├─ Flags: uint16                           │
│  ├─ Reserved                    (11 bytes)  │
│  └─ Redundant seek table offset: uint64     │
├─────────────────────────────────────────────┤
│  Chunk Data (contiguous, index order)       │
│  ├─ Chunk 0: [zstd frame | raw bytes]       │
│  ├─ Chunk 1: [zstd frame | raw bytes]       │
│  └─ ...                                     │
├─────────────────────────────────────────────┤
│  Seek Table                                 │
│  ├─ Entry count: uint32                     │
│  └─ Per entry (32 bytes) "<QIIQII":         │
│     offset, compressed_size, original_size, │
│     digest, overlap_prefix (reserved, 0),   │
│     flags (bit 0 = stored raw)              │
├─────────────────────────────────────────────┤
│  Manifest (msgpack)                         │
│  └─ Paths, sizes, mtimes, types, symlink    │
│     targets, permissions, chunk mappings    │
├─────────────────────────────────────────────┤
│  Footer (48 bytes)  struct "<QQQQII8s"      │
│  ├─ Seek table offset: uint64               │
│  ├─ Manifest offset: uint64                 │
│  ├─ Total original size: uint64             │
│  ├─ Archive digest: uint64                  │
│  ├─ Entry count: uint32                     │
│  ├─ Reserved: uint32                        │
│  └─ Magic: "BLTZEND\0"           (8 bytes)  │
└─────────────────────────────────────────────┘

The chunk-data region is contiguous and written in chunk-index order, starting at byte
32. The archive digest is the xxHash-64 of that entire region, so integrity can be
checked with one sequential read and no decompression.
```

### A.3 Installation — point at Releases, not `bin/`

```markdown
### Standalone Windows Executables (No Python Required)

Download the latest signed-off build from the
[Releases page](https://github.com/netfreakkk/BlitzPack/releases/latest):

* **`blitzpack-gui.exe`** — the desktop application (no console window).
* **`blitzpack.exe`** — the CLI archiver for scripts and terminal use.

Both are built from source by
[`.github/workflows/release.yml`](.github/workflows/release.yml) on every tagged
release, so you can trace any binary back to the exact commit that produced it.
```

### A.4 Usage — document `verify`

```markdown
# Check integrity without extracting (fast: whole-archive digest)
blitzpack verify project.blitz

# Paranoid mode: decompress and checksum every chunk
blitzpack verify project.blitz --deep

# Tune reader threads for a spinning disk
blitzpack compress ./my_project -o project.blitz --readers 2
```

### A.5 Project structure — add the new module

```diff
 blitzpack/              Core compression library
 ├── __init__.py         Public API exports
 ├── analyzer.py         File discovery, metadata, chunked/bundled classification
 ├── archive_format.py   Binary archive reader/writer + integrity verification
 ├── checksum.py         xxHash-64 digest computation
 ├── compressor.py       Producer-consumer parallel compression pipeline
+├── constants.py        Shared chunk/bundle tuning constants
 ├── decompressor.py     Producer-consumer parallel extraction pipeline
 ├── scheduler.py        Directory-order job scheduling with solid bundling
 └── utils.py            Path sanitization, progress types, formatting
```

---

## Appendix B — Full replacement files

These two modules change pervasively enough that a diff is harder to apply than a
replacement. Everything else in this plan is a targeted edit.

### B.1 `blitzpack/checksum.py`

```python
"""Checksum calculation using xxHash-64 for chunk and archive integrity verification."""

from pathlib import Path
from typing import BinaryIO

import xxhash

DEFAULT_BLOCK_SIZE = 4 * 1024 * 1024


def compute_digest(data: bytes) -> int:
    """Compute the 64-bit xxHash-64 digest of an in-memory buffer."""
    return xxhash.xxh64_intdigest(data)


def compute_file_digest(file_path: Path, block_size: int = DEFAULT_BLOCK_SIZE) -> int:
    """Compute the xxHash-64 digest of an entire file using streaming reads."""
    hasher = xxhash.xxh64()
    with open(file_path, "rb") as f:
        while block := f.read(block_size):
            hasher.update(block)
    return hasher.intdigest()


def compute_stream_digest(
    stream: BinaryIO, start: int, end: int, block_size: int = DEFAULT_BLOCK_SIZE
) -> int:
    """Compute the xxHash-64 digest of the byte range [start, end) of an open stream.

    Used to verify the whole-archive digest: the writer hashes every compressed frame
    in write order, and because frames are written contiguously in chunk-index order,
    that is byte-for-byte the same as hashing this range.
    """
    if end < start:
        raise ValueError(f"Invalid range: end ({end}) < start ({start})")

    hasher = xxhash.xxh64()
    stream.seek(start)
    remaining = end - start
    while remaining > 0:
        block = stream.read(min(block_size, remaining))
        if not block:
            raise EOFError(f"Unexpected EOF with {remaining} bytes left to hash")
        hasher.update(block)
        remaining -= len(block)
    return hasher.intdigest()


class IncrementalHasher:
    """Streaming xxHash-64 hasher for tracking archive content integrity."""

    __slots__ = ("_hasher",)

    def __init__(self) -> None:
        self._hasher = xxhash.xxh64()

    def update(self, data: bytes) -> None:
        self._hasher.update(data)

    def digest(self) -> int:
        return self._hasher.intdigest()
```

### B.2 `blitzpack/compressor.py`

```python
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
    except BaseException as exc:  # noqa: BLE001 - forwarded to the main thread verbatim
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
    except BaseException as exc:  # noqa: BLE001
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

    # Reader count is a device property, not a CPU property: NVMe benefits from a deep
    # queue, spinning disks are destroyed by it. Moderate default, caller can override.
    num_readers = readers if readers > 0 else max(2, min(8, num_workers))

    level_int = LEVEL_PROFILES.get(level.lower().strip(), 3) if isinstance(level, str) else int(level)

    start_time = time.perf_counter()

    # 1. Discovery
    manifest = FileAnalyzer(in_p).scan()

    # 2. Scheduling, in directory-traversal order
    scheduler = WorkScheduler(chunk_size=chunk_size, bundle_target=chunk_size)
    ordered_jobs = sorted(scheduler.schedule(manifest), key=lambda j: j.job_id)
    total_jobs = len(ordered_jobs)

    # job_id is assigned sequentially by the scheduler, so after sorting the chunk index
    # is simply the position in this list.
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
            # Manifest-only entries own no chunk. -1 makes that explicit rather than
            # pointing at chunk 0, which belongs to some other file.
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
        # Bundled files are appended below, from the bundle jobs themselves.

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

    # How many source files each job finalizes, for honest progress reporting.
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
        # Always tear down, including on the error path, so library and GUI callers
        # don't accumulate threads on every failure.
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

    # 5. Publish the finished archive
    for _attempt in range(5):
        try:
            if out_p.exists():
                out_p.unlink()
            temp_archive_path.rename(out_p)
            break
        except PermissionError:
            time.sleep(0.5)  # Windows: a scanner may briefly hold the temp handle
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
```

> **Note on `CompressionResult`:** adding `total_entries` and changing `total_files` to
> mean "regular files" is a breaking change for anything reading those fields. Update
> `cli.py::handle_compress` and the GUI's completion scorecard (search `gui.py` for
> `total_files`) when you apply this.

---

## Suggested commit sequence

Small, reviewable commits — each one should leave the test suite green:

1. `chore: stop tracking prebuilt binaries` (Phase 0.1)
2. `ci: run tests and lint on push and PR` (Phase 0.2)
3. `fix: analyze crashed passing ClassifiedFiles where a FileManifest was expected` (1.1)
4. `refactor: classify files along the seam the scheduler actually uses` (1.2)
5. `fix: fail loudly on unreadable or resized source files` (1.3, 1.4) — **the important one**
6. `fix: derive multi-chunk offsets from the seek table instead of a hardcoded 4 MB` (1.5)
7. `fix: always tear down pipeline threads, and time out the writer loop` (1.7)
8. `feat: verify whole-archive and per-chunk digests; add blitzpack verify` (2.1, 2.2)
9. `fix: restore POSIX permissions on extraction` (5.1)
10. `perf: read consecutive chunks of a file in one group on one handle` (3.1, 3.2)
11. `fix: report real file counts and time-throttled progress` (3.4, 3.5)
12. `fix: format_bytes never reached PB; drop dead blitz_core branch` (4.1, 4.2)
13. `test: cover analyze, read failures, corruption, and non-default chunk sizes` (Phase 5)
14. `docs: describe the pipeline and archive format as implemented` (Appendix A)

Items 5, 6, and 8 are the ones that change whether a BlitzPack archive can be trusted.
If you only do three things, do those.
