"""
Google Drive API client with Service Account authentication, chunked streaming,
exponential backoff, quota error handling, and credential redaction.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, Optional

import requests

logger = logging.getLogger("worker.model_cache.gdrive")

GDRIVE_API_BASE = "https://www.googleapis.com/drive/v3/files"
GDRIVE_OAUTH_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
CHUNK_SIZE_BYTES = 8 * 1024 * 1024  # 8 MB chunks


class GoogleDriveError(Exception):
    """Base error for Google Drive operations."""
    pass


class QuotaExceededError(GoogleDriveError):
    """Raised when Google Drive API download or rate quota is exceeded."""
    pass


def redact_secrets(text: str) -> str:
    """Redact sensitive keys, tokens, and authorization headers from strings."""
    if not isinstance(text, str):
        return text
    # Redact Bearer tokens
    text = re.sub(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{15,}", r"\1[REDACTED_TOKEN]", text, flags=re.IGNORECASE)
    # Redact private key blocks
    text = re.sub(r"-----BEGIN PRIVATE KEY-----[^-]+-----END PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]", text)
    # Redact API keys
    text = re.sub(r"(key=)[a-zA-Z0-9_\-]{20,}", r"\1[REDACTED_API_KEY]", text, flags=re.IGNORECASE)
    return text


class GoogleDriveClient:
    """Client for securely streaming files from Google Drive using Service Account credentials."""

    def __init__(
        self,
        service_account_info: Optional[Dict[str, Any]] = None,
        service_account_file: Optional[str] = None,
        api_key: Optional[str] = None,
        max_retries: int = 3,
        base_backoff_sec: float = 2.0,
        request_timeout: int = 60,
    ):
        self.sa_info = service_account_info
        self.sa_file = service_account_file
        self.api_key = api_key
        self.max_retries = max_retries
        self.base_backoff_sec = base_backoff_sec
        self.request_timeout = request_timeout

        self._creds = None
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0

        self._init_credentials()

    def _init_credentials(self) -> None:
        """Initialize Google Service Account credentials if available."""
        # Check environment variables if not passed explicitly
        if not self.sa_info and not self.sa_file:
            env_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
            if env_json:
                try:
                    self.sa_info = json.loads(env_json)
                except json.JSONDecodeError as e:
                    logger.warning("GDRIVE_SERVICE_ACCOUNT_JSON contained malformed JSON: %s", e)

            env_file = os.environ.get("GDRIVE_SERVICE_ACCOUNT_FILE")
            if env_file and os.path.exists(env_file):
                self.sa_file = env_file

        if not self.api_key:
            self.api_key = os.environ.get("GDRIVE_API_KEY")

        # Try to load credentials via google.oauth2 if available
        try:
            from google.oauth2 import service_account

            if self.sa_info:
                self._creds = service_account.Credentials.from_service_account_info(
                    self.sa_info, scopes=[GDRIVE_OAUTH_SCOPE]
                )
                logger.info("Initialized Google Drive client with Service Account from JSON dictionary.")
            elif self.sa_file and os.path.exists(self.sa_file):
                self._creds = service_account.Credentials.from_service_account_file(
                    self.sa_file, scopes=[GDRIVE_OAUTH_SCOPE]
                )
                logger.info("Initialized Google Drive client with Service Account from file %s", self.sa_file)
        except ImportError:
            logger.warning("google-auth not installed; service account authentication unavailable.")
        except Exception as e:
            logger.error("Failed to initialize Service Account credentials: %s", redact_secrets(str(e)))

    def get_access_token(self) -> Optional[str]:
        """Obtain a fresh OAuth2 access token for Google Drive API."""
        if self._creds is None:
            return None

        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token

        try:
            from google.auth.transport.requests import Request

            self._creds.refresh(Request())
            self._token = self._creds.token
            # Typically valid for 3600 seconds, refresh 5 minutes early
            self._token_expiry = now + 3300
            return self._token
        except Exception as e:
            msg = f"Failed to refresh Google Service Account access token: {redact_secrets(str(e))}"
            logger.error(msg)
            raise GoogleDriveError(msg) from e

    def get_file_metadata(self, file_id: str) -> Dict[str, Any]:
        """Fetch metadata for a Google Drive file."""
        headers = self._get_headers()
        params = {"fields": "id,name,size,md5Checksum,mimeType", "supportsAllDrives": "true"}
        if not self._token and self.api_key:
            params["key"] = self.api_key

        url = f"{GDRIVE_API_BASE}/{file_id}"
        resp = self._make_request_with_retry("GET", url, headers=headers, params=params)
        return resp.json()

    def stream_download(
        self,
        file_id: str,
        dest_file_handle: Any,
        expected_size: Optional[int] = None,
        progress_callback: Optional[Callable[[int, Optional[int]], None]] = None,
        hash_callback: Optional[Callable[[bytes], None]] = None,
        chunk_size: int = CHUNK_SIZE_BYTES,
    ) -> int:
        """
        Stream a file from Google Drive directly to a file handle in chunks.
        Never loads the entire file into memory. Returns total bytes written.
        """
        headers = self._get_headers()
        params = {"alt": "media", "supportsAllDrives": "true"}
        if not self._token and self.api_key:
            params["key"] = self.api_key

        url = f"{GDRIVE_API_BASE}/{file_id}"
        total_downloaded = 0

        # Execute streaming request with retries
        resp = self._make_request_with_retry("GET", url, headers=headers, params=params, stream=True)

        try:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                dest_file_handle.write(chunk)
                total_downloaded += len(chunk)
                if hash_callback:
                    hash_callback(chunk)
                if progress_callback:
                    progress_callback(total_downloaded, expected_size)
        except requests.exceptions.RequestException as e:
            raise GoogleDriveError(f"Stream interrupted during download of file {file_id}: {redact_secrets(str(e))}") from e
        finally:
            resp.close()

        return total_downloaded

    def _get_headers(self) -> Dict[str, str]:
        """Build request headers with Bearer token if available."""
        headers = {
            "Accept": "*/*",
            "User-Agent": "ComfyUI-RunPod-Bridge/1.0",
        }
        token = self.get_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _make_request_with_retry(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        params: Optional[Dict[str, Any]] = None,
        stream: bool = False,
    ) -> requests.Response:
        """Make an HTTP request with exponential backoff and quota error handling."""
        attempt = 0
        backoff = self.base_backoff_sec

        while attempt <= self.max_retries:
            attempt += 1
            try:
                resp = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    stream=stream,
                    timeout=self.request_timeout,
                )

                if resp.status_code == 200:
                    return resp

                # Handle errors
                status = resp.status_code
                error_body = ""
                try:
                    error_json = resp.json()
                    error_body = json.dumps(error_json)
                except Exception:
                    error_body = resp.text[:500]

                sanitized_body = redact_secrets(error_body)

                # Check for Google Drive quota limits (403/429)
                if status in (403, 429):
                    is_quota = any(
                        term in sanitized_body.lower()
                        for term in [
                            "ratelimitexceeded",
                            "userratelimitexceeded",
                            "dailylimitexceeded",
                            "downloadquotaexceeded",
                            "quota exceeded",
                        ]
                    )
                    if is_quota:
                        msg = (
                            f"Google Drive API quota exceeded for request ({status}): {sanitized_body}. "
                            "Consider using a Google Service Account with higher Drive limits or reducing cold start downloads."
                        )
                        logger.error(msg)
                        if attempt > self.max_retries:
                            raise QuotaExceededError(msg)
                        # Back off on quota errors
                        time.sleep(backoff)
                        backoff *= 2
                        continue

                # 404 Not Found
                if status == 404:
                    raise GoogleDriveError(
                        f"Google Drive file not found or inaccessible (404). Verify file ID and Service Account permissions."
                    )

                # 401 Unauthorized
                if status == 401:
                    # Token might have expired early, refresh and retry once
                    if attempt == 1 and self._creds:
                        self._token = None
                        headers["Authorization"] = f"Bearer {self.get_access_token()}"
                        continue
                    raise GoogleDriveError(f"Google Drive authentication failed (401): {sanitized_body}")

                # 5xx Server errors - retry
                if 500 <= status < 600:
                    logger.warning("Google Drive 5xx error (%d), attempt %d/%d: %s", status, attempt, self.max_retries, sanitized_body)
                    if attempt > self.max_retries:
                        raise GoogleDriveError(f"Google Drive server error ({status}): {sanitized_body}")
                    time.sleep(backoff)
                    backoff *= 2
                    continue

                # Other HTTP errors
                raise GoogleDriveError(f"Google Drive API returned HTTP {status}: {sanitized_body}")

            except requests.exceptions.Timeout as e:
                logger.warning("Google Drive request timed out, attempt %d/%d", attempt, self.max_retries)
                if attempt > self.max_retries:
                    raise GoogleDriveError(f"Google Drive request timed out after {self.max_retries} attempts.") from e
                time.sleep(backoff)
                backoff *= 2
            except requests.exceptions.RequestException as e:
                # Connection or network failure
                logger.warning("Google Drive network error: %s, attempt %d/%d", redact_secrets(str(e)), attempt, self.max_retries)
                if attempt > self.max_retries:
                    raise GoogleDriveError(f"Network error connecting to Google Drive: {redact_secrets(str(e))}") from e
                time.sleep(backoff)
                backoff *= 2

        raise GoogleDriveError(f"Max retries exceeded for Google Drive request to {url}")
