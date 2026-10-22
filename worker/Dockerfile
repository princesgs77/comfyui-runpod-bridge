# Reproducible RunPod Serverless ComfyUI Worker Image with Google Drive Model Cache
# Extends the official verified RunPod ComfyUI base image.
ARG BASE_VERSION=5.8.5-base
FROM runpod/worker-comfyui:${BASE_VERSION}

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    COMFY_BASE_DIR=/comfyui

WORKDIR /

# 1. Install Google Drive and caching subsystem dependencies into the worker virtualenv
COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# 2. (Optional Custom Nodes) To add custom nodes in your image, uncomment or add:
# RUN comfy-node-install ComfyUI-Impact-Pack ComfyUI-ControlNet-Aux

# 3. Copy Google Drive model caching subsystem and allowlisted manifest
COPY model_cache/ /model_cache/
COPY model_manifest.json /comfyui/model_manifest.json

# 4. Copy custom handler and entrypoint
COPY handler.py /handler.py
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Ensure python modules in root and /model_cache are on PYTHONPATH
ENV PYTHONPATH="/:/model_cache:${PYTHONPATH}"

EXPOSE 8188

CMD ["/start.sh"]
