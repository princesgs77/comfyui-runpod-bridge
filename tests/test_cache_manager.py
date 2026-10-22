"""
Unit tests for ModelCacheManager, covering downloads, atomic caching,
interrupted downloads, checksum verification, disk space checks, and locking.
"""

import hashlib
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import sys

# Add worker to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "worker")))

from model_cache.cache_manager import (
    CacheError,
    InsufficientDiskSpaceError,
    ModelCacheManager,
)
from model_cache.manifest import ModelEntry


class TestModelCacheManager(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_gdrive = MagicMock()
        self.cache_mgr = ModelCacheManager(
            base_dir=self.temp_dir,
            gdrive_client=self.mock_gdrive,
            disk_safety_buffer_bytes=1024,  # Small buffer for testing
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_successful_download_and_cache(self):
        """Test successful download, atomic rename, and caching."""
        content = b"fake_model_safetensors_data_12345"
        expected_sha = hashlib.sha256(content).hexdigest()

        entry = ModelEntry(
            model_id="sd15",
            gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
            category="checkpoints",
            dest_rel_path="models/checkpoints",
            filename="sd15.safetensors",
            file_size=len(content),
            expected_sha256=expected_sha,
        )

        def mock_stream(file_id, dest_file_handle, expected_size, hash_callback, **kwargs):
            dest_file_handle.write(content)
            if hash_callback:
                hash_callback(content)
            return len(content)

        self.mock_gdrive.stream_download.side_effect = mock_stream

        dest_path = self.cache_mgr.ensure_model(entry)
        self.assertTrue(os.path.exists(dest_path))
        with open(dest_path, "rb") as f:
            self.assertEqual(f.read(), content)

        # Cache hit test
        self.assertTrue(self.cache_mgr.is_cached(entry))
        self.mock_gdrive.stream_download.reset_mock()
        dest_path_2 = self.cache_mgr.ensure_model(entry)
        self.assertEqual(dest_path, dest_path_2)
        # Should NOT call stream_download on cache hit
        self.mock_gdrive.stream_download.assert_not_called()

    def test_interrupted_download_cleanup(self):
        """Test that an interrupted download cleans up .tmp file and does not leave valid cache."""
        entry = ModelEntry(
            model_id="sd15",
            gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
            category="checkpoints",
            dest_rel_path="models/checkpoints",
            filename="sd15_interrupted.safetensors",
            file_size=1000,
        )

        def mock_failing_stream(file_id, dest_file_handle, **kwargs):
            dest_file_handle.write(b"partial bytes")
            raise ConnectionResetError("Network dropped")

        self.mock_gdrive.stream_download.side_effect = mock_failing_stream

        with self.assertRaises(ConnectionResetError):
            self.cache_mgr.ensure_model(entry)

        dest_path = self.cache_mgr.get_dest_path(entry)
        self.assertFalse(os.path.exists(dest_path))
        self.assertFalse(self.cache_mgr.is_cached(entry))

        # Ensure no lingering .tmp files in destination folder
        dest_dir = os.path.dirname(dest_path)
        tmp_files = [f for f in os.listdir(dest_dir) if ".tmp." in f]
        self.assertEqual(len(tmp_files), 0)

    def test_checksum_mismatch(self):
        """Test that checksum verification failure raises CacheError and deletes file."""
        content = b"corrupted_model_data"
        wrong_sha = "0" * 64

        entry = ModelEntry(
            model_id="sd15",
            gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
            category="checkpoints",
            dest_rel_path="models/checkpoints",
            filename="sd15_corrupt.safetensors",
            file_size=len(content),
            expected_sha256=wrong_sha,
        )

        def mock_stream(file_id, dest_file_handle, hash_callback, **kwargs):
            dest_file_handle.write(content)
            if hash_callback:
                hash_callback(content)
            return len(content)

        self.mock_gdrive.stream_download.side_effect = mock_stream

        with self.assertRaises(CacheError) as ctx:
            self.cache_mgr.ensure_model(entry)
        self.assertIn("Checksum verification failed", str(ctx.exception))

        dest_path = self.cache_mgr.get_dest_path(entry)
        self.assertFalse(os.path.exists(dest_path))

    @patch("shutil.disk_usage")
    def test_insufficient_disk_space(self, mock_disk_usage):
        """Test that insufficient disk space raises InsufficientDiskSpaceError before download."""
        # 1000 bytes free, but 5000 required (+ 1024 buffer)
        mock_disk_usage.return_value = (100000, 99000, 1000)

        entry = ModelEntry(
            model_id="large_model",
            gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
            category="checkpoints",
            dest_rel_path="models/checkpoints",
            filename="large.safetensors",
            file_size=5000,
        )

        with self.assertRaises(InsufficientDiskSpaceError) as ctx:
            self.cache_mgr.ensure_model(entry)
        self.assertIn("Insufficient disk space", str(ctx.exception))
        # Ensure download was not even attempted
        self.mock_gdrive.stream_download.assert_not_called()

    def test_duplicate_download_locking(self):
        """Test that lock file prevents concurrent duplicate download of the same file."""
        entry = ModelEntry(
            model_id="sd15",
            gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
            category="checkpoints",
            dest_rel_path="models/checkpoints",
            filename="sd15_locked.safetensors",
            file_size=10,
        )

        dest_path = self.cache_mgr.get_dest_path(entry)
        dest_dir = os.path.dirname(dest_path)
        os.makedirs(dest_dir, exist_ok=True)
        lock_file = f"{dest_path}.lock"

        # Simulate an ongoing download by another worker by placing a fresh lock file
        with open(lock_file, "w") as f:
            f.write("pid=99999")

        # Mock cache hit after small wait
        def fake_wait(*args, **kwargs):
            # Once lock is checked, remove lock and write target file as if finished
            if os.path.exists(lock_file):
                os.remove(lock_file)
            with open(dest_path, "wb") as f:
                f.write(b"0123456789")

        with patch("time.sleep", side_effect=fake_wait):
            result = self.cache_mgr.ensure_model(entry)
            self.assertEqual(result, dest_path)
            self.assertFalse(os.path.exists(lock_file))


if __name__ == "__main__":
    unittest.main()
