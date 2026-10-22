"""
Model Cache Manager for local worker temporary disk.
Handles disk space validation, atomic streaming downloads, SHA-256 verification,
duplicate download locking, and cache hit checks.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
import uuid
from typing import Dict, List, Optional, Set

from .gdrive import GoogleDriveClient, GoogleDriveError
from .manifest import ModelEntry, ModelManifest

logger = logging.getLogger("worker.model_cache.cache_manager")

# Default safety buffer for disk space (1 GB)
DEFAULT_DISK_SAFETY_BUFFER_BYTES = 1024 * 1024 * 1024


class CacheError(Exception):
    """Base error for model cache operations."""
    pass


class InsufficientDiskSpaceError(CacheError):
    """Raised when the worker temporary disk does not have enough capacity for a model."""
    pass


class ModelCacheManager:
    """Manages downloading, verifying, caching, and locking of models on worker disk."""

    def __init__(
        self,
        base_dir: str = "/comfyui",
        manifest: Optional[ModelManifest] = None,
        gdrive_client: Optional[GoogleDriveClient] = None,
        disk_safety_buffer_bytes: int = DEFAULT_DISK_SAFETY_BUFFER_BYTES,
    ):
        self.base_dir = os.path.abspath(base_dir)
        self.manifest = manifest
        self.gdrive_client = gdrive_client or GoogleDriveClient()
        self.disk_safety_buffer_bytes = disk_safety_buffer_bytes
        self._active_models: Set[str] = set()

    def get_dest_path(self, entry: ModelEntry) -> str:
        """Resolve and validate the absolute destination path for a model entry."""
        target_dir = os.path.normpath(os.path.join(self.base_dir, entry.dest_rel_path))
        # Ensure target_dir stays inside base_dir
        if not target_dir.startswith(self.base_dir):
            raise CacheError(f"Target directory {target_dir} escapes base directory {self.base_dir}")

        dest_path = os.path.normpath(os.path.join(target_dir, entry.filename))
        if not dest_path.startswith(target_dir):
            raise CacheError(f"Destination path {dest_path} escapes target directory {target_dir}")

        return dest_path

    def is_cached(self, entry: ModelEntry) -> bool:
        """Check if a model file is already fully downloaded and cached."""
        dest_path = self.get_dest_path(entry)
        if not os.path.exists(dest_path):
            return False

        # If expected file_size is defined, ensure file is complete
        if entry.file_size is not None:
            actual_size = os.path.getsize(dest_path)
            if actual_size != entry.file_size:
                logger.warning(
                    "Cached model %s has unexpected size (%d vs expected %d). Invalidating.",
                    entry.model_id,
                    actual_size,
                    entry.file_size,
                )
                return False

        return True

    def check_disk_space(self, dest_dir: str, required_bytes: Optional[int]) -> None:
        """Verify that the target volume has sufficient free capacity."""
        if not required_bytes or required_bytes <= 0:
            return

        os.makedirs(dest_dir, exist_ok=True)
        try:
            total, used, free = shutil.disk_usage(dest_dir)
        except Exception as e:
            logger.warning("Could not check disk usage for %s: %s", dest_dir, e)
            return

        needed = required_bytes + self.disk_safety_buffer_bytes
        if free < needed:
            free_gb = free / (1024**3)
            req_gb = required_bytes / (1024**3)
            buf_gb = self.disk_safety_buffer_bytes / (1024**3)
            raise InsufficientDiskSpaceError(
                f"Insufficient disk space in '{dest_dir}': {free_gb:.2f} GB free, "
                f"but {req_gb:.2f} GB required (+ {buf_gb:.2f} GB safety margin)."
            )

    def ensure_model(self, entry: ModelEntry) -> str:
        """
        Ensure a model is present on the worker's disk.
        Returns the absolute path to the ready-to-use model file.
        """
        dest_path = self.get_dest_path(entry)
        dest_dir = os.path.dirname(dest_path)
        os.makedirs(dest_dir, exist_ok=True)

        # 1. Check cache hit
        if self.is_cached(entry):
            logger.info("Model Cache Hit: '%s' is already available at %s", entry.model_id, dest_path)
            self._active_models.add(dest_path)
            return dest_path

        # 2. Prevent concurrent downloads of the same file
        lock_file = f"{dest_path}.lock"
        start_lock_wait = time.time()
        while os.path.exists(lock_file):
            # Check if lock is stale (> 30 minutes)
            try:
                mtime = os.path.getmtime(lock_file)
                if time.time() - mtime > 1800:
                    logger.warning("Removing stale lock file: %s", lock_file)
                    os.remove(lock_file)
                    break
            except OSError:
                break

            logger.info("Another process is downloading '%s'. Waiting...", entry.model_id)
            time.sleep(2)
            if self.is_cached(entry):
                self._active_models.add(dest_path)
                return dest_path
            if time.time() - start_lock_wait > 900:  # 15 minutes timeout
                raise CacheError(f"Timed out waiting for concurrent download of {entry.model_id}")

        # Create lock file
        try:
            with open(lock_file, "w") as f:
                f.write(f"pid={os.getpid()} time={time.time()}")
        except Exception as e:
            logger.warning("Could not write lock file %s: %s", lock_file, e)

        tmp_path = f"{dest_path}.tmp.{uuid.uuid4().hex}"
        hasher = hashlib.sha256() if entry.expected_sha256 else None

        try:
            # 3. Check disk space before streaming
            self.check_disk_space(dest_dir, entry.file_size)

            logger.info(
                "Streaming download of model '%s' (Drive ID: %s) to %s",
                entry.model_id,
                entry.gdrive_file_id,
                dest_path,
            )

            # 4. Stream download to temporary file
            with open(tmp_path, "wb") as f:
                bytes_written = self.gdrive_client.stream_download(
                    file_id=entry.gdrive_file_id,
                    dest_file_handle=f,
                    expected_size=entry.file_size,
                    hash_callback=hasher.update if hasher else None,
                )

            # 5. Validate file size if configured
            if entry.file_size is not None and bytes_written != entry.file_size:
                raise CacheError(
                    f"Downloaded size ({bytes_written} bytes) did not match expected size "
                    f"({entry.file_size} bytes) for model '{entry.model_id}'."
                )

            # 6. Validate SHA-256 checksum if configured
            if entry.expected_sha256 and hasher:
                actual_sha256 = hasher.hexdigest().lower()
                expected_sha256 = entry.expected_sha256.lower()
                if actual_sha256 != expected_sha256:
                    raise CacheError(
                        f"Checksum verification failed for model '{entry.model_id}'. "
                        f"Expected SHA-256: {expected_sha256}, calculated: {actual_sha256}"
                    )
                logger.info("Checksum verified successfully for model '%s'", entry.model_id)

            # 7. Atomic rename
            os.replace(tmp_path, dest_path)
            logger.info("Model '%s' successfully cached at %s (%d bytes)", entry.model_id, dest_path, bytes_written)
            self._active_models.add(dest_path)
            return dest_path

        except Exception as e:
            # Clean up incomplete temporary download
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            logger.error("Download failed for model '%s': %s", entry.model_id, e)
            raise

        finally:
            # Always clean up lock file
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                except OSError:
                    pass

    def release_active(self, dest_path: str) -> None:
        """Mark model as no longer actively being used by the current job."""
        self._active_models.discard(dest_path)

    def prune_cache_if_needed(self, max_total_bytes: int) -> int:
        """
        Optional cache pruning. Removes oldest cached models if total cache size
        exceeds max_total_bytes. NEVER deletes actively running models or unrelated files.
        """
        if max_total_bytes <= 0:
            return 0

        # Scan models directory
        models_root = os.path.join(self.base_dir, "models")
        if not os.path.exists(models_root):
            return 0

        cached_files = []
        total_size = 0
        for root, _, files in os.walk(models_root):
            for file in files:
                if file.endswith((".lock", ".tmp")):
                    continue
                full_path = os.path.join(root, file)
                try:
                    st = os.stat(full_path)
                    cached_files.append((st.st_atime, st.st_size, full_path))
                    total_size += st.st_size
                except OSError:
                    continue

        if total_size <= max_total_bytes:
            return 0

        # Sort by access time ascending (oldest first)
        cached_files.sort(key=lambda x: x[0])
        freed = 0
        for atime, size, path in cached_files:
            if total_size - freed <= max_total_bytes:
                break
            # NEVER delete an active model
            if path in self._active_models:
                continue

            try:
                os.remove(path)
                freed += size
                logger.info("Pruned old cached model %s (freed %d bytes)", path, size)
            except OSError as e:
                logger.warning("Could not prune %s: %s", path, e)

        return freed
