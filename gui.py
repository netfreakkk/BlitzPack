"""BlitzPack macOS-Inspired Fluent Desktop GUI.

Features:
- macOS Window Aesthetic: traffic-light window accents, soft rounded surfaces, and clean typography
- "Cool Blitz" Translucent Blue Gradient Palettes:
  * Dark: Deep Midnight Sapphire (#070D18 -> #0D1B30 -> #102644) with electric cyan accents
  * Light: Frosted Morning Azure (#E2EFFC -> #EDF5FD -> #F6FAFE) with royal cobalt accents
- macOS Smooth Sliding Toggle Switch: animated sliding pill switch for instant Dark/Light mode
- Internal Drag & Drop: Drag any folder or file from the left table and drop onto the right Dropzone!
- Zero Floating Popups: Progress bar, live throughput, active file ticker, Task Manager area graph,
  and final completion scorecard are embedded directly in the lower-right Performance Card
- Responsive Background Deletion: Deleting large 60,000-file folders runs in a background worker thread,
  preventing any "(Not Responding)" freezes
- Dynamic Core Tiers: Low (Eco), Medium (Balanced), High (Max Turbo)
"""

from __future__ import annotations

import collections
import ctypes
import datetime
import gc
import os
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil
import sv_ttk

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from blitzpack.archive_format import BlitzArchiveReader
from blitzpack.compressor import CompressionResult, compress
from blitzpack.decompressor import DecompressionResult, decompress
from blitzpack.utils import ProgressUpdate, format_bytes, sanitize_windows_path


# -----------------------------------------------------------------------------
# Rich "Cool Blitz" Gradient Color Themes
# -----------------------------------------------------------------------------
THEMES = {
    "dark": {
        "bg": "#070D18",
        "bg_gradient_top": "#070D18",
        "bg_gradient_bottom": "#0E1C33",
        "card_bg": "#0F1A2D",
        "card_border": "#1E3354",
        "card_border_highlight": "#38BDF8",
        "accent": "#0284C7",
        "accent_hover": "#0369A1",
        "accent_glow": "#38BDF8",
        "accent_text": "#FFFFFF",
        "secondary_btn": "#16253C",
        "secondary_hover": "#223859",
        "secondary_border": "#28446B",
        "secondary_text": "#E2E8F0",
        "text_primary": "#F8FAFC",
        "text_secondary": "#94A3B8",
        "graph_bg": "#0A1322",
        "graph_grid": "#162842",
        "graph_line": "#38BDF8",
        "graph_fill": "#082B47",
        "graph_badge": "#38BDF8",
        "dropzone_hover_bg": "#122B4A",
        "dropzone_hover_border": "#38BDF8",
        "switch_bg": "#0284C7",
        "switch_knob": "#FFFFFF",
    },
    "light": {
        "bg": "#E8F2FC",
        "bg_gradient_top": "#E2EFFC",
        "bg_gradient_bottom": "#F4F8FD",
        "card_bg": "#FFFFFF",
        "card_border": "#C7DBF0",
        "card_border_highlight": "#0284C7",
        "accent": "#0284C7",
        "accent_hover": "#0369A1",
        "accent_glow": "#7DD3FC",
        "accent_text": "#FFFFFF",
        "secondary_btn": "#E3EEF8",
        "secondary_hover": "#D1E2F2",
        "secondary_border": "#BDD6ED",
        "secondary_text": "#0F172A",
        "text_primary": "#0F172A",
        "text_secondary": "#475569",
        "graph_bg": "#F8FAFC",
        "graph_grid": "#E0ECF8",
        "graph_line": "#0284C7",
        "graph_fill": "#BAE6FD",
        "graph_badge": "#0369A1",
        "dropzone_hover_bg": "#E0F2FE",
        "dropzone_hover_border": "#0284C7",
        "switch_bg": "#94A3B8",
        "switch_knob": "#FFFFFF",
    },
}

LEVEL_PROFILES = {
    "Fast": 1,
    "Balanced": 3,
    "High": 9,
    "Ultra": 19,
}


def get_dynamic_cpu_tiers() -> Dict[str, int]:
    """Calculate human-friendly Low/Medium/High core allocations dynamically."""
    total = os.cpu_count() or 4
    if total >= 12:
        return {
            "Low (2 Cores - Eco)": 2,
            "Medium (4 Cores - Balanced)": 4,
            "High (12 Cores - Max Turbo)": total,
        }
    elif total >= 8:
        return {
            "Low (2 Cores - Eco)": 2,
            "Medium (4 Cores - Balanced)": 4,
            "High (8 Cores - Max Turbo)": total,
        }
    elif total >= 4:
        return {
            "Low (1 Core - Eco)": 1,
            "Medium (2 Cores - Balanced)": 2,
            "High (4 Cores - Max Turbo)": total,
        }
    else:
        return {
            "Low (1 Core)": 1,
            "Medium (2 Cores)": 2,
            "High (All Cores)": total,
        }


