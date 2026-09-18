<div align="center">

# ⚡ BlitzPack

### Intelligent, High-Throughput Parallel Archiver for Windows, Linux & macOS

**21.6× FASTER than WinRAR on real-world projects • 100% Compression Density Parity • Seekable `.blitz` Format**

[![Tests](https://img.shields.io/badge/tests-19%20passed-success?style=flat-square&logo=pytest)](tests/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square&logo=python)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20linux%20%7C%20macos-lightgrey?style=flat-square)]()
[![Format](https://img.shields.io/badge/format-seekable%20.blitz-orange?style=flat-square)](FORMAT.md)

</div>

---

> [!IMPORTANT]
> ### 🚀 Critical Performance Tip for Windows Users
> On Windows, Microsoft Defender's real-time filter driver (`WdFilter.sys`) intercepts file opens from new or unsigned executables, adding ~18 ms of inspection latency to **every single code file** (`.js`, `.ts`, `.xml`, `.json`).
> 
> **To unlock full NVMe SSD hardware throughput and achieve 20-second compression on 60,000 files (instead of 130s+):**
> 
> * **Option A (Automatic):** Install via the unified [`BlitzPack-Setup.exe`](#downloads) installer — it includes an automatic Defender optimization toggle!
> * **Option B (Manual - 5 Seconds):** Run once in an Administrator PowerShell:
>   ```powershell
>   Add-MpPreference -ExclusionProcess "blitzpack.exe", "blitzpack-gui.exe"
>   ```

---

## 📊 Real-World Benchmarks

All multi-threaded tools were tested under identical conditions, pinned strictly to **4 CPU Threads** on standard/balanced profiles.

### 1. Large-Scale Production Dataset: React Native App (`kitty app`)
> **59,945 files • 4.75 GB uncompressed** (full `node_modules` + complete Android Gradle build tree)

| Archiving Tool | Compression Time | Throughput | Result |
| :--- | :---: | :---: | :--- |
| ⚡ **BlitzPack (GUI / 4 Cores)** | **20 seconds** 🚀 | **~238 MB/s** | 🏆 **21.6× FASTER than WinRAR** |
| 📦 **WinRAR 7.23 (4 Cores)** | **432 seconds** (~7.2 min) | 11.0 MB/s | Stalled on single-stream dictionary |

*WinRAR required over 7 minutes. BlitzPack finished in **20 seconds**, saving almost **7 full minutes** of waiting time!*

---

### 2. Standard Codebase Corpus: React Native Core
> **8,262 files • 132.6 MB uncompressed** (source code, configs, native `.so` binaries)

| Tool | Cold Cache Compress | Warm Cache Compress | Warm Extraction | Archive Size | Result |
| :--- | :---: | :---: | :---: | :---: | :--- |
| ⚡ **BlitzPack (`blitzpack.exe`)** | **2.39s** 🏆 | **2.29s** 🏆 | **8.82s** | **23.6 MB** | **Dominant Winner** 🥇 |
| 📦 **WinRAR 7.23** | 14.05s | 9.02s | 7.51s | **23.6 MB** | 5.88× slower on cold |
| 🗜️ **7-Zip 26.02 (.7z, LZMA2)** | 56.05s | 206.02s | 23.26s | 14.8 MB | 23× slower on cold |
| 🗜️ **7-Zip 26.02 (.zip, Deflate)** | 111.27s | 167.17s | 13.77s | 29.3 MB | 46× slower on cold |

* **Cold Compression:** BlitzPack finishes in **2.39 seconds** vs WinRAR's **14.05s** (**5.88× faster**).
* **Warm Compression:** BlitzPack finishes in **2.29 seconds** vs WinRAR's **9.02s** (**3.94× faster**).
* **Compression Ratio:** **Exact parity down to the decimal** (23.6 MB).

---

## 📦 Downloads

Prebuilt standalone binaries are available from the [Releases](https://github.com/netfreakkk/BlitzPack/releases):

| Download | Description |
| :--- | :--- |
| 💿 [**`BlitzPack-Setup.exe`**](dist/BlitzPack-Setup.exe) | **Unified Windows Installer (Recommended)**.<br>Installs both GUI and CLI, creates Desktop & Start Menu shortcuts, adds `blitzpack` to user `PATH`, and sets up Defender optimization in one click. |
| 🖥️ [**`blitzpack-gui.exe`**](dist/blitzpack-gui.exe) | Portable Desktop GUI app with dark mode & Mica blur. |
| 💻 [**`blitzpack.exe`**](dist/blitzpack.exe) | Portable single-binary CLI tool for terminal and scripts. |

---

## ✨ Features

- **🚀 Two-Tier Continuous Bundling:** Files $\ge$ 4 MB are chunked across cores; files < 4 MB are bundled in directory traversal order into ~4 MB solid blocks (up to 1,024 files each). Eliminates small-file overhead while matching solid archive compression ratios.
- **⚡ 32-Worker Parallel I/O Prefetching:** Dedicated multi-threaded I/O prefetchers saturate NVMe queue depths, completely eliminating worker thread starvation on cold storage.
- **🎯 Random-Access Extraction (`extract`):** Each chunk is independently addressable via an embedded seek table. Extract specific subdirectories or files without decompressing the rest of the archive!
- **🔁 100% Reproducible Archives (`--reproducible` / `-x`):** Guarantees byte-identical `.blitz` archives across different machines for deterministic builds.
- **🛡️ End-to-End xxHash-64 Verification:** Every chunk is checksummed during compression and verified on extraction. Whole-archive digest verified in a single linear pass.
- **🛑 Cooperative Cancellation:** Clean, responsive thread termination (`Ctrl+C`) with exit code `130` and automatic cleanup of partial files.
- **🎨 Modern Desktop GUI:** Beautiful dark-mode interface with translucent Windows 11 Mica backdrop, drag-and-drop archiving, and real-time MB/s throughput telemetry.

---

## 🛠️ CLI Usage

```bash
# Compress a directory (profiles: fast, balanced [default], high, ultra)
blitzpack compress ./my_project -o project.blitz --level balanced --workers 4

# Create a deterministic, bit-identical archive
blitzpack compress ./my_project -o project.blitz --reproducible

# Extract all files
blitzpack decompress project.blitz -o ./restored

# Random-access selective extraction (extract only specific files/folders)
blitzpack extract project.blitz src/components android/app -o ./restored

# Fast whole-archive integrity check
blitzpack verify project.blitz

# Deep frame-by-frame integrity check (decompresses and checks all checksums)
blitzpack verify project.blitz --deep

# Inspect archive manifest and chunk mapping
blitzpack list project.blitz

# Dry-run analysis and scheduling breakdown
blitzpack analyze ./my_project
```

---

## 🐍 Python Library Usage

```python
from blitzpack import compress, decompress, extract

# Compress directory
res = compress("path/to/folder", "archive.blitz", level="balanced", workers=4)
print(f"Compressed {res.total_files} files in {res.duration_seconds:.2f}s ({res.throughput_mb_s:.1f} MB/s)")

# Decompress archive
dec = decompress("archive.blitz", "path/to/output", workers=4)

# Selective extraction
extract("archive.blitz", "path/to/output", include=["src/", "package.json"])
```

---

## 🏗️ Architecture

```
┌─────────────────┐     ┌───────────────────┐     ┌─────────────────┐
│ 32 IO Prefetch  │────▶│   Bounded Queue   │────▶│    N Workers    │
│ (Disk Reader)   │     │   (JobPayload)    │     │   (zstd + xxh)  │
└─────────────────┘     └───────────────────┘     └────────┬────────┘
                                                           │
                                                  ┌────────▼────────┐
                                                  │   Main Thread   │
                                                  │   Sequential    │
                                                  │   Archive Writer│
                                                  └─────────────────┘
```

For the complete binary specification, byte layouts, struct formats, and invariants, see [**`FORMAT.md`**](FORMAT.md).

---

## 📄 License

MIT License © BlitzPack Contributors.
