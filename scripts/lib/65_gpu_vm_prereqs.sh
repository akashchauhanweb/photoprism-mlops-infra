# 65_gpu_vm_prereqs.sh — install docker + nvidia-container-toolkit on the GPU VM.
# Idempotent: skips installs that are already present. Runs before 70_reranker
# and 75_feedback_trainer.
#
# Assumes Ubuntu 22.04 / 24.04 on the GPU VM (CHI@UC bare-metal default image).

if [[ -z "${RERANKER_IP:-}" ]]; then
    warn "RERANKER_IP empty — skipping GPU VM prereqs"
    return 0
fi

SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i $RERANKER_SSH_KEY $RERANKER_SSH_USER@$RERANKER_IP"

log "checking GPU VM prereqs on $RERANKER_IP..."

# Probe: docker present + GPU runtime usable
if $SSH "docker --version >/dev/null 2>&1 && docker info 2>/dev/null | grep -q 'Runtimes:.*nvidia'"; then
    log "  docker + nvidia-container-toolkit already installed — skipping"
    return 0
fi

log "  installing docker (official apt repo) + nvidia-container-toolkit..."

# Single SSH session that does everything; output is streamed.
$SSH 'bash -s' <<'REMOTE_EOF'
set -euo pipefail

log()  { echo "[gpu-vm] $*"; }
warn() { echo "[gpu-vm] WARN: $*" >&2; }

# --- 1. Docker (official repo, not docker.io) ---
if ! command -v docker >/dev/null 2>&1; then
    log "installing docker..."
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc

    UBUNTU_CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $UBUNTU_CODENAME stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    sudo usermod -aG docker "$USER" || true
    sudo systemctl enable --now docker
else
    log "docker already present"
fi

# --- 2. NVIDIA Container Toolkit ---
if ! docker info 2>/dev/null | grep -q 'Runtimes:.*nvidia'; then
    log "installing nvidia-container-toolkit..."

    distribution=$(. /etc/os-release && echo "$ID$VERSION_ID")
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

    curl -fsSL "https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list" \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null

    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nvidia-container-toolkit

    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
else
    log "nvidia-container-toolkit already configured"
fi

# --- 3. Sanity: GPU visible inside a container ---
log "verifying GPU visibility from container..."
if sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi -L 2>/dev/null \
    | grep -q 'GPU '; then
    log "GPU verified inside container"
else
    warn "GPU not visible inside container — check 'nvidia-smi' on host"
fi

log "GPU VM prereqs OK"
REMOTE_EOF

log "GPU VM prereqs OK"
