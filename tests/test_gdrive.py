"""
Unit tests for GoogleDriveClient and credential redaction,
covering streaming download, quota error handling, and secret redaction.
"""

import io
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add worker to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "worker")))

from model_cache.gdrive import (
    GoogleDriveClient,
    GoogleDriveError,
    QuotaExceededError,
    redact_secrets,
)


class TestGoogleDrive(unittest.TestCase):

    def test_credential_redaction(self):
        """Test redacting tokens, keys, and private keys from strings."""
        sample_log = (
            "Error: Authorization failed for Bearer ya29.a0AfH6SMA827361928371928371 and "
            "key=AIzaSyD_8273648192837461928374. Key: -----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgk\n-----END PRIVATE KEY-----"
        )
        redacted = redact_secrets(sample_log)
        self.assertNotIn("ya29.a0AfH6SMA827361928371928371", redacted)
        self.assertNotIn("AIzaSyD_8273648192837461928374", redacted)
        self.assertNotIn("MIIEvgIBADANBgk", redacted)
        self.assertIn("[REDACTED_TOKEN]", redacted)
        self.assertIn("[REDACTED_API_KEY]", redacted)
        self.assertIn("[REDACTED_PRIVATE_KEY]", redacted)

    @patch("requests.request")
    def test_successful_streaming_download(self, mock_request):
        """Test successful chunked streaming download to file handle."""
        client = GoogleDriveClient(api_key="mock_key", max_retries=1)

        chunks = [b"chunk1_", b"chunk2_", b"chunk3_done"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_content.return_value = chunks
        mock_request.return_value = mock_resp

        out = io.BytesIO()
        bytes_written = client.stream_download(
            file_id="1A2B3C4D5E6F7G8H9I0J",
            dest_file_handle=out,
            chunk_size=10,
        )

        self.assertEqual(bytes_written, len(b"chunk1_chunk2_chunk3_done"))
        self.assertEqual(out.getvalue(), b"chunk1_chunk2_chunk3_done")

    @patch("requests.request")
    def test_quota_exceeded_error(self, mock_request):
        """Test that Google Drive 403 quota error raises QuotaExceededError."""
        client = GoogleDriveClient(api_key="mock_key", max_retries=1, base_backoff_sec=0.01)

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.return_value = {
            "error": {
                "errors": [{"reason": "rateLimitExceeded", "message": "User Rate Limit Exceeded"}],
                "code": 403,
                "message": "User Rate Limit Exceeded",
            }
        }
        mock_request.return_value = mock_resp

        out = io.BytesIO()
        with self.assertRaises(QuotaExceededError) as ctx:
            client.stream_download(
                file_id="1A2B3C4D5E6F7G8H9I0J",
                dest_file_handle=out,
            )
        self.assertIn("quota exceeded", str(ctx.exception).lower())

    @patch("requests.request")
    def test_file_not_found_404(self, mock_request):
        """Test 404 response raises GoogleDriveError with clear message."""
        client = GoogleDriveClient(api_key="mock_key", max_retries=0)

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "File not found"
        mock_request.return_value = mock_resp

        out = io.BytesIO()
        with self.assertRaises(GoogleDriveError) as ctx:
            client.stream_download(
                file_id="1A2B3C4D5E6F7G8H9I0J",
                dest_file_handle=out,
            )
        self.assertIn("404", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
