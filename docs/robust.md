BlitzPack — Robustness & Hardening Milestone
Goal: make extraction safe against hostile/corrupt archives, fix the two loose ends from the last review, and add regression tests — so the functionality is trustworthy before any UI work. Everything here is backward compatible: valid archives written by the current code still read and extract identically. These changes only reject inputs that were already unsafe or malformed.

Apply in this order; each commit leaves tests green.

1. Path traversal (Zip Slip) on extraction — SECURITY
2. Untrusted-archive DoS in the reader — SECURITY
3. Symlink extraction safety — SECURITY
4. Wire the dead --readers flag
5. Make the CI lint job pass
6. Tests
7. Commit sequence
1. Path traversal (Zip Slip) on extraction — SECURITY
decompress() builds out_p / entry.path and trusts the manifest. A crafted archive with entry.path = "../../evil.exe", an absolute path, or a Windows drive letter can make BlitzPack write outside the destination directory. This is the most important fix in the repo — extraction of an untrusted archive can currently overwrite arbitrary files.

All edits are in blitzpack/decompressor.py, inside decompress().

1a. Add a validated-join helper right after out_p is resolved
Find the top of decompress():

    arc_p = Path(archive_path).resolve()
    out_p = Path(output_dir).resolve()
    num_workers = workers or os.cpu_count() or 4
    created_output_root = not out_p.exists()
Add immediately below it:

    out_root = out_p  # already resolved above

    def _safe_target(rel_path: str) -> Path:
        """Resolve an archive path under out_root, refusing any escape (Zip Slip)."""
        resolved = (out_root / rel_path).resolve()
        if resolved != out_root and not resolved.is_relative_to(out_root):
            raise ArchiveFormatError(
                f"Refusing to extract outside destination (path traversal): {rel_path!r}"
            )
        return resolved
is_relative_to is available on Python 3.9+ and you require 3.10, so this is fine. A normal relative path resolves under out_root and passes; .., absolute paths, and C:\... drive-letter paths all reset or climb out of out_root and are rejected.

1b. Use it in the directory-creation loop (Section 2)
Replace:

    seen_dirs = set()
    for entry in reader.manifest:
        target_path = out_p / entry.path
        sanitized_target = sanitize_windows_path(target_path)
with:

    seen_dirs = set()
    for entry in reader.manifest:
        target_path = _safe_target(entry.path)
        sanitized_target = sanitize_windows_path(target_path)
This makes the traversal check fire before any os.makedirs, so a hostile archive can't even create a directory outside the destination.

1c. Use it when building the extract targets (Section 3)
Replace:

    for entry in reader.manifest:
        if entry.file_type != 0 or entry.size == 0:
            continue

        target_path = out_p / entry.path
with:

    for entry in reader.manifest:
        if entry.file_type != 0 or entry.size == 0:
            continue

        target_path = _safe_target(entry.path)
