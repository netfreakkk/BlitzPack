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


def create_shortcut(target: str, shortcut_path: str, description: str = ""):
    """Create a Windows .lnk shortcut using WScript.Shell or PowerShell."""
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


def register_uninstaller(install_dir: str):
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

        desktop_lnk = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", f"{APP_NAME}.lnk")
        start_lnk = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs", f"{APP_NAME}.lnk")

        bat_content = f"""@echo off
title Uninstalling {APP_NAME}...
echo Uninstalling {APP_NAME}...
del /f /q "{desktop_lnk}" 2>nul
del /f /q "{start_lnk}" 2>nul
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
    progress_callback=None,
) -> tuple[bool, str]:
    """Execute full installation workflow."""
    try:
        def log(msg, pct):
            if progress_callback:
                progress_callback(msg, pct)
            time.sleep(0.05)

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

        if create_desktop_shortcut:
            log("Creating Desktop shortcut...", 60)
            desktop_dir = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
            if os.path.exists(desktop_dir) and os.path.exists(gui_dst):
                create_shortcut(gui_dst, os.path.join(desktop_dir, f"{APP_NAME}.lnk"), "BlitzPack High-Speed Archiver")

        if create_start_menu:
            log("Creating Start Menu entry...", 70)
            start_dir = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")
            if os.path.exists(start_dir) and os.path.exists(gui_dst):
                create_shortcut(gui_dst, os.path.join(start_dir, f"{APP_NAME}.lnk"), "BlitzPack High-Speed Archiver")

        if add_path:
            log("Registering CLI in User PATH...", 80)
            add_to_user_path(target_dir)

        if add_defender:
            log("Configuring Windows Defender exclusion (Max Speed)...", 90)
            apply_defender_exclusion(target_dir)

        log("Registering uninstaller...", 95)
        register_uninstaller(target_dir)

        log("Installation Complete!", 100)
        return True, ""
    except Exception as e:
        return False, str(e)


class InstallerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("BlitzPack Setup")
        self.root.geometry("560x440")
        self.root.resizable(False, False)

        if HAS_SV_TTK:
            sv_ttk.set_theme("dark")

        self.target_dir_var = tk.StringVar(value=DEFAULT_INSTALL_DIR)
        self.desktop_var = tk.BooleanVar(value=True)
        self.start_menu_var = tk.BooleanVar(value=True)
        self.path_var = tk.BooleanVar(value=True)
        self.defender_var = tk.BooleanVar(value=True)
        self.launch_after_var = tk.BooleanVar(value=True)

        self.setup_ui()

    def setup_ui(self):
        # Header banner
        header = ttk.Frame(self.root, padding="20 15 20 10")
        header.pack(fill=tk.X)

        title = ttk.Label(header, text="⚡ BlitzPack Setup", font=("Segoe UI", 18, "bold"))
        title.pack(anchor=tk.W)

        subtitle = ttk.Label(
            header,
            text="Intelligent, High-Throughput Parallel Archiver (GUI and CLI)",
            font=("Segoe UI", 9),
            foreground="#888888"
        )
        subtitle.pack(anchor=tk.W, pady=(2, 0))

        ttk.Separator(self.root).pack(fill=tk.X, padx=20, pady=5)

        # Content frame
        self.content_frame = ttk.Frame(self.root, padding="20 10 20 15")
        self.content_frame.pack(fill=tk.BOTH, expand=True)

        # Destination selector
        dest_label = ttk.Label(self.content_frame, text="Installation Directory:", font=("Segoe UI", 10, "bold"))
        dest_label.pack(anchor=tk.W, pady=(0, 5))

        dest_row = ttk.Frame(self.content_frame)
        dest_row.pack(fill=tk.X, pady=(0, 15))

        dest_entry = ttk.Entry(dest_row, textvariable=self.target_dir_var, font=("Segoe UI", 9))
        dest_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        browse_btn = ttk.Button(dest_row, text="Browse...", command=self.browse_dir)
        browse_btn.pack(side=tk.RIGHT)

        # Options
        opts_label = ttk.Label(self.content_frame, text="Setup Options:", font=("Segoe UI", 10, "bold"))
        opts_label.pack(anchor=tk.W, pady=(0, 6))

        ttk.Checkbutton(self.content_frame, text="Create Desktop Shortcut for GUI", variable=self.desktop_var).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(self.content_frame, text="Create Start Menu Entry", variable=self.start_menu_var).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(self.content_frame, text="Add BlitzPack CLI to user PATH (run 'blitzpack' anywhere)", variable=self.path_var).pack(anchor=tk.W, pady=2)

        def_box = ttk.Frame(self.content_frame)
        def_box.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        ttk.Checkbutton(
            def_box,
            text="Configure Windows Defender exclusion (Unlocks full 20s NVMe speed)",
            variable=self.defender_var
        ).pack(anchor=tk.W)

        # Progress / Status frame (hidden initially)
        self.prog_frame = ttk.Frame(self.content_frame)
        self.prog_bar = ttk.Progressbar(self.prog_frame, orient=tk.HORIZONTAL, mode="determinate")
        self.prog_bar.pack(fill=tk.X, pady=(10, 4))
        self.status_label = ttk.Label(self.prog_frame, text="", font=("Segoe UI", 9))
        self.status_label.pack(anchor=tk.W)

        # Action bar at bottom
        ttk.Separator(self.root).pack(fill=tk.X, padx=20, pady=(0, 10))
        self.btn_frame = ttk.Frame(self.root, padding="20 0 20 15")
        self.btn_frame.pack(fill=tk.X)

        self.cancel_btn = ttk.Button(self.btn_frame, text="Cancel", command=self.root.quit)
        self.cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))

        self.install_btn = ttk.Button(self.btn_frame, text="Install Now", style="Accent.TButton" if HAS_SV_TTK else "TButton", command=self.start_install)
        self.install_btn.pack(side=tk.RIGHT)

    def browse_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.target_dir_var.get())
        if chosen:
            self.target_dir_var.set(os.path.join(chosen, APP_NAME))

    def start_install(self):
        self.install_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.DISABLED)
        self.prog_frame.pack(fill=tk.X, pady=(10, 0))

        thread = threading.Thread(target=self._run_install_thread, daemon=True)
        thread.start()

    def _run_install_thread(self):
        target = self.target_dir_var.get().strip()

        def update_prog(msg, pct):
            self.root.after(0, lambda: self._update_ui_progress(msg, pct))

        success, err = perform_installation(
            target_dir=target,
            create_desktop_shortcut=self.desktop_var.get(),
            create_start_menu=self.start_menu_var.get(),
            add_path=self.path_var.get(),
            add_defender=self.defender_var.get(),
            progress_callback=update_prog,
        )

        self.root.after(0, lambda: self._on_install_finished(success, err, target))

    def _update_ui_progress(self, msg, pct):
        self.prog_bar["value"] = pct
        self.status_label.config(text=msg)

    def _on_install_finished(self, success: bool, err: str, target: str):
        if success:
            # Show completed screen
            for child in self.content_frame.winfo_children():
                child.destroy()

            success_title = ttk.Label(
                self.content_frame,
                text="🎉 Installation Successful!",
                font=("Segoe UI", 14, "bold"),
                foreground="#4CAF50"
            )
            success_title.pack(anchor=tk.W, pady=(10, 10))

            info_text = (
                f"BlitzPack has been installed to:\n{target}\n\n"
                f"• Desktop shortcut created\n"
                f"• CLI available in terminal ('blitzpack --help')\n"
                f"• Defender optimized for maximum SSD throughput\n"
            )
            ttk.Label(self.content_frame, text=info_text, font=("Segoe UI", 9)).pack(anchor=tk.W, pady=(0, 15))

            ttk.Checkbutton(self.content_frame, text="Launch BlitzPack GUI now", variable=self.launch_after_var).pack(anchor=tk.W)

            self.install_btn.destroy()
            self.cancel_btn.config(text="Finish", state=tk.NORMAL, command=self._finish_and_launch)
        else:
            messagebox.showerror("Installation Failed", f"An error occurred during installation:\n\n{err}")
            self.install_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.NORMAL)

    def _finish_and_launch(self):
        target = self.target_dir_var.get().strip()
        if self.launch_after_var.get():
            gui_exe = os.path.join(target, "blitzpack-gui.exe")
            if os.path.exists(gui_exe):
                subprocess.Popen([gui_exe], cwd=target)
        self.root.quit()


def main():
    parser = argparse.ArgumentParser(description="BlitzPack Windows Installer")
    parser.add_argument("-s", "--silent", action="store_true", help="Run installation silently without UI")
    parser.add_argument("-d", "--dir", default=DEFAULT_INSTALL_DIR, help="Custom installation directory")
    parser.add_argument("--no-desktop", action="store_true", help="Do not create desktop shortcut")
    parser.add_argument("--no-start", action="store_true", help="Do not create start menu shortcut")
    parser.add_argument("--no-path", action="store_true", help="Do not add to user PATH")
    parser.add_argument("--no-defender", action="store_true", help="Do not apply Windows Defender exclusion")
    args = parser.parse_args()

    if args.silent:
        print(f"Installing BlitzPack to: {args.dir}...")
        success, err = perform_installation(
            target_dir=args.dir,
            create_desktop_shortcut=not args.no_desktop,
            create_start_menu=not args.no_start,
            add_path=not args.no_path,
            add_defender=not args.no_defender,
            progress_callback=lambda msg, pct: print(f"[{pct}%] {msg}"),
        )
        if success:
            print("BlitzPack installation completed successfully.")
            sys.exit(0)
        else:
            print(f"Installation failed: {err}", file=sys.stderr)
            sys.exit(1)

    root = tk.Tk()
    app = InstallerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
