# BlitzPack — Performance, Benchmarking & Production Roadmap

Target profile for this whole document: **low-end / laptop**, specifically a 4-core
budget: Intel Core 5 210H with affinity pinned to 4 performance cores, 16 GB DDR4,
NVMe SSD assumed. Every optimization is judged by whether it helps *that* machine —
not a 32-thread workstation. On 4 cores the constraints are different and some of the
current design choices (12–16 reader threads, worker counts scaled off `os.cpu_count`)
actively hurt.

Confidence tags:
- **[C]** confirmed from reading the code — the cost is structural, not speculative.
- **[H]** hypothesis — plausible from the code but must be confirmed with a profiler
  before you spend time on it. Do not trust these until Part 3's instrumentation says so.

---

## Part 1 — Where the time actually goes

### 1.0 Rule zero: instrument before you touch anything

You cannot "find the actual bottleneck" by reading code alone — you can only find
*candidates*. Before any optimization work, add phase timers so every claim below
becomes a number. This is a prerequisite, not optional.

Add a lightweight phase-timing accumulator to `compress()` and `decompress()`. Minimal
version — a module-level toggle so it costs nothing in production:

```python
# blitzpack/_profiling.py
import os, time, threading, collections

ENABLED = os.environ.get("BLITZ_PROFILE") == "1"
_lock = threading.Lock()
_totals = collections.defaultdict(float)
_counts = collections.defaultdict(int)

class span:
    __slots__ = ("name", "t0")
    def __init__(self, name): self.name = name
    def __enter__(self): self.t0 = time.perf_counter(); return self
    def __exit__(self, *a):
        if ENABLED:
            dt = time.perf_counter() - self.t0
            with _lock:
                _totals[self.name] += dt
                _counts[self.name] += 1

def report():
    if not ENABLED: return
    print("\n=== BlitzPack phase profile ===")
    for name, total in sorted(_totals.items(), key=lambda x: -x[1]):
        print(f"  {name:28s} {total:8.3f}s  x{_counts[name]}")
```

Then wrap the real cost centers — the scan, each reader's `read_job`, each worker's
`compress`, each worker's `compute_digest`, and the main thread's `write_chunk`. Run
with `BLITZ_PROFILE=1`. The relative sizes of `read_wait`, `compress`, `digest`, and
`write` on *your* corpora decide everything in Part 2. Aggregate wall-clock in threads
overlaps, so treat these as "total CPU-seconds per phase," not a timeline — but the
ratios are what you need.

Also capture, per run: peak RSS (`psutil.Process().memory_info().rss` sampled in a
thread), and thread count. Memory bandwidth is a real ceiling on DDR4 with 4 cores.

### 1.1 Compression pipeline — cost centers

Walking the actual code path in `compress()`:

**A. Serial discovery/scan phase — [C], high impact on cold cache.**
`FileAnalyzer.scan()` runs to completion *before the pipeline starts a single read*.
It's single-threaded: a LIFO `os.scandir` walk that touches every entry and calls
`dir_entry.stat(follow_symlinks=False)`. For a 60k-file tree on a **cold cache**, this
is tens of thousands of metadata lookups paid serially, with zero overlap with
compression. On Windows/NTFS the DirEntry carries stat data from the scandir itself
(cheap), so the cost is directory-enumeration latency, not extra syscalls — but it's
still a pure-latency prelude. This is the single most likely reason cold-cache numbers
lag warm-cache by ~2.5x in your existing README table. **Time-to-first-byte is gated
entirely by full-tree enumeration.**

**B. zstd compression — [C], the dominant CPU cost on warm cache.**
Each worker holds one `ZstdCompressor(level=level)` and one-shot compresses each
payload. `compress()` releases the GIL, so with 4 workers you get real 4-core
parallelism here. On warm cache this *is* the throughput number. Levers: level,
zstd advanced params, and dictionaries (Part 2).

**C. xxHash digest of raw data in the worker — [H], possibly non-trivial.**
`compute_digest(raw)` runs on every payload before compression. `xxhash.xxh64_intdigest`
on a `bytes` object — the C extension releases the GIL for large buffers, but the
call + int-boxing has per-call overhead. At level 1 ("fast"), compression is so cheap
that **hashing can rival compression time**. Profile `digest` vs `compress` at level 1
specifically. If digest is >20% of worker time at low levels, it's a real target
(hash the *compressed* frame instead, or fold hashing into a zstd streaming pass).

