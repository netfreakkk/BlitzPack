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
from blitzpack.compressor import SourceReadError, LEVEL_PROFILES
from blitzpack.scheduler import WorkScheduler
from blitzpack.utils import ProgressUpdate, format_bytes, format_throughput, BlitzCancelled

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
        sys.exit(4)

    out_path = Path(args.output).resolve() if args.output else in_path.with_suffix(".blitz")
    workers = args.workers or os.cpu_count() or 4

    raw_level = str(args.level).lower().strip()
    if raw_level in LEVEL_PROFILES:
        level = LEVEL_PROFILES[raw_level]
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
        f"[bold cyan]Threads:[/] {workers} workers  |  [bold cyan]Profile:[/] {profile_name}"
        + ("  |  [bold cyan]Reproducible:[/] Yes" if getattr(args, "reproducible", False) else ""),
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
            readers=getattr(args, "readers", 0),
            deterministic=getattr(args, "reproducible", False),
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
        sys.exit(4)

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
                cur = f" [{Path(update.current_file).name}]" if getattr(update, "current_file", None) else ""
                progress.update(
                    task_id,
                    completed=update.bytes_processed,
                    total=update.total_bytes,
                    description=f"Extracting{cur} ({format_throughput(update.current_speed_bps, 1.0)})..."
                )

        res = decompress(
            archive_path=arc_path,
            output_dir=out_dir,
            workers=workers,
            progress_callback=on_progress
        )

    console.print(Panel(
        f"[bold green]Extraction Complete![/]\n\n"
        f"Extracted Files:  {res.total_files}\n"
        f"Total Size:       {format_bytes(res.extracted_bytes)}\n"
        f"Throughput:       [bold]{res.throughput_mb_s:.1f} MB/s[/]\n"
        f"Duration:         {res.duration_seconds:.2f}s",
        title="Extraction Complete",
        border_style="green"
    ))