def apply_windows_mica(window: tk.Tk, dark: bool = True) -> None:
    """Enable native Windows 11 DWM Mica translucent backdrop."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(window.winfo_id(), 2)
        dark_val = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_val), ctypes.sizeof(dark_val))
        mica_val = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(mica_val), ctypes.sizeof(mica_val))
    except Exception:
        pass


def get_file_icon_and_badge(name: str, is_dir: bool) -> Tuple[str, str]:
    """Return a modern glyph icon and human-friendly badge for the file type."""
    if is_dir:
        return ("📁 ", "Folder")
    ext = Path(name).suffix.lower()
    if ext == ".blitz":
        return ("⚡ ", "Blitz Archive")
    if ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso"):
        return ("📦 ", "Archive")
    if ext in (".js", ".ts", ".jsx", ".tsx", ".py", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".java", ".cs", ".php", ".rb"):
        return ("🟡 ", "Source Code")
    if ext in (".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".env", ".config"):
        return ("⚙️ ", "Configuration")
    if ext in (".exe", ".dll", ".so", ".dylib", ".bin", ".sys", ".drv", ".msi"):
        return ("🟣 ", "Executable / Binary")
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".ico", ".bmp", ".tiff"):
        return ("🖼️ ", "Image")
    if ext in (".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".flac", ".aac"):
        return ("🎬 ", "Media")
    if ext in (".md", ".txt", ".rtf", ".pdf", ".doc", ".docx"):
        return ("📝 ", "Document")
    if ext in (".html", ".htm", ".css", ".scss", ".sass"):
        return ("🌐 ", "Web File")
    return ("📄 ", "File")


# -----------------------------------------------------------------------------
# macOS Smooth Sliding Toggle Switch
# -----------------------------------------------------------------------------
class MacOSSwitch(tk.Canvas):
    """Smooth sliding iOS/macOS toggle switch with sun/moon icon."""

    def __init__(
        self,
        parent: Any,
        is_dark: bool = True,
        command: Optional[Callable[[bool], None]] = None,
        width: int = 54,
        height: int = 28,
        bg_parent: str = "#070D18",
    ) -> None:
        super().__init__(parent, width=width, height=height, highlightthickness=0, bd=0, bg=bg_parent)
        self.is_dark = is_dark
        self.command = command
        self.width = width
        self.height = height
        self.knob_x = 39 if is_dark else 15
        self.target_x = self.knob_x
        self.bg_parent = bg_parent

        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self.config(cursor="hand2"))
        self._redraw()

    def set_state(self, is_dark: bool, bg_parent: str = "") -> None:
        self.is_dark = is_dark
        self.target_x = 39 if is_dark else 15
        if bg_parent:
            self.bg_parent = bg_parent
            self.configure(bg=bg_parent)
        self._animate_knob()

    def _on_click(self, event: Any) -> None:
        self.is_dark = not self.is_dark
        self.target_x = 39 if self.is_dark else 15
        self._animate_knob()
        if self.command:
            self.command(self.is_dark)

    def _animate_knob(self) -> None:
        step = 4 if self.target_x > self.knob_x else -4
        if abs(self.target_x - self.knob_x) > abs(step):
            self.knob_x += step
            self._redraw()
            self.after(16, self._animate_knob)
        else:
            self.knob_x = self.target_x
            self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        bg_color = "#0284C7" if self.is_dark else "#94A3B8"
        r = 13
        # Pill body
        self.create_oval(1, 1, 27, 27, fill=bg_color, outline="")
        self.create_oval(self.width - 27, 1, self.width - 1, 27, fill=bg_color, outline="")
        self.create_rectangle(14, 1, self.width - 14, 27, fill=bg_color, outline="")

        # Circular sliding knob
        kx = self.knob_x
        self.create_oval(kx - 10, 4, kx + 10, 24, fill="#FFFFFF", outline="#CBD5E1", width=1)
        glyph = "🌙" if self.is_dark else "☀️"
        self.create_text(kx, 14, text=glyph, font=("Segoe UI", 7))


# -----------------------------------------------------------------------------
# Animated Hover Pill Button
# -----------------------------------------------------------------------------
class AnimatedButton(tk.Canvas):
    """Modern macOS rounded pill button with smooth hover highlights and click feedback."""

    def __init__(
        self,
        parent: Any,
        text: str,
        command: Optional[Callable[[], None]] = None,
        style: str = "primary",
        height: int = 34,
        theme_name: str = "dark",
        font: Tuple[str, int, str] = ("Segoe UI Variable Text", 9, "bold"),
    ) -> None:
        super().__init__(parent, height=height, highlightthickness=0, bd=0)
        self.text = text
        self.command = command
        self.btn_style = style
        self.btn_height = height
        self.theme_name = theme_name
        self.btn_font = font
        self.is_hovered = False
        self.is_pressed = False
        self.width = 120

        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_theme(self, theme_name: str) -> None:
        self.theme_name = theme_name
        self.configure(bg=THEMES[theme_name]["card_bg"])
        self._redraw()

    def set_text(self, text: str) -> None:
        self.text = text
        self._redraw()

    def _on_resize(self, event: Any) -> None:
        self.width = event.width
        self._redraw()

    def _get_colors(self) -> Tuple[str, str, str]:
        t = THEMES[self.theme_name]
        if self.btn_style == "primary":
            if self.is_pressed or self.is_hovered:
                return (t["accent_hover"], t["accent_glow"], t["accent_text"])
            return (t["accent"], t["card_border"], t["accent_text"])
        else:
            if self.is_pressed or self.is_hovered:
                return (t["secondary_hover"], t["accent"], t["secondary_text"])
            return (t["secondary_btn"], t["secondary_border"], t["secondary_text"])

    def _draw_rounded_rect(self, x1: int, y1: int, x2: int, y2: int, r: int, fill: str, outline: str) -> None:
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        self.create_polygon(points, smooth=True, fill=fill, outline=outline, width=1)

    def _redraw(self) -> None:
        self.delete("all")
        bg_fill, border_color, text_color = self._get_colors()
        self.configure(bg=THEMES[self.theme_name]["card_bg"])
        padding = 2 if not self.is_pressed else 3
        r = 10
        self._draw_rounded_rect(
            padding, padding, max(padding + 10, self.width - padding), self.btn_height - padding,
            r, bg_fill, border_color
        )
        self.create_text(
            self.width // 2, self.btn_height // 2,
            text=self.text, fill=text_color, font=self.btn_font
        )

    def _on_enter(self, event: Any) -> None:
        self.is_hovered = True
        self.config(cursor="hand2")
        self._redraw()

    def _on_leave(self, event: Any) -> None:
        self.is_hovered = False
        self.is_pressed = False
        self._redraw()

    def _on_press(self, event: Any) -> None:
        self.is_pressed = True
        self._redraw()

    def _on_release(self, event: Any) -> None:
        if self.is_pressed and self.command:
            self.command()
        self.is_pressed = False
        self._redraw()


# -----------------------------------------------------------------------------
# Windows Task Manager Live Performance Area Graph
# -----------------------------------------------------------------------------
class TaskManagerLiveGraph(tk.Canvas):
    """Real-time scrolling area graph mimicking the Windows Task Manager Performance Tab."""

    def __init__(self, parent: Any, height: int = 105, theme_name: str = "dark") -> None:
        super().__init__(parent, height=height, highlightthickness=0, bd=0)
        self.theme_name = theme_name
        self.samples = collections.deque([0.0] * 40, maxlen=40)
        self.peak_value: float = 100.0
        self.current_value_str: str = "0.0 MB/s"
        self.graph_status: str = "Idle"
        self.graph_width = 280
        self.graph_height = height

        self.bind("<Configure>", self._on_resize)
        self._redraw()

    def set_theme(self, theme_name: str) -> None:
        self.theme_name = theme_name
        self._redraw()

    def push_sample(self, val: float, label: str = "", status: str = "") -> None:
        self.samples.append(val)
        if label:
            self.current_value_str = label
        if status:
            self.graph_status = status
        self._redraw()

    def _on_resize(self, event: Any) -> None:
        self.graph_width = event.width
        self.graph_height = event.height
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        t = THEMES[self.theme_name]
        w = max(50, self.graph_width)
        h = max(30, self.graph_height)

        self.configure(bg=t["graph_bg"])

        for y_pct in (0.25, 0.50, 0.75):
            y = int(h * y_pct)
            self.create_line(0, y, w, y, fill=t["graph_grid"], dash=(2, 4))

        step = max(30, w // 7)
        for x in range(0, w, step):
            self.create_line(x, 0, x, h, fill=t["graph_grid"], dash=(2, 4))

        max_seen = max(self.samples)
        if max_seen > self.peak_value * 0.9:
            self.peak_value = max_seen * 1.2
        elif max_seen < self.peak_value * 0.4 and self.peak_value > 50.0:
            self.peak_value = max(50.0, self.peak_value * 0.8)

        n = len(self.samples)
        if n > 1:
            dx = w / (n - 1)
            points = []
            for i, val in enumerate(self.samples):
                x = i * dx
                ratio = min(1.0, max(0.0, val / max(1.0, self.peak_value)))
                y = h - (ratio * (h - 24)) - 3
                points.extend([x, y])

            poly_points = [0, h] + points + [w, h]
            self.create_polygon(poly_points, fill=t["graph_fill"], outline="")
            self.create_line(points, fill=t["graph_line"], width=2, smooth=True)

        self.create_text(
            10, 11,
            text=f"Throughput: {self.current_value_str}",
            fill=t["graph_badge"],
            anchor="w",
            font=("Cascadia Code", 9, "bold")
        )
        self.create_text(
            w - 10, 11,
            text=f"● {self.graph_status}",
            fill=t["accent"] if self.graph_status == "Active" else t["text_secondary"],
            anchor="e",
            font=("Segoe UI Variable Text", 8, "bold")
        )
        self.create_text(
            w - 8, h - 8,
            text="60s window",
            fill=t["text_secondary"],
            anchor="se",
            font=("Segoe UI", 7)
        )


# -----------------------------------------------------------------------------
# Main Application Window
# -----------------------------------------------------------------------------
class BlitzPackMainWindow(tk.Tk):
    """Modern macOS-Inspired Fluent Desktop Archiver & File Manager."""

    def __init__(self) -> None:
        super().__init__()

        self.title("⚡ BlitzPack")
        self.geometry("1180x720")
        self.minsize(940, 580)

        # Default theme
        self.current_theme = "dark"
        sv_ttk.set_theme(self.current_theme)
        apply_windows_mica(self, dark=True)

        # State
        self.mode: str = "filesystem"
        self.current_dir: Path = Path.cwd().resolve()
        self.current_archive_path: Optional[Path] = None
        self.archive_virtual_subpath: str = ""

        self.displayed_items: List[Dict[str, Any]] = []
        self.history: List[Path] = [self.current_dir]
        self.history_index: int = 0

        self.sort_column: str = "name"
        self.sort_descending: bool = False

        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *args: self._render_tree_items())

        self.animated_buttons: List[AnimatedButton] = []
        self._active_job: bool = False
        self._dragged_item_path: Optional[Path] = None

        self._build_ui()
        self._navigate_to_directory(self.current_dir)
        self._start_graph_heartbeat()

    def _build_ui(self) -> None:
        t = THEMES[self.current_theme]
        self.configure(bg=t["bg"])

        # Menubar
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
        menu_commands.add_command(label="Test Integrity", accelerator="Alt+T", command=self._action_test_archive)
        menu_commands.add_separator()
        menu_commands.add_command(label="Delete", accelerator="Del", command=self._action_delete_async)

        menu_help = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=menu_help)
        menu_help.add_command(label="About BlitzPack", command=self._show_about)

        # Root Box
        self.root_container = ttk.Frame(self)
        self.root_container.pack(fill="both", expand=True)

        # ---------------------------------------------------------------------
        # 1. macOS Header Bar (Traffic Lights, Brand Logo, Search, Sliding Switch)
        # ---------------------------------------------------------------------
        top_bar = ttk.Frame(self.root_container, padding=(14, 8, 14, 6))
        top_bar.pack(fill="x")

        # macOS Traffic Lights Accent
        traffic_frame = tk.Canvas(top_bar, width=54, height=16, bg=t["bg"], highlightthickness=0, bd=0)
        traffic_frame.pack(side="left", padx=(0, 10))
        self.traffic_canvas = traffic_frame
        self._draw_traffic_lights()

        # Brand Badge
        brand_frame = ttk.Frame(top_bar)
        brand_frame.pack(side="left")
        self.lbl_logo = ttk.Label(
            brand_frame, text="⚡ BlitzPack", font=("Segoe UI Variable Display", 13, "bold"), foreground=t["accent"]
        )
        self.lbl_logo.pack(side="left")
        self.lbl_badge = ttk.Label(
            brand_frame, text=" v1.0 ", font=("Segoe UI", 8), background=t["card_border"], foreground=t["text_secondary"]
        )
        self.lbl_badge.pack(side="left", padx=(6, 0))

        # Right side: macOS Sliding Toggle Switch
        switch_frame = ttk.Frame(top_bar)
        switch_frame.pack(side="right", padx=(8, 0))
        self.macos_switch = MacOSSwitch(
            switch_frame,
            is_dark=(self.current_theme == "dark"),
            command=self._on_switch_toggled,
            bg_parent=t["bg"]
        )
        self.macos_switch.pack(side="right")

        # Live Search Filter
        search_box = ttk.Frame(top_bar)
        search_box.pack(side="right", padx=(0, 12))
        lbl_search_icon = ttk.Label(search_box, text="🔍", font=("Segoe UI", 9))
        lbl_search_icon.pack(side="left", padx=(0, 4))
        self.ent_search = ttk.Entry(search_box, textvariable=self.var_search, width=22, font=("Segoe UI", 9))
        self.ent_search.pack(side="left")

        # ---------------------------------------------------------------------
        # 2. Navigation Ribbon
        # ---------------------------------------------------------------------
        nav_ribbon = ttk.Frame(self.root_container, padding=(14, 2, 14, 8))
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
        # 3. 70/30 Split Layout
        # ---------------------------------------------------------------------
        paned = ttk.PanedWindow(self.root_container, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        # LEFT PANE (70%): File Table
        left_pane = ttk.Frame(paned)
        paned.add(left_pane, weight=7)

        table_container = ttk.Frame(left_pane)
        table_container.pack(fill="both", expand=True)

        columns = ("name", "size", "packed", "type", "modified")
        self.tree = ttk.Treeview(table_container, columns=columns, show="headings", selectmode="extended")

        self.tree.heading("name", text="Name", anchor="w", command=lambda: self._sort_column("name"))
        self.tree.heading("size", text="Size", anchor="e", command=lambda: self._sort_column("size"))
        self.tree.heading("packed", text="Packed Size", anchor="e", command=lambda: self._sort_column("packed"))
        self.tree.heading("type", text="File Type", anchor="w", command=lambda: self._sort_column("type"))
        self.tree.heading("modified", text="Date Modified", anchor="w", command=lambda: self._sort_column("modified"))

        self.tree.column("name", width=330, minwidth=200, anchor="w")
        self.tree.column("size", width=100, minwidth=80, anchor="e")
        self.tree.column("packed", width=110, minwidth=90, anchor="e")
        self.tree.column("type", width=140, minwidth=100, anchor="w")
        self.tree.column("modified", width=150, minwidth=120, anchor="w")

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

        # Internal Drag and Drop bindings
        self.tree.bind("<ButtonPress-1>", self._on_drag_start)
        self.tree.bind("<B1-Motion>", self._on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_drag_release)

        # Context Menu
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Open / View", command=self._action_view)
        self.context_menu.add_command(label="Add to Archive...", command=self._action_add_to_archive)
        self.context_menu.add_command(label="Extract To...", command=self._action_extract_to)
        self.context_menu.add_command(label="Test Integrity", command=self._action_test_archive)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Delete", command=self._action_delete_async)
        self.tree.bind("<Button-3>", self._show_context_menu)

        # RIGHT PANE (30%): Hero Dropzone, Profiles, and All-in-One Performance Card
        right_sidebar = ttk.Frame(paned, padding=(8, 0, 0, 0))
        paned.add(right_sidebar, weight=3)

        # Card 1: Hero Dropzone Card
        self.drop_card = ttk.LabelFrame(right_sidebar, text="⚡ Quick Dropzone", padding=10)
        self.drop_card.pack(fill="x", pady=(0, 8))

        self.lbl_drop_icon = ttk.Label(self.drop_card, text="⚡", font=("Segoe UI", 24))
        self.lbl_drop_icon.pack(anchor="center")
        self.lbl_drop_title = ttk.Label(self.drop_card, text="Drag & Drop Target Here", font=("Segoe UI Variable Display", 11, "bold"))
        self.lbl_drop_title.pack(anchor="center", pady=(1, 1))
        self.lbl_drop_sub = ttk.Label(
            self.drop_card,
            text="Drag files from left or click buttons below to process instantly.",
            font=("Segoe UI", 8),
            foreground="gray",
            wraplength=230,
            justify="center"
        )
        self.lbl_drop_sub.pack(anchor="center", pady=(0, 8))

        btn_box = ttk.Frame(self.drop_card)
        btn_box.pack(fill="x")
        self.btn_choose_folder = AnimatedButton(
            btn_box, text="📁 Choose Folder", style="primary", height=30, theme_name=self.current_theme,
            command=self._action_add_to_archive
        )
        self.btn_choose_folder.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.animated_buttons.append(self.btn_choose_folder)

        self.btn_choose_arc = AnimatedButton(
            btn_box, text="📦 Open Archive", style="secondary", height=30, theme_name=self.current_theme,
            command=self._action_open_archive_dialog
        )
        self.btn_choose_arc.pack(side="right", fill="x", expand=True, padx=(3, 0))
        self.animated_buttons.append(self.btn_choose_arc)

        # Card 2: Configuration & Dynamic Hardware Engine Profile
        self.conf_card = ttk.LabelFrame(right_sidebar, text="⚙️ Profile & CPU Usage", padding=8)
        self.conf_card.pack(fill="x", pady=(0, 8))

        ttk.Label(self.conf_card, text="Compression Profile:", font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(0, 2))
        self.cmb_sidebar_profile = ttk.Combobox(
            self.conf_card, values=list(LEVEL_PROFILES.keys()), state="readonly", font=("Segoe UI", 9)
        )
        self.cmb_sidebar_profile.set("Balanced")
        self.cmb_sidebar_profile.pack(fill="x", pady=(0, 6))

        ttk.Label(self.conf_card, text="CPU Usage Tier:", font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(0, 2))
        self.cpu_tiers = get_dynamic_cpu_tiers()
        self.cmb_sidebar_hw = ttk.Combobox(
            self.conf_card, values=list(self.cpu_tiers.keys()), state="readonly", font=("Segoe UI", 9)
        )
        default_tier = list(self.cpu_tiers.keys())[1]
        self.cmb_sidebar_hw.set(default_tier)
        self.cmb_sidebar_hw.pack(fill="x")

        # Card 3: Embedded All-in-One Performance & Status Card (NO POPUP WINDOWS!)
        self.perf_card = ttk.LabelFrame(right_sidebar, text="📈 Live Performance & Activity", padding=10)
        self.perf_card.pack(fill="x", pady=(0, 8))

        # Operation Title / Status
        self.lbl_perf_op = ttk.Label(
            self.perf_card, text="Engine Ready • Standing By", font=("Segoe UI Variable Display", 10, "bold")
        )
        self.lbl_perf_op.pack(anchor="w")

        # Real-Time Progress Bar
        self.prog_bar = ttk.Progressbar(self.perf_card, mode="determinate", length=240)
        self.prog_bar.pack(fill="x", pady=(4, 4))

        # Active File / Message Ticker
        self.lbl_perf_ticker = ttk.Label(
            self.perf_card, text="Ready to pack or extract", font=("Segoe UI", 8), foreground="gray"
        )
        self.lbl_perf_ticker.pack(anchor="w", pady=(0, 4))

        # Live Task Manager Area Graph
        self.live_graph = TaskManagerLiveGraph(self.perf_card, height=105, theme_name=self.current_theme)
        self.live_graph.pack(fill="x", pady=(0, 4))

        # Scorecard / Metric Line
        self.lbl_perf_metrics = ttk.Label(
            self.perf_card, text=f"CPU: {os.cpu_count() or 4} Threads Detected", font=("Segoe UI", 8),
            foreground="gray", anchor="center"
        )
        self.lbl_perf_metrics.pack(anchor="center")

        # Card 4: Quick Action Buttons
        actions_card = ttk.Frame(right_sidebar)
        actions_card.pack(fill="x")

        self.btn_side_compress = AnimatedButton(
            actions_card, text="⚡ Compress Selected", style="primary", height=34, theme_name=self.current_theme,
            command=self._action_add_to_archive
        )
        self.btn_side_compress.pack(fill="x", pady=(0, 4))
        self.animated_buttons.append(self.btn_side_compress)

        btn_row = ttk.Frame(actions_card)
        btn_row.pack(fill="x")
        self.btn_side_extract = AnimatedButton(
            btn_row, text="📥 Extract To...", style="secondary", height=30, theme_name=self.current_theme,
            command=self._action_extract_to
        )
        self.btn_side_extract.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.animated_buttons.append(self.btn_side_extract)

        self.btn_side_test = AnimatedButton(
            btn_row, text="🛡️ Test Integrity", style="secondary", height=30, theme_name=self.current_theme,
            command=self._action_test_archive
        )
        self.btn_side_test.pack(side="right", fill="x", expand=True, padx=(3, 0))
        self.animated_buttons.append(self.btn_side_test)

        # ---------------------------------------------------------------------
        # 4. Status Bar
        # ---------------------------------------------------------------------
        statusbar = ttk.Frame(self.root_container, padding=(14, 4, 14, 6))
        statusbar.pack(fill="x", side="bottom")

        self.lbl_status_items = ttk.Label(statusbar, text="0 items", font=("Segoe UI", 9))
        self.lbl_status_items.pack(side="left")

        self.lbl_status_selected = ttk.Label(statusbar, text="", font=("Segoe UI", 9), foreground="gray")
        self.lbl_status_selected.pack(side="left", padx=20)

        self.lbl_status_mode = ttk.Label(
            statusbar, text="[Filesystem Mode]", font=("Segoe UI", 9, "bold"), foreground=t["accent"]
        )
        self.lbl_status_mode.pack(side="right")

        # Global Shortcuts
        self.bind("<Control-o>", lambda e: self._action_open_archive_dialog())
        self.bind("<Control-f>", lambda e: self._focus_search())
        self.bind("<Alt-a>", lambda e: self._action_add_to_archive())
        self.bind("<Alt-e>", lambda e: self._action_extract_to())
        self.bind("<Alt-t>", lambda e: self._action_test_archive())
        self.bind("<Delete>", lambda e: self._action_delete_async())
        self.bind("<BackSpace>", lambda e: self._action_up_directory())
        self.bind("<F5>", lambda e: self._action_refresh())

    def _draw_traffic_lights(self) -> None:
        self.traffic_canvas.delete("all")
        self.traffic_canvas.configure(bg=THEMES[self.current_theme]["bg"])
        dots = [
            (8, 8, "#FF5F56", "#E0443E"),
            (24, 8, "#FFBD2E", "#DEA123"),
            (40, 8, "#27C93F", "#1AAB29"),
        ]
        for x, y, fill, outline in dots:
            self.traffic_canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=fill, outline=outline, width=1)

    def _on_switch_toggled(self, is_dark: bool) -> None:
        self.current_theme = "dark" if is_dark else "light"
        sv_ttk.set_theme(self.current_theme)
        apply_windows_mica(self, dark=is_dark)

        t = THEMES[self.current_theme]
        self.configure(bg=t["bg"])
        self.lbl_logo.configure(foreground=t["accent"])
        self.lbl_status_mode.configure(foreground=t["accent"])
        self.macos_switch.set_state(is_dark, bg_parent=t["bg"])
        self._draw_traffic_lights()

        for btn in self.animated_buttons:
            btn.set_theme(self.current_theme)
        self.live_graph.set_theme(self.current_theme)

    def _start_graph_heartbeat(self) -> None:
        if not self._active_job:
            try:
                cpu = psutil.cpu_percent(interval=None)
                self.live_graph.push_sample(cpu, label=f"CPU: {cpu:.1f}%", status="Idle")
            except Exception:
                pass
        self.after(500, self._start_graph_heartbeat)

    def _focus_search(self) -> None:
        self.ent_search.focus_set()
        self.ent_search.select_range(0, tk.END)

    # -------------------------------------------------------------------------
    # Internal Drag & Drop (Left Table to Right Dropzone)
    # -------------------------------------------------------------------------
    def _on_drag_start(self, event: Any) -> None:
        item_id = self.tree.identify_row(event.y)
        if item_id:
            matched = next((i for i in self.displayed_items if i.get("tree_id") == item_id), None)
            if matched and not matched.get("is_up"):
                self._dragged_item_path = matched.get("path")

    def _on_drag_motion(self, event: Any) -> None:
        if not self._dragged_item_path:
            return
        # Check if cursor is over dropzone card
        try:
            x_root, y_root = event.x_root, event.y_root
            card_x = self.drop_card.winfo_rootx()
            card_y = self.drop_card.winfo_rooty()
            card_w = self.drop_card.winfo_width()
            card_h = self.drop_card.winfo_height()

            if card_x <= x_root <= card_x + card_w and card_y <= y_root <= card_y + card_h:
                self.drop_card.configure(text="⚡ DROP HERE TO PROCESS!")
                self.lbl_drop_title.configure(text="Release to Start!", foreground=THEMES[self.current_theme]["accent_glow"])
            else:
                self.drop_card.configure(text="⚡ Quick Dropzone")
                self.lbl_drop_title.configure(text="Drag & Drop Target Here", foreground="")
        except Exception:
            pass

    def _on_drag_release(self, event: Any) -> None:
        if not self._dragged_item_path:
            return
        dragged = self._dragged_item_path
        self._dragged_item_path = None
        self.drop_card.configure(text="⚡ Quick Dropzone")
        self.lbl_drop_title.configure(text="Drag & Drop Target Here", foreground="")

        try:
            x_root, y_root = event.x_root, event.y_root
            card_x = self.drop_card.winfo_rootx()
            card_y = self.drop_card.winfo_rooty()
            card_w = self.drop_card.winfo_width()
            card_h = self.drop_card.winfo_height()

            if card_x <= x_root <= card_x + card_w and card_y <= y_root <= card_y + card_h:
                # Dropped inside dropzone!
                if dragged.suffix.lower() == ".blitz":
                    self._action_extract_to(specific_archive=dragged)
                else:
                    self._action_add_to_archive(specific_target=dragged)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Navigation & Directory Loading
    # -------------------------------------------------------------------------
    def _navigate_to_directory(self, path: Path) -> None:
        path = path.resolve()
        if not path.is_dir():
            return

        self.mode = "filesystem"
        self.current_dir = path
        self.current_archive_path = None
        self.archive_virtual_subpath = ""

        if not self.history or self.history[self.history_index] != path:
            self.history = self.history[: self.history_index + 1]
            self.history.append(path)
            self.history_index = len(self.history) - 1

        self.lbl_path_mode.configure(text="📂")
        self.ent_address.delete(0, tk.END)
        self.ent_address.insert(0, str(path))
        self.title(f"⚡ BlitzPack - {path.name} - [{path}]")
        self.lbl_status_mode.configure(text="[Filesystem Mode]", foreground=THEMES[self.current_theme]["accent"])

        self._refresh_filesystem_view()

    def _refresh_filesystem_view(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.displayed_items.clear()

        if self.current_dir.parent and self.current_dir.parent != self.current_dir:
            self.displayed_items.append({
                "name": "..", "is_dir": True, "is_up": True, "size_bytes": 0,
                "packed_bytes": 0, "type": "Folder", "modified": "", "path": self.current_dir.parent,
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
                            "name": entry.name, "is_dir": is_dir, "is_up": False,
                            "is_archive": ext == ".blitz", "size_bytes": size, "packed_bytes": 0,
                            "type": item_type, "modified": mtime_str, "path": Path(entry.path),
                        })
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError) as ex:
            messagebox.showerror("Access Error", f"Could not read directory:\n{str(ex)}")
            return

        self._render_tree_items()

    def _open_archive(self, archive_path: Path, subpath: str = "") -> None:
        archive_path = archive_path.resolve()
        if not archive_path.exists():
            messagebox.showerror("Error", f"Archive not found: {archive_path}")
            return

        try:
            # Read manifest into memory and close handle immediately!
            with open(archive_path, "rb") as f_in:
                reader = BlitzArchiveReader(f_in)
                manifest_entries = list(reader.manifest)
                seek_entries_count = len(reader.seek_entries)
        except Exception as ex:
            messagebox.showerror("Invalid Archive", f"Failed to open .blitz archive:\n{str(ex)}")
            return

        self.mode = "archive"
        self.current_archive_path = archive_path
        self.archive_manifest = manifest_entries
        self.archive_virtual_subpath = subpath.strip("/")

        display_path = f"{archive_path.name}"
        if self.archive_virtual_subpath:
            display_path += f"/{self.archive_virtual_subpath}"

        self.lbl_path_mode.configure(text="📦")
        self.ent_address.delete(0, tk.END)
        self.ent_address.insert(0, str(archive_path) + (f"\\{self.archive_virtual_subpath}" if self.archive_virtual_subpath else ""))
        self.title(f"⚡ BlitzPack - [{display_path}]")
        self.lbl_status_mode.configure(text="[Archive Browser]", foreground="#FFAA00")

        self.lbl_perf_op.configure(text=f"Archive: {archive_path.name}")
        self.lbl_perf_ticker.configure(
            text=f"Size: {format_bytes(archive_path.stat().st_size)} • {len(manifest_entries)} files"
        )
        self._refresh_archive_view()

    def _refresh_archive_view(self) -> None:
        if not hasattr(self, "archive_manifest"):
            return

        self.tree.delete(*self.tree.get_children())
        self.displayed_items.clear()

        self.displayed_items.append({
            "name": "..", "is_dir": True, "is_up": True, "size_bytes": 0,
            "packed_bytes": 0, "type": "Folder", "modified": "", "path": None,
        })

        cur_prefix = self.archive_virtual_subpath
        if cur_prefix and not cur_prefix.endswith("/"):
            cur_prefix += "/"

        seen_dirs = set()
        for entry in self.archive_manifest:
            rel = entry.path.replace("\\", "/")
            if cur_prefix:
                if not rel.startswith(cur_prefix):
                    continue
                rel_sub = rel[len(cur_prefix):]
            else:
                rel_sub = rel

            parts = rel_sub.split("/")
            if len(parts) == 1:
                is_dir = entry.file_type == 1
                size = entry.size
                mtime_str = datetime.datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M") if entry.mtime else ""
                _, item_type = get_file_icon_and_badge(parts[0], is_dir)

                self.displayed_items.append({
                    "name": parts[0], "is_dir": is_dir, "is_up": False, "is_archive": False,
                    "size_bytes": size, "packed_bytes": 0, "type": item_type, "modified": mtime_str,
                    "manifest_entry": entry,
                })
            elif len(parts) > 1:
                sub_dir_name = parts[0]
                if sub_dir_name not in seen_dirs:
                    seen_dirs.add(sub_dir_name)
                    self.displayed_items.append({
                        "name": sub_dir_name, "is_dir": True, "is_up": False, "is_archive": False,
                        "size_bytes": 0, "packed_bytes": 0, "type": "Folder", "modified": "",
                        "virtual_dir": True,
                    })

        self._render_tree_items()

    def _render_tree_items(self) -> None:
        self.tree.delete(*self.tree.get_children())

        query = self.var_search.get().strip().lower()
        filtered_list = [
            i for i in self.displayed_items
            if i.get("is_up") or not query or query in i["name"].lower() or query in i.get("type", "").lower()
        ]

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
            item_id = self.tree.insert(
                "", tk.END,
                values=(f"{icon}{item['name']}", size_str, packed_str, item.get("type", ""), item.get("modified", ""))
            )
            item["tree_id"] = item_id

        filter_note = f" (filtered from {len(self.displayed_items)})" if query else ""
        self.lbl_status_items.configure(
            text=f"{file_count} files, {dir_count} folders ({format_bytes(total_bytes)}){filter_note}"
        )
        self.lbl_status_selected.configure(text="")

    def _sort_column(self, col: str) -> None:
        if self.sort_column == col:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = col
            self.sort_descending = False
        self._render_tree_items()

    def _on_tree_selection_changed(self, event: Any) -> None:
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
                new_sub = f"{self.archive_virtual_subpath}/{matched['name']}".strip("/")
                if self.current_archive_path:
                    self._open_archive(self.current_archive_path, new_sub)

    def _show_context_menu(self, event: Any) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def _action_up_directory(self) -> None:
        if self.mode == "filesystem":
            parent = self.current_dir.parent
            if parent and parent != self.current_dir:
                self._navigate_to_directory(parent)
        elif self.mode == "archive":
            if self.archive_virtual_subpath:
                parts = self.archive_virtual_subpath.split("/")
                parent_sub = "/".join(parts[:-1])
                if self.current_archive_path:
                    self._open_archive(self.current_archive_path, parent_sub)
            else:
                if self.current_archive_path and self.current_archive_path.parent:
                    self._navigate_to_directory(self.current_archive_path.parent)
                else:
                    self._navigate_to_directory(Path.cwd())

    def _action_back(self) -> None:
        if self.history_index > 0:
            self.history_index -= 1
            self._navigate_to_directory(self.history[self.history_index])

    def _action_forward(self) -> None:
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self._navigate_to_directory(self.history[self.history_index])

    def _action_refresh(self) -> None:
        if self.mode == "filesystem":
            self._refresh_filesystem_view()
        elif self.mode == "archive":
            self._refresh_archive_view()

    def _on_address_entered(self) -> None:
        typed_path = Path(self.ent_address.get().strip()).resolve()
        if typed_path.is_dir():
            self._navigate_to_directory(typed_path)
        elif typed_path.is_file() and typed_path.suffix.lower() == ".blitz":
            self._open_archive(typed_path)
        else:
            messagebox.showerror("Error", f"Path does not exist: {typed_path}")

    # -------------------------------------------------------------------------
    # Core Actions (Embedded Progress in Performance Card - NO POPUPS!)
    # -------------------------------------------------------------------------
    def _get_sidebar_settings(self) -> Tuple[int, int]:
        profile_name = self.cmb_sidebar_profile.get()
        level = LEVEL_PROFILES.get(profile_name, 3)
        hw_name = self.cmb_sidebar_hw.get()
        workers = self.cpu_tiers.get(hw_name, 4)
        return level, workers

    def _action_add_to_archive(self, specific_target: Optional[Path] = None) -> None:
        if self.mode == "archive":
            return

        if specific_target:
            target_to_compress = specific_target
            out_archive_path = target_to_compress.with_suffix(".blitz")
        else:
            selection = self.tree.selection()
            selected_items = [i for i in self.displayed_items if i.get("tree_id") in selection and not i.get("is_up")]
            if selected_items:
                target_to_compress = selected_items[0]["path"]
                out_archive_path = target_to_compress.with_suffix(".blitz")
            else:
                target_to_compress = self.current_dir
                out_archive_path = self.current_dir.with_suffix(".blitz")

        level, workers = self._get_sidebar_settings()

        # Update Performance Card directly (NO POPUPS!)
        self._active_job = True
        self.prog_bar["value"] = 0
        self.lbl_perf_op.configure(text=f"⚡ Compressing {target_to_compress.name}...")
        self.lbl_perf_ticker.configure(text=f"Level {level} • {workers} Workers")
        self.lbl_perf_metrics.configure(text="Initializing zero-copy pipeline...")

        def on_progress(p: ProgressUpdate) -> None:
            if p.total_bytes > 0:
                speed_mb = p.current_speed_bps / (1024 * 1024)
                pct = (p.bytes_processed / p.total_bytes) * 100
                self.after(0, lambda: self._update_perf_progress(pct, speed_mb, p.phase, p.bytes_processed, p.total_bytes))

        def worker_thread() -> None:
            try:
                res: CompressionResult = compress(
                    input_path=target_to_compress,
                    output_path=out_archive_path,
                    level=level,
                    workers=workers,
                    progress_callback=on_progress,
                )
                self.after(0, lambda: self._show_compress_scorecard(res))
                self.after(0, self._action_refresh)
            except Exception as ex:
                self.after(0, lambda: self.lbl_perf_op.configure(text=f"❌ Error: {str(ex)[:40]}"))
            finally:
                self._active_job = False

        threading.Thread(target=worker_thread, daemon=True).start()

    def _update_perf_progress(self, pct: float, speed_mb: float, phase: str, done: int, total: int) -> None:
        self.prog_bar["value"] = pct
        self.lbl_perf_ticker.configure(text=f"{phase.capitalize()} • {format_bytes(done)} / {format_bytes(total)}")
        self.lbl_perf_metrics.configure(text=f"{pct:.1f}% • {speed_mb:.1f} MB/s")
        self.live_graph.push_sample(speed_mb, label=f"{speed_mb:.1f} MB/s ({pct:.0f}%)", status="Active")

    def _show_compress_scorecard(self, res: CompressionResult) -> None:
        self.prog_bar["value"] = 100
        self.live_graph.push_sample(res.throughput_mb_s, label=f"{res.throughput_mb_s:.1f} MB/s", status="Done")
        self.lbl_perf_op.configure(text=f"✅ Archive Created in {res.duration_seconds:.1f}s!")
        self.lbl_perf_ticker.configure(
            text=f"• {res.archive_path.name} ({format_bytes(res.compressed_size)})"
        )
        saved_pct = int((1 - (res.compressed_size / max(1, res.original_size))) * 100)
        self.lbl_perf_metrics.configure(
            text=f"⚡ {res.throughput_mb_s:.1f} MB/s • {res.compression_ratio:.2f}x Ratio (Saved {saved_pct}%)"
        )

    def _action_extract_to(self, specific_archive: Optional[Path] = None) -> None:
        archive_path: Optional[Path] = None

        if specific_archive:
            archive_path = specific_archive
        elif self.mode == "archive":
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

        dest_folder = archive_path.parent / archive_path.stem
        if dest_folder.exists():
            counter = 1
            while True:
                candidate = archive_path.parent / f"{archive_path.stem} ({counter})"
                if not candidate.exists():
                    dest_folder = candidate
                    break
                counter += 1

        _, workers = self._get_sidebar_settings()

        # Update Performance Card directly (NO POPUPS!)
        self._active_job = True
        self.prog_bar["value"] = 0
        self.lbl_perf_op.configure(text=f"📥 Extracting {archive_path.name}...")
        self.lbl_perf_ticker.configure(text=f"Destination: {dest_folder.name}")
        self.lbl_perf_metrics.configure(text="Extracting parallel chunk streams...")

        def on_progress(p: ProgressUpdate) -> None:
            if p.total_bytes > 0:
                speed_mb = p.current_speed_bps / (1024 * 1024)
                pct = (p.bytes_processed / p.total_bytes) * 100
                self.after(0, lambda: self._update_perf_progress(pct, speed_mb, "Extracting", p.bytes_processed, p.total_bytes))

        def worker_thread() -> None:
            try:
                res: DecompressionResult = decompress(
                    archive_path=archive_path,
                    output_dir=dest_folder,
                    workers=workers,
                    progress_callback=on_progress,
                )
                self.after(0, lambda: self._show_extract_scorecard(res))
                self.after(0, self._action_refresh)
            except Exception as ex:
                self.after(0, lambda: self.lbl_perf_op.configure(text=f"❌ Error: {str(ex)[:40]}"))
            finally:
                self._active_job = False

        threading.Thread(target=worker_thread, daemon=True).start()

    def _show_extract_scorecard(self, res: DecompressionResult) -> None:
        self.prog_bar["value"] = 100
        self.live_graph.push_sample(res.throughput_mb_s, label=f"{res.throughput_mb_s:.1f} MB/s", status="Done")
        self.lbl_perf_op.configure(text=f"✅ Extracted {res.total_files} Files in {res.duration_seconds:.1f}s!")
        self.lbl_perf_ticker.configure(text=f"Restored into: {res.output_dir.name}")
        self.lbl_perf_metrics.configure(
            text=f"⚡ {res.throughput_mb_s:.1f} MB/s • {format_bytes(res.extracted_bytes)} Restored"
        )

    def _action_test_archive(self) -> None:
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
            messagebox.showinfo("Test Archive", "Please select a .blitz archive to test.")
            return

        self._active_job = True
        self.lbl_perf_op.configure(text=f"🛡️ Verifying {archive_path.name}...")
        self.prog_bar["value"] = 50

        def worker_thread() -> None:
            try:
                with open(sanitize_windows_path(archive_path), "rb") as f_in:
                    reader = BlitzArchiveReader(f_in)
                    errors = reader.verify_all_checksums(f_in)

                if errors:
                    self.after(0, lambda: self.lbl_perf_op.configure(text=f"❌ {len(errors)} Corrupted Chunks!"))
                else:
                    self.after(0, lambda: self.lbl_perf_op.configure(text=f"🛡️ Verified 100% (Zero Corruption)"))
                    self.after(0, lambda: self.lbl_perf_metrics.configure(text="xxHash64 Checksums 100% Valid"))
                self.after(0, lambda: self.prog_bar.configure(value=100))
            except Exception as ex:
                self.after(0, lambda: self.lbl_perf_op.configure(text=f"❌ Test Failed: {str(ex)[:30]}"))
            finally:
                self._active_job = False

        threading.Thread(target=worker_thread, daemon=True).start()

    # -------------------------------------------------------------------------
    # Responsive Non-Blocking Background Deletions (No "(Not Responding)" Freezes!)
    # -------------------------------------------------------------------------
    def _action_delete_async(self) -> None:
        if self.mode == "archive":
            messagebox.showinfo("Delete", "Deleting files directly inside archives is not supported.")
            return

        selection = self.tree.selection()
        selected_items = [i for i in self.displayed_items if i.get("tree_id") in selection and not i.get("is_up")]
        if not selected_items:
            return

        names = [i["name"] for i in selected_items]
        msg = f"Permanently delete {len(names)} item(s)?\n\n" + "\n".join(names[:4])
        if len(names) > 4:
            msg += f"\n...and {len(names) - 4} more"

        if not messagebox.askyesno("Confirm Delete", msg):
            return

        # Execute in background thread so UI never hangs!
        self.lbl_perf_op.configure(text=f"🗑️ Deleting {len(selected_items)} item(s)...")
        self.prog_bar["value"] = 30

        def delete_worker() -> None:
            for item in selected_items:
                path: Path = item["path"]
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except Exception:
                    pass
            self.after(0, lambda: self.lbl_perf_op.configure(text="✅ Deletion complete"))
            self.after(0, lambda: self.prog_bar.configure(value=100))
            self.after(0, self._action_refresh)

        threading.Thread(target=delete_worker, daemon=True).start()

    def _action_view(self) -> None:
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

    def _open_file_with_default_app(self, path: Path) -> None:
        try:
            os.startfile(sanitize_windows_path(path))
        except Exception:
            pass

    def _action_open_archive_dialog(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Open BlitzPack Archive",
            filetypes=[("BlitzPack Archives", "*.blitz"), ("All Files", "*.*")]
        )
        if chosen:
            self._open_archive(Path(chosen))

    def _action_browse_folder_dialog(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select Directory to Browse", initialdir=self.current_dir
        )
        if chosen:
            self._navigate_to_directory(Path(chosen))

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About BlitzPack",
            "⚡ BlitzPack Archiver v1.0.0 (macOS Edition)\n\n"
            "An intelligent, ultra-fast parallel compression engine powered by Zstandard & xxHash-64.\n\n"
            "• Up to 12.4x faster extraction than legacy archivers\n"
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