**D. Per-job Python orchestration overhead — [H], scales with file count.**
For a many-small-files tree, the cost is not compression, it's *Python*. Every job is
a `queue.put`/`queue.get` round trip, a dataclass allocation (`JobPayload`,
`JobResult`), a `task_done`, dict inserts into `pending`, and several O(n) passes in
manifest construction (`chunks_by_path`, `files_per_job`, the manifest loops). On
60k+ items this GIL-bound bookkeeping runs on top of everything else and does not
parallelize. Bundling already amortizes *compression* across small files, but the
manifest is still built per-file. **This is the hidden tax on the workload BlitzPack
most wants to win (source trees / node_modules).**

**E. Small-file `open()` storm in bundle reads — [C], Windows-heavy.**
`SequentialReader.read_job` for a bundle does a fresh `open()/read()/close()` per
member. With `max_bundle_members=256` and `bundle_target=4MB`, a tree of 1–5 KB files
produces bundles capped at 256 members → still tens of thousands of `open()` calls,
each hitting NTFS + Defender. `open()` on Windows is expensive. Raising the member cap
(Part 2.4) directly cuts syscall count.

**F. In-order writing / head-of-line blocking — [C] but low impact.**
The main writer only advances when `next_write_id` is ready, so a slow early chunk
buffers later completed chunks in `pending`. Because chunks are ≤ `chunk_size` (4 MB),
the stall is bounded and small — *not* worth fixing for its own sake. (It becomes
relevant only if you raise chunk size a lot; see Part 2.6.)

**G. Redundant `sorted()` — [C], trivial.**
`sorted(scheduler.schedule(manifest), key=lambda j: j.job_id)` re-sorts a list the
scheduler already emits in `job_id` order. O(n log n) for nothing. Make it `list(...)`.
Micro, but free.

### 1.2 Decompression pipeline — cost centers

**H. Single writer thread — [C], the extraction bottleneck for small files.**
`_writer_thread_loop` is **one thread** doing every `open()/write()/close()` and every
`seek()/write()` for the whole archive. Workers decompress in parallel across 4 cores,
then funnel into one serializing writer. For a bundle chunk with 256 members, that one
thread does 256 opens back-to-back while the decompress workers idle waiting for
`write_queue` space. Extraction of many small files is **writer-bound, not
decompress-bound.** Your "12.4x faster extraction" is versus your own older code, not
versus this thread being the ceiling. Parallelizing the writer (Part 2.7) is the
biggest decompress win available.

**I. Single archive reader thread — [H], likely fine on NVMe.**
One thread reads the whole archive sequentially. Sequential NVMe read is fast and this
is probably not the limiter, but confirm it isn't starving workers on your SSD.

**J. `wb+` truncate + seek per multi-chunk file — [C], minor.**
Multi-chunk files are opened `wb+`, `truncate(total_size)` (which on NTFS without
`SetFileValidData` privilege writes zeros / extends), then seeked per chunk. For huge
files this pre-zero can cost. Low priority for the low-end target unless you test
multi-GB single files.

### 1.3 Summary table

| # | Cost center | Confidence | Cold cache | Warm cache | Workload most affected |
|---|-------------|-----------|-----------|-----------|------------------------|
| A | Serial scan before pipeline | C | **High** | Low | Many files |
| B | zstd compress | C | Med | **High** | All (ratio + speed) |
| C | xxHash of raw data | H | Low | Med (at low levels) | All, esp. level 1 |
| D | Per-job Python overhead | H | Med | **High** | Many small files |
| E | Bundle `open()` storm | C | **High** | Med | Many small files (Windows) |
| F | In-order write HOL blocking | C | Low | Low | Few huge files |
| H | Single decompress writer | C | **High** | **High** | Extraction, many small files |
| I | Single decompress reader | H | Med | Low | Extraction, huge archives |

The through-line: **BlitzPack's stated advantage is many-files parallelism, and its
two biggest un-addressed bottlenecks (A serial scan, H single writer) are both on the
many-files path.** Those are where the cold-cache wins against WinRAR/7-Zip actually
live.

---

## Part 2 — Optimizations, ordered by expected payoff on 4 cores

Do them in this order; re-profile after each. Don't stack unmeasured changes.