def handle_extract(args: argparse.Namespace) -> None:
    """Selective random-access extraction."""
    arc_path = Path(args.archive).resolve()
    if not arc_path.is_file():
        console.print(f"[bold red]Error:[/] Archive file does not exist: {arc_path}")
        sys.exit(4)

    out_dir = Path(args.output).resolve() if args.output else arc_path.with_suffix("")
    workers = args.workers or os.cpu_count() or 4

    console.print(Panel(
        f"[bold cyan]Archive:[/] {arc_path}\n"
        f"[bold cyan]Extracting:[/] {', '.join(args.paths)}\n"
        f"[bold cyan]Destination:[/] {out_dir}",
        title="BlitzPack Extract (Selective)",
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


def handle_list(args: argparse.Namespace) -> None:
    arc_path = Path(args.archive).resolve()
    if not arc_path.is_file():
        console.print(f"[bold red]Error:[/] Archive file does not exist: {arc_path}")
        sys.exit(4)

    from blitzpack.multi_decompress import get_archive_format, inspect_archive
    fmt = get_archive_format(arc_path)

    if fmt == "blitz":
        with open(arc_path, "rb") as f:
            reader = BlitzArchiveReader(f)

            table = Table(title=f"Archive: {arc_path.name} (BlitzPack format)", show_lines=True)
            table.add_column("Type", justify="center", style="cyan")
            table.add_column("Path", style="magenta")
            table.add_column("Size", justify="right")
            table.add_column("Chunks", justify="center")

            type_names = {0: "File", 1: "Dir", 2: "Link"}

            for entry in reader.manifest:
                type_str = type_names.get(entry.file_type, "?")
                chunk_span = (
                    "-" if entry.start_chunk == -1 else (
                        str(entry.start_chunk) if entry.start_chunk == entry.end_chunk
                        else f"{entry.start_chunk}..{entry.end_chunk}"
                    )
                )
                table.add_row(
                    type_str,
                    entry.path,
                    format_bytes(entry.size) if entry.file_type == 0 else "-",
                    chunk_span
                )

            console.print(table)
            console.print(
                f"\nTotal: [bold cyan]{len(reader.manifest)}[/] entries, "
                f"[bold cyan]{format_bytes(reader.footer.total_original_size)}[/] uncompressed in "
                f"[bold cyan]{len(reader.seek_entries)}[/] chunks.\n"
            )
    else:
        entries = inspect_archive(arc_path)
        table = Table(title=f"Archive: {arc_path.name} ({fmt.upper()} format)", show_lines=True)
        table.add_column("Type", justify="center", style="cyan")
        table.add_column("Path", style="magenta")
        table.add_column("Size", justify="right")
        table.add_column("Packed Size", justify="right")

        type_names = {0: "File", 1: "Dir", 2: "Link"}
        total_size = 0
        for entry in entries:
            type_str = type_names.get(entry.file_type, "?")
            total_size += entry.size
            packed = getattr(entry, "packed_size", 0)
            table.add_row(
                type_str,
                entry.path,
                format_bytes(entry.size) if entry.file_type == 0 else "-",
                format_bytes(packed) if packed > 0 else "-"
            )

        console.print(table)
        console.print(
            f"\nTotal: [bold cyan]{len(entries)}[/] entries, "
            f"[bold cyan]{format_bytes(total_size)}[/] uncompressed.\n"
        )


def handle_analyze(args: argparse.Namespace) -> None:
    in_path = Path(args.input).resolve()
    if not in_path.exists():
        console.print(f"[bold red]Error:[/] Target path does not exist: {in_path}")
        sys.exit(4)

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
        sys.exit(4)

    from blitzpack.multi_decompress import get_archive_format, test_archive
    fmt = get_archive_format(arc_path)

    if fmt == "blitz":
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
    else:
        console.print(Panel(
            f"[bold cyan]Archive:[/] {arc_path}\n[bold cyan]Format:[/] {fmt.upper()}",
            title="Archive Integrity Verification",
            border_style="cyan",
        ))
        start = time.perf_counter()
        ok, msg = test_archive(arc_path)
        if ok:
            console.print(Panel(
                f"[bold green]Archive is intact.[/]\n\n"
                f"Status:   {msg}\n"
                f"Elapsed:  {time.perf_counter() - start:.2f}s",
                title="Verification Complete",
                border_style="green",
            ))
        else:
            console.print(Panel(
                f"[bold red]Verification FAILED[/]\n\n{msg}",
                title="Corrupt Archive",
                border_style="red",
            ))
            sys.exit(2)


def handle_register_shell(args: argparse.Namespace) -> None:
    from blitzpack.shell_integration import register_shell_context_menu
    if register_shell_context_menu():
        console.print("[bold green]Success:[/] BlitzPack Windows Explorer context menus registered successfully!")
    else:
        console.print("[bold red]Failed:[/] Could not write context menu keys to Windows Registry.")
        sys.exit(1)


def handle_unregister_shell(args: argparse.Namespace) -> None:
    from blitzpack.shell_integration import unregister_shell_context_menu
    if unregister_shell_context_menu():
        console.print("[bold green]Success:[/] BlitzPack Windows Explorer context menus unregistered.")
    else:
        console.print("[bold red]Failed:[/] Could not remove context menu keys from Windows Registry.")
        sys.exit(1)


def main() -> None:
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
    p_comp.add_argument("-x", "--reproducible", action="store_true", help="Generate deterministic byte-for-byte identical archives")
    p_comp.set_defaults(func=handle_compress)

    # Decompress (Extract All)
    p_decomp = subparsers.add_parser("decompress", help="Extract all files from an archive (.blitz, .rar, .zip, .7z, .tar*, etc.)")
    p_decomp.add_argument("archive", help="Path to archive (.blitz, .rar, .zip, .7z, .tar*, etc.)")
    p_decomp.add_argument("-o", "--output", help="Target extraction directory")
    p_decomp.add_argument("-w", "--workers", type=int, default=0, help="CPU worker count (default: all cores)")
    p_decomp.set_defaults(func=handle_decompress)

    # Extract (Selective Random Access)
    p_extract = subparsers.add_parser(
        "extract", help="Extract only specific files or folders (random access)"
    )
    p_extract.add_argument("archive", help="Path to archive (.blitz, .rar, .zip, .7z, .tar*, etc.)")
    p_extract.add_argument("paths", nargs="+", help="Archive-relative path(s) to extract")
    p_extract.add_argument("-o", "--output", help="Target extraction directory")
    p_extract.add_argument("-w", "--workers", type=int, default=0, help="CPU worker count")
    p_extract.set_defaults(func=handle_extract)

    # List
    p_list = subparsers.add_parser("list", help="List archive entries (.blitz, .rar, .zip, .7z, .tar*, etc.)")
    p_list.add_argument("archive", help="Path to archive (.blitz, .rar, .zip, .7z, .tar*, etc.)")
    p_list.set_defaults(func=handle_list)

    # Verify
    p_verify = subparsers.add_parser(
        "verify", aliases=["test"], help="Check archive integrity without extracting"
    )
    p_verify.add_argument("archive", help="Path to archive (.blitz, .rar, .zip, .7z, .tar*, etc.)")
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

    # Register Shell
    p_reg = subparsers.add_parser("register-shell", help="Register WinRAR-style context menus in Windows Explorer")
    p_reg.set_defaults(func=handle_register_shell)

    # Unregister Shell
    p_unreg = subparsers.add_parser("unregister-shell", help="Unregister BlitzPack context menus from Windows Explorer")
    p_unreg.set_defaults(func=handle_unregister_shell)

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
    except Exception as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
