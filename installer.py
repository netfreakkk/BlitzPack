"""BlitzPack Windows Setup & Installer.

Installs BlitzPack GUI + CLI, creates Desktop/Start Menu shortcuts,
adds to PATH, and configures Windows Defender Exclusion for maximum speed.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import winreg

from blitzpack.shell_integration import register_shell_context_menu

try:
    import sv_ttk
    HAS_SV_TTK = True
except ImportError:
    HAS_SV_TTK = False

APP_NAME = "BlitzPack"
APP_VERSION = "1.0.0"
PUBLISHER = "BlitzPack Team"

DEFAULT_INSTALL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "Programs",
    APP_NAME,
)

MIT_LICENSE_TEXT = """MIT License

Copyright (c) 2026 BlitzPack Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


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


def get_start_menu_dir() -> str:
    """Resolve the active Windows Start Menu Programs directory."""
    try:
        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)
            ]
        FOLDERID_Programs = GUID(0xA77F5D77, 0x2E2B, 0x44C3, (ctypes.c_ubyte * 8)(0xA6, 0xA2, 0xAB, 0xA6, 0x01, 0x05, 0x4A, 0x51))
        path_ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(FOLDERID_Programs), 0, None, ctypes.byref(path_ptr)) == 0:
            if path_ptr.value and os.path.exists(path_ptr.value):
                return path_ptr.value
    except Exception:
        pass
    return os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")


def get_payload_path(filename: str) -> str:
    """Resolve path to bundled payload file."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    payload_sub = os.path.join(base, "payload", filename)
    if os.path.exists(payload_sub):
        return payload_sub
    dist_sub = os.path.join(base, "dist", filename)
    if os.path.exists(dist_sub):
        return dist_sub
    return os.path.join(base, filename)


def create_shortcut(target: str, shortcut_path: str, description: str = "") -> bool:
    """Create a Windows .lnk shortcut using WScript.Shell or PowerShell."""
    try:
        os.makedirs(os.path.dirname(shortcut_path), exist_ok=True)
    except Exception:
        pass

    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(shortcut_path)
        shortcut.TargetPath = target
        shortcut.WorkingDirectory = os.path.dirname(target)
        shortcut.Description = description
        shortcut.IconLocation = target
        shortcut.Save()
        return True
    except Exception:
        ps_cmd = (
            f'$WshShell = New-Object -ComObject WScript.Shell; '
            f'$Shortcut = $WshShell.CreateShortcut("{shortcut_path}"); '
            f'$Shortcut.TargetPath = "{target}"; '
            f'$Shortcut.WorkingDirectory = "{os.path.dirname(target)}"; '
            f'$Shortcut.Description = "{description}"; '
            f'$Shortcut.Save()'
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False


def add_to_user_path(directory: str) -> bool:
    """Add directory to User PATH in Windows Registry if not already present."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current_path, _ = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current_path = ""

            paths = [p.strip() for p in current_path.split(";") if p.strip()]
            norm_dir = os.path.normpath(directory).lower()
            if not any(os.path.normpath(p).lower() == norm_dir for p in paths):
                new_path = f"{current_path};{directory}" if current_path else directory
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, None
        )
        return True
    except Exception:
        return False


