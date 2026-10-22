#!/usr/bin/env bash
set -e

# Start SSH server if PUBLIC_KEY is set (enables remote access/debugging)
if [ -n "$PUBLIC_KEY" ]; then
    mkdir -p ~/.ssh
    echo "$PUBLIC_KEY" > ~/.ssh/authorized_keys
    chmod 700 ~/.ssh
    chmod 600 ~/.ssh/authorized_keys

    for key_type in rsa ecdsa ed25519; do
        key_file="/etc/ssh/ssh_host_${key_type}_key"
        if [ ! -f "$key_file" ]; then
            ssh-keygen -t "$key_type" -f "$key_file" -q -N ''
        fi
    done

    service ssh start && echo "worker-comfyui: SSH server started" || echo "worker-comfyui: SSH server could not be started" >&2
fi

# Use libtcmalloc for optimized memory management if available
TCMALLOC="$(ldconfig -p 2>/dev/null | grep -Po "libtcmalloc.so.\d" | head -n 1 || true)"
if [ -n "$TCMALLOC" ]; then
    export LD_PRELOAD="${TCMALLOC}"
fi

# ---------------------------------------------------------------------------
# GPU pre-flight check
# ---------------------------------------------------------------------------
echo "worker-comfyui: Checking GPU availability..."
if ! GPU_CHECK=$(python3 -c "
import torch
try:
    torch.cuda.init()
    name = torch.cuda.get_device_name(0)
    cap = torch.cuda.get_device_capability(0)
    _ = (torch.zeros(8, device='cuda') + 1).sum().item()
    torch.cuda.synchronize()
    print(f'OK: {name} (sm_{cap[0]}{cap[1]}), torch {torch.__version__}, cuda {torch.version.cuda}')
except Exception as e:
    print(f'FAIL: {e}')
    exit(1)
" 2>&1); then
    echo "worker-comfyui: GPU check warning / failure: $GPU_CHECK"
    echo "Continuing startup (some CPU test environments may not have CUDA enabled)."
else
    echo "worker-comfyui: GPU verified — $GPU_CHECK"
fi

# Ensure ComfyUI-Manager runs in offline network mode inside container
if command -v comfy-manager-set-mode >/dev/null 2>&1; then
    comfy-manager-set-mode offline || echo "worker-comfyui: Could not set ComfyUI-Manager network_mode" >&2
fi

echo "worker-comfyui: Starting local ComfyUI instance..."
: "${COMFY_LOG_LEVEL:=DEBUG}"
COMFY_PID_FILE="/tmp/comfyui.pid"

# Start ComfyUI in the background
python -u /comfyui/main.py --disable-auto-launch --disable-metadata --listen --verbose "${COMFY_LOG_LEVEL}" --log-stdout &
echo $! > "$COMFY_PID_FILE"

echo "worker-comfyui: Starting RunPod Handler with Google Drive model cache..."
if [ "$SERVE_API_LOCALLY" == "true" ]; then
    python -u /handler.py --rp_serve_api --rp_api_host=0.0.0.0
else
    python -u /handler.py
fi
