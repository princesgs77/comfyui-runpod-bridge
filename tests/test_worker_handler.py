"""
Synthetic Integration Test for RunPod Serverless ComfyUI Worker Handler.
Validates the end-to-end handler lifecycle with fake model downloads and workflow execution.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import sys

# Add worker to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "worker")))

import handler as worker_handler
from model_cache.manifest import ModelManifest


class TestWorkerHandler(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_base_dir = worker_handler.COMFY_BASE_DIR
        worker_handler.COMFY_BASE_DIR = self.temp_dir

        # Reset singletons
        worker_handler._manifest = None
        worker_handler._cache_manager = None
        worker_handler._resolver = None

        self.manifest_data = {
            "version": "1.0",
            "models": [
                {
                    "model_id": "test_checkpoint",
                    "gdrive_file_id": "1A2B3C4D5E6F7G8H9I0J",
                    "category": "checkpoints",
                    "dest_rel_path": "models/checkpoints",
                    "filename": "fake_checkpoint.safetensors",
                    "file_size": 24,
                }
            ]
        }
        self.manifest_file = os.path.join(self.temp_dir, "model_manifest.json")
        with open(self.manifest_file, "w") as f:
            json.dump(self.manifest_data, f)

        worker_handler.MANIFEST_PATH = self.manifest_file

    def tearDown(self):
        worker_handler.COMFY_BASE_DIR = self.orig_base_dir
        worker_handler._manifest = None
        worker_handler._cache_manager = None
        worker_handler._resolver = None
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_handler_rejects_empty_input(self):
        """Handler should reject missing or invalid job input with clear error."""
        res = worker_handler.handler({"id": "job_1", "input": None})
        self.assertIn("error", res)
        self.assertIn("Please provide input", res["error"])

    def test_handler_rejects_missing_workflow(self):
        """Handler should reject job input without workflow dictionary."""
        res = worker_handler.handler({"id": "job_2", "input": {"foo": "bar"}})
        self.assertIn("error", res)
        self.assertIn("Missing or invalid 'workflow'", res["error"])

    @patch("requests.get")
    @patch("requests.post")
    @patch("handler.WebSocket")
    def test_synthetic_end_to_end_job_execution(self, mock_ws_cls, mock_post, mock_get):
        """
        Synthetic integration test: downloads fake model from mocked Google Drive,
        pre-flights workflow, queues ComfyUI prompt, simulates WebSocket execution,
        and retrieves output images.
        """
        # 1. Mock Google Drive stream download
        fake_weight_data = b"FAKE_WEIGHTS_BINARY_DATA"

        def fake_stream(file_id, dest_file_handle, **kwargs):
            dest_file_handle.write(fake_weight_data)
            return len(fake_weight_data)

        # 2. Mock ComfyUI HTTP endpoints
        def fake_get(url, **kwargs):
            mock_resp = MagicMock()
            if "/object_info" in url:
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "CheckpointLoaderSimple": {
                        "input": {
                            "required": {
                                "ckpt_name": [["fake_checkpoint.safetensors"]]
                            }
                        }
                    }
                }
            elif "/history/" in url:
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "fake_prompt_id": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "out_001.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        }
                    }
                }
            elif "/view" in url:
                mock_resp.status_code = 200
                mock_resp.content = b"\x89PNG\r\n\x1a\nFAKE_IMAGE_BYTES"
            elif f"http://{worker_handler.COMFY_HOST}/" in url:
                mock_resp.status_code = 200
            else:
                mock_resp.status_code = 200
                mock_resp.json.return_value = {}
            return mock_resp

        mock_get.side_effect = fake_get

        # 3. Mock ComfyUI /prompt POST response
        mock_prompt_resp = MagicMock()
        mock_prompt_resp.status_code = 200
        mock_prompt_resp.json.return_value = {"prompt_id": "fake_prompt_id", "number": 1}
        mock_post.return_value = mock_prompt_resp

        # 4. Mock WebSocket lifecycle messages
        mock_ws_inst = MagicMock()
        ws_messages = [
            json.dumps({"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 1}}}}),
            json.dumps({"type": "execution_start", "data": {"prompt_id": "fake_prompt_id"}}),
            json.dumps({"type": "executing", "data": {"node": "4", "prompt_id": "fake_prompt_id"}}),
            json.dumps({"type": "executing", "data": {"node": None, "prompt_id": "fake_prompt_id"}}),  # Done
        ]
        mock_ws_inst.recv.side_effect = ws_messages
        mock_ws_cls.return_value = mock_ws_inst

        # 5. Execute handler
        job_payload = {
            "id": "synthetic_job_100",
            "input": {
                "workflow": {
                    "4": {
                        "class_type": "CheckpointLoaderSimple",
                        "inputs": {"ckpt_name": "fake_checkpoint.safetensors"}
                    }
                }
            }
        }

        with patch.object(worker_handler.GoogleDriveClient, "stream_download", side_effect=fake_stream):
            result = worker_handler.handler(job_payload)

        # 6. Verify assertions
        self.assertEqual(result.get("status"), "COMPLETED")
        self.assertEqual(result.get("prompt_id"), "fake_prompt_id")
        self.assertIn("images", result)
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["filename"], "out_001.png")

        # Verify model was downloaded to disk
        expected_model_path = os.path.join(self.temp_dir, "models", "checkpoints", "fake_checkpoint.safetensors")
        self.assertTrue(os.path.exists(expected_model_path))
        with open(expected_model_path, "rb") as f:
            self.assertEqual(f.read(), fake_weight_data)


if __name__ == "__main__":
    unittest.main()
