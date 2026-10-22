"""
Unit tests for ModelResolver, covering workflow parsing, explicit model lists,
and actionable error reporting for unresolved models.
"""

import os
import shutil
import tempfile
import unittest
import sys

# Add worker to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "worker")))

from model_cache.manifest import ModelManifest
from model_cache.model_resolver import ModelResolver, UnresolvedModelError


class TestModelResolver(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.manifest_data = {
            "version": "1.0",
            "models": [
                {
                    "model_id": "sd15",
                    "gdrive_file_id": "1A2B3C4D5E6F7G8H9I0J",
                    "category": "checkpoints",
                    "filename": "v1-5-pruned-emaonly.safetensors",
                },
                {
                    "model_id": "lcm_lora",
                    "gdrive_file_id": "2A2B3C4D5E6F7G8H9I0J",
                    "category": "loras",
                    "filename": "lcm-lora-sdv1-5.safetensors",
                },
                {
                    "model_id": "sd_vae",
                    "gdrive_file_id": "3A2B3C4D5E6F7G8H9I0J",
                    "category": "vae",
                    "filename": "vae-ft-mse-840000-ema-pruned.safetensors",
                }
            ]
        }
        self.manifest = ModelManifest.from_dict(self.manifest_data)
        self.resolver = ModelResolver(manifest=self.manifest, base_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_resolve_missing_workflow_models(self):
        """Workflow referencing allowlisted models missing from disk returns them for download."""
        workflow = {
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "v1-5-pruned-emaonly.safetensors"}
            },
            "10": {
                "class_type": "LoraLoader",
                "inputs": {"lora_name": "lcm-lora-sdv1-5.safetensors", "model": ["4", 0]}
            }
        }

        needed = self.resolver.resolve_job_models(workflow=workflow)
        self.assertEqual(len(needed), 2)
        filenames = {m.filename for m in needed}
        self.assertEqual(filenames, {"v1-5-pruned-emaonly.safetensors", "lcm-lora-sdv1-5.safetensors"})

    def test_models_already_on_disk_are_skipped(self):
        """Models physically present in ComfyUI folders are not queued for download."""
        ckpt_dir = os.path.join(self.temp_dir, "models", "checkpoints")
        os.makedirs(ckpt_dir, exist_ok=True)
        # Create dummy file on disk
        with open(os.path.join(ckpt_dir, "v1-5-pruned-emaonly.safetensors"), "wb") as f:
            f.write(b"existing_model")

        workflow = {
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "v1-5-pruned-emaonly.safetensors"}
            }
        }

        needed = self.resolver.resolve_job_models(workflow=workflow)
        self.assertEqual(len(needed), 0)

    def test_unresolved_unknown_model_raises_actionable_error(self):
        """Referencing an unknown model produces UnresolvedModelError with actionable details."""
        workflow = {
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "unregistered_custom_model_v9.safetensors"}
            }
        }

        with self.assertRaises(UnresolvedModelError) as ctx:
            self.resolver.resolve_job_models(workflow=workflow)

        err_msg = str(ctx.exception)
        self.assertIn("unregistered_custom_model_v9.safetensors", err_msg)
        self.assertIn("CheckpointLoaderSimple", err_msg)
        self.assertIn("model_manifest.json", err_msg)
        self.assertIn("Allowlisted manifest entries", err_msg)

    def test_explicit_model_prefetch(self):
        """Explicit model list is resolved and queued for download."""
        explicit = ["sd_vae"]
        needed = self.resolver.resolve_job_models(explicit_models=explicit)
        self.assertEqual(len(needed), 1)
        self.assertEqual(needed[0].model_id, "sd_vae")


if __name__ == "__main__":
    unittest.main()
