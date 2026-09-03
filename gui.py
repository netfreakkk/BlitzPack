"""BlitzPack WinRAR-Style Modern GUI Application.

Features:
- Windows 11 Fluent 2 / macOS inspired modern aesthetic (via sv-ttk)
- 70/30 Split Layout:
  * Left (70%): File Explorer & Archive Browser with color-coded file type badges and instant live search
  * Right (30%): Hero Dropzone, Smart Hardware Profile Selector, and Real-Time Telemetry HUD
- Top Action Ribbon: App brand badge, search filter, and instant Dark/Light theme switcher
- Dual-mode File Manager & In-Archive Virtual Browser (browse inside .blitz files seamlessly)
- Interactive Sortable Table (Name, Size, Packed Size, Type, Modified Date)
- Live Multi-threaded Telemetry HUD with animated Speedometer (MB/s), Ratio, and Progress
- Archive Integrity Check (Test Mode via XXH64)
"""

from __future__ import annotations

import datetime
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

import sv_ttk
import psutil

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from blitzpack.analyzer import FileEntry
from blitzpack.archive_format import BlitzArchiveReader
from blitzpack.compressor import CompressionResult, compress
from blitzpack.decompressor import DecompressionResult, decompress
from blitzpack.utils import ProgressUpdate, format_bytes, format_throughput, sanitize_windows_path


# Compression Profiles (Human-friendly)
LEVEL_PROFILES = {
    "Fast": 1,
    "Balanced": 3,
    "High": 9,
    "Ultra": 19,
}