### 2.1 Right-size the thread pools for core count — [do first, cheap, low-end critical]

On a 4-core pin, the current code launches `max(2, min(8, workers))` = **4 readers +
4 workers + 1 writer + main = 10 threads for 4 cores.** Readers and workers both want
CPU (readers less so — `file.read` releases the GIL during the syscall, but the Python
around it doesn't). This is oversubscription, and on a low-core box it causes context-
switch thrash and cache eviction that a 32-thread box hides.

Changes:
- Decouple reader count from worker count. Readers are a *device* property. Default to
  **2** on ≤4 cores, and let `--readers` override (which — reminder from the last
  review — must actually be wired through `handle_compress`).
- Consider `num_workers = min(requested, os.cpu_count())` and, when pinned, respect
  `len(os.sched_getaffinity(0))` on Linux / `psutil.Process().cpu_affinity()` on
  Windows so "4 workers" means 4, not "cpu_count() = 8 logical".

```python
try:
    import psutil
    _avail = len(psutil.Process().cpu_affinity())
except Exception:
    _avail = os.cpu_count() or 4
num_workers = workers or _avail
num_readers = readers if readers > 0 else (2 if _avail <= 4 else min(8, _avail))
```

Expected: measurable warm-cache gain on 4 cores purely from less oversubscription.
**[H]** — but cheap to try and easy to measure.

### 2.2 Overlap the scan with the pipeline — [biggest cold-cache win]

Right now: `scan()` → `schedule()` → pipeline, strictly serial. The scan's metadata
latency is dead time. Make discovery a *producer* that streams file entries into the
scheduler while readers start consuming the earliest jobs.

Two implementation tiers:

**Tier 1 (simpler): parallelize the directory walk.** Replace the single-threaded LIFO
walk with a small pool of directory-scanning threads pulling subdirectories off a
shared queue. Directory enumeration is largely I/O-latency-bound, so 2–4 scanner
threads overlap that latency. The manifest still completes before scheduling, but the
scan itself finishes far sooner on cold cache.

**Tier 2 (bigger, more payoff): streaming schedule.** Emit bundle/chunk jobs as files
are discovered rather than after the full walk. The complication is that the *manifest*
(written into the archive) needs the complete entry list and each file's final chunk
mapping. Solution: build the manifest incrementally as jobs are created (you already
compute `start_chunk`/`end_chunk` deterministically from job order), and finalize it at
the end. This lets compression of the first files begin while the tail of a 60k-file
tree is still being enumerated — hiding essentially all of cost center A on cold cache.

Start with Tier 1, measure, then decide if Tier 2's complexity is worth it. On warm
cache the scan is cheap and this barely matters — this is a **cold-cache** play, which
is exactly the column where you beat WinRAR.

### 2.3 zstd parameter tuning + dictionaries — [ratio win, closes the 21% gap]

Your README's own numbers show BlitzPack producing a **1.25 GB archive vs WinRAR's
1.03 GB** — you win on speed but lose 21% on ratio. Two levers:

**a) Advanced zstd params for the large/chunk tier.** For chunked large files, enable
long-distance matching and a larger window:

```python
params = zstd.ZstdCompressionParameters.from_level(
    level, enable_ldm=True, window_log=27,
)
compressor = zstd.ZstdCompressor(compression_params=params)
```

LDM helps large files with long-range redundancy (logs, VM images, datasets) at modest
cost. **[H]** — measure ratio delta and speed cost on your large-file corpus.

**b) Trained dictionaries for the bundle (small-file) tier — the real ratio weapon.**
Many small files in a source tree share enormous structure (imports, boilerplate, JSON
keys). zstd dictionary training (`zstd.train_dictionary`) builds a shared dictionary
from a sample of the corpus; compressing each small bundle *with* that dictionary can
dramatically improve ratio on small, similar files — the exact case where solid
archivers (7-Zip solid mode) currently beat you. Flow:
1. During scan, sample N small files (e.g. up to 100 MB of them).
2. `dict_data = zstd.train_dictionary(dict_size, samples)`.
3. Store the dictionary as a dedicated first chunk in the archive (new format flag).
4. Bundle workers use `ZstdCompressor(dict_data=dict_data)`; decompress side loads it.

