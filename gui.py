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
import os
from pathlib import Path
import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil
import sv_ttk

from blitzpack.compressor import CompressionResult, compress
from blitzpack.decompressor import DecompressionResult, decompress
from blitzpack.multi_decompress import (
    get_archive_format,
    inspect_archive,
    is_supported_archive,
    test_archive,
)
from blitzpack.shell_integration import (
    is_shell_context_menu_registered,
    register_shell_context_menu,
    unregister_shell_context_menu,
)
from blitzpack.utils import BlitzCancelled, ProgressUpdate, format_bytes, sanitize_windows_path

# Enable Windows Per-Monitor High-DPI Awareness (V2) for crisp rendering
if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

GWLP_WNDPROC = -4
WM_DROPFILES = 0x0233
LRESULT = ctypes.c_longlong
HWND = ctypes.c_void_p
UINT = ctypes.c_uint
WPARAM = ctypes.c_void_p
LPARAM = ctypes.c_void_p
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


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


def apply_windows_dark_titlebar(window: tk.Tk, dark: bool = True) -> None:
    """Set native Windows title bar dark/light mode attribute cleanly without backdrop blur glitches."""
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(window.winfo_id(), 2) or window.winfo_id()
        dark_val = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_val), ctypes.sizeof(dark_val))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(dark_val), ctypes.sizeof(dark_val))
    except Exception:
        pass


