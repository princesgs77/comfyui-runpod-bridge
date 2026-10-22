"""
Unit tests for ModelManifest and ModelEntry validation,
covering valid entries, unknown models, and path traversal rejection.
"""

import unittest
import sys
import os

# Add worker to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "worker")))

from model_cache.manifest import (
    ModelManifest,
    ModelEntry,
    ManifestValidationError,
)


class TestModelManifest(unittest.TestCase):

    def test_valid_manifest_entry(self):
        """Test creating and validating a legitimate manifest entry."""
        entry = ModelEntry(
            model_id="checkpoints/sd15.safetensors",
            gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
            category="checkpoints",
            dest_rel_path="models/checkpoints",
            filename="sd15.safetensors",
            file_size=4000000000,
            expected_sha256="a" * 64,
            description="SD 1.5 Base Model",
        )
        # Should validate without raising
        entry.validate()
        self.assertEqual(entry.filename, "sd15.safetensors")

    def test_unknown_model_category_rejection(self):
        """Reject entry with unknown model category."""
        entry = ModelEntry(
            model_id="malicious/exec.bin",
            gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
            category="unauthorized_category",
            dest_rel_path="models/unauthorized",
            filename="exec.bin",
        )
        with self.assertRaises(ManifestValidationError) as ctx:
            entry.validate()
        self.assertIn("Unknown model category", str(ctx.exception))

    def test_path_traversal_in_filename_rejection(self):
        """Reject filename attempting path traversal (slashes, backslashes, ..)."""
        bad_filenames = [
            "../../etc/passwd.safetensors",
            "../models/root.safetensors",
            "folder/model.safetensors",
            "folder\\model.safetensors",
            "..",
            "model.exe",  # unsupported extension
        ]
        for bad_name in bad_filenames:
            with self.subTest(filename=bad_name):
                entry = ModelEntry(
                    model_id=f"test/{bad_name}",
                    gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
                    category="checkpoints",
                    dest_rel_path="models/checkpoints",
                    filename=bad_name,
                )
                with self.assertRaises(ManifestValidationError):
                    entry.validate()

    def test_path_traversal_in_dest_rel_path_rejection(self):
        """Reject dest_rel_path attempting directory escape."""
        bad_dest_paths = [
            "/etc/models",
            "../models/checkpoints",
            "models/../../secret",
            "..",
        ]
        for bad_path in bad_dest_paths:
            with self.subTest(dest_path=bad_path):
                entry = ModelEntry(
                    model_id="test/safe.safetensors",
                    gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
                    category="checkpoints",
                    dest_rel_path=bad_path,
                    filename="safe.safetensors",
                )
                with self.assertRaises(ManifestValidationError):
                    entry.validate()

    def test_invalid_gdrive_id_rejection(self):
        """Reject non-alphanumeric or short/malformed Google Drive file IDs."""
        bad_ids = [
            "",
            "short",
            "../../escaped",
            "has spaces in id 12345",
            "https://drive.google.com/file/d/123",  # raw URL not allowed
        ]
        for bad_id in bad_ids:
            with self.subTest(gdrive_id=bad_id):
                entry = ModelEntry(
                    model_id="test/safe.safetensors",
                    gdrive_file_id=bad_id,
                    category="checkpoints",
                    dest_rel_path="models/checkpoints",
                    filename="safe.safetensors",
                )
                with self.assertRaises(ManifestValidationError):
                    entry.validate()

    def test_invalid_sha256_rejection(self):
        """Reject malformed checksums."""
        bad_hashes = [
            "123",
            "not_a_hash",
            "g" * 64,  # invalid hex
            "a" * 63,  # wrong length
        ]
        for bad_hash in bad_hashes:
            with self.subTest(sha256=bad_hash):
                entry = ModelEntry(
                    model_id="test/safe.safetensors",
                    gdrive_file_id="1A2B3C4D5E6F7G8H9I0J",
                    category="checkpoints",
                    dest_rel_path="models/checkpoints",
                    filename="safe.safetensors",
                    expected_sha256=bad_hash,
                )
                with self.assertRaises(ManifestValidationError):
                    entry.validate()

    def test_manifest_from_dict_and_resolve(self):
        """Test constructing manifest from dictionary and resolving models."""
        data = {
            "version": "1.0",
            "models": [
                {
                    "model_id": "sd15",
                    "gdrive_file_id": "1A2B3C4D5E6F7G8H9I0J",
                    "category": "checkpoints",
                    "filename": "v1-5-pruned.safetensors",
                    "file_size": 2048,
                },
                {
                    "model_id": "lora_anime",
                    "gdrive_file_id": "2A2B3C4D5E6F7G8H9I0J",
                    "category": "loras",
                    "filename": "anime.safetensors",
                }
            ]
        }
        manifest = ModelManifest.from_dict(data)
        self.assertEqual(len(manifest.list_entries()), 2)

        # Resolve by ID
        entry1 = manifest.resolve("sd15")
        self.assertIsNotNone(entry1)
        self.assertEqual(entry1.filename, "v1-5-pruned.safetensors")

        # Resolve by filename
        entry2 = manifest.resolve("anime.safetensors")
        self.assertIsNotNone(entry2)
        self.assertEqual(entry2.model_id, "lora_anime")

        # Unknown model returns None
        self.assertIsNone(manifest.resolve("unknown_model.safetensors"))


if __name__ == "__main__":
    unittest.main()
