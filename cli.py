"""BlitzPack Command-Line Interface."""

import argparse
import os
from pathlib import Path
import sys
import time
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from blitzpack.archive_format import ArchiveFormatError
from blitzpack import (
    BlitzArchiveReader,
    FileAnalyzer,
    compress,
    decompress,
)
from blitzpack.scheduler import WorkScheduler
from blitzpack.utils import ProgressUpdate, format_bytes, format_throughput

# Ensure safe rendering across all Windows legacy / UTF-8 terminals
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console(highlight=False, legacy_windows=False)


def handle_compress(args: argparse.Namespace) -> None:
    in_path = Path(args.input).resolve()
    if not in_path.exists():
        console.print(f"[bold red]Error:[/] Target path does not exist: {in_path}")
        sys.exit(1)

    out_path = Path(args.output).resolve() if args.output else in_path.with_suffix(".blitz")
    workers = args.workers or os.cpu_count() or 4
    
    raw_level = str(args.level).lower().strip()
    profile_map = {"fast": 1, "balanced": 3, "high": 9, "ultra": 19}
    if raw_level in profile_map:
        level = profile_map[raw_level]
        profile_name = raw_level.capitalize()
    else:
        try:
            level = int(raw_level)
            profile_name = {1: "Fast", 3: "Balanced", 9: "High", 19: "Ultra"}.get(level, f"Level {level}")
        except ValueError:
            level = 3
            profile_name = "Balanced"

    console.print(Panel(
        f"[bold cyan]Input:[/] {in_path}\n"
        f"[bold cyan]Output:[/] {out_path}\n"
        f"[bold cyan]Threads:[/] {workers} workers  |  [bold cyan]Profile:[/] {profile_name}",
        title="BlitzPack Compress",
        border_style="cyan"
    ))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("• {task.completed}/{task.total} bytes"),
        TimeRemainingColumn(),
        console=console
    ) as progress:
        task_id = progress.add_task("Compressing...", total=100)

        def on_progress(update: ProgressUpdate):
            if update.total_bytes > 0:
                progress.update(
                    task_id,
                    completed=update.bytes_processed,
                    total=update.total_bytes,
                    description=f"Compressing ({format_throughput(update.current_speed_bps, 1.0)})..."
                )

        res = compress(
            input_path=in_path,
            output_path=out_path,
            level=level,
            workers=workers,
            progress_callback=on_progress
        )

    console.print(Panel(
        f"[bold green]Archive successfully created![/]\n\n"
        f"Original Size:    {format_bytes(res.original_size)}\n"
        f"Compressed Size:  {format_bytes(res.compressed_size)}\n"
        f"Ratio:            [bold]{res.compression_ratio:.2f}x[/]\n"
        f"Throughput:       [bold]{res.throughput_mb_s:.1f} MB/s[/]\n"
        f"Duration:         {res.duration_seconds:.2f}s ({res.chunks_created} independent chunks)",
        title="Compression Complete",
        border_style="green"
    ))


def handle_decompress(args: argparse.Namespace) -> None:
    arc_path = Path(args.archive).resolve()
    if not arc_path.is_file():
        console.print(f"[bold red]Error:[/] Archive file does not exist: {arc_path}")
        sys.exit(1)

    out_dir = Path(args.output).resolve() if args.output else arc_path.with_suffix("")
    workers = args.workers or os.cpu_count() or 4

    console.print(Panel(
        f"[bold cyan]Archive:[/] {arc_path}\n"
        f"[bold cyan]Destination:[/] {out_dir}\n"
        f"[bold cyan]Threads:[/] {workers} workers",
        title="BlitzPack Decompress",
        border_style="cyan"
    ))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console
    ) as progress:
        task_id = progress.add_task("Extracting...", total=100)

        def on_progress(update: ProgressUpdate):
            if update.total_bytes > 0:
                progress.update(
                    task_id,
                    completed=update.bytes_processed,
                    total=update.total_bytes,
                    description=f"Extracting ({format_throughput(update.current_speed_bps, 1.0)})..."
                )

        res = decompress(
            archive_path=arc_path,
            output_dir=out_dir,
            workers=workers,
            progress_callback=on_progress
        )

    console.print(Panel(
        f"[bold green]Archive successfully extracted![/]\n\n"
        f"Files Extracted:  {res.total_files}\n"
        f"Total Size:       {format_bytes(res.extracted_bytes)}\n"
        f"Throughput:       [bold]{res.throughput_mb_s:.1f} MB/s[/]\n"
        f"Duration:         {res.duration_seconds:.2f}s",
        title="Extraction Complete",
        border_style="green"
    ))


