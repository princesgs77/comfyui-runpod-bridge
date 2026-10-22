# ComfyUI Remote — System Architecture

This document describes the end-to-end architecture of **ComfyUI Remote**, a hybrid cloud deployment combining **Render.com Free**, **RunPod Serverless GPUs**, and **Google Drive Model Storage**.

---

## 1. High-Level Architecture Overview

```
┌────────────────────────────────────────────────────────┐
│                   Browser Client                       │
│  - ComfyUI Node Editor / Test Web UI                  │
│  - Connects to Render Bridge over HTTPS & WSS          │
└──────────────────────────┬─────────────────────────────┘
                           │ HTTPS / WSS (Bearer Auth)
                           ▼
┌────────────────────────────────────────────────────────┐
│           Render.com Free Tier Web Service             │
│  - Express 5 + WebSocket Server                        │
│  - Protects secret RunPod API Key & Google Credentials  │
│  - Compatibility Layer: /prompt, /history, /object_info│
│  - In-memory job state & output view cache             │
└──────────────────────────┬─────────────────────────────┘
                           │ RunPod Serverless API (HTTPS)
                           │ POST /v2/{endpoint}/run
                           │ GET  /v2/{endpoint}/status/{id}
                           ▼
┌────────────────────────────────────────────────────────┐
│            RunPod Serverless GPU Worker                │
│  (Container boots on demand, scales to 0 when idle)    │
│                                                        │
│  ┌──────────────────────────────────────────────────┐  │
│  │ 1. Worker Handler (handler.py)                    │  │
│  │    - Validates workflow JSON graph               │  │
│  │    - Resolves required model weights             │  │
│  └───────────────────────┬──────────────────────────┘  │
│                          │                             │
│                          ▼                             │
│  ┌──────────────────────────────────────────────────┐  │
│  │ 2. Google Drive Model Cache Subsystem            │  │
│  │    - Streams missing models from Google Drive    │  │
│  │    - SHA-256 verification & atomic rename        │  │
│  │    - Local worker disk cache (/comfyui/models)   │  │
│  └───────────────────────┬──────────────────────────┘  │
│                          │                             │
│                          ▼                             │
│  ┌──────────────────────────────────────────────────┐  │
│  │ 3. Local ComfyUI Server (127.0.0.1:8188)         │  │
│  │    - Pre-flight model verification               │  │
│  │    - GPU-accelerated KSampler execution          │  │
│  │    - Outputs saved to /comfyui/output            │  │
│  └──────────────────────────────────────────────────┘  │
└──────────────────────────┬─────────────────────────────┘
                           │
                           │ OAuth2 Service Account API (Chunked Stream)
                           ▼
┌────────────────────────────────────────────────────────┐
│                   Google Drive                         │
│  - Permanent cloud archive for models (10-50+ GB)      │
│  - Checkpoints, LoRAs, VAEs, Text Encoders, Upscalers │
│  - No recurring storage fee on RunPod volumes          │
└────────────────────────────────────────────────────────┘
```

---

## 2. Request & Execution Lifecycle

### Step 1: Prompt Submission (Frontend → Render)
1. The user builds or loads an API-format workflow JSON in the browser.
2. The browser sends `POST /prompt` (or `POST /api/run`) with `Authorization: Bearer <APP_ACCESS_TOKEN>`.
3. The Render bridge validates:
   - Bearer authentication.
   - Payload format (non-empty workflow graph object).
   - Payload size (< 10 MB limit).
4. Render forwards the job to RunPod Serverless:
   `POST https://api.runpod.ai/v2/{endpoint_id}/run`
5. RunPod returns an asynchronous job ID: `{"id": "...", "status": "IN_QUEUE"}`.
6. Render creates a tracking entry mapping `prompt_id <-> runpod_job_id` and starts a background poller.

