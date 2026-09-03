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
    level = args.level

    console.print(Panel(
        f"[bold cyan]Input:[/] {in_path}\n"
        f"[bold cyan]Output:[/] {out_path}\n"
        f"[bold cyan]Threads:[/] {workers} workers  |  [bold cyan]Level:[/] {level}",
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
    analyzer = FileAnalyzer(in_path)
    manifest = analyzer.scan()
    classified = analyzer.classify(manifest)
    scheduler = WorkScheduler()
    jobs = scheduler.schedule(classified)

    table = Table(title="File Profiling & Scheduling Plan", show_lines=True)
    table.add_column("Category", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Total Size", justify="right")
    table.add_column("Scheduled Strategy", style="dim")

    table.add_row(
        "Large (>16 MB)",
        str(len(classified.large)),
        format_bytes(sum(f.size for f in classified.large)),
        "Split into 4 MB chunks"
    )
    table.add_row(
        "Medium (64 KB - 16 MB)",
        str(len(classified.medium)),
        format_bytes(sum(f.size for f in classified.medium)),
        "Individual 1:1 compression jobs"
    )
    table.add_row(
        "Small (<64 KB)",
        str(len(classified.small)),
        format_bytes(sum(f.size for f in classified.small)),
        "Bundled by file extension into ~4 MB solid batches"
    )

    console.print()
    console.print(table)
    console.print(f"\n[bold]Dispatch Plan:[/] Generated [bold green]{len(jobs)}[/] parallel jobs sorted by LPT for optimal thread utilization.\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="blitzpack",
        description="BlitzPack: Intelligent Parallel Compression Engine"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Compress
    p_comp = subparsers.add_parser("compress", help="Compress files or folders into a .blitz archive")
    p_comp.add_argument("input", help="Source folder or file path")
    p_comp.add_argument("-o", "--output", help="Output .blitz archive file path")
    p_comp.add_argument("-l", "--level", type=int, default=3, help="Zstd compression level 1-19 (default: 3)")
    p_comp.add_argument("-w", "--workers", type=int, default=0, help="CPU worker count (default: all cores)")
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

    # Analyze
    p_analyze = subparsers.add_parser("analyze", help="Profile files and display scheduling breakdown (dry run)")
    p_analyze.add_argument("input", help="Source folder or file path")
    p_analyze.set_defaults(func=handle_analyze)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