This is a format change (bump `CURRENT_VERSION`, add a `FLAG_HAS_DICT` and a dictionary
region) but it's the highest-leverage ratio improvement and it plays directly to
BlitzPack's "lots of small files" thesis. **[H]** on magnitude — but dictionaries
routinely give 2–5x ratio improvement on small-similar-file corpora, so this is where
"crush 7-Zip on the many-files case" could actually become true.

**c) Honest framing.** Also just raise the default profile if the benchmark shows 4
cores can afford it. Level 3 is very conservative; on 4 cores level 6–9 may still beat
WinRAR's wall-clock while closing most of the ratio gap. The benchmark's level sweep
(Part 3) tells you the sweet spot for *this* hardware.

### 2.4 Cut the small-file `open()` storm — [cold-cache, Windows]

Raise `MAX_BUNDLE_MEMBERS` well above 256 (try 1024–4096) so a tree of tiny files
produces far fewer bundles and far fewer `open()` calls, and larger solid blocks (which
also compress better). The only cost is coarser seek granularity for random extraction,
which is fine for the bundle tier. Combine with keeping `bundle_target` (4 MB) as the
primary flush trigger so bundles don't get pathologically large. **[C]** that this cuts
syscalls; **[H]** on wall-clock magnitude — measure on a node_modules-style tree.

Also consider, for the bundle reader, an optional path that `mmap`s or batch-reads
rather than per-file `open`, though on Windows the win is limited by NTFS. Lower
priority than raising the cap.

### 2.5 Reduce per-job Python overhead — [warm-cache, many-files]

If Part 1's profiler shows cost center D is real:
- **Batch queue traffic.** Instead of one queue item per job, have readers push small
  *batches* of payloads (e.g. lists of 16) so `queue.put/get` and `task_done` amortize.
  Same for results. Fewer GIL-bound round trips.
- **Avoid redundant passes.** Collapse the three O(n) loops in manifest construction
  (`chunks_by_path`, the entry loop, `files_per_job`) into as few passes as possible.
- Drop the redundant `sorted()` (cost center G).
- Keep dataclasses `slots=True` (already done) — good.

This is grind work with real payoff only on huge file counts; let the profiler justify
it before doing it.

### 2.6 Optional: out-of-order writing — [only if you raise chunk size]

If you keep 4 MB chunks, skip this (HOL blocking is negligible — cost center F). If you
ever raise chunk size for large-file throughput, the in-order write becomes a real
stall. To remove it, write chunks as they complete (the seek table already records true
offsets per index) and make the whole-archive digest order-independent — e.g. combine
per-chunk digests commutatively, or hash the seek table region instead of the raw
chunk-data range. That change ripples into `verify()`, so only do it with the benchmark
showing large-file throughput actually gated by ordering. **Low priority for the
low-end / many-small-files target.**

### 2.7 Parallelize the decompression writer — [biggest decompress win]

Replace the single `_writer_thread_loop` with a small pool of writer threads for the
**independent-file** case (bundles and single-chunk files — each output file is written
by exactly one chunk, so no coordination needed). Keep multi-chunk large files on a
dedicated path (or shard by target path so a given file is always handled by the same
writer, preserving the shared handle + truncate logic).

Design: hash each `ExtractTarget.target_path` to a writer index (`hash(path) % W`) so
all chunks of one file go to one writer, then run W writer threads. This turns the
serial `open()/write()/close()` storm on extraction into a 4-wide parallel one — the
mirror of what makes compression fast. **[C]** that the current writer is serial;
**[H]** on speedup, but for many-small-file extraction this is likely the largest
single improvement in the whole codebase.

### 2.8 Memory-bandwidth hygiene — [DDR4, 4 cores]

On DDR4 with 4 cores you can become bandwidth-bound moving GBs through queues. Cheap
wins: avoid `b"".join(parts)` re-allocation where a preallocated `bytearray` +
`memoryview` slices would do; don't hold `pending`/`write_queue` deeper than necessary
(tighten `maxsize`); free references promptly (the decompressor already `del`s). **[H]**,
low-to-medium — profile RSS and see if you're near memory-bandwidth saturation before
investing.

### 2.9 Priority recap