### Step 2: Worker Initialization & Model Preparation (RunPod Worker)
1. RunPod schedules the job to a GPU worker container. If no worker is active, RunPod spins up a container from the custom Docker image.
2. The worker handler `handler(job)` intercepts the job before ComfyUI queues it:
   - **Resolver**: Inspects workflow loader nodes (`CheckpointLoaderSimple`, `LoraLoader`, `VAELoader`, `UNETLoader`, etc.) and any explicit `input.models` prefetch requests.
   - **Cache Check**: Checks whether the referenced model files exist on the worker's temporary disk (`/comfyui/models/...`).
   - **Google Drive Stream**: For any missing models, the `ModelCacheManager` authenticates against Google Drive via Google Service Account credentials (`GDRIVE_SERVICE_ACCOUNT_JSON`), verifies disk capacity, and streams the model file in 8 MB chunks into a temporary file (`.tmp.<uuid>`).
   - **Validation & Atomic Cache**: Computes SHA-256 on the fly, verifies file size, and atomically moves the file (`os.replace`) into the destination directory. A file-level lock prevents concurrent duplicate downloads of the same model.
3. If any model is not found in `model_manifest.json` and not on disk, the job fails immediately with an actionable error listing the unresolved reference and available manifest models.

### Step 3: ComfyUI Workflow Execution
1. The worker handler uploads any input images (`input.images`) to ComfyUI's `/upload/image` endpoint.
2. The worker runs `validate_workflow_models(workflow)` against ComfyUI's `/object_info`. Because the models were downloaded into `/comfyui/models/...`, ComfyUI recognizes them.
3. The handler connects to local ComfyUI via WebSocket (`ws://127.0.0.1:8188/ws?clientId=...`) and sends `POST http://127.0.0.1:8188/prompt`.
4. ComfyUI executes the graph on the GPU. The handler listens to WebSocket execution events (`execution_start`, `executing`, `progress`, `executed`).

### Step 4: Output Collection & Delivery
1. When execution completes, the handler queries `http://127.0.0.1:8188/history/{prompt_id}`.
2. Output images and videos (MP4, WebP, PNG) are read from `/comfyui/output/`, converted to base64, and included in the RunPod job result payload.
3. RunPod marks the job `COMPLETED`.
4. The Render bridge poller receives the `COMPLETED` status, decodes the base64 files into an in-memory cache, and emits the ComfyUI `executed` event to the browser WebSocket client.
5. The browser displays the generated outputs and fetches full-resolution images via `GET /view?filename=...`.

---

## 3. Google Drive Model Caching Subsystem

### Security & Integrity Principles
- **No Public URL Hacks**: Uses the official Google Drive API v3 `alt=media` endpoint with OAuth2 Bearer tokens minted from a Google Cloud Service Account.
- **Strict Allowlist**: Models must be registered in `model_manifest.json`. Arbitrary Google Drive file IDs or external URLs are rejected.
- **Path Traversal Protection**: Filenames and destination relative paths are validated against directory traversal (`..`, `/`, `\`) and restricted to standard subdirectories (`models/checkpoints`, `models/loras`, etc.).
- **Zero-RAM Buffering**: Large models (10–50 GB) are streamed in 8 MB chunks directly to disk.
- **Atomic Renaming**: Partial or failed downloads are discarded immediately. Files are only moved into place after complete download and SHA-256 verification.
- **Worker Disk Lifespan**: Downloaded models persist on the worker's temporary disk for subsequent jobs while the container is alive (warm worker). Once RunPod terminates the worker, temporary storage is reclaimed.

---

## 4. Cost Optimization & Safeguards

| Resource | Service | Cost Mechanics | Safeguards |
| :--- | :--- | :--- | :--- |
| Web Service | Render Free | $0 / month | Lightweight Express app, spins down after 15 min idle, wakes on request. |
| Model Storage | Google Drive | Included in Google Account / Free 15GB+ | Permanent archive; no RunPod Network Volume hourly fees. |
| GPU Compute | RunPod Serverless | Per-second execution billing (e.g., $0.0003–$0.0008/sec depending on GPU) | Idle workers scale to 0; `REFRESH_WORKER=false` reuses warm workers; execution timeout safeguard (30 min max). |
| Disk Storage | RunPod Worker Disk | Free temporary container disk (typically 20–50 GB included per template) | Disk capacity check before downloading; optional cache pruning; no persistent volume fee. |

> [!WARNING]
> **Google Drive API Quotas**: Google Drive enforces daily download quotas (typically ~750 GB/day per service account or per file). Downloading 50 GB models on every single cold start can exhaust the quota. For high-volume production, a RunPod Network Volume or S3 model bucket is recommended.
