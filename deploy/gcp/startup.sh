#!/usr/bin/env bash
# Runs on the VM at every boot, as root, via the `startup-script` metadata key.
#
# Machine preparation ONLY: Docker, the NVIDIA stack if this VM has a GPU, the model file, and
# the directories the stack mounts. It deliberately knows nothing about this repository - the
# source arrives separately (deploy.sh), so re-running this never depends on git credentials
# the VM does not have, and a reboot cannot roll the service back to an older commit.
#
# Google re-runs it on every boot, so every step below has to be safe to repeat.
set -euo pipefail

APP_DIR=/opt/quitsmoke
MODEL_URL=https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
MODEL_FILE=Qwen3-4B-Q4_K_M.gguf
# The GGUF is 2.4 GB. A partial download looks like a present file and then fails inside
# llama-server with an unhelpful error, so the size is checked rather than the existence.
MODEL_MIN_BYTES=2000000000

log() { echo "[startup $(date -Is)] $*"; }

# ---------------------------------------------------------------- docker
if ! command -v docker >/dev/null 2>&1; then
    log "installing docker"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
else
    log "docker already installed"
fi

# ---------------------------------------------------------------- gpu, if there is one
# lspci rather than nvidia-smi: nvidia-smi is what we are deciding whether to install.
if lspci 2>/dev/null | grep -qi nvidia; then
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        log "NVIDIA hardware present, installing the driver"
        # Google's own installer. It picks the driver version that matches the attached card and
        # the kernel, which is the part that goes wrong when you choose a version by hand.
        curl -fsSL -o /opt/install_gpu_driver.py \
            https://raw.githubusercontent.com/GoogleCloudPlatform/compute-gpu-installation/main/linux/install_gpu_driver.py
        python3 /opt/install_gpu_driver.py || log "WARNING: driver install failed, CPU profile still works"
    fi
    if ! dpkg -l nvidia-container-toolkit >/dev/null 2>&1; then
        log "installing the NVIDIA container toolkit"
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
            | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
            | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
            > /etc/apt/sources.list.d/nvidia-container-toolkit.list
        apt-get update -qq
        apt-get install -y -qq nvidia-container-toolkit
        nvidia-ctk runtime configure --runtime=docker
        systemctl restart docker
    fi
else
    log "no GPU on this VM - the cpu profile is the one to run"
fi

# ---------------------------------------------------------------- directories and the model
mkdir -p "$APP_DIR"/{models,exports,backups}

model_path="$APP_DIR/models/$MODEL_FILE"
if [ ! -f "$model_path" ] || [ "$(stat -c %s "$model_path")" -lt "$MODEL_MIN_BYTES" ]; then
    log "downloading the model (2.4 GB)"
    # To a temporary name, moved only once complete. A half-written file at the real path would
    # pass the size check on the next boot and never be repaired.
    curl -fL --retry 5 --retry-delay 10 -o "$model_path.part" "$MODEL_URL"
    mv "$model_path.part" "$model_path"
    log "model downloaded"
else
    log "model already present"
fi

# The API container runs as uid 10001 and writes exports and backups into these.
chown -R 10001:10001 "$APP_DIR/exports" "$APP_DIR/backups"

log "ready"