| Priority | Change | Cold/Warm | Effort | Confidence |
|----------|--------|-----------|--------|-----------|
| 1 | 2.1 right-size threads for 4 cores | Warm | XS | H |
| 2 | 2.7 parallel decompression writer | Both | M | C/H |
| 3 | 2.2 Tier 1 parallel scan | Cold | M | C/H |
| 4 | 2.3b trained dictionaries (ratio) | Both | L | H |
| 5 | 2.4 raise bundle member cap | Cold | XS | C/H |
| 6 | 2.5 per-job overhead batching | Warm | M | H |
| 7 | 2.3a LDM for large files | Both | S | H |
| 8 | 2.2 Tier 2 streaming scan | Cold | L | H |

XS/S/M/L = effort. Ship 1, 5 immediately (tiny), then let the benchmark rank the rest.

---

## Part 3 — Factual benchmarking plan

The goal is numbers you'd stand behind in the README — reproducible, fair, and honest
about variance. This is designed for **your** box: Core 5 210H, 4 P-cores pinned,
16 GB DDR4.

### 3.1 Pin to 4 performance cores (control the variable)

"Use only 4 performance cores" has two interpretations — pick one and state it:
- **4 physical P-cores, HT allowed** (8 logical): a realistic "4 fast cores" laptop.
- **4 logical processors total**: emulates a genuinely 4-thread budget chip.

Identify which logical processors are your P-cores (Task Manager → Details → right-click
→ Set affinity shows the list; or HWiNFO64 labels P vs E cores). Then pin the whole
benchmark process — and crucially the *competitor* processes too — to the same set, so
every tool gets identical silicon.

Cleanest, auditable method in the harness (Python), which also pins child processes you
spawn:

```python
import psutil
P_CORES = [0, 2, 4, 6]   # <-- replace with YOUR P-core logical IDs
psutil.Process().cpu_affinity(P_CORES)
# child processes (7z.exe, rar.exe) started via subprocess inherit this affinity
```

For BlitzPack itself also pass `--workers 4`. For a strict 4-thread test, pin to 4
logical IDs and set every tool to 4 threads (below). **Disable Turbo variance** as much
as you can: plug in the laptop, set Windows power plan to a fixed "High performance",
and note that thermal throttling on a thin laptop will skew long runs — keep runs short
and watch for clock droop (HWiNFO can log effective clocks; discard runs where clocks
sagged).

### 3.2 Cold vs warm cache — define and enforce

- **Warm cache:** run the operation once (discard), then measure the next N runs. The
  file data is in the OS standby list / page cache.
- **Cold cache (Windows):** the OS caches aggressively; you must evict between runs.
  Reliable options, in order of preference:
  1. **EmptyStandbyList.exe** (or Sysinternals RAMMap `-Et` / "Empty → Standby List").
     Run `EmptyStandbyList.exe standbylist` immediately before each cold run. Scriptable.
  2. Copy the dataset onto a spot you then evict, or read a large "cache-buster" file
     (> RAM) to push the dataset out of standby — cruder, less exact.
  3. Reboot between runs — gold standard, impractical for N≥5.

  Document exactly which you used. Cold-cache numbers are only comparable if every tool
  faces the same eviction method.

- **Windows Defender is a massive, real variable** on archive I/O (it scans every file
  opened/created). Run the benchmark **twice**: once with the dataset directory and the
  output directory added to Defender exclusions, once without. Report both, and disclose
  it. A "we beat WinRAR" claim that silently had Defender off is exactly the kind of
  thing to avoid. If WinRAR/7-Zip run with Defender on, BlitzPack must too.

### 3.3 Datasets — reproducible and representative

Pick corpora others can obtain, covering the axes that stress different cost centers:

| Dataset | What it stresses | Source |
|---------|------------------|--------|
| **Silesia corpus** (~212 MB) | Ratio comparison, mixed content — the standard | sun.aei.polsl.pl/~sdeor/index.php?page=silesia |
| **CPython or Linux kernel source tree** | Many small files, bundle tier, `open()` storm, scan | git checkout, then benchmark the working tree |
| **A real node_modules** (e.g. `npm i` on a mid app) | Extreme small-file count, worst case for scan + writer | reproducible from a committed package-lock |
| **Few large binaries** (a Linux ISO, a few GB of video) | Chunk tier, large-file throughput, LDM | any large files you can name |
| **Already-compressed set** (mp4/zip/jpg) | Overhead + "stored" fast-path, worst-case ratio | your own media |