def apply_defender_exclusion(directory: str) -> bool:
    """Add blitzpack binaries and directory to Windows Defender exclusions."""
    try:
        ps_cmd = (
            f'Add-MpPreference -ExclusionProcess "blitzpack.exe", "blitzpack-gui.exe" -ErrorAction SilentlyContinue; '
            f'Add-MpPreference -ExclusionPath "{directory}" -ErrorAction SilentlyContinue'
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def register_uninstaller(install_dir: str) -> None:
    """Register BlitzPack in Windows Add/Remove Programs registry."""
    try:
        uninstall_key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, uninstall_key) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, f"{APP_NAME} (High-Speed Parallel Archiver)")
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
            gui_exe = os.path.join(install_dir, "blitzpack-gui.exe")
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, gui_exe)
            uninstall_bat = os.path.join(install_dir, "uninstall.bat")
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{uninstall_bat}"')

        desktop_lnk = os.path.join(get_desktop_dir(), f"{APP_NAME}.lnk")
        start_lnk = os.path.join(get_start_menu_dir(), f"{APP_NAME}.lnk")

        bat_content = f"""@echo off
title Uninstalling {APP_NAME}...
echo Uninstalling {APP_NAME}...
echo Removing Windows Explorer context menu integration...
reg delete "HKCU\\Software\\Classes\\*\\shell\\BlitzPack" /f 2>nul
reg delete "HKCU\\Software\\Classes\\Directory\\shell\\BlitzPack" /f 2>nul
reg delete "HKCU\\Software\\Classes\\BlitzPack.Archive" /f 2>nul
reg delete "HKCU\\Software\\Classes\\.blitz" /f 2>nul
for %%E in (zip rar 7z tar gz bz2 xz tgz tbz2 txz cbz cbr cb7 jar war) do (
    reg delete "HKCU\\Software\\Classes\\SystemFileAssociations\\.%%E\\shell\\BlitzPack" /f 2>nul
)
del /f /q "{desktop_lnk}" 2>nul
del /f /q "{start_lnk}" 2>nul
reg delete "HKCU\\Software\\Classes\\SystemFileAssociations\\.blitz" /f 2>nul
reg delete "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{APP_NAME}" /f 2>nul
echo Cleaning files...
start /b "" cmd /c timeout /t 2 ^& rd /s /q "{install_dir}"
echo {APP_NAME} uninstalled successfully.
"""
        with open(uninstall_bat, "w", encoding="utf-8") as f:
            f.write(bat_content)
    except Exception:
        pass


def perform_installation(
    target_dir: str,
    create_desktop_shortcut: bool,
    create_start_menu: bool,
    add_path: bool,
    add_defender: bool,
    add_shell_menu: bool = True,
    progress_callback=None,
) -> tuple[bool, str]:
    """Execute full installation workflow."""
    try:
        def log(msg: str, pct: int) -> None:
            if progress_callback:
                progress_callback(msg, pct)
            time.sleep(0.08)

        log("Preparing installation directory...", 10)
        os.makedirs(target_dir, exist_ok=True)

        log("Extracting BlitzPack CLI binary...", 25)
        cli_src = get_payload_path("blitzpack.exe")
        cli_dst = os.path.join(target_dir, "blitzpack.exe")
        if os.path.exists(cli_src):
            shutil.copy2(cli_src, cli_dst)

        log("Extracting BlitzPack Desktop GUI binary...", 45)
        gui_src = get_payload_path("blitzpack-gui.exe")
        gui_dst = os.path.join(target_dir, "blitzpack-gui.exe")
        if os.path.exists(gui_src):
            shutil.copy2(gui_src, gui_dst)

        ico_src = get_payload_path("blitzpack.ico")
        if os.path.exists(ico_src):
            shutil.copy2(ico_src, os.path.join(target_dir, "blitzpack.ico"))
        png_src = get_payload_path("blitzpack.png")
        if os.path.exists(png_src):
            shutil.copy2(png_src, os.path.join(target_dir, "blitzpack.png"))

        if create_desktop_shortcut:
            log("Creating Desktop shortcut...", 60)
            desktop_dir = get_desktop_dir()
            if os.path.exists(gui_dst):
                create_shortcut(gui_dst, os.path.join(desktop_dir, f"{APP_NAME}.lnk"), "BlitzPack High-Speed Archiver")

        if create_start_menu:
            log("Creating Start Menu entry...", 70)
            start_dir = get_start_menu_dir()
            if os.path.exists(gui_dst):
                create_shortcut(gui_dst, os.path.join(start_dir, f"{APP_NAME}.lnk"), "BlitzPack High-Speed Archiver")

        if add_shell_menu:
            log("Configuring Windows Explorer context menus...", 80)
            register_shell_context_menu(target_dir)

        if add_path:
            log("Registering CLI in User PATH...", 88)
            add_to_user_path(target_dir)

        if add_defender:
            log("Configuring Windows Defender exclusion (Max Speed)...", 94)
            apply_defender_exclusion(target_dir)

        log("Registering uninstaller...", 98)
        register_uninstaller(target_dir)

        log("Installation Complete!", 100)
        return True, ""
    except Exception as e:
        return False, str(e)


