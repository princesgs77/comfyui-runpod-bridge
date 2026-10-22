# ComfyUI Remote — Deployment Guide

This guide provides step-by-step instructions for deploying the **Render Bridge** and the **RunPod Serverless Worker with Google Drive Model Cache**.

---

## 1. Google Drive & Service Account Setup

To allow your serverless worker to securely stream models without making your Google Drive public:

1. **Create a Google Cloud Project**:
   - Go to the [Google Cloud Console](https://console.cloud.google.com/).
   - Create a project (e.g., `comfyui-model-archive`).
2. **Enable the Google Drive API**:
   - Navigate to **APIs & Services > Library**.
   - Search for **Google Drive API** and click **Enable**.
3. **Create a Service Account**:
   - Go to **APIs & Services > Credentials**.
   - Click **Create Credentials > Service Account**.
   - Name it (e.g., `runpod-model-worker`) and grant it the role **Viewer** or continue to finish.
   - Note the generated email address (e.g., `runpod-model-worker@comfyui-model-archive.iam.gserviceaccount.com`).
4. **Generate Service Account JSON Key**:
   - Click on the created service account, go to the **Keys** tab.
   - Click **Add Key > Create new key > JSON**.
   - Download the JSON key file. Keep it safe and **NEVER commit it to Git**.
5. **Share Your Google Drive Model Folder**:
   - In Google Drive, create a dedicated folder (e.g., `ComfyUI Models`).
   - Click **Share** and add your Service Account email with **Viewer** permission.
   - Upload your checkpoint files, LoRAs, VAEs, and upscalers into this folder.
   - For each file, click **Share > Copy link** to extract its **File ID** (the string between `/d/` and `/view`).

---

## 2. Configure Model Manifest (`worker/model_manifest.json`)

Update `worker/model_manifest.json` with your real Google Drive File IDs and model metadata:

```json
{
  "version": "1.0.0",
  "models": [
    {
      "model_id": "checkpoints/v1-5-pruned-emaonly.safetensors",
      "gdrive_file_id": "PASTE_YOUR_REAL_GOOGLE_DRIVE_FILE_ID_HERE",
      "category": "checkpoints",
      "dest_rel_path": "models/checkpoints",
      "filename": "v1-5-pruned-emaonly.safetensors",
      "file_size": 4265147572,
      "expected_sha256": "cc6c52bb9799a46d46e85712297f0d08e1784340905cd08a59700e7559217e1b",
      "description": "Stable Diffusion 1.5 Base Model"
    }
  ]
}
```

> [!TIP]
> You can leave `expected_sha256` blank or omit it if you do not want to pre-calculate SHA-256 for all models, but specifying it guarantees byte-for-byte corruption detection.

---

## 3. Build & Publish Worker Docker Image

Because the worker container requires GPU/CUDA drivers, build and push the image using a container registry (Docker Hub or GitHub Container Registry):

### Using Docker Hub
```bash
# 1. Login to Docker Hub
docker login

# 2. Build worker image
docker build -t your_dockerhub_user/comfyui-runpod-worker:v1.0.0 -f worker/Dockerfile worker/

# 3. Push to registry
docker push your_dockerhub_user/comfyui-runpod-worker:v1.0.0
```

### (Optional) Adding Custom Nodes
If you want to bake custom nodes (e.g., Impact-Pack, ControlNet-Aux) into your image, add them to `worker/Dockerfile`:
```dockerfile
RUN comfy-node-install ComfyUI-Impact-Pack ComfyUI-ControlNet-Aux
```

---

## 4. Deploy to RunPod Serverless

1. Log into your [RunPod Console](https://www.runpod.io/console/serverless).
2. Go to **Serverless > Templates > New Template**:
   - **Template Name**: `comfyui-gdrive-worker`
   - **Container Image**: `your_dockerhub_user/comfyui-runpod-worker:v1.0.0`
   - **Container Disk**: Set to **40 GB** or **50 GB** (gives ample room to cache models).
   - **Environment Variables**:
     - `GDRIVE_SERVICE_ACCOUNT_JSON`: Paste the entire minified JSON string of your Google Service Account key file.
     - `MODEL_MANIFEST_PATH`: `/comfyui/model_manifest.json`
     - `REFRESH_WORKER`: `false` (critical: keeps the worker alive between jobs so cached models do not get deleted immediately).
3. Click **Save Template**.
4. Go to **Serverless > Endpoints > New Endpoint**:
   - **Endpoint Name**: `comfyui-remote-endpoint`
   - **Select Template**: `comfyui-gdrive-worker`
   - **GPU Types**: Select an affordable GPU tier (e.g., RTX 3090, RTX 4090, or L4).
   - **Max Workers**: 1 (or 2 if multiple users).
   - **Idle Timeout**: 10–30 seconds.
   - Click **Create Endpoint**.
5. Copy your **Endpoint ID** (e.g., `vllm-xxxxx` or 14-character alphanumeric string) and your **API Key** from Account Settings.

---

## 5. Deploy Render Bridge on Render.com Free Tier

1. Log into [Render Dashboard](https://dashboard.render.com/).
2. Click **New + > Web Service**.
3. Select **Build and deploy from a Git repository** and connect `princesgs77/comfyui-runpod-bridge`.
4. Configure service settings:
   - **Name**: `comfyui-runpod-bridge`
   - **Region**: Choose the region closest to your location.
   - **Branch**: `main`
   - **Runtime**: `Node`
   - **Build Command**: `npm install`
   - **Start Command**: `npm start`
   - **Instance Type**: `Free`
5. Configure Environment Variables under the **Environment** tab:
   - `RUNPOD_API_KEY`: Your secret RunPod API key.
   - `RUNPOD_ENDPOINT_ID`: Your RunPod Serverless endpoint ID.
   - `APP_ACCESS_TOKEN`: A secure random secret string used to protect your bridge (e.g., `my_secret_token_abc123`).
6. Click **Deploy Web Service**. Render will build and deploy the app at a public URL: `https://comfyui-runpod-bridge.onrender.com`.

---

## 6. Verification & Running Workflows

### Small Image Generation Test
1. Open your Render bridge URL in your browser: `https://comfyui-runpod-bridge.onrender.com`.
2. Enter your `APP_ACCESS_TOKEN` in the **Bridge Access Token** field.
3. Click **Check Connection & Health**:
   - Both badges should turn green: `Bridge: OK` and `RunPod: Online` or `OK`.
   - Your allowlisted models from `model_manifest.json` will populate the manifest list.
4. In ComfyUI, export an API-format workflow: **Workflow Menu > Export (API)**.
5. In the test UI, select the `.json` file and click **Submit Workflow**.
6. The test UI will track execution status via WebSocket and display generated images when finished.

### Video Generation Test
For video generation (e.g., AnimateDiff or Stable Video Diffusion):
1. Ensure the diffusion model or motion module is defined in `model_manifest.json` under category `diffusion_models` or `animatediff_models`.
2. Submit your video workflow API JSON.
3. The bridge detects video formats (`.mp4`, `.webm`) and automatically renders them in an interactive video player.

---

## 7. Diagnostics, Logs & Rollback

- **Worker Logs**: In the RunPod Serverless dashboard, click your endpoint, go to **Jobs**, and click on any job ID to view real-time stdout logs (`Downloading model...`, `ComfyUI execution started...`).
- **Render Logs**: In the Render dashboard, click **Logs** to view HTTP and WebSocket request traces.
- **Rollback Strategy**:
  If a new worker image version introduces a bug or crash:
  1. In RunPod Console, go to **Serverless > Templates**.
  2. Edit your template and revert the container image tag to the previous known good tag (e.g., `v1.0.0` instead of `v1.1.0`).
  3. Click **Update Template**. Future jobs will immediately run on the reverted image without downtime.