Report file count and total bytes for each. The many-small-files trees are where
BlitzPack's design should shine and where the honest comparison against 7-Zip solid
mode matters most. Keep at least one corpus > your 16 GB RAM if you want a truly cold
large-file test (otherwise it all fits in cache and "cold" is hard to sustain).

### 3.4 Competitors — exact, matched flags

Fairness = same thread count, disclosed levels, same cache state. Suggested commands
(adjust paths); **match threads to your pin (4)**:

- **7-Zip** (`7z.exe`), zstd or LZMA2:
  - LZMA2, comparable-ish speed: `7z a -t7z -m0=LZMA2 -mx=5 -mmt4 out.7z <dir>`
  - Max threads honesty: `-mmt4` caps to 4. Sweep `-mx=1,3,5,9`.
  - For an apples-to-apples *algorithm* comparison, if your 7-Zip build has zstd
    (`-m0=zstd`), test that too so you compare pipeline vs pipeline at the same codec.
- **WinRAR** (`Rar.exe`, the CLI):
  - `Rar.exe a -m3 -mt4 out.rar <dir>` (`-m1..-m5` compression, `-mt4` threads).
  - Note RAR's algorithm differs; disclose it (your README already does — keep that).
- **BlitzPack**: `blitzpack compress <dir> -o out.blitz -w 4 --level <L> --readers <R>`
  across a level and reader sweep.
- **Baseline references** worth including: `tar` alone (I/O floor), `zstd -T4 -<L>` on a
  tarball (pure-codec ceiling — tells you how much overhead BlitzPack's format +
  Python add over raw zstd at the same level/threads). This last one is the most
  useful internal yardstick: if BlitzPack is far behind `tar | zstd -T4`, the gap is
  your Python/orchestration overhead, not the codec.

Measure **decompression/extraction** for every tool too, not just compression — that's
where cost center H lives and where your "12.4x" claim needs an external anchor.

### 3.5 Metrics, runs, and statistics

Per (tool, dataset, level, cache-state, defender-state):
- Wall-clock compress time and extract time (`time.perf_counter` around the subprocess).
- Output archive size (bytes) and ratio (`original / archive`).
- Throughput MB/s = original_bytes / compress_seconds.
- Peak RSS (sample the process tree every 50 ms in a watcher thread).
- Average CPU utilization (psutil `cpu_percent`) — shows whether you're actually using
  the 4 cores or stalling on I/O.
- **Correctness gate:** after each BlitzPack run, extract and assert a recursive
  hash of the tree equals the source (you already have `_hash_directory` in tests).
  A fast archiver that corrupts data is disqualified, not faster.

Runs: **≥ 5** per cell (7 is better). Report **median** as the headline with **min–max**
range; discard the first warm run explicitly. Never report a single number. Variance on
a thermally-limited laptop is real — if min–max spread exceeds ~15%, say so and
investigate throttling.

### 3.6 Harness