# Smart Hardware Profiles
def get_hardware_profiles() -> Dict[str, int]:
    cpu_count = os.cpu_count() or 4
    # On hybrid Intel/AMD CPUs, 4 P-cores is the turbo sweet spot for sustained speed
    p_core_count = 4 if cpu_count >= 8 else max(1, cpu_count // 2)
    return {
        f"🚀 P-Core Turbo ({p_core_count} Cores - Max Speed)": p_core_count,
        f"⚡ All Threads ({cpu_count} Cores)": cpu_count,
        "🔋 Quiet Mode (2 Cores)": 2,
    }


def get_file_icon_and_badge(name: str, is_dir: bool) -> Tuple[str, str]:
    """Return a modern glyph icon and human-friendly badge for the file type."""
    if is_dir:
        return ("📁 ", "Folder")
    ext = Path(name).suffix.lower()
    if ext == ".blitz":
        return ("⚡ ", "Blitz Archive")
    if ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso"):
        return ("📦 ", "Archive")
    if ext in (".js", ".ts", ".jsx", ".tsx", ".py", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".java", ".cs", ".php", ".rb", ".swift", ".kt"):
        return ("🟡 ", "Source Code")
    if ext in (".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".env", ".config"):
        return ("⚙️ ", "Configuration")
    if ext in (".exe", ".dll", ".so", ".dylib", ".bin", ".sys", ".drv", ".msi"):
        return ("🟣 ", "Executable / Binary")
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".ico", ".bmp", ".tiff"):
        return ("🖼️ ", "Image")
    if ext in (".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".flac", ".aac", ".ogg"):
        return ("🎬 ", "Media")
    if ext in (".md", ".txt", ".rtf", ".pdf", ".doc", ".docx", ".epub"):
        return ("📝 ", "Document")
    if ext in (".html", ".htm", ".css", ".scss", ".sass", ".less"):
        return ("🌐 ", "Web File")
    return ("📄 ", "File")


class ProgressDialog(tk.Toplevel):
    """Modern popup modal displaying real-time progress of compression or extraction."""

    def __init__(self, parent: tk.Tk, title: str, operation_name: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.geometry("520x220")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Center on parent
        x = parent.winfo_x() + (parent.winfo_width() // 2) - 260
        y = parent.winfo_y() + (parent.winfo_height() // 2) - 110
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.cancelled = False

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        self.lbl_operation = ttk.Label(
            frame, text=operation_name, font=("Segoe UI Variable Display", 12, "bold")
        )
        self.lbl_operation.pack(anchor="w", pady=(0, 10))

        self.progress_bar = ttk.Progressbar(frame, mode="determinate", length=480)
        self.progress_bar.pack(fill="x", pady=(0, 10))

        self.lbl_status = ttk.Label(
            frame, text="Initializing...", font=("Segoe UI Variable Text", 10)
        )
        self.lbl_status.pack(anchor="w", pady=(0, 4))

        self.lbl_speed = ttk.Label(
            frame, text="Throughput: 0 MB/s", font=("Cascadia Code", 9), foreground="#4CC2FF"
        )
        self.lbl_speed.pack(anchor="w", pady=(0, 5))

        self.lbl_resources = ttk.Label(
            frame, text="CPU: 0% | RAM: 0 MB", font=("Segoe UI", 8), foreground="gray"
        )
        self.lbl_resources.pack(anchor="w")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        self.process = psutil.Process(os.getpid())
        self._update_resources()

    def _update_resources(self) -> None:
        if self.cancelled:
            return
        
        try:
            cpu = self.process.cpu_percent() / (psutil.cpu_count() or 1)
            mem = self.process.memory_info().rss
            self.lbl_resources.configure(text=f"Process CPU: {cpu:.1f}% | App RAM: {format_bytes(mem)}")
        except Exception:
            pass
            
        self.after(1000, self._update_resources)

    def update_progress(self, current: int, total: int, speed_bps: float, message: str = "") -> None:
        if total > 0:
            pct = (current / total) * 100
            self.progress_bar["value"] = pct
            status_text = f"{pct:.1f}% ({format_bytes(current)} / {format_bytes(total)})"
            if message:
                status_text = f"{message} • {status_text}"
            self.lbl_status.configure(text=status_text)
            self.lbl_speed.configure(
                text=f"Throughput: {speed_bps / (1024 * 1024):.1f} MB/s"
            )

    def _on_close(self) -> None:
        pass


class AddToArchiveDialog(tk.Toplevel):
    """Modern dialog for configuring compression settings."""

    def __init__(self, parent: tk.Tk, target_paths: List[Path], default_out: Path, initial_profile: str = "Balanced", initial_workers: int = 4) -> None:
        super().__init__(parent)
        self.title("Add to Archive")
        self.geometry("540x400")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Center on parent
        x = parent.winfo_x() + (parent.winfo_width() // 2) - 270
        y = parent.winfo_y() + (parent.winfo_height() // 2) - 200
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.result: Optional[Tuple[Path, int, int]] = None
        self.target_paths = target_paths

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        # Archive Path
        ttk.Label(frame, text="Archive Path (.blitz):", font=("Segoe UI Variable Text", 10, "bold")).pack(anchor="w")
        dest_box = ttk.Frame(frame)
        dest_box.pack(fill="x", pady=(4, 15))

        self.ent_dest_path = ttk.Entry(dest_box, font=("Segoe UI", 10))
        self.ent_dest_path.insert(0, str(default_out))
        self.ent_dest_path.pack(side="left", fill="x", expand=True, padx=(0, 8))

        btn_browse = ttk.Button(dest_box, text="Browse...", command=self._browse_save_path)
        btn_browse.pack(side="right")

        # Compression Profile
        ttk.Label(frame, text="Compression Profile:", font=("Segoe UI Variable Text", 10, "bold")).pack(anchor="w")
        self.cmb_profile = ttk.Combobox(
            frame,
            values=list(LEVEL_PROFILES.keys()),
            state="readonly",
            font=("Segoe UI", 10),
        )
        if initial_profile in LEVEL_PROFILES:
            self.cmb_profile.set(initial_profile)
        else:
            self.cmb_profile.set("Balanced")
        self.cmb_profile.pack(fill="x", pady=(4, 15))

        # CPU Worker Threads
        ttk.Label(frame, text="CPU Worker Threads:", font=("Segoe UI Variable Text", 10, "bold")).pack(anchor="w")
        threads_frame = ttk.Frame(frame)
        threads_frame.pack(fill="x", pady=(4, 20))

        cpu_count = os.cpu_count() or 4
        self.spn_workers = ttk.Spinbox(threads_frame, from_=1, to=64, width=6)
        self.spn_workers.set(initial_workers or cpu_count)
        self.spn_workers.pack(side="left", padx=(0, 10))

        ttk.Label(
            threads_frame,
            text=f"(Detected {cpu_count} logical execution threads)",
            foreground="gray",
        ).pack(side="left")

        # Action Buttons
        btn_box = ttk.Frame(frame)
        btn_box.pack(fill="x", side="bottom")

        btn_ok = ttk.Button(btn_box, text="  OK  ", style="Accent.TButton", command=self._on_ok)
        btn_ok.pack(side="right", padx=(8, 0))

        btn_cancel = ttk.Button(btn_box, text="Cancel", command=self.destroy)
        btn_cancel.pack(side="right")

    def _browse_save_path(self) -> None:
        chosen = filedialog.asksaveasfilename(
            title="Save Archive As",
            initialfile=Path(self.ent_dest_path.get()).name,
            initialdir=Path(self.ent_dest_path.get()).parent,
            filetypes=[("BlitzPack Archive", "*.blitz")],
            defaultextension=".blitz",
        )
        if chosen:
            self.ent_dest_path.delete(0, tk.END)
            self.ent_dest_path.insert(0, chosen)

    def _on_ok(self) -> None:
        target = Path(self.ent_dest_path.get()).resolve()
        if not target.suffix.lower() == ".blitz":
            target = target.with_suffix(".blitz")

        level = LEVEL_PROFILES.get(self.cmb_profile.get(), 3)
        try:
            workers = int(self.spn_workers.get())
        except ValueError:
            workers = os.cpu_count() or 4

        self.result = (target, level, workers)
        self.destroy()


class ExtractArchiveDialog(tk.Toplevel):
    """Modern dialog for configuring extraction settings."""

    def __init__(self, parent: tk.Tk, archive_path: Path, default_dest: Path, initial_workers: int = 4) -> None:
        super().__init__(parent)
        self.title("Extract Archive")
        self.geometry("540x300")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Center on parent
        x = parent.winfo_x() + (parent.winfo_width() // 2) - 270
        y = parent.winfo_y() + (parent.winfo_height() // 2) - 150
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.result: Optional[Tuple[Path, int, bool]] = None
        self.archive_path = archive_path

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Extract Destination Folder:", font=("Segoe UI Variable Text", 10, "bold")).pack(anchor="w")
        dest_box = ttk.Frame(frame)
        dest_box.pack(fill="x", pady=(4, 15))

        self.ent_dest_path = ttk.Entry(dest_box, font=("Segoe UI", 10))
        self.ent_dest_path.insert(0, str(default_dest))
        self.ent_dest_path.pack(side="left", fill="x", expand=True, padx=(0, 8))

        btn_browse = ttk.Button(dest_box, text="Browse...", command=self._browse_dest)
        btn_browse.pack(side="right")

        # Delete after extraction option
        self.var_delete = tk.BooleanVar(value=False)
        chk_delete = ttk.Checkbutton(
            frame,
            text="Delete archive after successful extraction",
            variable=self.var_delete
        )
        chk_delete.pack(anchor="w", pady=(0, 15))

        # CPU Worker Threads
        threads_frame = ttk.Frame(frame)
        threads_frame.pack(fill="x", pady=(0, 20))

        ttk.Label(threads_frame, text="Worker Threads:").pack(side="left", padx=(0, 8))
        cpu_count = os.cpu_count() or 4
        self.spn_workers = ttk.Spinbox(threads_frame, from_=1, to=64, width=6)
        self.spn_workers.set(initial_workers or cpu_count)
        self.spn_workers.pack(side="left", padx=(0, 8))

        # Action Buttons
        btn_box = ttk.Frame(frame)
        btn_box.pack(fill="x", side="bottom")

        btn_ok = ttk.Button(btn_box, text="  Extract  ", style="Accent.TButton", command=self._on_ok)
        btn_ok.pack(side="right", padx=(8, 0))

        btn_cancel = ttk.Button(btn_box, text="Cancel", command=self.destroy)
        btn_cancel.pack(side="right")

    def _browse_dest(self) -> None:
        chosen = filedialog.askdirectory(
            title="Choose Extraction Directory",
            initialdir=self.ent_dest_path.get(),
        )
        if chosen:
            self.ent_dest_path.delete(0, tk.END)
            self.ent_dest_path.insert(0, chosen)

    def _on_ok(self) -> None:
        dest = Path(self.ent_dest_path.get()).resolve()
        try:
            workers = int(self.spn_workers.get())
        except ValueError:
            workers = os.cpu_count() or 4

        self.result = (dest, workers, self.var_delete.get())
        self.destroy()


class BlitzPackMainWindow(tk.Tk):
    """Main Modern Windows 11 / macOS Fluent File & Archive Browser Window."""

    def __init__(self) -> None:
        super().__init__()

        self.title("⚡ BlitzPack")
        self.geometry("1180x700")
        self.minsize(920, 560)

        # Set default theme to modern dark
        sv_ttk.set_theme("dark")
        self.current_theme = "dark"

        # Mode State: "filesystem" or "archive"
        self.mode: str = "filesystem"
        self.current_dir: Path = Path.cwd().resolve()
        self.current_archive_path: Optional[Path] = None
        self.archive_reader: Optional[BlitzArchiveReader] = None
        self.archive_virtual_subpath: str = ""

        # Items in current view
        self.displayed_items: List[Dict[str, Any]] = []

        # Navigation History (Back / Forward)
        self.history: List[Path] = [self.current_dir]
        self.history_index: int = 0

        # Sort State
        self.sort_column: str = "name"
        self.sort_descending: bool = False

        # Live Search Filter Variable
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *args: self._render_tree_items())

        self._build_ui()
        self._navigate_to_directory(self.current_dir)

    def _build_ui(self) -> None:
        """Construct the entire Fluent UI: Header bar, Navigation Ribbon, 70/30 Split, and Telemetry Hub."""
        # Top Menu Bar
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        menu_file = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=menu_file)
        menu_file.add_command(label="Open Archive...", accelerator="Ctrl+O", command=self._action_open_archive_dialog)
        menu_file.add_command(label="Browse Folder...", accelerator="Ctrl+F", command=self._action_browse_folder_dialog)
        menu_file.add_separator()
        menu_file.add_command(label="Exit", accelerator="Alt+F4", command=self.quit)

        menu_commands = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Commands", menu=menu_commands)
        menu_commands.add_command(label="Add to Archive...", accelerator="Alt+A", command=self._action_add_to_archive)
        menu_commands.add_command(label="Extract To...", accelerator="Alt+E", command=self._action_extract_to)
        menu_commands.add_command(label="Test Archive Integrity", accelerator="Alt+T", command=self._action_test_archive)
        menu_commands.add_separator()
        menu_commands.add_command(label="Delete", accelerator="Del", command=self._action_delete)

        menu_options = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Options", menu=menu_options)
        menu_options.add_command(label="Toggle Dark/Light Theme", command=self._toggle_theme)

        menu_help = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=menu_help)
        menu_help.add_command(label="About BlitzPack", command=self._show_about)

        # Root Container
        root_container = ttk.Frame(self)
        root_container.pack(fill="both", expand=True)

        # ---------------------------------------------------------------------
        # 1. Top Header Bar (Brand, Search Bar, Theme Switcher)
        # ---------------------------------------------------------------------
        top_bar = ttk.Frame(root_container, padding=(12, 8, 12, 6))
        top_bar.pack(fill="x")

        # Brand Badge
        brand_frame = ttk.Frame(top_bar)
        brand_frame.pack(side="left")
        lbl_logo = ttk.Label(brand_frame, text="⚡ BlitzPack", font=("Segoe UI Variable Display", 13, "bold"), foreground="#4CC2FF")
        lbl_logo.pack(side="left")
        lbl_badge = ttk.Label(brand_frame, text=" v1.0 ", font=("Segoe UI", 8), background="#2A2A2E", foreground="#888888")
        lbl_badge.pack(side="left", padx=(8, 0))

        # Right-side utilities: Theme Toggle and Search Bar
        self.btn_theme = ttk.Button(top_bar, text=" 🌓 Theme ", width=10, command=self._toggle_theme)
        self.btn_theme.pack(side="right", padx=(8, 0))

        search_box = ttk.Frame(top_bar)
        search_box.pack(side="right", padx=(0, 8))
        lbl_search_icon = ttk.Label(search_box, text="🔍", font=("Segoe UI", 9))
        lbl_search_icon.pack(side="left", padx=(0, 4))
        self.ent_search = ttk.Entry(search_box, textvariable=self.var_search, width=24, font=("Segoe UI", 9))
        self.ent_search.pack(side="left")

        # ---------------------------------------------------------------------
        # 2. Navigation & Breadcrumb Ribbon
        # ---------------------------------------------------------------------
        nav_ribbon = ttk.Frame(root_container, padding=(12, 2, 12, 8))
        nav_ribbon.pack(fill="x")

        self.btn_back = ttk.Button(nav_ribbon, text=" ◀ ", width=3, command=self._action_back)
        self.btn_back.pack(side="left", padx=(0, 2))

        self.btn_forward = ttk.Button(nav_ribbon, text=" ▶ ", width=3, command=self._action_forward)
        self.btn_forward.pack(side="left", padx=(0, 2))

        self.btn_up = ttk.Button(nav_ribbon, text=" ⬆ ", width=3, command=self._action_up_directory)
        self.btn_up.pack(side="left", padx=(0, 6))

        self.btn_home = ttk.Button(nav_ribbon, text=" 🏠 ", width=3, command=lambda: self._navigate_to_directory(Path.home()))
        self.btn_home.pack(side="left", padx=(0, 8))

        self.lbl_path_mode = ttk.Label(nav_ribbon, text="📂", font=("Segoe UI", 10))
        self.lbl_path_mode.pack(side="left", padx=(0, 4))

        self.ent_address = ttk.Entry(nav_ribbon, font=("Segoe UI", 10))
        self.ent_address.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.ent_address.bind("<Return>", lambda e: self._on_address_entered())

        btn_go = ttk.Button(nav_ribbon, text=" Go ", width=4, command=self._on_address_entered)
        btn_go.pack(side="left", padx=(0, 4))

        self.btn_refresh = ttk.Button(nav_ribbon, text=" 🔄 ", width=3, command=self._action_refresh)
        self.btn_refresh.pack(side="left")

        # ---------------------------------------------------------------------
        # 3. 70/30 Split Layout (Left: File Manager, Right: Hero Dropzone & Telemetry)
        # ---------------------------------------------------------------------
        paned = ttk.PanedWindow(root_container, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        # LEFT PANE (70%): File Table
        left_pane = ttk.Frame(paned)
        paned.add(left_pane, weight=7)

        # Table Container
        table_container = ttk.Frame(left_pane)
        table_container.pack(fill="both", expand=True)

        columns = ("name", "size", "packed", "type", "modified")
        self.tree = ttk.Treeview(
            table_container,
            columns=columns,
            show="headings",
            selectmode="extended",
        )

        self.tree.heading("name", text="Name", anchor="w", command=lambda: self._sort_column("name"))
        self.tree.heading("size", text="Size", anchor="e", command=lambda: self._sort_column("size"))
        self.tree.heading("packed", text="Packed", anchor="e", command=lambda: self._sort_column("packed"))
        self.tree.heading("type", text="Type", anchor="w", command=lambda: self._sort_column("type"))
        self.tree.heading("modified", text="Modified", anchor="w", command=lambda: self._sort_column("modified"))

        self.tree.column("name", width=340, anchor="w")
        self.tree.column("size", width=95, anchor="e")
        self.tree.column("packed", width=95, anchor="e")
        self.tree.column("type", width=130, anchor="w")
        self.tree.column("modified", width=140, anchor="w")

        scroll_y = ttk.Scrollbar(table_container, orient="vertical", command=self.tree.yview)
        scroll_x = ttk.Scrollbar(table_container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")

        table_container.rowconfigure(0, weight=1)
        table_container.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Return>", lambda e: self._on_tree_double_click(None))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection_changed)

        # Context Menu
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Open / View", command=self._action_view)
        self.context_menu.add_command(label="Add to Archive...", command=self._action_add_to_archive)
        self.context_menu.add_command(label="Extract To...", command=self._action_extract_to)
        self.context_menu.add_command(label="Test Integrity", command=self._action_test_archive)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Delete", command=self._action_delete)
        self.tree.bind("<Button-3>", self._show_context_menu)

        # RIGHT PANE (30%): Hero Dropzone, Profiles & Live Telemetry Hub
        right_sidebar = ttk.Frame(paned, padding=(8, 0, 0, 0))
        paned.add(right_sidebar, weight=3)

        # Card 1: Hero Dropzone Card
        drop_card = ttk.LabelFrame(right_sidebar, text="⚡ Quick Dropzone", padding=12)
        drop_card.pack(fill="x", pady=(0, 10))

        lbl_drop_icon = ttk.Label(drop_card, text="⚡", font=("Segoe UI", 26))
        lbl_drop_icon.pack(anchor="center")
        lbl_drop_title = ttk.Label(drop_card, text="Drop or Select Target", font=("Segoe UI Variable Display", 11, "bold"))
        lbl_drop_title.pack(anchor="center", pady=(2, 2))
        lbl_drop_sub = ttk.Label(
            drop_card,
            text="Compress any folder or extract .blitz archives with multi-threaded performance.",
            font=("Segoe UI", 8),
            foreground="gray",
            wraplength=230,
            justify="center"
        )
        lbl_drop_sub.pack(anchor="center", pady=(0, 10))

        btn_box = ttk.Frame(drop_card)
        btn_box.pack(fill="x")
        self.btn_choose_folder = ttk.Button(btn_box, text="📁 Choose Folder", style="Accent.TButton", command=self._action_add_to_archive)
        self.btn_choose_folder.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.btn_choose_arc = ttk.Button(btn_box, text="📦 Open Archive", command=self._action_open_archive_dialog)
        self.btn_choose_arc.pack(side="right", fill="x", expand=True, padx=(4, 0))

        # Card 2: Configuration & Hardware Engine Profile
        conf_card = ttk.LabelFrame(right_sidebar, text="⚙️ Profile & Hardware", padding=10)
        conf_card.pack(fill="x", pady=(0, 10))

        ttk.Label(conf_card, text="Compression Profile:", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 2))
        self.cmb_sidebar_profile = ttk.Combobox(
            conf_card,
            values=list(LEVEL_PROFILES.keys()),
            state="readonly",
            font=("Segoe UI", 9),
        )
        self.cmb_sidebar_profile.set("Balanced")
        self.cmb_sidebar_profile.pack(fill="x", pady=(0, 8))

        ttk.Label(conf_card, text="Hardware Thread Profile:", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 2))
        self.hw_profiles = get_hardware_profiles()
        self.cmb_sidebar_hw = ttk.Combobox(
            conf_card,
            values=list(self.hw_profiles.keys()),
            state="readonly",
            font=("Segoe UI", 9),
        )
        # Default to P-Core Turbo (first entry)
        default_hw = list(self.hw_profiles.keys())[0]
        self.cmb_sidebar_hw.set(default_hw)
        self.cmb_sidebar_hw.pack(fill="x")

        # Card 3: Live Telemetry & Speedometer HUD
        self.telemetry_card = ttk.LabelFrame(right_sidebar, text="📊 Live Telemetry HUD", padding=12)
        self.telemetry_card.pack(fill="x", pady=(0, 10))

        self.lbl_telemetry_speed = ttk.Label(
            self.telemetry_card,
            text="0.0 MB/s",
            font=("Cascadia Code", 22, "bold"),
            foreground="#4CC2FF"
        )
        self.lbl_telemetry_speed.pack(anchor="center")

        self.telemetry_progress = ttk.Progressbar(self.telemetry_card, mode="determinate")
        self.telemetry_progress.pack(fill="x", pady=(8, 6))

        self.lbl_telemetry_ratio = ttk.Label(
            self.telemetry_card,
            text="Engine Ready • Ready to Pack",
            font=("Segoe UI", 9),
            anchor="center"
        )
        self.lbl_telemetry_ratio.pack(anchor="center")

        self.lbl_telemetry_sub = ttk.Label(
            self.telemetry_card,
            text=f"CPU: {os.cpu_count() or 4} Threads | Zero-Copy Pipeline",
            font=("Segoe UI", 8),
            foreground="gray",
            anchor="center"
        )
        self.lbl_telemetry_sub.pack(anchor="center", pady=(4, 0))

        # Card 4: Quick Action Buttons
        actions_card = ttk.Frame(right_sidebar)
        actions_card.pack(fill="x")

        self.btn_side_compress = ttk.Button(
            actions_card,
            text="⚡ Compress Selected",
            style="Accent.TButton",
            command=self._action_add_to_archive
        )
        self.btn_side_compress.pack(fill="x", pady=(0, 4))

        btn_row = ttk.Frame(actions_card)
        btn_row.pack(fill="x")
        self.btn_side_extract = ttk.Button(
            btn_row,
            text="📥 Extract To...",
            command=self._action_extract_to
        )
        self.btn_side_extract.pack(side="left", fill="x", expand=True, padx=(0, 3))

        self.btn_side_test = ttk.Button(
            btn_row,
            text="🛡️ Test Integrity",
            command=self._action_test_archive
        )
        self.btn_side_test.pack(side="right", fill="x", expand=True, padx=(3, 0))

        # ---------------------------------------------------------------------
        # 4. Status Bar
        # ---------------------------------------------------------------------
        statusbar = ttk.Frame(root_container, padding=(12, 4, 12, 6))
        statusbar.pack(fill="x", side="bottom")

        self.lbl_status_items = ttk.Label(statusbar, text="0 items", font=("Segoe UI", 9))
        self.lbl_status_items.pack(side="left")

        self.lbl_status_selected = ttk.Label(statusbar, text="", font=("Segoe UI", 9), foreground="gray")
        self.lbl_status_selected.pack(side="left", padx=20)

        self.lbl_status_mode = ttk.Label(
            statusbar, text="[Filesystem Mode]", font=("Segoe UI", 9, "bold"), foreground="#4CC2FF"
        )
        self.lbl_status_mode.pack(side="right")

        # Global Keyboard Shortcuts
        self.bind("<Control-o>", lambda e: self._action_open_archive_dialog())
        self.bind("<Control-f>", lambda e: self._focus_search())
        self.bind("<Alt-a>", lambda e: self._action_add_to_archive())
        self.bind("<Alt-e>", lambda e: self._action_extract_to())
        self.bind("<Alt-t>", lambda e: self._action_test_archive())
        self.bind("<Delete>", lambda e: self._action_delete())
        self.bind("<BackSpace>", lambda e: self._action_up_directory())
        self.bind("<F5>", lambda e: self._action_refresh())

    def _focus_search(self) -> None:
        self.ent_search.focus_set()
        self.ent_search.select_range(0, tk.END)

    # -------------------------------------------------------------------------
    # Navigation & Directory Loading
    # -------------------------------------------------------------------------

    def _navigate_to_directory(self, path: Path) -> None:
        """Switch to filesystem mode and load directory contents."""
        path = path.resolve()
        if not path.is_dir():
            return

        self.mode = "filesystem"
        self.current_dir = path
        self.current_archive_path = None
        self.archive_reader = None
        self.archive_virtual_subpath = ""

        # Update history
        if not self.history or self.history[self.history_index] != path:
            self.history = self.history[: self.history_index + 1]
            self.history.append(path)
            self.history_index = len(self.history) - 1

        self.lbl_path_mode.configure(text="📂")
        self.ent_address.delete(0, tk.END)
        self.ent_address.insert(0, str(path))
        self.title(f"⚡ BlitzPack - {path.name} - [{path}]")
        self.lbl_status_mode.configure(text="[Filesystem Mode]", foreground="#4CC2FF")

        self.btn_side_extract.configure(state="normal")
        self.btn_side_test.configure(state="disabled")

        self._refresh_filesystem_view()

    def _refresh_filesystem_view(self) -> None:
        """Scan current directory and populate the treeview."""
        self.tree.delete(*self.tree.get_children())
        self.displayed_items.clear()

        # Add parent navigation item if not at root
        if self.current_dir.parent and self.current_dir.parent != self.current_dir:
            self.displayed_items.append({
                "name": "..",
                "is_dir": True,
                "is_up": True,
                "size_bytes": 0,
                "packed_bytes": 0,
                "type": "Folder",
                "modified": "",
                "path": self.current_dir.parent,
            })

        try:
            with os.scandir(self.current_dir) as it:
                for entry in it:
                    try:
                        stat = entry.stat()
                        is_dir = entry.is_dir()
                        size = stat.st_size if not is_dir else 0
                        mtime_str = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                        ext = Path(entry.name).suffix.lower()
                        _, item_type = get_file_icon_and_badge(entry.name, is_dir)

                        self.displayed_items.append({
                            "name": entry.name,
                            "is_dir": is_dir,
                            "is_up": False,
                            "is_archive": ext == ".blitz",
                            "size_bytes": size,
                            "packed_bytes": 0,
                            "type": item_type,
                            "modified": mtime_str,
                            "path": Path(entry.path),
                        })
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError) as ex:
            messagebox.showerror("Access Error", f"Could not read directory:\n{str(ex)}")
            return

        self._render_tree_items()

    def _open_archive(self, archive_path: Path, subpath: str = "") -> None:
        """Switch to archive mode and browse inside a .blitz file."""
        archive_path = archive_path.resolve()
        if not archive_path.exists():
            messagebox.showerror("Error", f"Archive not found: {archive_path}")
            return

        try:
            f_in = open(archive_path, "rb")
            reader = BlitzArchiveReader(f_in)
        except Exception as ex:
            messagebox.showerror("Invalid Archive", f"Failed to open .blitz archive:\n{str(ex)}")
            return

        self.mode = "archive"
        self.current_archive_path = archive_path
        self.archive_reader = reader
        self.archive_virtual_subpath = subpath.strip("/")

        display_path = f"{archive_path.name}"
        if self.archive_virtual_subpath:
            display_path += f"/{self.archive_virtual_subpath}"

        self.lbl_path_mode.configure(text="📦")
        self.ent_address.delete(0, tk.END)
        self.ent_address.insert(0, str(archive_path) + (f"\\{self.archive_virtual_subpath}" if self.archive_virtual_subpath else ""))
        self.title(f"⚡ BlitzPack - [{display_path}]")
        self.lbl_status_mode.configure(text="[Archive Browser]", foreground="#FFAA00")

        self.btn_side_extract.configure(state="normal")
        self.btn_side_test.configure(state="normal")

        # Update HUD to show archive info
        self.lbl_telemetry_ratio.configure(text=f"Archive: {archive_path.name}")
        self.lbl_telemetry_sub.configure(
            text=f"Size: {format_bytes(archive_path.stat().st_size)} • {len(reader.manifest)} files"
        )

        self._refresh_archive_view()

    def _refresh_archive_view(self) -> None:
        """Parse archive manifest entries for the current virtual subpath."""
        if not self.archive_reader:
            return

        self.tree.delete(*self.tree.get_children())
        self.displayed_items.clear()

        # Add ".." to go up inside archive or exit archive
        self.displayed_items.append({
            "name": "..",
            "is_dir": True,
            "is_up": True,
            "size_bytes": 0,
            "packed_bytes": 0,
            "type": "Folder",
            "modified": "",
            "path": None,
        })

        cur_prefix = self.archive_virtual_subpath
        if cur_prefix and not cur_prefix.endswith("/"):
            cur_prefix += "/"

        seen_dirs = set()

        for entry in self.archive_reader.manifest:
            rel = entry.path.replace("\\", "/")
            if cur_prefix:
                if not rel.startswith(cur_prefix):
                    continue
                rel_sub = rel[len(cur_prefix):]
            else:
                rel_sub = rel

            parts = rel_sub.split("/")
            if len(parts) == 1:
                # Direct child file or directory
                is_dir = entry.file_type == 1
                size = entry.size
                mtime_str = datetime.datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M") if entry.mtime else ""
                _, item_type = get_file_icon_and_badge(parts[0], is_dir)

                self.displayed_items.append({
                    "name": parts[0],
                    "is_dir": is_dir,
                    "is_up": False,
                    "is_archive": False,
                    "size_bytes": size,
                    "packed_bytes": 0,
                    "type": item_type,
                    "modified": mtime_str,
                    "manifest_entry": entry,
                })
            elif len(parts) > 1:
                # Child directory in current level
                sub_dir_name = parts[0]
                if sub_dir_name not in seen_dirs:
                    seen_dirs.add(sub_dir_name)
                    self.displayed_items.append({
                        "name": sub_dir_name,
                        "is_dir": True,
                        "is_up": False,
                        "is_archive": False,
                        "size_bytes": 0,
                        "packed_bytes": 0,
                        "type": "Folder",
                        "modified": "",
                        "virtual_dir": True,
                    })

        self._render_tree_items()

    def _render_tree_items(self) -> None:
        """Render displayed items into Treeview table with sorting, badges, and search filtering."""
        self.tree.delete(*self.tree.get_children())

        # Live search filter
        query = self.var_search.get().strip().lower()
        if query:
            filtered_list = [
                i for i in self.displayed_items
                if i.get("is_up") or query in i["name"].lower() or query in i.get("type", "").lower()
            ]
        else:
            filtered_list = self.displayed_items

        def sort_key(item: Dict[str, Any]) -> Tuple[int, Any]:
            if item.get("is_up"):
                return (-2, "")
            is_folder = item.get("is_dir", False)
            folder_rank = -1 if is_folder else 1

            val = item.get(self.sort_column, "")
            if self.sort_column in ("size", "packed"):
                val = item.get(f"{self.sort_column}_bytes", 0)
            elif isinstance(val, str):
                val = val.lower()
            return (folder_rank, val)

        sorted_list = sorted(filtered_list, key=sort_key, reverse=self.sort_descending)

        total_bytes = 0
        file_count = 0
        dir_count = 0

        for item in sorted_list:
            if item.get("is_up"):
                icon = "⬆️ "
                size_str = ""
            elif item.get("is_dir"):
                icon = "📁 "
                size_str = ""
                dir_count += 1
            else:
                icon, _ = get_file_icon_and_badge(item["name"], False)
                size_str = format_bytes(item.get("size_bytes", 0))
                total_bytes += item.get("size_bytes", 0)
                file_count += 1

            packed_str = format_bytes(item.get("packed_bytes", 0)) if item.get("packed_bytes", 0) > 0 else "-"

            name_col = f"{icon}{item['name']}"
            item_id = self.tree.insert(
                "",
                tk.END,
                values=(
                    name_col,
                    size_str,
                    packed_str,
                    item.get("type", ""),
                    item.get("modified", ""),
                ),
            )
            item["tree_id"] = item_id

        # Update Status Bar
        filter_note = f" (filtered from {len(self.displayed_items)})" if query else ""
        self.lbl_status_items.configure(
            text=f"{file_count} files, {dir_count} folders ({format_bytes(total_bytes)}){filter_note}"
        )
        self.lbl_status_selected.configure(text="")

    def _sort_column(self, col: str) -> None:
        """Handle header click column sorting."""
        if self.sort_column == col:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = col
            self.sort_descending = False
        self._render_tree_items()

    # -------------------------------------------------------------------------
    # Tree Events & Actions
    # -------------------------------------------------------------------------

    def _on_tree_selection_changed(self, event: Any) -> None:
        """Update selection status label."""
        selection = self.tree.selection()
        selected_items = [i for i in self.displayed_items if i.get("tree_id") in selection and not i.get("is_up")]
        if not selected_items:
            self.lbl_status_selected.configure(text="")
            return

        total_bytes = sum(i.get("size_bytes", 0) for i in selected_items)
        if len(selected_items) == 1:
            self.lbl_status_selected.configure(
                text=f"Selected: {selected_items[0]['name']} ({format_bytes(total_bytes)})"
            )
        else:
            self.lbl_status_selected.configure(
                text=f"{len(selected_items)} items selected ({format_bytes(total_bytes)})"
            )

    def _on_tree_double_click(self, event: Any) -> None:
        """Handle double-clicking an item in the file tree."""
        selection = self.tree.selection()
        if not selection:
            return

        item_id = selection[0]
        matched = next((i for i in self.displayed_items if i.get("tree_id") == item_id), None)
        if not matched:
            return

        if self.mode == "filesystem":
            if matched.get("is_up"):
                self._action_up_directory()
            elif matched.get("is_dir"):
                self._navigate_to_directory(matched["path"])
            elif matched.get("is_archive"):
                self._open_archive(matched["path"])
            else:
                self._open_file_with_default_app(matched["path"])
        elif self.mode == "archive":
            if matched.get("is_up"):
                self._action_up_directory()
            elif matched.get("is_dir"):
                # Sub-directory navigation inside archive
                new_sub = f"{self.archive_virtual_subpath}/{matched['name']}".strip("/")
                if self.current_archive_path:
                    self._open_archive(self.current_archive_path, new_sub)
            else:
                messagebox.showinfo(
                    "Archive Member",
                    f"File: {matched['name']}\nSize: {format_bytes(matched.get('size_bytes', 0))}\n\nUse 'Extract' to extract this file."
                )

    def _show_context_menu(self, event: Any) -> None:
        """Display right-click context menu."""
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def _action_up_directory(self) -> None:
        """Navigate up one directory level or return from archive mode."""
        if self.mode == "filesystem":
            parent = self.current_dir.parent
            if parent and parent != self.current_dir:
                self._navigate_to_directory(parent)
        elif self.mode == "archive":
            if self.archive_virtual_subpath:
                # Go up inside archive virtual tree
                parts = self.archive_virtual_subpath.split("/")
                parent_sub = "/".join(parts[:-1])
                if self.current_archive_path:
                    self._open_archive(self.current_archive_path, parent_sub)
            else:
                # Exit archive mode back to filesystem
                if self.current_archive_path and self.current_archive_path.parent:
                    self._navigate_to_directory(self.current_archive_path.parent)
                else:
                    self._navigate_to_directory(Path.cwd())

    def _action_back(self) -> None:
        """Navigate back in history."""
        if self.history_index > 0:
            self.history_index -= 1
            target = self.history[self.history_index]
            self._navigate_to_directory(target)

    def _action_forward(self) -> None:
        """Navigate forward in history."""
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            target = self.history[self.history_index]
            self._navigate_to_directory(target)

    def _action_refresh(self) -> None:
        """Refresh current view."""
        if self.mode == "filesystem":
            self._refresh_filesystem_view()
        elif self.mode == "archive":
            self._refresh_archive_view()

    def _on_address_entered(self) -> None:
        """Navigate to the path typed into the address bar."""
        typed_path = Path(self.ent_address.get().strip()).resolve()
        if typed_path.is_dir():
            self._navigate_to_directory(typed_path)
        elif typed_path.is_file() and typed_path.suffix.lower() == ".blitz":
            self._open_archive(typed_path)
        else:
            messagebox.showerror("Error", f"Path does not exist: {typed_path}")

    # -------------------------------------------------------------------------
    # Core Archive Actions (Compress, Extract, Test, Delete)
    # -------------------------------------------------------------------------

    def _get_sidebar_settings(self) -> Tuple[int, int]:
        """Fetch current level and worker thread count from sidebar."""
        profile_name = self.cmb_sidebar_profile.get()
        level = LEVEL_PROFILES.get(profile_name, 3)

        hw_name = self.cmb_sidebar_hw.get()
        workers = self.hw_profiles.get(hw_name, 4)
        return level, workers

    def _action_add_to_archive(self) -> None:
        """Open Add Dialog and execute parallel compression."""
        if self.mode == "archive":
            messagebox.showinfo("Add to Archive", "Please navigate to a filesystem directory to compress files.")
            return

        selection = self.tree.selection()
        selected_items = [i for i in self.displayed_items if i.get("tree_id") in selection and not i.get("is_up")]

        if selected_items:
            first_path = selected_items[0]["path"]
            default_archive = first_path.with_suffix(".blitz")
            targets = [i["path"] for i in selected_items]
        else:
            default_archive = self.current_dir.with_suffix(".blitz")
            targets = [self.current_dir]

        target_to_compress = targets[0] if len(targets) == 1 else self.current_dir
        initial_level_name = self.cmb_sidebar_profile.get()
        _, initial_workers = self._get_sidebar_settings()

        dialog = AddToArchiveDialog(self, targets, default_archive, initial_level_name, initial_workers)
        self.wait_window(dialog)

        if not dialog.result:
            return

        out_archive_path, level, workers = dialog.result

        # Progress Modal & Telemetry HUD update
        progress_dlg = ProgressDialog(self, "Compressing", f"Creating {out_archive_path.name}...")
        self.lbl_telemetry_ratio.configure(text=f"Compressing {target_to_compress.name}...")
        self.lbl_telemetry_sub.configure(text=f"Engine Active • {workers} Workers")

        def on_progress(p: ProgressUpdate) -> None:
            if p.total_bytes > 0:
                speed_mb = p.current_speed_bps / (1024 * 1024)
                pct = (p.bytes_processed / p.total_bytes) * 100
                self.after(0, lambda: progress_dlg.update_progress(
                    p.bytes_processed, p.total_bytes, p.current_speed_bps, p.phase.capitalize()
                ))
                # Update right sidebar HUD live
                self.after(0, lambda: self._update_sidebar_telemetry(speed_mb, pct, p.message))

        def worker_thread() -> None:
            try:
                res: CompressionResult = compress(
                    input_path=target_to_compress,
                    output_path=out_archive_path,
                    level=level,
                    workers=workers,
                    progress_callback=on_progress,
                )
                self.after(0, progress_dlg.destroy)
                self.after(0, lambda: self._show_compress_complete(res))
                self.after(0, self._action_refresh)
            except Exception as ex:
                self.after(0, progress_dlg.destroy)
                self.after(0, lambda: messagebox.showerror("Compression Failed", f"Error during compression:\n{str(ex)}"))
                self.after(0, lambda: self._reset_sidebar_telemetry())

        threading.Thread(target=worker_thread, daemon=True).start()

    def _update_sidebar_telemetry(self, speed_mb: float, pct: float, status_msg: str) -> None:
        """Update the right sidebar telemetry card in real-time."""
        self.lbl_telemetry_speed.configure(text=f"{speed_mb:.1f} MB/s")
        self.telemetry_progress["value"] = pct
        self.lbl_telemetry_ratio.configure(text=f"{pct:.1f}% Complete")
        if status_msg:
            self.lbl_telemetry_sub.configure(text=status_msg[:35])

    def _reset_sidebar_telemetry(self) -> None:
        """Reset the right sidebar telemetry card to idle state."""
        self.lbl_telemetry_speed.configure(text="0.0 MB/s")
        self.telemetry_progress["value"] = 0
        self.lbl_telemetry_ratio.configure(text="Engine Ready • Ready to Pack")
        self.lbl_telemetry_sub.configure(text=f"CPU: {os.cpu_count() or 4} Threads | Zero-Copy Pipeline")

    def _show_compress_complete(self, res: CompressionResult) -> None:
        """Display successful compression metrics and update HUD."""
        self.lbl_telemetry_speed.configure(text=f"{res.throughput_mb_s:.1f} MB/s")
        self.telemetry_progress["value"] = 100
        self.lbl_telemetry_ratio.configure(text=f"{res.compression_ratio:.2f}x Ratio • Saved {int((1 - 1/res.compression_ratio)*100)}%")
        self.lbl_telemetry_sub.configure(text=f"Done in {res.duration_seconds:.2f}s ({res.chunks_created} chunks)")

        messagebox.showinfo(
            "Compression Complete",
            f"⚡ Archive Created: {res.archive_path.name}\n\n"
            f"• Original Size:    {format_bytes(res.original_size)} ({res.total_files} files)\n"
            f"• Compressed Size:  {format_bytes(res.compressed_size)}\n"
            f"• Ratio:            {res.compression_ratio:.2f}x\n"
            f"• Speed:            {res.throughput_mb_s:.1f} MB/s\n"
            f"• Duration:         {res.duration_seconds:.2f}s\n"
            f"• Parallel Chunks:  {res.chunks_created}",
        )

    def _action_extract_to(self) -> None:
        """Open Extract Dialog and launch parallel decompression."""
        archive_path: Optional[Path] = None

        if self.mode == "archive":
            archive_path = self.current_archive_path
        else:
            selection = self.tree.selection()
            selected_items = [i for i in self.displayed_items if i.get("tree_id") in selection and not i.get("is_up")]
            for item in selected_items:
                if item.get("is_archive") or item.get("path", Path()).suffix.lower() == ".blitz":
                    archive_path = item["path"]
                    break

        if not archive_path or not archive_path.exists():
            messagebox.showinfo("Extract", "Please select a .blitz archive or open one to extract.")
            return

        default_dest = archive_path.parent / archive_path.stem
        if default_dest.exists():
            counter = 1
            while True:
                candidate = archive_path.parent / f"{archive_path.stem} ({counter})"
                if not candidate.exists():
                    default_dest = candidate
                    break
                counter += 1

        _, initial_workers = self._get_sidebar_settings()
        dialog = ExtractArchiveDialog(self, archive_path, default_dest, initial_workers)
        self.wait_window(dialog)

        if not dialog.result:
            return

        dest_folder, workers, delete_after = dialog.result

        progress_dlg = ProgressDialog(self, "Extracting", f"Extracting {archive_path.name}...")
        self.lbl_telemetry_ratio.configure(text=f"Extracting {archive_path.name}...")
        self.lbl_telemetry_sub.configure(text=f"Engine Active • {workers} Workers")

        def on_progress(p: ProgressUpdate) -> None:
            if p.total_bytes > 0:
                speed_mb = p.current_speed_bps / (1024 * 1024)
                pct = (p.bytes_processed / p.total_bytes) * 100
                self.after(0, lambda: progress_dlg.update_progress(
                    p.bytes_processed, p.total_bytes, p.current_speed_bps, p.phase.capitalize()
                ))
                self.after(0, lambda: self._update_sidebar_telemetry(speed_mb, pct, p.message))

        def worker_thread() -> None:
            try:
                res: DecompressionResult = decompress(
                    archive_path=archive_path,
                    output_dir=dest_folder,
                    workers=workers,
                    progress_callback=on_progress,
                )
                if delete_after and archive_path.exists():
                    try:
                        archive_path.unlink()
                    except OSError:
                        pass

                self.after(0, progress_dlg.destroy)
                self.after(0, lambda: self._show_extract_complete(res))
                self.after(0, self._action_refresh)
            except Exception as ex:
                self.after(0, progress_dlg.destroy)
                self.after(0, lambda: messagebox.showerror("Extraction Failed", f"Error during extraction:\n{str(ex)}"))
                self.after(0, lambda: self._reset_sidebar_telemetry())

        threading.Thread(target=worker_thread, daemon=True).start()

    def _show_extract_complete(self, res: DecompressionResult) -> None:
        """Display successful extraction metrics."""
        self.lbl_telemetry_speed.configure(text=f"{res.throughput_mb_s:.1f} MB/s")
        self.telemetry_progress["value"] = 100
        self.lbl_telemetry_ratio.configure(text="Extraction Complete!")
        self.lbl_telemetry_sub.configure(text=f"{res.total_files} files restored in {res.duration_seconds:.2f}s")

        messagebox.showinfo(
            "Extraction Complete",
            f"✅ Successfully Extracted Archive\n\n"
            f"• Destination:   {res.output_dir}\n"
            f"• Total Files:   {res.total_files}\n"
            f"• Extracted:     {format_bytes(res.extracted_bytes)}\n"
            f"• Throughput:    {res.throughput_mb_s:.1f} MB/s\n"
            f"• Duration:      {res.duration_seconds:.2f}s",
        )

    def _action_test_archive(self) -> None:
        """Verify archive chunk integrity using xxHash-64 digests."""
        archive_path: Optional[Path] = None

        if self.mode == "archive":
            archive_path = self.current_archive_path
        else:
            selection = self.tree.selection()
            selected_items = [i for i in self.displayed_items if i.get("tree_id") in selection and not i.get("is_up")]
            for item in selected_items:
                if item.get("is_archive") or item.get("path", Path()).suffix.lower() == ".blitz":
                    archive_path = item["path"]
                    break

        if not archive_path or not archive_path.exists():
            messagebox.showinfo("Test Archive", "Please select or open a .blitz archive to test integrity.")
            return

        progress_dlg = ProgressDialog(self, "Testing Integrity", f"Verifying {archive_path.name}...")

        def worker_thread() -> None:
            try:
                with open(sanitize_windows_path(archive_path), "rb") as f_in:
                    reader = BlitzArchiveReader(f_in)
                    errors = reader.verify_all_checksums(f_in)

                self.after(0, progress_dlg.destroy)
                if errors:
                    self.after(0, lambda: messagebox.showerror(
                        "Integrity Check Failed",
                        f"Found {len(errors)} corrupted chunks in {archive_path.name}!\n\n" + "\n".join(errors[:5])
                    ))
                else:
                    self.after(0, lambda: messagebox.showinfo(
                        "Integrity Check Passed",
                        f"🛡️ Verified 100% of chunks in {archive_path.name}!\n\n"
                        f"• Chunks Verified: {len(reader.seek_entries)}\n"
                        f"• Total Files:     {len(reader.manifest)}\n"
                        f"• Archive Digest:  {reader.footer.archive_digest:#x}\n"
                        f"• Result:          PASS (Zero corruption)"
                    ))
            except Exception as ex:
                self.after(0, progress_dlg.destroy)
                self.after(0, lambda: messagebox.showerror("Test Error", f"Failed to test archive:\n{str(ex)}"))

        threading.Thread(target=worker_thread, daemon=True).start()

    def _action_view(self) -> None:
        """Open or view selected file."""
        selection = self.tree.selection()
        if not selection:
            return
        item_id = selection[0]
        matched = next((i for i in self.displayed_items if i.get("tree_id") == item_id), None)
        if not matched:
            return

        if self.mode == "filesystem":
            if matched.get("is_dir"):
                self._navigate_to_directory(matched["path"])
            elif matched.get("is_archive"):
                self._open_archive(matched["path"])
            else:
                self._open_file_with_default_app(matched["path"])
        elif self.mode == "archive":
            if matched.get("is_dir"):
                new_sub = f"{self.archive_virtual_subpath}/{matched['name']}".strip("/")
                if self.current_archive_path:
                    self._open_archive(self.current_archive_path, new_sub)

    def _action_delete(self) -> None:
        """Delete selected files from filesystem."""
        if self.mode == "archive":
            messagebox.showinfo("Delete", "Deleting files directly inside compressed archives is not supported.")
            return

        selection = self.tree.selection()
        selected_items = [i for i in self.displayed_items if i.get("tree_id") in selection and not i.get("is_up")]
        if not selected_items:
            return

        names = [i["name"] for i in selected_items]
        msg = f"Are you sure you want to permanently delete {len(names)} items?\n\n" + "\n".join(names[:5])
        if len(names) > 5:
            msg += f"\n...and {len(names) - 5} more"

        if not messagebox.askyesno("Confirm Delete", msg):
            return

        for item in selected_items:
            path: Path = item["path"]
            try:
                if path.is_dir():
                    import shutil
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except Exception as ex:
                messagebox.showerror("Delete Error", f"Failed to delete {path.name}:\n{str(ex)}")

        self._action_refresh()

    def _open_file_with_default_app(self, path: Path) -> None:
        """Open file using Windows default associated application."""
        try:
            os.startfile(sanitize_windows_path(path))
        except Exception:
            pass

    def _action_open_archive_dialog(self) -> None:
        """Browse and open a .blitz archive."""
        chosen = filedialog.askopenfilename(
            title="Open BlitzPack Archive",
            filetypes=[("BlitzPack Archives", "*.blitz"), ("All Files", "*.*")]
        )
        if chosen:
            self._open_archive(Path(chosen))

    def _action_browse_folder_dialog(self) -> None:
        """Browse to a folder."""
        chosen = filedialog.askdirectory(
            title="Select Directory to Browse",
            initialdir=self.current_dir
        )
        if chosen:
            self._navigate_to_directory(Path(chosen))

    def _toggle_theme(self) -> None:
        """Toggle between Dark and Light Fluent UI themes."""
        sv_ttk.toggle_theme()
        self.current_theme = "light" if self.current_theme == "dark" else "dark"

    def _show_about(self) -> None:
        """Display About information."""
        messagebox.showinfo(
            "About BlitzPack",
            "⚡ BlitzPack Archiver v1.0.0\n\n"
            "An intelligent, ultra-fast parallel compression engine powered by Zstandard & xxHash-64.\n\n"
            "• Up to 4.7x faster than WinRAR on multi-file repositories\n"
            "• Zero-copy multi-queue I/O pipeline\n"
            "• Independent seekable random-access chunks\n\n"
            "Open Source (MIT License)\n"
            "https://github.com/netfreakkk/BlitzPack"
        )


def main() -> None:
    app = BlitzPackMainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
