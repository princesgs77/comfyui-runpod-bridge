"""
RunPod Serverless ComfyUI Worker Handler with Google Drive Model Cache.
Extends the official RunPod worker-comfyui handler by downloading and caching
models from Google Drive into the worker's temporary disk before workflow execution.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sys
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests
try:
    import runpod
except ImportError:
    runpod = None
from websocket import WebSocket

# Import model caching subsystem
from model_cache.cache_manager import (
    CacheError,
    InsufficientDiskSpaceError,
    ModelCacheManager,
)
from model_cache.gdrive import (
    GoogleDriveClient,
    GoogleDriveError,
    QuotaExceededError,
    redact_secrets,
)
from model_cache.manifest import ModelManifest, ManifestValidationError
from model_cache.model_resolver import (
    MODEL_LOADER_REGISTRY,
    ModelResolver,
    UnresolvedModelError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("worker.handler")

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1:8188")
COMFY_BASE_DIR = os.environ.get("COMFY_BASE_DIR", "/comfyui")
COMFY_PID_FILE = "/tmp/comfyui.pid"

# Retry configurations
COMFY_API_AVAILABLE_MAX_RETRIES = int(os.environ.get("COMFY_API_AVAILABLE_MAX_RETRIES", "500"))
COMFY_API_AVAILABLE_INTERVAL_MS = int(os.environ.get("COMFY_API_AVAILABLE_INTERVAL_MS", "50"))
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"
MANIFEST_PATH = os.environ.get("MODEL_MANIFEST_PATH", os.path.join(COMFY_BASE_DIR, "model_manifest.json"))

# Global singletons
_manifest: Optional[ModelManifest] = None
_cache_manager: Optional[ModelCacheManager] = None
_resolver: Optional[ModelResolver] = None


def get_cache_subsystem() -> Tuple[ModelManifest, ModelCacheManager, ModelResolver]:
    """Initialize or return the cached manifest and manager singletons."""
    global _manifest, _cache_manager, _resolver
    if _manifest is None or _cache_manager is None or _resolver is None:
        # Load manifest
        manifest_file = MANIFEST_PATH
        if not os.path.exists(manifest_file):
            alt_path = os.path.join(os.path.dirname(__file__), "model_manifest.json")
            if os.path.exists(alt_path):
                manifest_file = alt_path

        if os.path.exists(manifest_file):
            logger.info("Loading model manifest from: %s", manifest_file)
            _manifest = ModelManifest.from_file(manifest_file)
        else:
            logger.warning("No model manifest file found at %s. Manifest initialized empty.", manifest_file)
            _manifest = ModelManifest()

        gdrive = GoogleDriveClient()
        _cache_manager = ModelCacheManager(base_dir=COMFY_BASE_DIR, manifest=_manifest, gdrive_client=gdrive)
        _resolver = ModelResolver(manifest=_manifest, base_dir=COMFY_BASE_DIR)

    return _manifest, _cache_manager, _resolver


def _is_comfyui_process_alive() -> bool:
    """Check if the ComfyUI background process is still running."""
    if not os.path.exists(COMFY_PID_FILE):
        return True
    try:
        with open(COMFY_PID_FILE, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def check_server(url: str, retries: int = 50, delay_ms: int = 50) -> bool:
    """Verify that ComfyUI HTTP server is responsive."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=3)
            if resp.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass

        if not _is_comfyui_process_alive():
            logger.error("ComfyUI process died during server check.")
            return False

        time.sleep(delay_ms / 1000.0)
    return False