While here, also bounds-check the single-chunk index (the multi-chunk branch already has a check; the single-chunk branch can KeyError on a corrupt start_chunk). Replace the else: single-chunk branch:

        else:
            chunk_to_targets[entry.start_chunk].append(
with:

        else:
            if not 0 <= entry.start_chunk < len(reader.seek_entries):
                raise ArchiveFormatError(
                    f"Manifest entry {entry.path!r} references out-of-range chunk {entry.start_chunk}"
                )
            chunk_to_targets[entry.start_chunk].append(
1d. Use it in the metadata-restore loop (Section 5)
Replace:

    for entry in reader.manifest:
        target_path = out_p / entry.path
        sanitized_target = sanitize_windows_path(target_path)
with:

    for entry in reader.manifest:
        target_path = _safe_target(entry.path)
        sanitized_target = sanitize_windows_path(target_path)
That's the whole traversal fix — every place that turns entry.path into a filesystem path now goes through _safe_target.

2. Untrusted-archive DoS in the reader — SECURITY
BlitzArchiveReader._read_structure reads a uint32 seek-table count and a manifest region whose length comes from attacker-controlled offsets, with no sanity checks. A crafted footer can point the reader at absurd offsets or claim billions of entries, causing huge allocations / crashes. Add cheap bounds checks — all in blitzpack/archive_format.py, method _read_structure.

2a. Validate the footer offsets before trusting them
After the block that builds self.footer (the self.footer = ArchiveFooter(...)), add:

        # Sanity-bound the offsets from the (possibly hostile) footer.
        if not (HEADER_STRUCT.size <= self.footer.seek_table_offset <= file_len - FOOTER_STRUCT.size):
            raise ArchiveFormatError(
                f"Seek table offset {self.footer.seek_table_offset} outside file bounds"
            )
        if self.footer.manifest_offset != 0 and not (
            self.footer.seek_table_offset <= self.footer.manifest_offset <= file_len - FOOTER_STRUCT.size
        ):
            raise ArchiveFormatError(
                f"Manifest offset {self.footer.manifest_offset} outside file bounds"
            )
2b. Bound the seek-table entry count against physical space
Replace:

        num_entries = struct.unpack("<I", raw_count)[0]

        for _ in range(num_entries):
with:

        num_entries = struct.unpack("<I", raw_count)[0]

        # A seek entry is fixed-size; reject counts that can't physically fit.
        table_region_end = (
            self.footer.manifest_offset
            if self.footer.manifest_offset > 0
            else file_len - FOOTER_STRUCT.size
        )
        max_possible = max(0, (table_region_end - (self.footer.seek_table_offset + 4)) // SEEK_ENTRY_STRUCT.size)
        if num_entries > max_possible:
            raise ArchiveFormatError(
                f"Seek table claims {num_entries} entries but only {max_possible} fit in the file"
            )

        for _ in range(num_entries):
2c. Harden the manifest unpack
Replace:

            manifest_data = self._stream.read(manifest_len)
            raw_manifest = msgpack.unpackb(manifest_data, raw=False)
with:

            if manifest_len < 0:
                raise ArchiveFormatError("Negative manifest length (corrupt offsets)")
            manifest_data = self._stream.read(manifest_len)
            if len(manifest_data) != manifest_len:
                raise ArchiveFormatError("Truncated manifest region")
            try:
                # msgpack's default max_*_len are bounded by len(manifest_data), so a
                # bounded slice already bounds allocation; strict_map_key keeps our
                # short integer-ish keys working across versions.
                raw_manifest = msgpack.unpackb(manifest_data, raw=False, strict_map_key=False)
            except (ValueError, msgpack.exceptions.UnpackException) as exc:
                raise ArchiveFormatError(f"Corrupt manifest: {exc}") from exc
            if not isinstance(raw_manifest, list):
                raise ArchiveFormatError("Manifest is not a list")
Wrap the per-entry construction (for d in raw_manifest: self.manifest.append(...)) so a missing key is a clean format error rather than a KeyError:

            for d in raw_manifest:
                try:
                    self.manifest.append(ManifestEntry(
                        path=d["p"], size=d["s"], mtime=d["t"], file_type=d["y"],
                        symlink_target=d.get("l"), permissions=d["m"], win_attrs=d["w"],
                        start_chunk=d["sc"], start_offset=d["so"],
                        end_chunk=d["ec"], end_offset=d["eo"],
                    ))
                except (KeyError, TypeError) as exc:
                    raise ArchiveFormatError(f"Malformed manifest entry: {exc}") from exc
(msgpack.exceptions is importable as msgpack.exceptions once import msgpack is present, which it already is at the top of the file.)

3. Symlink extraction safety — SECURITY
Restoring os.symlink(entry.symlink_target, ...) from an untrusted archive can create a link pointing anywhere on disk (e.g. target /etc/passwd or ..\..\Windows). Default to refusing links that escape the destination; make escapes opt-in.

In blitzpack/decompressor.py, add a parameter to decompress():

def decompress(
    archive_path: Path | str,
    output_dir: Path | str,
    workers: int = 0,
    verify_first: bool = False,
    cleanup_on_error: bool = False,
    allow_external_symlinks: bool = False,
    progress_callback: Optional[ProgressCallback] = None,
) -> DecompressionResult:
Then in Section 5's symlink branch, replace:

        if entry.file_type == 2 and entry.symlink_target:
            try:
                if os.path.islink(sanitized_target) or os.path.exists(sanitized_target):
                    os.remove(sanitized_target)
                os.symlink(entry.symlink_target, sanitized_target)
            except OSError:
                pass
with:

        if entry.file_type == 2 and entry.symlink_target:
            # Where would this link resolve to? Refuse targets that escape out_root.
            link_dest = (target_path.parent / entry.symlink_target).resolve()
            if not allow_external_symlinks and not link_dest.is_relative_to(out_root):
                skipped_symlinks += 1
                continue
            try:
                if os.path.islink(sanitized_target) or os.path.exists(sanitized_target):
                    os.remove(sanitized_target)
                os.symlink(entry.symlink_target, sanitized_target)
            except OSError:
                pass
Initialize the counter just before that loop (# 5. Restore ...):

    skipped_symlinks = 0
Optional but nice: surface it. Add skipped_symlinks: int = 0 to DecompressionResult and pass skipped_symlinks=skipped_symlinks in the return, so the CLI/GUI can warn "N unsafe symlinks skipped." If you don't want the dataclass change yet, at least the links are no longer silently created outside the destination.

4. Wire the dead --readers flag
The flag is parsed but never forwarded (confirmed in the last review). In cli.py, handle_compress, find the compress(...) call and add readers=args.readers:

        res = compress(
            input_path=in_path,
            output_path=out_path,
            level=level,
            workers=workers,
            readers=args.readers,
            progress_callback=on_progress,
        )
5. Make the CI lint job pass
ci.yml runs ruff check . but there's no ruff config and the tree has pre-existing unused imports (F401), so the lint job goes red. Two steps:

5a. Add a ruff config to pyproject.toml
[tool.ruff]
line-length = 110
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B"]
ignore = ["E501"]  # long theme dicts / docstrings in gui.py
5b. Remove the unused imports
The reliable way — let ruff fix every file at once, then eyeball the diff:

ruff check . --fix
git diff        # review before committing
For reference, the confirmed unused imports in the package (in case you prefer manual):

blitzpack/analyzer.py: from typing import Dict, List, Optional, Set → from typing import List, Optional
blitzpack/archive_format.py: drop import io; from typing import BinaryIO, Dict, Generator, List, Optional → drop Generator
gui.py: gc, time, and possibly others — let --fix handle it.
If you'd rather not clean gui.py right now, make the lint advisory instead of blocking by changing the last line of ci.yml to run: ruff check . --exit-zero — but fixing is better and cheap.

6. Tests
Add to tests/test_integrity.py. These lock in the security fixes so a future refactor can't silently reintroduce them.

import struct
from blitzpack.archive_format import (
    BlitzArchiveWriter, BlitzArchiveReader, ManifestEntry, ArchiveFormatError, FLAG_STORED,
)
from blitzpack.checksum import compute_digest


def _build_archive_with_manifest_path(archive_path, rel_path):
    """Write a minimal valid archive whose single file claims `rel_path`."""
    data = b"pwned"
    with open(archive_path, "wb") as f:
        w = BlitzArchiveWriter(f)
        w.write_chunk(0, data, len(data), compute_digest(data), 0, FLAG_STORED)
        w.finalize(
            [ManifestEntry(
                path=rel_path, size=len(data), mtime=0.0, file_type=0,
                symlink_target=None, permissions=0o644, win_attrs=0,
                start_chunk=0, start_offset=0, end_chunk=0, end_offset=len(data),
            )],
            total_original_size=len(data),
        )


def test_path_traversal_is_blocked(tmp_path: Path):
    """A manifest path escaping the destination must be refused, and nothing written out."""
    archive = tmp_path / "evil.blitz"
    _build_archive_with_manifest_path(archive, "../escaped.txt")

    dest = tmp_path / "out"
    with pytest.raises(ArchiveFormatError):
        decompress(archive, dest)

    assert not (tmp_path / "escaped.txt").exists()


def test_absolute_path_is_blocked(tmp_path: Path):
    archive = tmp_path / "evil2.blitz"
    # An absolute-looking POSIX path; on Windows use a drive path to prove the same point.
    bad = "/tmp/escaped_abs.txt" if os.name != "nt" else "C:/Windows/Temp/escaped_abs.txt"
    _build_archive_with_manifest_path(archive, bad)

    dest = tmp_path / "out2"
    with pytest.raises(ArchiveFormatError):
        decompress(archive, dest)


def test_absurd_seek_count_rejected(tmp_path: Path):
    """A corrupt seek-table count must be rejected, not turned into a huge allocation."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello")
    archive = tmp_path / "a.blitz"
    compress(src, archive, level=1)

    with open(archive, "rb") as f:
        off = BlitzArchiveReader(f).footer.seek_table_offset

    with open(archive, "r+b") as f:
        f.seek(off)
        f.write(struct.pack("<I", 0xFFFFFFFF))  # claim 4 billion entries

    with open(archive, "rb") as f:
        with pytest.raises(ArchiveFormatError):
            BlitzArchiveReader(f)


def test_truncated_archive_rejected(tmp_path: Path):
    """A file cut off mid-stream must raise a clean format error, not crash."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x" * 5000)
    archive = tmp_path / "a.blitz"
    compress(src, archive, level=1)

    full = archive.read_bytes()
    (tmp_path / "cut.blitz").write_bytes(full[: len(full) // 2])

    with open(tmp_path / "cut.blitz", "rb") as f:
        with pytest.raises(ArchiveFormatError):
            BlitzArchiveReader(f)
Optional POSIX-only symlink test:

@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
def test_external_symlink_skipped(tmp_path: Path):
    """A symlink pointing outside the destination must not be created by default."""
    archive = tmp_path / "link.blitz"
    data = b""
    with open(archive, "wb") as f:
        w = BlitzArchiveWriter(f)
        # one stored empty chunk so the archive is structurally valid
        w.write_chunk(0, data, 0, compute_digest(data), 0, FLAG_STORED)
        w.finalize(
            [ManifestEntry(
                path="link", size=0, mtime=0.0, file_type=2,
                symlink_target="/etc/passwd", permissions=0o777, win_attrs=0,
                start_chunk=-1, start_offset=0, end_chunk=-1, end_offset=0,
            )],
            total_original_size=0,
        )

    dest = tmp_path / "out"
    decompress(archive, dest)  # should not raise
    assert not (dest / "link").is_symlink()
7. Commit sequence
fix(security): reject path traversal (Zip Slip) on extraction — §1
fix(security): bound seek count and manifest against hostile archives — §2
fix(security): refuse symlinks that escape the destination by default — §3
fix(cli): actually pass --readers through to compress() — §4
ci: add ruff config and clear unused imports so lint passes — §5
test: cover traversal, absolute paths, corrupt counts, truncation, symlinks — §6
After this milestone, extraction is safe against untrusted input, the reader degrades gracefully on corruption, CI is green, and --readers works. That's the robust base to build the UI and the promising features (selective/random-access extraction is the natural next one — the format already supports it) on top of.

Quick note on what this does and doesn't cover
This milestone is hardening, not new capability. It makes BlitzPack safe to point at an archive you didn't create. It deliberately leaves for the next milestone:

Selective extraction (extract <archive> <path>) — the format's big differentiator.
The performance work (parallel decompression writer, overlapped scan, dictionaries).
Encryption.
Do the hardening first (this file), then we build outward.
