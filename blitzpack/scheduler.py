"""Work scheduler implementing strict sequential disk-order dispatch and continuous bundling.

By yielding jobs in exact manifest (directory) order, we preserve OS read-ahead cache
locality. Small and medium files are aggregated into solid bundles purely by sequential
proximity rather than extension, acting like a tarball for maximum disk sequentiality.
"""

from dataclasses import dataclass, field
from typing import List, Optional
from .analyzer import FileEntry, FileManifest

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024       # 4 MB
DEFAULT_BUNDLE_TARGET = 4 * 1024 * 1024    # 4 MB
DEFAULT_MAX_BUNDLE_MEMBERS = 256           # Maximum files in a single solid bundle


@dataclass(slots=True)
class BundleMember:
    entry: FileEntry
    offset_in_bundle: int
    size: int


@dataclass(slots=True)
class CompressionJob:
    job_id: int
    job_type: str                         # "chunk", "bundle"
    estimated_size: int
    # For bundles
    bundle_members: List[BundleMember] = field(default_factory=list)
    # For chunk jobs
    source_entry: Optional[FileEntry] = None
    offset: int = 0
    length: int = 0


class WorkScheduler:
    """Transforms a manifest into a strictly ordered sequence of compression jobs."""

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        bundle_target: int = DEFAULT_BUNDLE_TARGET,
        max_bundle_members: int = DEFAULT_MAX_BUNDLE_MEMBERS,
    ) -> None:
        self.chunk_size = chunk_size
        self.bundle_target = bundle_target
        self.max_bundle_members = max_bundle_members

    def schedule(self, manifest: FileManifest) -> List[CompressionJob]:
        jobs: List[CompressionJob] = []
        next_job_id = 0

        current_bundle: List[BundleMember] = []
        current_bundle_size = 0

        def flush_bundle():
            nonlocal next_job_id, current_bundle, current_bundle_size
            if current_bundle:
                jobs.append(CompressionJob(
                    job_id=next_job_id,
                    job_type="bundle",
                    estimated_size=current_bundle_size,
                    bundle_members=current_bundle
                ))
                next_job_id += 1
                current_bundle = []
                current_bundle_size = 0

        for entry in manifest.entries:
            if entry.file_type != 0 or entry.size == 0:
                continue  # Skip dirs, symlinks, empty files

            if entry.size >= self.chunk_size:
                # Flush pending bundle before processing a large file
                # to strictly maintain sequential order
                flush_bundle()

                # Chunk the large file
                curr_offset = 0
                while curr_offset < entry.size:
                    length = min(self.chunk_size, entry.size - curr_offset)
                    jobs.append(CompressionJob(
                        job_id=next_job_id,
                        job_type="chunk",
                        estimated_size=length,
                        source_entry=entry,
                        offset=curr_offset,
                        length=length,
                    ))
                    next_job_id += 1
                    curr_offset += length

            else:
                # Add to current solid bundle
                current_bundle.append(BundleMember(
                    entry=entry,
                    offset_in_bundle=current_bundle_size,
                    size=entry.size
                ))
                current_bundle_size += entry.size

                if current_bundle_size >= self.bundle_target or len(current_bundle) >= self.max_bundle_members:
                    flush_bundle()

        # Flush any remaining items
        flush_bundle()

        # Jobs are kept exactly in manifest (directory traversal) order!
        return jobs