def _fetch_object_info() -> Optional[Dict[str, Any]]:
    """Fetch ComfyUI /object_info endpoint."""
    try:
        resp = requests.get(f"http://{COMFY_HOST}/object_info", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.warning("Could not fetch /object_info from ComfyUI: %s", e)
    return None


def _loader_field_options(object_info: Dict[str, Any], class_type: str, field: str) -> Optional[List[str]]:
    """Return available option strings for a node input field."""
    node_info = object_info.get(class_type)
    if not isinstance(node_info, dict):
        return None
    inputs = node_info.get("input", {})
    if not isinstance(inputs, dict):
        return None
    for section in ("required", "optional"):
        section_inputs = inputs.get(section)
        if not isinstance(section_inputs, dict):
            continue
        spec = section_inputs.get(field)
        if isinstance(spec, (list, tuple)) and len(spec) > 0 and isinstance(spec[0], list):
            return spec[0]
    return None


def validate_workflow_models(workflow: Dict[str, Any]) -> Optional[str]:
    """
    Pre-flight check verifying every model referenced in workflow exists in ComfyUI /object_info.
    Surfaces missing models cleanly before job starts.
    """
    if not isinstance(workflow, dict):
        return None

    object_info = _fetch_object_info()
    if object_info is None:
        logger.info("Skipping workflow model pre-flight check (could not fetch /object_info)")
        return None

    problems = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or class_type not in MODEL_LOADER_REGISTRY:
            continue

        model_type, fields = MODEL_LOADER_REGISTRY[class_type]
        for field in fields:
            val = inputs.get(field)
            if not isinstance(val, str):
                continue
            options = _loader_field_options(object_info, class_type, field)
            if options is not None and val not in options:
                problems.append(
                    f"Node {node_id} ({class_type}): file '{val}' not found in registered {model_type}."
                )

    if not problems:
        return None

    msg = "Workflow validation failed: the following files are not available in ComfyUI:\n"
    msg += "\n".join(f"  • {p}" for p in problems)
    msg += "\n\nEnsure required models are defined in model_manifest.json with their Google Drive file ID."
    return msg


def upload_images(images: List[Dict[str, str]]) -> Dict[str, Any]:
    """Upload input images into ComfyUI /upload/image endpoint."""
    url = f"http://{COMFY_HOST}/upload/image"
    errors = []

    for img in images:
        name = img.get("name")
        data_str = img.get("image", "")
        if not name or not data_str:
            continue

        try:
            # Handle data:image/png;base64,... header if present
            if "," in data_str:
                data_str = data_str.split(",", 1)[1]
            raw_bytes = base64.b64decode(data_str)

            files = {"image": (name, raw_bytes)}
            data = {"overwrite": "true"}
            resp = requests.post(url, files=files, data=data, timeout=30)
            if resp.status_code != 200:
                errors.append(f"Failed to upload '{name}': {resp.status_code} {resp.text}")
        except Exception as e:
            errors.append(f"Error uploading '{name}': {e}")

    if errors:
        return {"status": "error", "details": errors}
    return {"status": "success"}


def queue_workflow(workflow: Dict[str, Any], client_id: str, comfy_org_api_key: Optional[str] = None) -> Dict[str, Any]:
    """Queue workflow with ComfyUI /prompt endpoint."""
    url = f"http://{COMFY_HOST}/prompt"
    payload = {"prompt": workflow, "client_id": client_id}
    headers = {"Content-Type": "application/json"}
    if comfy_org_api_key:
        headers["X-API-KEY"] = comfy_org_api_key

    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"ComfyUI rejected prompt ({resp.status_code}): {resp.text}")
    return resp.json()


def get_image_data(filename: str, subfolder: str, image_type: str) -> Optional[str]:
    """Fetch generated image bytes from ComfyUI /view endpoint and encode to base64."""
    params = {"filename": filename, "subfolder": subfolder, "type": image_type}
    url = f"http://{COMFY_HOST}/view?{urllib.parse.urlencode(params)}"
    resp = requests.get(url, timeout=30)
    if resp.status_code == 200:
        return base64.b64encode(resp.content).decode("utf-8")
    return None