class InstallerGUI:
    """Traditional multi-step wizard installer for BlitzPack."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} Setup Wizard")
        self.root.geometry("620x490")
        self.root.minsize(620, 490)
        self.root.resizable(False, False)

        if HAS_SV_TTK:
            sv_ttk.set_theme("dark")

        # Configure AppUserModelID for Windows Taskbar icon grouping
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BlitzPack.Installer.1.0.0")
            except Exception:
                pass

        self._set_app_icon()

        # Installation settings
        self.target_dir_var = tk.StringVar(value=DEFAULT_INSTALL_DIR)
        self.license_agree_var = tk.StringVar(value="agree")
        self.desktop_var = tk.BooleanVar(value=True)
        self.start_menu_var = tk.BooleanVar(value=True)
        self.shell_menu_var = tk.BooleanVar(value=True)
        self.path_var = tk.BooleanVar(value=True)
        self.defender_var = tk.BooleanVar(value=True)
        self.launch_after_var = tk.BooleanVar(value=True)

        self.current_step = 0
        self.steps = ["license", "options", "ready", "installing", "finished"]

        self._build_shell()
        self._show_step(0)

    def _set_app_icon(self) -> None:
        """Apply high-resolution multi-format icon to installer window."""
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            for cand in [os.path.join(base, "assets", "blitzpack.ico"), os.path.join(base, "blitzpack.ico")]:
                if os.path.isfile(cand):
                    self.root.iconbitmap(default=cand)
                    break
            for cand in [os.path.join(base, "assets", "blitzpack.png"), os.path.join(base, "blitzpack.png")]:
                if os.path.isfile(cand):
                    self._icon_photo = tk.PhotoImage(file=cand)
                    self.root.iconphoto(False, self._icon_photo)
                    break
        except Exception:
            pass

    def _build_shell(self) -> None:
        self.header_frame = ttk.Frame(self.root, padding=(24, 16, 24, 12))
        self.header_frame.pack(fill=tk.X)

        self.lbl_header_title = ttk.Label(
            self.header_frame, text="", font=("Segoe UI Variable Display", 13, "bold")
        )
        self.lbl_header_title.pack(anchor=tk.W)

        self.lbl_header_sub = ttk.Label(
            self.header_frame, text="", font=("Segoe UI", 9), foreground="#94A3B8"
        )
        self.lbl_header_sub.pack(anchor=tk.W, pady=(2, 0))

        ttk.Separator(self.root).pack(fill=tk.X, padx=20)

        self.content_container = ttk.Frame(self.root, padding=(24, 16, 24, 16))
        self.content_container.pack(fill=tk.BOTH, expand=True)

        ttk.Separator(self.root).pack(fill=tk.X, padx=20)

        self.nav_frame = ttk.Frame(self.root, padding=(24, 12, 24, 16))
        self.nav_frame.pack(fill=tk.X)

        self.btn_cancel = ttk.Button(self.nav_frame, text="Cancel", width=10, command=self._on_cancel)
        self.btn_cancel.pack(side=tk.RIGHT, padx=(8, 0))

        self.btn_next = ttk.Button(self.nav_frame, text="Next >", width=12, style="Accent.TButton" if HAS_SV_TTK else "TButton", command=self._on_next)
        self.btn_next.pack(side=tk.RIGHT, padx=(8, 0))

        self.btn_back = ttk.Button(self.nav_frame, text="< Back", width=10, command=self._on_back)
        self.btn_back.pack(side=tk.RIGHT)

    def _clear_content(self) -> None:
        for w in self.content_container.winfo_children():
            w.destroy()

    def _show_step(self, step_index: int) -> None:
        self.current_step = step_index
        self._clear_content()

        step_name = self.steps[step_index]

        if step_name == "license":
            self._render_license_page()
        elif step_name == "options":
            self._render_options_page()
        elif step_name == "ready":
            self._render_ready_page()
        elif step_name == "installing":
            self._render_installing_page()
        elif step_name == "finished":
            self._render_finished_page()

    def _render_license_page(self) -> None:
        self.lbl_header_title.configure(text="⚡ License Agreement")
        self.lbl_header_sub.configure(text="Please review the license terms before installing BlitzPack.")

        ttk.Label(
            self.content_container,
            text="If you accept the terms of the agreement, select the option below to continue.",
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(0, 8))

        text_frame = ttk.Frame(self.content_container)
        text_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        txt_license = tk.Text(
            text_frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            height=9,
            bg="#0D1726",
            fg="#E2E8F0",
            relief="solid",
            bd=1,
            highlightthickness=0,
        )
        scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=txt_license.yview)
        txt_license.configure(yscrollcommand=scroll.set)
        txt_license.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        txt_license.insert(tk.END, MIT_LICENSE_TEXT)
        txt_license.configure(state=tk.DISABLED)

        radio_box = ttk.Frame(self.content_container)
        radio_box.pack(fill=tk.X)

        rb_agree = ttk.Radiobutton(
            radio_box,
            text="I accept the agreement",
            variable=self.license_agree_var,
            value="agree",
            command=self._update_nav_buttons,
        )
        rb_agree.pack(anchor=tk.W, pady=(2, 2))

        rb_disagree = ttk.Radiobutton(
            radio_box,
            text="I do not accept the agreement",
            variable=self.license_agree_var,
            value="disagree",
            command=self._update_nav_buttons,
        )
        rb_disagree.pack(anchor=tk.W)

        self.btn_back.configure(state=tk.DISABLED)
        self.btn_next.configure(text="Next >", state=tk.NORMAL if self.license_agree_var.get() == "agree" else tk.DISABLED)
        self.btn_cancel.configure(state=tk.NORMAL)

    def _render_options_page(self) -> None:
        self.lbl_header_title.configure(text="📁 Destination Location & Setup Options")
        self.lbl_header_sub.configure(text="Select where BlitzPack will be installed and which shortcuts to create.")

        ttk.Label(self.content_container, text="Installation Directory:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 4))

        dest_box = ttk.Frame(self.content_container)
        dest_box.pack(fill=tk.X, pady=(0, 14))

        ent_dest = ttk.Entry(dest_box, textvariable=self.target_dir_var, font=("Segoe UI", 9))
        ent_dest.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        btn_browse = ttk.Button(dest_box, text="Browse...", command=self._browse_directory)
        btn_browse.pack(side=tk.RIGHT)

        ttk.Label(self.content_container, text="Additional Tasks:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 6))

        ttk.Checkbutton(
            self.content_container,
            text="Create a Desktop shortcut (Places BlitzPack on your active Desktop)",
            variable=self.desktop_var,
        ).pack(anchor=tk.W, pady=2)

        ttk.Checkbutton(
            self.content_container,
            text="Create a Start Menu program shortcut",
            variable=self.start_menu_var,
        ).pack(anchor=tk.W, pady=2)

        ttk.Checkbutton(
            self.content_container,
            text="Add Windows Explorer context menu (Right-click to compress/extract)",
            variable=self.shell_menu_var,
        ).pack(anchor=tk.W, pady=2)

        ttk.Checkbutton(
            self.content_container,
            text="Add BlitzPack CLI to user PATH (enables 'blitzpack' in any terminal)",
            variable=self.path_var,
        ).pack(anchor=tk.W, pady=2)

        ttk.Checkbutton(
            self.content_container,
            text="Configure Windows Defender exclusion (Recommended for max NVMe speed)",
            variable=self.defender_var,
        ).pack(anchor=tk.W, pady=2)

        self.btn_back.configure(state=tk.NORMAL)
        self.btn_next.configure(text="Next >", state=tk.NORMAL)
        self.btn_cancel.configure(state=tk.NORMAL)

    def _render_ready_page(self) -> None:
        self.lbl_header_title.configure(text="⚡ Ready to Install")
        self.lbl_header_sub.configure(text="Setup is ready to begin installing BlitzPack on your computer.")

        ttk.Label(
            self.content_container,
            text="Click Install to proceed with the following configuration:",
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(0, 8))

        summary_frame = ttk.Frame(self.content_container, padding=10)
        summary_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        lines = [
            f"• Install Destination:\n   {self.target_dir_var.get()}",
            "• Shortcuts to Create:" + (" Desktop" if self.desktop_var.get() else "") + (" Start Menu" if self.start_menu_var.get() else ""),
            "• Explorer Context Menu: " + ("Enabled (WinRAR style)" if self.shell_menu_var.get() else "Disabled"),
            "• System PATH Registration: " + ("Enabled" if self.path_var.get() else "Disabled"),
            "• Windows Defender Exclusion: " + ("Enabled (Max Turbo)" if self.defender_var.get() else "Disabled"),
        ]
        lbl_summary = ttk.Label(
            summary_frame,
            text="\n\n".join(lines),
            font=("Segoe UI", 9),
            justify=tk.LEFT,
        )
        lbl_summary.pack(anchor=tk.W)

        self.btn_back.configure(state=tk.NORMAL)
        self.btn_next.configure(text="Install", state=tk.NORMAL)
        self.btn_cancel.configure(state=tk.NORMAL)

    def _render_installing_page(self) -> None:
        self.lbl_header_title.configure(text="⏳ Installing BlitzPack...")
        self.lbl_header_sub.configure(text="Please wait while Setup copies files and applies system settings.")

        ttk.Label(
            self.content_container,
            text="Extracting binaries, registering shortcuts, and optimizing NVMe throughput...",
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(20, 10))

        self.prog_bar = ttk.Progressbar(self.content_container, mode="determinate")
        self.prog_bar.pack(fill=tk.X, pady=(0, 8))

        self.lbl_install_status = ttk.Label(
            self.content_container,
            text="Starting setup pipeline...",
            font=("Segoe UI", 9),
            foreground="#94A3B8",
        )
        self.lbl_install_status.pack(anchor=tk.W)

        self.btn_back.configure(state=tk.DISABLED)
        self.btn_next.configure(state=tk.DISABLED)
        self.btn_cancel.configure(state=tk.DISABLED)

        threading.Thread(target=self._run_install_worker, daemon=True).start()

    def _run_install_worker(self) -> None:
        target = self.target_dir_var.get().strip()

        def on_prog(msg: str, pct: int) -> None:
            self.root.after(0, lambda: self._update_install_progress(msg, pct))

        success, err = perform_installation(
            target_dir=target,
            create_desktop_shortcut=self.desktop_var.get(),
            create_start_menu=self.start_menu_var.get(),
            add_path=self.path_var.get(),
            add_defender=self.defender_var.get(),
            add_shell_menu=self.shell_menu_var.get(),
            progress_callback=on_prog,
        )

        self.root.after(0, lambda: self._on_install_done(success, err))

    def _update_install_progress(self, msg: str, pct: int) -> None:
        if hasattr(self, "prog_bar"):
            self.prog_bar["value"] = pct
        if hasattr(self, "lbl_install_status"):
            self.lbl_install_status.configure(text=msg)

    def _on_install_done(self, success: bool, err: str) -> None:
        if success:
            self._show_step(4)
        else:
            messagebox.showerror("Installation Error", f"Installation failed:\n\n{err}")
            self.btn_back.configure(state=tk.NORMAL)
            self.btn_cancel.configure(state=tk.NORMAL)

    def _render_finished_page(self) -> None:
        self.lbl_header_title.configure(text="🎉 Completing the BlitzPack Setup Wizard")
        self.lbl_header_sub.configure(text="BlitzPack has been successfully installed on your computer.")

        ttk.Label(
            self.content_container,
            text="Setup has finished installing BlitzPack on your system.\n"
                 "The application may be launched using the created shortcuts.",
            font=("Segoe UI", 10),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(15, 20))

        ttk.Checkbutton(
            self.content_container,
            text="Launch BlitzPack GUI now",
            variable=self.launch_after_var,
        ).pack(anchor=tk.W, pady=(0, 10))

        desktop_dir = get_desktop_dir()
        ttk.Label(
            self.content_container,
            text=f"✓ Desktop shortcut created at:\n   {desktop_dir}\\{APP_NAME}.lnk",
            font=("Segoe UI", 8),
            foreground="#38BDF8",
        ).pack(anchor=tk.W)

        self.btn_back.pack_forget()
        self.btn_cancel.pack_forget()
        self.btn_next.configure(text="Finish", state=tk.NORMAL, command=self._on_finish)

    def _update_nav_buttons(self) -> None:
        if self.steps[self.current_step] == "license":
            is_agreed = self.license_agree_var.get() == "agree"
            self.btn_next.configure(state=tk.NORMAL if is_agreed else tk.DISABLED)

    def _browse_directory(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.target_dir_var.get())
        if chosen:
            self.target_dir_var.set(os.path.join(chosen, APP_NAME))

    def _on_next(self) -> None:
        if self.current_step == 0:
            if self.license_agree_var.get() != "agree":
                return
            self._show_step(1)
        elif self.current_step == 1:
            if not self.target_dir_var.get().strip():
                messagebox.showwarning("Directory Required", "Please specify an installation directory.")
                return
            self._show_step(2)
        elif self.current_step == 2:
            self._show_step(3)

    def _on_back(self) -> None:
        if self.current_step > 0:
            self._show_step(self.current_step - 1)

    def _on_cancel(self) -> None:
        if messagebox.askyesno("Exit Setup", "Are you sure you want to cancel the BlitzPack installation?"):
            self.root.quit()

    def _on_finish(self) -> None:
        target = self.target_dir_var.get().strip()
        if self.launch_after_var.get():
            gui_exe = os.path.join(target, "blitzpack-gui.exe")
            if os.path.exists(gui_exe):
                subprocess.Popen([gui_exe], cwd=target)
        self.root.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="BlitzPack Windows Installer")
    parser.add_argument("-s", "--silent", action="store_true", help="Run installation silently without UI")
    parser.add_argument("-d", "--dir", default=DEFAULT_INSTALL_DIR, help="Custom installation directory")
    parser.add_argument("--no-desktop", action="store_true", help="Do not create desktop shortcut")
    parser.add_argument("--no-start", action="store_true", help="Do not create start menu shortcut")
    parser.add_argument("--no-path", action="store_true", help="Do not add to user PATH")
    parser.add_argument("--no-defender", action="store_true", help="Do not apply Windows Defender exclusion")
    parser.add_argument("--no-shell-menu", action="store_true", help="Do not register Windows Explorer context menus")
    args = parser.parse_args()

    if args.silent:
        print(f"Installing BlitzPack to: {args.dir}...")
        success, err = perform_installation(
            target_dir=args.dir,
            create_desktop_shortcut=not args.no_desktop,
            create_start_menu=not args.no_start,
            add_path=not args.no_path,
            add_defender=not args.no_defender,
            add_shell_menu=not args.no_shell_menu,
            progress_callback=lambda msg, pct: print(f"[{pct}%] {msg}"),
        )
        if success:
            print("BlitzPack installation completed successfully.")
            sys.exit(0)
        else:
            print(f"Installation failed: {err}", file=sys.stderr)
            sys.exit(1)

    root = tk.Tk()
    InstallerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