def get_desktop_dir() -> str:
    """Resolve the real, active Windows Desktop directory (including OneDrive-redirected Desktops)."""
    try:
        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)
            ]
        FOLDERID_Desktop = GUID(0xB4BFCC3A, 0xDB2C, 0x424C, (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        path_ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(FOLDERID_Desktop), 0, None, ctypes.byref(path_ptr)) == 0:
            if path_ptr.value and os.path.exists(path_ptr.value):
                return path_ptr.value
    except Exception:
        pass

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            val, _ = winreg.QueryValueEx(key, "Desktop")
            expanded = os.path.expandvars(val)
            if os.path.exists(expanded):
                return expanded
    except Exception:
        pass

    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    if not os.path.exists(desktop):
        od = os.path.join(os.environ.get("USERPROFILE", ""), "OneDrive", "Desktop")
        if os.path.exists(od):
            return od
    return desktop


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
# Sleek Navigation Rail Icon Button
# -----------------------------------------------------------------------------
class NavRailButton(tk.Canvas):
    """Sleek vertical rail icon button with glowing active pill indicator."""

    def __init__(
        self,
        parent: Any,
        icon: str,
        label: str,
        command: Optional[Callable[[], None]] = None,
        is_active: bool = False,
        width: int = 58,
        height: int = 50,
    ) -> None:
        super().__init__(parent, width=width, height=height, highlightthickness=0, bd=0, bg="#060B14")
        self.icon = icon
        self.label = label
        self.command = command
        self.is_active = is_active
        self.is_hovered = False
        self.width = width
        self.height = height

        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._redraw()

    def set_active(self, active: bool) -> None:
        self.is_active = active
        self._redraw()

    def _on_enter(self, event: Any) -> None:
        self.is_hovered = True
        self.config(cursor="hand2")
        self._redraw()

    def _on_leave(self, event: Any) -> None:
        self.is_hovered = False
        self._redraw()

    def _on_click(self, event: Any) -> None:
        if self.command:
            self.command()

    def _draw_rounded_rect(self, x1: int, y1: int, x2: int, y2: int, r: int, fill: str, outline: str, width: int = 1) -> None:
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        self.create_polygon(points, smooth=True, fill=fill, outline=outline, width=width)

    def _redraw(self) -> None:
        self.delete("all")
        self.configure(bg="#060B14")

        # Active glow pill or hover background (inspired by modern dashboard reference)
        if self.is_active:
            self._draw_rounded_rect(4, 3, self.width - 4, self.height - 3, 10, "#102644", "#38BDF8", width=1)
            self.create_line(5, 12, 5, self.height - 12, fill="#38BDF8", width=3, capstyle=tk.ROUND)
            icon_color = "#38BDF8"
            text_color = "#F8FAFC"
        elif self.is_hovered:
            self._draw_rounded_rect(4, 3, self.width - 4, self.height - 3, 8, "#0F1A2D", "#1E3354", width=1)
            icon_color = "#E2E8F0"
            text_color = "#CBD5E1"
        else:
            icon_color = "#94A3B8"
            text_color = "#64748B"

        self.create_text(self.width // 2, 17, text=self.icon, font=("Segoe UI Emoji", 13), fill=icon_color)
        self.create_text(self.width // 2, 37, text=self.label, font=("Segoe UI Variable Text", 7, "bold"), fill=text_color)


# -----------------------------------------------------------------------------
# Animated Hover Pill Button
# -----------------------------------------------------------------------------
class AnimatedButton(tk.Canvas):
    """Modern rounded pill button with smooth hover highlights and click feedback."""

    def __init__(
        self,
        parent: Any,
        text: str,
        command: Optional[Callable[[], None]] = None,
        style: str = "primary",
        height: int = 34,
        theme_name: str = "dark",
        font: Tuple[str, int, str] = ("Segoe UI Variable Text", 9, "bold"),
        bg_parent: str = "",
    ) -> None:
        super().__init__(parent, height=height, highlightthickness=0, bd=0)
        self.text = text
        self.command = command
        self.btn_style = style
        self.btn_height = height
        self.theme_name = theme_name
        self.btn_font = font
        self.bg_parent = bg_parent
        self.is_hovered = False
        self.is_pressed = False
        self.width = 120

        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_theme(self, theme_name: str, bg_parent: str = "") -> None:
        self.theme_name = theme_name
        if bg_parent:
            self.bg_parent = bg_parent
        bg_canvas = self.bg_parent if self.bg_parent else THEMES[self.theme_name]["card_bg"]
        self.configure(bg=bg_canvas)
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
                return (t["accent_hover"], "#7DD3FC", t["accent_text"])
            return (t["accent"], "#38BDF8", t["accent_text"])
        elif self.btn_style == "danger":
            if self.is_pressed or self.is_hovered:
                return ("#DC2626", "#F87171", "#FFFFFF")
            return ("#991B1B", "#F87171", "#FFFFFF")
        else:
            if self.is_pressed or self.is_hovered:
                return (t["secondary_hover"], t["accent_glow"], t["secondary_text"])
            return (t["secondary_btn"], t["secondary_border"], t["secondary_text"])

    def _draw_rounded_rect(self, x1: int, y1: int, x2: int, y2: int, r: int, fill: str, outline: str, width: int = 1) -> None:
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        self.create_polygon(points, smooth=True, fill=fill, outline=outline, width=width)

    def _redraw(self) -> None:
        self.delete("all")
        bg_fill, border_color, text_color = self._get_colors()
        bg_canvas = self.bg_parent if self.bg_parent else THEMES[self.theme_name]["card_bg"]
        self.configure(bg=bg_canvas)
        padding = 2 if not self.is_pressed else 3
        r = 8
        outline_w = 2 if (self.btn_style == "primary" and (self.is_hovered or self.is_pressed)) else 1
        self._draw_rounded_rect(
            padding, padding, max(padding + 10, self.width - padding), self.btn_height - padding,
            r, bg_fill, border_color, width=outline_w
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
    """Modern Windows Fluent Desktop Archiver & File Manager."""

    def __init__(self) -> None:
        super().__init__()

        self.title("⚡ BlitzPack")
        self.geometry("1180x720")
        self.minsize(940, 580)

        # Default theme
        self.current_theme = "dark"

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

        self.rail_buttons: Dict[str, NavRailButton] = {}
        self.animated_buttons: List[AnimatedButton] = []
        self._active_job: bool = False
        self._cancel_event: Optional[threading.Event] = None
        self._dragged_item_path: Optional[Path] = None
        self._hwnd: Optional[int] = None
        self._old_wndproc: Optional[int] = None
        self._wndproc_ref: Any = None

        self._build_ui()
        self._apply_theme_styling(is_dark=True)
        self._setup_native_drag_and_drop()
        self._navigate_to_directory(self.current_dir)
        self._start_graph_heartbeat()
        self._handle_cli_launch_args()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _handle_cli_launch_args(self) -> None:
        """Process CLI arguments when launched from Windows Explorer context menu."""
        args = sys.argv[1:]
        if not args:
            return

        first = args[0]
        if first == "--extract-here" and len(args) > 1:
            target = Path(args[1]).resolve()
            if target.exists() and is_supported_archive(target):
                self.after(200, lambda: self._action_extract_to(specific_archive=target, dest_override=target.parent))
        elif first == "--extract-to" and len(args) > 1:
            target = Path(args[1]).resolve()
            if target.exists() and is_supported_archive(target):
                self.after(200, lambda: self._action_extract_to(specific_archive=target, dest_override=target.parent / target.stem))
        elif first == "--compress" and len(args) > 1:
            target = Path(args[1]).resolve()
            if target.exists():
                self.after(200, lambda: self._action_add_to_archive(specific_target=target))
        elif first == "--quick-compress" and len(args) > 1:
            target = Path(args[1]).resolve()
            if target.exists():
                self.after(200, lambda: self._action_quick_compress(target))
        else:
            target = Path(first).resolve()
            if target.exists():
                if is_supported_archive(target):
                    self.after(200, lambda: self._open_archive(target))
                elif target.is_dir():
                    self.after(200, lambda: self._navigate_to_directory(target))

    def _action_quick_compress(self, target: Path) -> None:
        """Quickly compress target into <target>.blitz without modal prompt."""
        target = target.resolve()
        if not target.exists():
            return

        out_archive = target.with_suffix(".blitz") if target.is_file() else target.parent / f"{target.name}.blitz"
        if out_archive.exists():
            counter = 1
            while True:
                candidate = target.parent / f"{target.stem} ({counter}).blitz"
                if not candidate.exists():
                    out_archive = candidate
                    break
                counter += 1

        level, workers = self._get_sidebar_settings()

        self._active_job = True
        self._cancel_event = threading.Event()
        self.btn_cancel_job.pack(fill="x", pady=(6, 0))
        self.prog_bar["value"] = 0
        self.lbl_perf_op.configure(text=f"⚡ Compressing {target.name}...")
        self.lbl_perf_ticker.configure(text=f"Creating: {out_archive.name}")
        self.lbl_perf_metrics.configure(text=f"Profile: Level {level} • {workers} Workers")

        def on_progress(p: ProgressUpdate) -> None:
            if p.total_bytes > 0:
                speed_mb = p.current_speed_bps / (1024 * 1024)
                pct = (p.bytes_processed / p.total_bytes) * 100
                self.after(0, lambda: self._update_perf_progress(pct, speed_mb, p.phase, p.bytes_processed, p.total_bytes))

        def worker_thread() -> None:
            try:
                res: CompressionResult = compress(
                    input_path=target,
                    output_path=out_archive,
                    level=level,
                    workers=workers,
                    cancel_event=self._cancel_event,
                    progress_callback=on_progress,
                )
                self.after(0, lambda: self._show_compress_scorecard(res))
                self.after(0, self._action_refresh)
            except BlitzCancelled:
                self.after(0, lambda: self.lbl_perf_op.configure(text="⚠️ Compression Cancelled"))
                self.after(0, lambda: self.lbl_perf_ticker.configure(text="Pipeline halted cleanly"))
            except Exception as ex:
                err_msg = str(ex)[:40]
                self.after(0, lambda m=err_msg: self.lbl_perf_op.configure(text=f"❌ Error: {m}"))
            finally:
                self._active_job = False
                self._cancel_event = None
                self.after(0, self.btn_cancel_job.pack_forget)

        threading.Thread(target=worker_thread, daemon=True).start()

    def _toggle_shell_integration(self) -> None:
        """Toggle Windows Explorer right-click context menu integration."""
        is_reg = is_shell_context_menu_registered()
        if is_reg:
            if messagebox.askyesno(
                "Windows Shell Integration",
                "BlitzPack Explorer context menus are currently ENABLED.\n\n"
                "Would you like to remove BlitzPack from the Windows Explorer right-click menu?"
            ):
                if unregister_shell_context_menu():
                    messagebox.showinfo("Windows Shell Integration", "Context menus successfully removed.")
                else:
                    messagebox.showerror("Error", "Failed to remove registry keys.")
        else:
            if messagebox.askyesno(
                "Windows Shell Integration",
                "Enable BlitzPack WinRAR-style right-click context menus in Windows Explorer?\n\n"
                "This allows you to:\n"
                "• Right-click any file/folder to compress\n"
                "• Right-click any archive (.blitz, .rar, .zip, .7z, .tar, etc.) to extract"
            ):
                if register_shell_context_menu():
                    messagebox.showinfo("Windows Shell Integration", "Context menus successfully registered!")
                else:
                    messagebox.showerror("Error", "Failed to register context menus.")

    def _on_close(self) -> None:
        if self._cancel_event:
            self._cancel_event.set()
        self._cleanup_native_drag_and_drop()
        self.destroy()

    def _setup_native_drag_and_drop(self) -> None:
        """Register native Windows WM_DROPFILES hook to accept files/folders dragged from Explorer."""
        if sys.platform != "win32":
            return
        try:
            self.update_idletasks()
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32

            hwnd = user32.GetAncestor(self.winfo_id(), 2) or self.winfo_id()
            if not hwnd:
                return
            self._hwnd = hwnd

            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, HWND, UINT, WPARAM, LPARAM]
            user32.CallWindowProcW.restype = LRESULT
            user32.GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [HWND, ctypes.c_int, ctypes.c_void_p]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            shell32.DragAcceptFiles.argtypes = [HWND, ctypes.c_bool]
            shell32.DragAcceptFiles.restype = None
            shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
            shell32.DragQueryFileW.restype = ctypes.c_uint
            shell32.DragFinish.argtypes = [ctypes.c_void_p]
            shell32.DragFinish.restype = None

            self._old_wndproc = user32.GetWindowLongPtrW(self._hwnd, GWLP_WNDPROC)

            def py_wndproc(hwnd_val: Any, msg: int, wparam: Any, lparam: Any) -> int:
                if msg == WM_DROPFILES:
                    hdrop = wparam
                    try:
                        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                        dropped_paths = []
                        for i in range(count):
                            buf = ctypes.create_unicode_buffer(1024)
                            shell32.DragQueryFileW(hdrop, i, buf, 1024)
                            if buf.value:
                                dropped_paths.append(buf.value)
                        shell32.DragFinish(hdrop)
                        if dropped_paths:
                            self.after(0, lambda p=dropped_paths: self._on_external_drop(p))
                    except Exception:
                        pass
                    return 0
                return user32.CallWindowProcW(self._old_wndproc, hwnd_val, msg, wparam, lparam)

            self._wndproc_ref = WNDPROC(py_wndproc)
            user32.SetWindowLongPtrW(self._hwnd, GWLP_WNDPROC, ctypes.cast(self._wndproc_ref, ctypes.c_void_p))
            shell32.DragAcceptFiles(self._hwnd, True)
        except Exception:
            pass

    def _cleanup_native_drag_and_drop(self) -> None:
        """Restore original window procedure cleanly to avoid crashes on shutdown."""
        if sys.platform != "win32":
            return
        if self._old_wndproc and self._hwnd:
            try:
                ctypes.windll.user32.SetWindowLongPtrW(self._hwnd, GWLP_WNDPROC, self._old_wndproc)
            except Exception:
                pass
            self._old_wndproc = None

    def _on_external_drop(self, paths: List[str]) -> None:
        """Handle files or folders dragged from an external Windows File Explorer window."""
        if not paths:
            return
        valid_paths = [Path(p) for p in paths if os.path.exists(p)]
        if not valid_paths:
            return

        # If single archive dropped, open or inspect it
        if len(valid_paths) == 1 and valid_paths[0].is_file() and is_supported_archive(valid_paths[0]):
            self._open_archive(valid_paths[0])
            return

        # If dropped target is a directory or file, add to archive
        self._action_add_to_archive(specific_target=valid_paths[0])

    def _apply_theme_styling(self, is_dark: bool = True) -> None:
        """Apply unified permanent dark theme styling to all ttk widgets, canvases, and windows title bar."""
        self.current_theme = "dark"
        sv_ttk.set_theme("dark")
        apply_windows_dark_titlebar(self, dark=True)

        t = THEMES[self.current_theme]
        self.configure(bg=t["bg"])

        style = ttk.Style(self)
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["text_primary"])
        style.configure("TLabelframe", background=t["card_bg"], bordercolor=t["card_border"])
        style.configure("TLabelframe.Label", background=t["card_bg"], foreground=t["text_primary"], font=("Segoe UI Variable Display", 9, "bold"))

        style.configure(
            "Treeview",
            background=t["card_bg"],
            foreground=t["text_primary"],
            fieldbackground=t["card_bg"],
            rowheight=28,
            font=("Segoe UI", 9),
        )
        style.map(
            "Treeview",
            background=[("selected", t["accent"])],
            foreground=[("selected", t["accent_text"])],
        )
        style.configure("Treeview.Heading", font=("Segoe UI Variable Display", 9, "bold"))

        if hasattr(self, "lbl_logo"):
            self.lbl_logo.configure(foreground=t["accent"], background=t["bg"])
        if hasattr(self, "lbl_badge"):
            self.lbl_badge.configure(background=t["card_border"], foreground=t["text_secondary"])
        if hasattr(self, "lbl_engine_status"):
            self.lbl_engine_status.configure(background=t["card_bg"], foreground="#38BDF8")
        if hasattr(self, "lbl_status_mode"):
            self.lbl_status_mode.configure(foreground=t["accent"], background=t["bg"])
        if hasattr(self, "lbl_drop_title"):
            self.lbl_drop_title.configure(background=t["card_bg"], foreground=t["text_primary"])
        if hasattr(self, "lbl_drop_sub"):
            self.lbl_drop_sub.configure(background=t["card_bg"], foreground=t["text_secondary"])
        if hasattr(self, "lbl_drop_icon"):
            self.lbl_drop_icon.configure(background=t["card_bg"], foreground=t["accent"])
        if hasattr(self, "lbl_perf_op"):
            self.lbl_perf_op.configure(background=t["card_bg"], foreground=t["text_primary"])
        if hasattr(self, "lbl_perf_ticker"):
            self.lbl_perf_ticker.configure(background=t["card_bg"], foreground=t["text_secondary"])
        if hasattr(self, "lbl_perf_metrics"):
            self.lbl_perf_metrics.configure(background=t["card_bg"], foreground=t["text_secondary"])
        if hasattr(self, "lbl_status_items"):
            self.lbl_status_items.configure(background=t["bg"], foreground=t["text_primary"])
        if hasattr(self, "lbl_status_selected"):
            self.lbl_status_selected.configure(background=t["bg"], foreground=t["text_secondary"])
        if hasattr(self, "lbl_path_mode"):
            self.lbl_path_mode.configure(background=t["bg"], foreground=t["text_primary"])

        # Update animated buttons with parent card background
        if hasattr(self, "btn_choose_folder"):
            self.btn_choose_folder.set_theme(self.current_theme, bg_parent=t["card_bg"])
        if hasattr(self, "btn_choose_arc"):
            self.btn_choose_arc.set_theme(self.current_theme, bg_parent=t["card_bg"])
        if hasattr(self, "btn_cancel_job"):
            self.btn_cancel_job.set_theme(self.current_theme, bg_parent=t["card_bg"])
        if hasattr(self, "btn_side_extract"):
            self.btn_side_extract.set_theme(self.current_theme, bg_parent=t["bg"])
        if hasattr(self, "btn_side_test"):
            self.btn_side_test.set_theme(self.current_theme, bg_parent=t["bg"])

        if hasattr(self, "live_graph"):
            self.live_graph.set_theme(self.current_theme)

    def _on_switch_toggled(self, is_dark: bool) -> None:
        pass

    def _update_rail_active(self, active_key: str) -> None:
        """Update active glowing pill on navigation rail buttons."""
        for key, btn in self.rail_buttons.items():
            btn.set_active(key == active_key)

    def _update_rail_active_by_path(self, path: Path) -> None:
        """Determine and highlight the active rail button based on navigated path."""
        try:
            resolved = path.resolve()
            desktop_path = Path(get_desktop_dir()).resolve()
            if resolved == (Path.home() / "Downloads").resolve():
                self._update_rail_active("downloads")
            elif resolved == (Path.home() / "Documents").resolve():
                self._update_rail_active("docs")
            elif resolved == desktop_path:
                self._update_rail_active("desktop")
            elif resolved == Path.home().resolve():
                self._update_rail_active("home")
            else:
                self._update_rail_active("files")
        except Exception:
            self._update_rail_active("files")

    def _action_focus_files(self) -> None:
        """Focus File Explorer view in the workspace."""
        if self.mode == "archive":
            self._navigate_to_directory(self.current_dir)
        self._update_rail_active("files")

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
        menu_commands.add_separator()
        menu_commands.add_command(label="Windows Shell Integration...", command=self._toggle_shell_integration)

        menu_help = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=menu_help)
        menu_help.add_command(label="About BlitzPack", command=self._show_about)

        # Root Box
        self.root_container = ttk.Frame(self)
        self.root_container.pack(fill="both", expand=True)

        # ---------------------------------------------------------------------
        # 1. Header Bar (Brand Logo, Engine Status Pill, Search Entry)
        # ---------------------------------------------------------------------
        top_bar = ttk.Frame(self.root_container, padding=(14, 10, 14, 8))
        top_bar.pack(fill="x")

        # Brand Badge (Cleanly left-aligned with zero fake traffic lights)
        brand_frame = ttk.Frame(top_bar)
        brand_frame.pack(side="left")
        self.lbl_logo = ttk.Label(
            brand_frame, text="⚡ BlitzPack", font=("Segoe UI Variable Display", 13, "bold"), foreground=t["accent"]
        )
        self.lbl_logo.pack(side="left")
        self.lbl_badge = ttk.Label(
            brand_frame, text=" v1.0 ", font=("Segoe UI", 8), background=t["card_border"], foreground=t["text_secondary"]
        )
        self.lbl_badge.pack(side="left", padx=(8, 8))

        # Engine Status Pill
        self.lbl_engine_status = ttk.Label(
            brand_frame, text="● Engine Ready", font=("Segoe UI Variable Text", 8, "bold"),
            background=t["card_bg"], foreground="#38BDF8", padding=(6, 2)
        )
        self.lbl_engine_status.pack(side="left")

        # Live Search Filter (Right aligned)
        search_box = ttk.Frame(top_bar)
        search_box.pack(side="right", padx=(0, 2))
        lbl_search_icon = ttk.Label(search_box, text="🔍", font=("Segoe UI", 9))
        lbl_search_icon.pack(side="left", padx=(0, 4))
        self.ent_search = ttk.Entry(search_box, textvariable=self.var_search, width=24, font=("Segoe UI", 9))
        self.ent_search.pack(side="left")

        # ---------------------------------------------------------------------
        # Main Body Layout: Navigation Rail (Left) + Workspace Area (Right)
        # ---------------------------------------------------------------------
        body_container = ttk.Frame(self.root_container)
        body_container.pack(fill="both", expand=True)

        # Left Vertical Navigation Rail (Inspired by modern dashboard dock)
        self.rail_frame = tk.Frame(body_container, width=64, bg="#060B14", highlightbackground="#152033", highlightthickness=1)
        self.rail_frame.pack(side="left", fill="y", padx=(6, 0), pady=(0, 6))
        self.rail_frame.pack_propagate(False)

        # Rail Quick Nav Buttons
        rail_items = [
            ("files", "📁", "Files", self._action_focus_files, True),
            ("home", "🏠", "Home", lambda: self._navigate_to_directory(Path.home()), False),
            ("downloads", "📥", "Downloads", lambda: self._navigate_to_directory(Path.home() / "Downloads"), False),
            ("docs", "📝", "Docs", lambda: self._navigate_to_directory(Path.home() / "Documents"), False),
            ("desktop", "🖥️", "Desktop", lambda: self._navigate_to_directory(Path(get_desktop_dir())), False),
            ("archives", "📦", "Archives", self._action_open_archive_dialog, False),
        ]
        for key, icon, label, cmd, is_active in rail_items:
            btn = NavRailButton(
                self.rail_frame,
                icon=icon,
                label=label,
                command=cmd,
                is_active=is_active,
                width=58,
                height=50,
            )
            btn.pack(side="top", pady=3, padx=2)
            self.rail_buttons[key] = btn

        # Workspace Area (Right of Navigation Rail)
        workspace_frame = ttk.Frame(body_container)
        workspace_frame.pack(side="right", fill="both", expand=True)

        # ---------------------------------------------------------------------
        # 2. Navigation Ribbon
        # ---------------------------------------------------------------------
        nav_ribbon = ttk.Frame(workspace_frame, padding=(8, 2, 14, 8))
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
        # 3. Status Bar (Pack side="bottom" inside workspace_frame)
        # ---------------------------------------------------------------------
        statusbar = ttk.Frame(workspace_frame, padding=(8, 4, 14, 6))
        statusbar.pack(fill="x", side="bottom")

        self.lbl_status_items = ttk.Label(statusbar, text="0 items", font=("Segoe UI", 9))
        self.lbl_status_items.pack(side="left")

        self.lbl_status_selected = ttk.Label(statusbar, text="", font=("Segoe UI", 9), foreground="gray")
        self.lbl_status_selected.pack(side="left", padx=20)

        self.lbl_status_mode = ttk.Label(
            statusbar, text="[Filesystem Mode]", font=("Segoe UI", 9, "bold"), foreground=t["accent"]
        )
        self.lbl_status_mode.pack(side="right")

        # ---------------------------------------------------------------------
        # 4. Main Split View (70% Table, 30% Decluttered Sidebar)
        # ---------------------------------------------------------------------
        paned = ttk.PanedWindow(workspace_frame, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=(8, 14), pady=(0, 6))

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

        # RIGHT PANE (30%): Decluttered Sidebar
        right_sidebar = ttk.Frame(paned, padding=(8, 0, 0, 0))
        paned.add(right_sidebar, weight=3)

        # Card 1: Sleek Dropzone Card
        self.drop_card = ttk.LabelFrame(right_sidebar, text="⚡ Quick Dropzone", padding=(10, 8, 10, 10))
        self.drop_card.pack(fill="x", pady=(0, 8))

        self.lbl_drop_icon = ttk.Label(self.drop_card, text="⚡", font=("Segoe UI", 20))
        self.lbl_drop_icon.pack(anchor="center")
        self.lbl_drop_title = ttk.Label(self.drop_card, text="Drag & Drop Files or Folders Here", font=("Segoe UI Variable Display", 10, "bold"))
        self.lbl_drop_title.pack(anchor="center", pady=(1, 1))
        self.lbl_drop_sub = ttk.Label(
            self.drop_card,
            text="Drop from Explorer or left pane to instantly compress or open.",
            font=("Segoe UI", 8),
            foreground="gray",
            wraplength=230,
            justify="center"
        )
        self.lbl_drop_sub.pack(anchor="center", pady=(0, 6))

        btn_box = ttk.Frame(self.drop_card)
        btn_box.pack(fill="x")
        self.btn_choose_folder = AnimatedButton(
            btn_box, text="➕ Add to Archive", style="primary", height=32, theme_name=self.current_theme,
            command=self._action_add_to_archive, bg_parent=t["card_bg"]
        )
        self.btn_choose_folder.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.animated_buttons.append(self.btn_choose_folder)

        self.btn_choose_arc = AnimatedButton(
            btn_box, text="📦 Open Archive", style="secondary", height=32, theme_name=self.current_theme,
            command=self._action_open_archive_dialog, bg_parent=t["card_bg"]
        )
        self.btn_choose_arc.pack(side="right", fill="x", expand=True, padx=(3, 0))
        self.animated_buttons.append(self.btn_choose_arc)

        # Card 2: Streamlined 2-Column Settings
        self.conf_card = ttk.LabelFrame(right_sidebar, text="⚙️ Compression Settings", padding=(8, 6, 8, 8))
        self.conf_card.pack(fill="x", pady=(0, 8))

        settings_grid = ttk.Frame(self.conf_card)
        settings_grid.pack(fill="x")
        settings_grid.columnconfigure(0, weight=1)
        settings_grid.columnconfigure(1, weight=1)

        # Col 0: Profile
        col0 = ttk.Frame(settings_grid)
        col0.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        ttk.Label(col0, text="Profile:", font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(0, 2))
        self.cmb_sidebar_profile = ttk.Combobox(
            col0, values=list(LEVEL_PROFILES.keys()), state="readonly", font=("Segoe UI", 9)
        )
        self.cmb_sidebar_profile.set("Balanced")
        self.cmb_sidebar_profile.pack(fill="x")

        # Col 1: Hardware Cores
        col1 = ttk.Frame(settings_grid)
        col1.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        ttk.Label(col1, text="Hardware Cores:", font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(0, 2))
        self.cpu_tiers = get_dynamic_cpu_tiers()
        self.cmb_sidebar_hw = ttk.Combobox(
            col1, values=list(self.cpu_tiers.keys()), state="readonly", font=("Segoe UI", 9)
        )
        default_tier = list(self.cpu_tiers.keys())[1]
        self.cmb_sidebar_hw.set(default_tier)
        self.cmb_sidebar_hw.pack(fill="x")

        # Card 3: Embedded Live Performance & Activity Card
        self.perf_card = ttk.LabelFrame(right_sidebar, text="📈 Live Activity", padding=(10, 8, 10, 8))
        self.perf_card.pack(fill="x", pady=(0, 8))

        # Operation Title / Status
        self.lbl_perf_op = ttk.Label(
            self.perf_card, text="Engine Ready • Standing By", font=("Segoe UI Variable Display", 9, "bold")
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

        # Live Task Manager Area Graph (85px compact)
        self.live_graph = TaskManagerLiveGraph(self.perf_card, height=85, theme_name=self.current_theme)
        self.live_graph.pack(fill="x", pady=(0, 4))

        # Scorecard / Metric Line
        self.lbl_perf_metrics = ttk.Label(
            self.perf_card, text=f"CPU: {os.cpu_count() or 4} Threads Detected", font=("Segoe UI", 8),
            foreground="gray", anchor="center"
        )
        self.lbl_perf_metrics.pack(anchor="center")

        # Active Job Cancel Button (Visible dynamically during compression/extraction)
        self.btn_cancel_job = AnimatedButton(
            self.perf_card, text="🛑 Cancel Operation", style="danger", height=28, theme_name=self.current_theme,
            command=self._action_cancel_job, bg_parent=t["card_bg"]
        )
        self.animated_buttons.append(self.btn_cancel_job)

        # Card 4: Quick Action Buttons (Extract & Test in a single row)
        actions_row = ttk.Frame(right_sidebar)
        actions_row.pack(fill="x")

        self.btn_side_extract = AnimatedButton(
            actions_row, text="📥 Extract Selected...", style="secondary", height=32, theme_name=self.current_theme,
            command=self._action_extract_to, bg_parent=t["bg"]
        )
        self.btn_side_extract.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.animated_buttons.append(self.btn_side_extract)

        self.btn_side_test = AnimatedButton(
            actions_row, text="🛡️ Test Integrity", style="secondary", height=32, theme_name=self.current_theme,
            command=self._action_test_archive, bg_parent=t["bg"]
        )
        self.btn_side_test.pack(side="right", fill="x", expand=True, padx=(3, 0))
        self.animated_buttons.append(self.btn_side_test)

        # Global Shortcuts
        self.bind("<Control-o>", lambda e: self._action_open_archive_dialog())
        self.bind("<Control-f>", lambda e: self._focus_search())
        self.bind("<Alt-a>", lambda e: self._action_add_to_archive())
        self.bind("<Alt-e>", lambda e: self._action_extract_to())
        self.bind("<Alt-t>", lambda e: self._action_test_archive())
        self.bind("<Delete>", lambda e: self._action_delete_async())
        self.bind("<BackSpace>", lambda e: self._action_up_directory())
        self.bind("<F5>", lambda e: self._action_refresh())
        self.bind("<Escape>", lambda e: self._action_cancel_job())

    def _action_cancel_job(self) -> None:
        """Cooperatively halt any in-flight compression or decompression job."""
        if self._active_job and self._cancel_event and not self._cancel_event.is_set():
            self._cancel_event.set()
            self.lbl_perf_op.configure(text="⏳ Cancelling...")
            self.lbl_perf_ticker.configure(text="Halting worker pipelines cleanly...")
            self.btn_cancel_job.pack_forget()

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
        self._update_rail_active_by_path(path)

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
                        _, item_type = get_file_icon_and_badge(entry.name, is_dir)

                        self.displayed_items.append({
                            "name": entry.name, "is_dir": is_dir, "is_up": False,
                            "is_archive": is_supported_archive(entry.name), "size_bytes": size, "packed_bytes": 0,
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
            manifest_entries = inspect_archive(archive_path)
        except Exception as ex:
            messagebox.showerror("Invalid Archive", f"Failed to open archive:\n{str(ex)}")
            return

        fmt_label = get_archive_format(archive_path).upper()
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
        self.lbl_status_mode.configure(text=f"[{fmt_label} Browser]", foreground="#FFAA00")

        self.lbl_perf_op.configure(text=f"Archive: {archive_path.name}")
        self.lbl_perf_ticker.configure(
            text=f"Size: {format_bytes(archive_path.stat().st_size)} • {len(manifest_entries)} files"
        )
        self._update_rail_active("archives")
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
        elif typed_path.is_file() and is_supported_archive(typed_path):
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
        self._cancel_event = threading.Event()
        self.btn_cancel_job.pack(fill="x", pady=(6, 0))
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
                    cancel_event=self._cancel_event,
                    progress_callback=on_progress,
                )
                self.after(0, lambda: self._show_compress_scorecard(res))
                self.after(0, self._action_refresh)
            except BlitzCancelled:
                self.after(0, lambda: self.lbl_perf_op.configure(text="⚠️ Compression Cancelled"))
                self.after(0, lambda: self.lbl_perf_ticker.configure(text="Pipeline halted cleanly"))
            except Exception as ex:
                err_msg = str(ex)[:40]
                self.after(0, lambda m=err_msg: self.lbl_perf_op.configure(text=f"❌ Error: {m}"))
            finally:
                self._active_job = False
                self._cancel_event = None
                self.after(0, self.btn_cancel_job.pack_forget)

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

    def _action_extract_to(
        self, specific_archive: Optional[Path] = None, dest_override: Optional[Path] = None
    ) -> None:
        archive_path: Optional[Path] = None

        if specific_archive:
            archive_path = specific_archive
        elif self.mode == "archive":
            archive_path = self.current_archive_path
        else:
            selection = self.tree.selection()
            selected_items = [i for i in self.displayed_items if i.get("tree_id") in selection and not i.get("is_up")]
            for item in selected_items:
                if item.get("is_archive") or is_supported_archive(item.get("path", Path())):
                    archive_path = item["path"]
                    break

        if not archive_path or not archive_path.exists():
            messagebox.showinfo("Extract", "Please select an archive or open one to extract.")
            return

        if dest_override:
            dest_folder = dest_override
        else:
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
        self._cancel_event = threading.Event()
        self.btn_cancel_job.pack(fill="x", pady=(6, 0))
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
                    cancel_event=self._cancel_event,
                    progress_callback=on_progress,
                )
                self.after(0, lambda: self._show_extract_scorecard(res))
                self.after(0, self._action_refresh)
            except BlitzCancelled:
                self.after(0, lambda: self.lbl_perf_op.configure(text="⚠️ Extraction Cancelled"))
                self.after(0, lambda: self.lbl_perf_ticker.configure(text="Pipeline halted cleanly"))
            except Exception as ex:
                err_msg = str(ex)[:40]
                self.after(0, lambda m=err_msg: self.lbl_perf_op.configure(text=f"❌ Error: {m}"))
            finally:
                self._active_job = False
                self._cancel_event = None
                self.after(0, self.btn_cancel_job.pack_forget)

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
                if item.get("is_archive") or is_supported_archive(item.get("path", Path())):
                    archive_path = item["path"]
                    break

        if not archive_path or not archive_path.exists():
            messagebox.showinfo("Test Archive", "Please select an archive to test.")
            return

        self._active_job = True
        self.lbl_perf_op.configure(text=f"🛡️ Verifying {archive_path.name}...")
        self.prog_bar["value"] = 50

        def worker_thread() -> None:
            try:
                ok, msg = test_archive(archive_path)
                if ok:
                    self.after(0, lambda: self.lbl_perf_op.configure(text="🛡️ Verified 100% (Zero Corruption)"))
                    self.after(0, lambda m=msg: self.lbl_perf_metrics.configure(text=m))
                    self.after(0, lambda: self.prog_bar.configure(value=100))
                else:
                    self.after(0, lambda: self.lbl_perf_op.configure(text="❌ Test Failed!"))
                    self.after(0, lambda m=msg: self.lbl_perf_metrics.configure(text=m))
                    self.after(0, lambda: self.prog_bar.configure(value=0))
            except Exception as ex:
                err_msg = str(ex)[:40]
                self.after(0, lambda m=err_msg: self.lbl_perf_op.configure(text=f"❌ Test Failed: {m}"))
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
            title="Open Archive (BlitzPack, RAR, ZIP, 7-Zip, TAR)",
            filetypes=[
                (
                    "All Supported Archives",
                    "*.blitz;*.zip;*.rar;*.7z;*.tar;*.gz;*.tgz;*.bz2;*.xz;*.cbz;*.cbr;*.cb7;*.jar;*.war",
                ),
                ("BlitzPack Archives (*.blitz)", "*.blitz"),
                ("ZIP Archives (*.zip, *.cbz, *.jar, *.war)", "*.zip;*.cbz;*.jar;*.war"),
                ("RAR Archives (*.rar, *.cbr)", "*.rar;*.cbr"),
                ("7-Zip Archives (*.7z, *.cb7)", "*.7z;*.cb7"),
                (
                    "TAR & Compressed Streams (*.tar, *.tar.gz, *.tgz, *.tar.bz2, *.tar.xz, *.gz, *.bz2, *.xz)",
                    "*.tar;*.tar.gz;*.tgz;*.tar.bz2;*.tbz2;*.tar.xz;*.txz;*.gz;*.bz2;*.xz",
                ),
                ("All Files (*.*)", "*.*"),
            ],
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
            "⚡ BlitzPack Archiver v1.0.0 (Windows Edition)\n\n"
            "An intelligent, high-throughput parallel archiver powered by Zstandard & xxHash-64.\n\n"
            "• Up to 3.8× faster than WinRAR on real-world projects\n"
            "• 100% compression density parity\n"
            "• 32-worker parallel I/O prefetching & seekable .blitz format\n\n"
            "Open Source (MIT License)\n"
            "https://github.com/netfreakkk/BlitzPack"
        )


def main() -> None:
    app = BlitzPackMainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
