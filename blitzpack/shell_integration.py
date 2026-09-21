"""Windows Explorer Shell Context Menu Integration for BlitzPack.

Registers WinRAR-style right-click context menus in Windows File Explorer
under HKEY_CURRENT_USER\\Software\\Classes (requires zero administrator privileges).
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import List, Optional

try:
    import winreg
    REG_ROOT = winreg.HKEY_CURRENT_USER
    REG_SZ = winreg.REG_SZ
except ImportError:
    winreg = None  # type: ignore
    REG_ROOT = None  # type: ignore
    REG_SZ = 1  # type: ignore

from .multi_decompress import SUPPORTED_ARCHIVE_EXTENSIONS

CLASSES_SUB = r"Software\Classes"


def _set_reg_value(hkey: Optional[int], subkey: str, name: str, value: str, reg_type: int = REG_SZ) -> None:
    """Recursively create subkey path and set string/expand string value."""
    if winreg is None or hkey is None:
        return
    with winreg.CreateKey(hkey, subkey) as key:
        winreg.SetValueEx(key, name, 0, reg_type, value)


def _delete_reg_key_tree(hkey: Optional[int], subkey: str) -> None:
    """Recursively delete a registry key and all its subkeys."""
    if winreg is None or hkey is None:
        return
    try:
        with winreg.OpenKey(hkey, subkey, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    sub = winreg.EnumKey(key, 0)
                    _delete_reg_key_tree(hkey, f"{subkey}\\{sub}")
                except OSError:
                    break
        winreg.DeleteKey(hkey, subkey)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _notify_shell() -> None:
    """Notify Windows Shell that file associations have changed."""
    if sys.platform != "win32":
        return
    try:
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except Exception:
        pass


def resolve_gui_executable(install_dir: Optional[str] = None) -> str:
    """Determine the command line target for blitzpack-gui."""
    if sys.platform != "win32":
        return ""
    if install_dir:
        exe_path = os.path.join(install_dir, "blitzpack-gui.exe")
        if os.path.isfile(exe_path):
            return exe_path

    # If running compiled PyInstaller bundle
    if getattr(sys, "frozen", False):
        curr_exe = sys.executable
        if "gui" in os.path.basename(curr_exe).lower():
            return curr_exe
        gui_candidate = os.path.join(os.path.dirname(curr_exe), "blitzpack-gui.exe")
        if os.path.isfile(gui_candidate):
            return gui_candidate
        return curr_exe

    # If running as Python script from development repo
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gui_script = os.path.join(base, "gui.py")
    py_exe = sys.executable
    return f'"{py_exe}" "{gui_script}"'


def register_shell_context_menu(install_dir: Optional[str] = None) -> bool:
    """Register BlitzPack WinRAR-style context menus in Windows File Explorer."""
    if sys.platform != "win32" or winreg is None:
        return False
    try:
        gui_target = resolve_gui_executable(install_dir)
        gui_icon = gui_target if gui_target.endswith(".exe") else sys.executable

        # ---------------------------------------------------------------------
        # 1. Right-Click on ANY File (*) or Directory (Directory)
        # ---------------------------------------------------------------------
        targets: List[str] = [r"*\shell\BlitzPack", r"Directory\shell\BlitzPack"]

        for target in targets:
            base_key = f"{CLASSES_SUB}\\{target}"
            _set_reg_value(REG_ROOT, base_key, "", "")
            _set_reg_value(REG_ROOT, base_key, "MUIVerb", "⚡ BlitzPack")
            _set_reg_value(REG_ROOT, base_key, "Icon", f'"{gui_icon}",0')
            _set_reg_value(REG_ROOT, base_key, "SubCommands", "")

            # Subcommand 1: Add to archive...
            add_key = f"{base_key}\\shell\\Add"
            _set_reg_value(REG_ROOT, add_key, "MUIVerb", "Add to BlitzPack archive...")
            _set_reg_value(REG_ROOT, add_key, "Icon", f'"{gui_icon}",0')
            _set_reg_value(REG_ROOT, f"{add_key}\\command", "", f'{gui_target} --compress "%1"')

            # Subcommand 2: Quick compress to <name>.blitz
            quick_key = f"{base_key}\\shell\\QuickCompress"
            _set_reg_value(REG_ROOT, quick_key, "MUIVerb", 'Compress to "%1.blitz"')
            _set_reg_value(REG_ROOT, quick_key, "Icon", f'"{gui_icon}",0')
            _set_reg_value(REG_ROOT, f"{quick_key}\\command", "", f'{gui_target} --quick-compress "%1"')

        # ---------------------------------------------------------------------
        # 2. Right-Click on ALL Supported Archives (SystemFileAssociations)
        # ---------------------------------------------------------------------
        for ext in sorted(SUPPORTED_ARCHIVE_EXTENSIONS):
            base_key = f"{CLASSES_SUB}\\SystemFileAssociations\\{ext}\\shell\\BlitzPack"
            _set_reg_value(REG_ROOT, base_key, "", "")
            _set_reg_value(REG_ROOT, base_key, "MUIVerb", "⚡ BlitzPack")
            _set_reg_value(REG_ROOT, base_key, "Icon", f'"{gui_icon}",0')
            _set_reg_value(REG_ROOT, base_key, "SubCommands", "")

            # Verb 1: Extract Here
            here_key = f"{base_key}\\shell\\ExtractHere"
            _set_reg_value(REG_ROOT, here_key, "MUIVerb", "⚡ Extract Here")
            _set_reg_value(REG_ROOT, here_key, "Icon", f'"{gui_icon}",0')
            _set_reg_value(REG_ROOT, f"{here_key}\\command", "", f'{gui_target} --extract-here "%1"')

            # Verb 2: Extract to <folder>\
            to_key = f"{base_key}\\shell\\ExtractTo"
            _set_reg_value(REG_ROOT, to_key, "MUIVerb", '⚡ Extract to "%1\\"')
            _set_reg_value(REG_ROOT, to_key, "Icon", f'"{gui_icon}",0')
            _set_reg_value(REG_ROOT, f"{to_key}\\command", "", f'{gui_target} --extract-to "%1"')

            # Verb 3: Open with BlitzPack
            open_key = f"{base_key}\\shell\\Open"
            _set_reg_value(REG_ROOT, open_key, "MUIVerb", "⚡ Open with BlitzPack")
            _set_reg_value(REG_ROOT, open_key, "Icon", f'"{gui_icon}",0')
            _set_reg_value(REG_ROOT, f"{open_key}\\command", "", f'{gui_target} "%1"')

        # ---------------------------------------------------------------------
        # 3. Default File Association for .blitz archives
        # ---------------------------------------------------------------------
        _set_reg_value(REG_ROOT, f"{CLASSES_SUB}\\.blitz", "", "BlitzPack.Archive")
        prog_id = f"{CLASSES_SUB}\\BlitzPack.Archive"
        _set_reg_value(REG_ROOT, prog_id, "", "BlitzPack Compressed Archive")
        _set_reg_value(REG_ROOT, f"{prog_id}\\DefaultIcon", "", f'"{gui_icon}",0')
        _set_reg_value(REG_ROOT, f"{prog_id}\\shell\\open\\command", "", f'{gui_target} "%1"')

        _notify_shell()
        return True
    except Exception:
        return False


def unregister_shell_context_menu() -> bool:
    """Remove BlitzPack context menus and file associations from Windows Registry."""
    if sys.platform != "win32" or winreg is None:
        return False
    try:
        _delete_reg_key_tree(REG_ROOT, f"{CLASSES_SUB}\\*\\shell\\BlitzPack")
        _delete_reg_key_tree(REG_ROOT, f"{CLASSES_SUB}\\Directory\\shell\\BlitzPack")

        for ext in SUPPORTED_ARCHIVE_EXTENSIONS:
            _delete_reg_key_tree(REG_ROOT, f"{CLASSES_SUB}\\SystemFileAssociations\\{ext}\\shell\\BlitzPack")

        _delete_reg_key_tree(REG_ROOT, f"{CLASSES_SUB}\\.blitz")
        _delete_reg_key_tree(REG_ROOT, f"{CLASSES_SUB}\\BlitzPack.Archive")

        _notify_shell()
        return True
    except Exception:
        return False


def is_shell_context_menu_registered() -> bool:
    """Check if BlitzPack Explorer context menu is currently registered."""
    if sys.platform != "win32" or winreg is None:
        return False
    try:
        with winreg.OpenKey(REG_ROOT, f"{CLASSES_SUB}\\*\\shell\\BlitzPack"):
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
