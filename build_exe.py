"""Build script to compile BlitzPack into a standalone Windows .exe using PyInstaller."""

import subprocess
import sys
from pathlib import Path


def build() -> None:
    root_dir = Path(__file__).parent.resolve()
    gui_script = root_dir / "gui.py"
    output_name = "BlitzPack"

    print("==================================================")
    print("       Building BlitzPack Standalone EXE          ")
    print("==================================================")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",  # No console window
        "--onefile",   # Single standalone .exe
        f"--name={output_name}",
        "--collect-data=sv_ttk",
        "--collect-all=zstandard",
        "--collect-all=xxhash",
        "--collect-all=msgpack",
        str(gui_script),
    ]

    print("Running PyInstaller command:")
    print(" ".join(cmd))
    print("\nCompiling... (this may take 30-60 seconds)")

    result = subprocess.run(cmd, cwd=root_dir)

    if result.returncode == 0:
        exe_path = root_dir / "dist" / f"{output_name}.exe"
        size_mb = exe_path.stat().st_size / (1024 * 1024) if exe_path.exists() else 0
        print("\n==================================================")
        print("          BUILD SUCCEEDED!                        ")
        print("==================================================")
        print(f"Executable Location: {exe_path}")
        print(f"File Size:          {size_mb:.2f} MB")
        print("\nYou can now double-click BlitzPack.exe directly!")
    else:
        print(f"\nBuild failed with exit code: {result.returncode}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    build()