def validate_input(job_input: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate job input format."""
    if job_input is None:
        return None, "Please provide input object."

    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON in job input string."

    if not isinstance(job_input, dict):
        return None, "Job input must be a dictionary."

    workflow = job_input.get("workflow")
    if not workflow or not isinstance(workflow, dict):
        return None, "Missing or invalid 'workflow' dictionary in job input."

    return job_input, None


def handler(job: Dict[str, Any]) -> Dict[str, Any]:
    """
    RunPod Serverless Handler for ComfyUI jobs.
    Orchestrates Google Drive model caching, ComfyUI queueing, and WebSocket result collection.
    """
    job_id = job.get("id", str(uuid.uuid4()))
    raw_input = job.get("input")

    # 1. Validate Input
    job_input, err = validate_input(raw_input)
    if err:
        return {"error": err}

    workflow = job_input["workflow"]
    input_images = job_input.get("images")
    explicit_models = job_input.get("models")
    prefetch_models = job_input.get("prefetch_models")
    comfy_org_api_key = job_input.get("comfy_org_api_key")

    logger.info("Starting ComfyUI job %s", job_id)

    # 2. Google Drive Model Resolution and Caching
    try:
        manifest, cache_mgr, resolver = get_cache_subsystem()
        needed_models = resolver.resolve_job_models(
            workflow=workflow,
            explicit_models=explicit_models,
            prefetch_models=prefetch_models,
        )

        if needed_models:
            logger.info("Job requires downloading %d missing model(s) from Google Drive...", len(needed_models))
            for model_entry in needed_models:
                cache_mgr.ensure_model(model_entry)
            logger.info("All required models downloaded and cached successfully.")

    except (UnresolvedModelError, InsufficientDiskSpaceError, QuotaExceededError, CacheError, GoogleDriveError) as e:
        msg = redact_secrets(str(e))
        logger.error("Model preparation failed for job %s: %s", job_id, msg)
        return {"error": msg}
    except Exception as e:
        msg = f"Unexpected model caching failure: {redact_secrets(str(e))}"
        logger.error(msg, exc_info=True)
        return {"error": msg}

    # 3. Check ComfyUI server availability
    if not check_server(f"http://{COMFY_HOST}/", COMFY_API_AVAILABLE_MAX_RETRIES, COMFY_API_AVAILABLE_INTERVAL_MS):
        return {"error": f"ComfyUI server ({COMFY_HOST}) not reachable."}

    # 4. Upload input images if provided
    if input_images:
        upload_res = upload_images(input_images)
        if upload_res.get("status") == "error":
            return {"error": "Failed uploading input image(s)", "details": upload_res.get("details")}

    # 5. Pre-flight check models against ComfyUI registered options
    preflight_err = validate_workflow_models(workflow)
    if preflight_err:
        logger.error("Workflow pre-flight model validation failed:\n%s", preflight_err)
        return {"error": preflight_err}

    # 6. Connect WebSocket and Queue Prompt
    client_id = str(uuid.uuid4())
    ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
    ws = None
    output_images = []

    try:
        ws = WebSocket()
        ws.connect(ws_url, timeout=10)

        # Queue prompt
        queue_res = queue_workflow(workflow, client_id, comfy_org_api_key)
        prompt_id = queue_res.get("prompt_id")
        if not prompt_id:
            return {"error": "No prompt_id returned from ComfyUI", "details": queue_res}

        logger.info("Workflow queued with prompt_id: %s", prompt_id)

        # 7. Listen to WebSocket for completion
        execution_done = False
        start_time = time.time()
        timeout_sec = int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))  # 30 min max

        while not execution_done:
            if time.time() - start_time > timeout_sec:
                return {"error": f"Job timed out after {timeout_sec} seconds."}

            msg_raw = ws.recv()
            if not msg_raw or isinstance(msg_raw, bytes):
                # Binary messages are preview images
                continue

            try:
                msg = json.loads(msg_raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            data = msg.get("data", {})

            if msg_type == "status":
                queue_remaining = data.get("status", {}).get("exec_info", {}).get("queue_remaining", 0)
                logger.debug("ComfyUI queue remaining: %d", queue_remaining)

            elif msg_type == "execution_start":
                if data.get("prompt_id") == prompt_id:
                    logger.info("Execution started for prompt %s", prompt_id)

            elif msg_type == "executing":
                node_id = data.get("node")
                curr_prompt = data.get("prompt_id")
                if curr_prompt == prompt_id:
                    if node_id is None:
                        # Execution finished!
                        execution_done = True
                        logger.info("Execution finished for prompt %s", prompt_id)
                        break
                    else:
                        logger.debug("Executing node: %s", node_id)

            elif msg_type == "execution_error":
                if data.get("prompt_id") == prompt_id:
                    logger.error("Execution error: %s", data)
                    return {
                        "error": "ComfyUI execution error",
                        "node_id": data.get("node_id"),
                        "node_type": data.get("node_type"),
                        "exception_message": data.get("exception_message"),
                        "exception_type": data.get("exception_type"),
                    }

        # 8. Retrieve output artifacts from /history
        hist_resp = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
        if hist_resp.status_code == 200:
            hist_data = hist_resp.json().get(prompt_id, {})
            outputs = hist_data.get("outputs", {})
            for node_id, node_out in outputs.items():
                # Process images
                if "images" in node_out and isinstance(node_out["images"], list):
                    for img_meta in node_out["images"]:
                        fname = img_meta.get("filename")
                        sub = img_meta.get("subfolder", "")
                        itype = img_meta.get("type", "output")
                        b64_data = get_image_data(fname, sub, itype)
                        if b64_data:
                            output_images.append({
                                "filename": fname,
                                "subfolder": sub,
                                "type": itype,
                                "node_id": node_id,
                                "data": b64_data,
                            })

                # Process animated / video formats (gifs, webp, mp4)
                if "gifs" in node_out and isinstance(node_out["gifs"], list):
                    for gif_meta in node_out["gifs"]:
                        fname = gif_meta.get("filename")
                        sub = gif_meta.get("subfolder", "")
                        itype = gif_meta.get("type", "output")
                        b64_data = get_image_data(fname, sub, itype)
                        if b64_data:
                            output_images.append({
                                "filename": fname,
                                "subfolder": sub,
                                "type": itype,
                                "node_id": node_id,
                                "format": "video",
                                "data": b64_data,
                            })

        return {
            "status": "COMPLETED",
            "prompt_id": prompt_id,
            "images": output_images,
            "message": f"Successfully generated {len(output_images)} output item(s).",
        }

    except Exception as e:
        msg = redact_secrets(str(e))
        logger.error("Job %s execution failed: %s", job_id, msg, exc_info=True)
        return {"error": msg}

    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


if __name__ == "__main__":
    if runpod is not None:
        logger.info("Initializing ComfyUI RunPod Serverless worker handler...")
        runpod.serverless.start({"handler": handler})
    else:
        logger.error("runpod module not installed. Please install requirements.txt inside the container.")