def handle_list(args: argparse.Namespace) -> None:
    arc_path = Path(args.archive).resolve()
    if not arc_path.is_file():
        console.print(f"[bold red]Error:[/] File does not exist: {arc_path}")
        sys.exit(1)

    with open(arc_path, "rb") as f:
        reader = BlitzArchiveReader(f)

    table = Table(title=f"Archive Contents: {arc_path.name}", show_lines=False)
    table.add_column("Type", style="cyan", width=8)
    table.add_column("Size", justify="right", width=12)
    table.add_column("Modified", width=20)
    table.add_column("Path", style="bold")

    type_names = {0: "File", 1: "Dir", 2: "Symlink"}

    for entry in reader.manifest:
        mtime_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.mtime))
        table.add_row(
            type_names.get(entry.file_type, "Unknown"),
            format_bytes(entry.size) if entry.file_type == 0 else "-",
            mtime_str,
            entry.path + (" -> " + entry.symlink_target if entry.symlink_target else "")
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(reader.manifest)} entries, {format_bytes(reader.footer.total_original_size)} uncompressed, {len(reader.seek_entries)} seekable chunks[/]\n")


def handle_analyze(args: argparse.Namespace) -> None:
    in_path = Path(args.input).resolve()
    if not in_path.exists():
        console.print(f"[bold red]Error:[/] Target path does not exist: {in_path}")
        sys.exit(1)

    analyzer = FileAnalyzer(in_path)
    manifest = analyzer.scan()
    classified = analyzer.classify(manifest)
    scheduler = WorkScheduler()
    jobs = scheduler.schedule(manifest)

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

def main() -> None:
    # If launched with no arguments (e.g. user double-clicked the .exe in Windows Explorer):
    if len(sys.argv) == 1:
        try:
            from gui import main as gui_main
            gui_main()
            return
        except Exception as e:
            console.print(f"[bold red]Failed to launch GUI:[/] {e}")
            input("\nPress Enter to exit...")
            return

    parser = argparse.ArgumentParser(
        prog="blitzpack",
        description="BlitzPack: Intelligent Parallel Compression Engine"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Compress
    p_comp = subparsers.add_parser("compress", help="Compress files or folders into a .blitz archive")
    p_comp.add_argument("input", help="Source folder or file path")
    p_comp.add_argument("-o", "--output", help="Output .blitz archive file path")
    p_comp.add_argument("-l", "--level", default="balanced", help="Compression profile: fast, balanced (default), high, ultra")
    p_comp.add_argument("-w", "--workers", type=int, default=0, help="CPU worker count (default: all cores)")
    p_comp.add_argument("-r", "--readers", type=int, default=0, help="Disk reader threads (default: auto; use 1-2 for spinning disks)")
    p_comp.set_defaults(func=handle_compress)

    # Decompress
    p_decomp = subparsers.add_parser("decompress", help="Extract a .blitz archive")
    p_decomp.add_argument("archive", help="Path to .blitz archive")
    p_decomp.add_argument("-o", "--output", help="Target extraction directory")
    p_decomp.add_argument("-w", "--workers", type=int, default=0, help="CPU worker count (default: all cores)")
    p_decomp.set_defaults(func=handle_decompress)

    # List
    p_list = subparsers.add_parser("list", help="List archive entries")
    p_list.add_argument("archive", help="Path to .blitz archive")
    p_list.set_defaults(func=handle_list)

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

    # Analyze
    p_analyze = subparsers.add_parser("analyze", help="Profile files and display scheduling breakdown (dry run)")
    p_analyze.add_argument("input", help="Source folder or file path")
    p_analyze.set_defaults(func=handle_analyze)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