Write `benchmark.py` (commit it — the README currently `.gitignore`s a benchmark
script, which is why the numbers aren't reproducible). Shape:

```python
# benchmark.py  — run: python benchmark.py --config bench.toml
# - reads a config listing tools (command templates), datasets (paths), levels
# - pins affinity to P_CORES
# - for each cell: N runs, evicting standby list before cold runs
# - samples RSS/CPU in a watcher thread
# - verifies BlitzPack roundtrip correctness
# - writes results.csv: tool,dataset,level,cache,defender,run,seconds,bytes,ratio,rss,cpu
# - prints median/min/max summary table
```

Keep tool invocations as string templates in config so adding a competitor is a config
edit, not a code change. Emit raw `results.csv` (every run, not just aggregates) so
anyone can recompute the stats — that raw file is what makes the benchmark *factual*.

### 3.7 Reporting template (for the README)

For each dataset, a table with: tool, level/preset, compress median (cold), compress
median (warm), extract median, archive size, ratio, peak RSS, and the disclosed
Defender/thread/cache settings in a caption. Include one sentence on hardware (Core 5
210H, 4 P-cores pinned, 16 GB DDR4, NVMe, Windows 11, Defender state) and one on
methodology (N runs, median reported, eviction method). That caption is the difference
between a benchmark and a marketing claim.

### 3.8 What "crushing WinRAR/7-Zip on low-end" realistically looks like

Be clear-eyed about the target outcome so the benchmark can confirm or refute it:
- **Compression wall-clock (warm & cold):** BlitzPack should win on many-files trees —
  that's the parallel-pipeline thesis, and 4 cores is enough to beat single-threaded
  file handling. This is your strongest claim.
- **Extraction wall-clock:** should win *after* Part 2.7 (parallel writer); may not
  before it.
- **Ratio:** you will likely still trail LZMA2 `-mx9` until dictionaries (2.3b) land,
  and possibly after. The honest headline is "comparable ratio, far better speed on
  multi-file workloads at low core counts" — not "smaller archives than 7-Zip max."
  Let the numbers set the claim, not the other way around.

---

## Part 4 — Production-readiness roadmap

Grouped by what blocks "real users can trust this with real data." Roughly prioritized
within each group.

### 4.1 Correctness & security — do these before advertising it as a real archiver

1. **Path-traversal (Zip Slip) on extraction — [security bug].** `decompress()` builds
   `target_path = out_p / entry.path` and trusts the manifest. A malicious or corrupt
   archive with `entry.path` like `..\..\Windows\System32\...`, an absolute path, or a
   drive letter can write **outside** the output directory. Compression sanitizes paths
   going in, but extraction must not trust them coming out. Before writing any file,
   resolve the target and assert it stays under `out_p`:
   ```python
   dest = (out_p / entry.path).resolve()
   if not dest.is_relative_to(out_p.resolve()):   # py3.9+: is_relative_to
       raise ArchiveFormatError(f"Refusing unsafe path in archive: {entry.path!r}")
   ```
   Also reject absolute paths and `..` components at read time. This is the single most
   important item in this document for real-world safety.

2. **Untrusted-archive DoS in the reader — [security].** `BlitzArchiveReader` reads a
   `uint32` seek-table count and loops that many times, and `msgpack.unpackb`s a
   manifest region of attacker-controlled length with no limits. A crafted footer can
   make the reader attempt huge allocations. Add sanity bounds: cap `num_entries`
   against remaining file size (`(seek_table_offset..manifest_offset)` must physically
   hold that many 32-byte entries), and pass `msgpack.unpackb(..., max_*_len=...)` limits
   / a max manifest size. Fuzz the reader with corrupted inputs (see 4.4).

3. **Symlink extraction safety — [security].** Restoring `os.symlink(entry.symlink_target,
   ...)` from an untrusted archive can create links pointing anywhere (a following write
   could then escape). Validate symlink targets, or make symlink restoration opt-in
   (`--restore-symlinks`), defaulting to skip with a warning.

4. **Cryptographic integrity vs. corruption integrity.** xxHash catches accidental
   corruption, not tampering (it's not cryptographic). If you ever claim "integrity"
   for security purposes, that's misleading. Either scope the claim to corruption
   detection (fine, and true), or add an optional authenticated mode.

5. **Optional encryption — [feature, but a trust prerequisite for many users].** AES-256
   in an AEAD mode (GCM) over chunks, key from a password via a real KDF (scrypt/argon2).
   Keep it opt-in and clearly separate from the xxHash checksums. This is table-stakes to
   compete with WinRAR/7-Zip for anyone archiving sensitive data.

### 4.2 Functional gaps that a "real archiver" is expected to have

6. **Selective / random-access extraction — [your differentiator, currently unused].**
   The whole point of the seekable format + seek table is extracting one file or subtree
   without decompressing everything — and there's no command for it. Add
   `blitzpack extract <archive> <path...> -o <dir>`, using the manifest + seek table to
   read only the needed chunks. This is a genuine advantage over solid `.7z`/`.rar`
   (which must stream through preceding data) and it's already 90% supported by the format.
7. **Append / update / delete entries.** Real archivers add and remove files without a
   full rewrite. Even a "rewrite but present it as update" is fine to start.
8. **stdin/stdout streaming (`tar`-like).** `blitzpack compress - -o -` for pipelines and
   Docker layers. Enables `blitzpack c dir | ssh host blitzpack x`.
9. **Deterministic / reproducible archives.** A `--reproducible` flag: sort entries,
   zero or clamp mtimes, drop volatile metadata — so the same input yields byte-identical
   output. Valued for build systems and content-addressed storage.
10. **File-level dedup.** Hash files during scan; store identical files once (many trees
    have duplicate assets/deps). Cheap given you already checksum, and a ratio win no
    per-file compressor gets.
11. **Richer metadata preservation.** Currently: mode, mtime, symlink target, win_attrs.
    Consider: Windows Alternate Data Streams, POSIX xattrs, ownership (uid/gid) with
    `--numeric-owner`, hardlink detection. Scope explicitly and document what's preserved.

### 4.3 API, CLI & operational polish

12. **Cancellation in the library API.** Long compress/decompress can't be stopped
    cleanly (the GUI runs them on a thread). Add a `cancel_event: threading.Event`
    parameter both loops check — the pipeline already has `stop_event` internally, so
    expose it. Important for the GUI's responsiveness and for embedding.
13. **Exit codes & error taxonomy.** Distinct, documented exit codes (0 ok, 2 corrupt,
    3 unreadable source, 4 partial). You added `verify` returning 2 — extend the scheme
    across commands.
14. **`--skip-unreadable` mode.** The fail-fast fix from the last review is correct as a
    default; power users want a lenient mode that drops unreadable files, records them in
    the manifest as skipped, and reports them on the result. (Non-trivial — needs the
    manifest to omit them before finalize; do it with a test.)
15. **Logging + `--verbose`/`--quiet`/`--json`.** Structured/JSON output makes BlitzPack
    scriptable and CI-friendly; `--quiet` for pipelines.
16. **`--version`, richer `--help`, and a man page.** Small, expected.
17. **Memory ceiling (`--max-memory`).** Bound in-flight queue depth so extraction of a
    huge archive on a 16 GB laptop can't OOM. Ties into 2.8.

### 4.4 Testing, CI & distribution — what makes it maintainable

18. **Fuzz the archive reader.** `atheris`/`hypothesis` against `BlitzArchiveReader` with
    corrupted/truncated/hostile inputs. This backs up items 1–2 and catches format bugs
    before users do.
19. **Property-based roundtrip tests.** `hypothesis` generating random trees (names,
    sizes, nesting, unicode, empty files, large files) and asserting byte-identical
    roundtrip — far stronger than the current fixed cases.
20. **Cross-platform CI matrix** (you added Linux+Windows — add macOS if you claim it),
    plus a **symlink/permission** job on POSIX.
21. **Performance regression guard in CI.** A tiny fixed corpus timed on each PR, failing
    if throughput regresses beyond a threshold. Cheap insurance once the perf work lands.
22. **Publish to PyPI with wheels**, signed release artifacts, and published SHA-256
    checksums for the `.exe`s (right now users download opaque binaries from Releases —
    checksums + provenance make them trustworthy). Consider Sigstore/GitHub attestations.
23. **A format specification document** (`FORMAT.md`) — the on-disk `.blitz` layout,
    versioning policy, and compatibility guarantees. A format nobody can independently
    implement or validate isn't production-grade. You already have most of this in the
    README; promote it to a real spec with the exact struct definitions and invariants
    (contiguity, digest definition, path rules).

### 4.5 Suggested sequencing

- **Ship now (safety):** 1 (path traversal), 2 (reader DoS bounds) — these are bugs, not
  features, and they gate any "use it on real/untrusted data" claim.
- **Next (differentiation + trust):** 6 (selective extract — cheap, high-visibility),
  4/5 (integrity framing + optional encryption), 23 (format spec).
- **Then (robustness):** 12 (cancellation), 13–14 (error model), 18–19 (fuzz + property
  tests), 22 (PyPI/signing).
- **Opportunistic:** 7–11 (append/stream/dedup/metadata) as user demand appears.

---

## One-paragraph executive summary

The two bottlenecks that most limit BlitzPack on a 4-core laptop are both on its
signature many-files path: the **serial scan** that front-loads all metadata latency
before compression starts (cold cache), and the **single decompression writer** that
serializes extraction of thousands of small files. Fix those (Parts 2.2 and 2.7),
right-size the thread pools for low core counts (2.1), and add trained dictionaries
(2.3b) to close the ratio gap, and the "beats WinRAR/7-Zip on low-end" claim becomes
defensible — *once the Part 3 benchmark, with pinned cores, disclosed cache/Defender
state, and ≥5 runs reporting medians, actually shows it.* Before shipping to real users,
close the path-traversal and untrusted-reader security holes (4.1) — those are the only
items here that are non-negotiable regardless of performance.
