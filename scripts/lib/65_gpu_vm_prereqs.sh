# 65_gpu_vm_prereqs.sh — install docker + nvidia-container-toolkit on the GPU VM
# AND start a DCGM exporter container so cluster-Prometheus can scrape GPU metrics
# (via the autossh reverse-tunnel deployed by 15_monitoring_stack.sh).
#
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

need_install=1
if $SSH "docker --version >/dev/null 2>&1 && docker info 2>/dev/null | grep -q 'Runtimes:.*nvidia'"; then
    log "  docker + nvidia-container-toolkit already installed"
    need_install=0
fi

if (( need_install == 1 )); then
    log "  installing docker (official apt repo) + nvidia-container-toolkit..."
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

    # Only reconfigure + restart docker if nvidia runtime isn't already wired in
    if ! sudo grep -q '"nvidia"' /etc/docker/daemon.json 2>/dev/null; then
        sudo nvidia-ctk runtime configure --runtime=docker
        sudo systemctl restart docker
    else
        log "docker already configured for nvidia runtime — not restarting"
    fi
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
fi

# --- 4. DCGM exporter container — always reconcile to current image ---
DCGM_IMAGE="${DCGM_EXPORTER_IMAGE:-nvcr.io/nvidia/k8s/dcgm-exporter:3.3.7-3.5.0-ubuntu22.04}"

log "ensuring DCGM exporter on $RERANKER_IP (image=$DCGM_IMAGE)..."
$SSH "bash -s" <<REMOTE_EOF
set -euo pipefail
DESIRED="$DCGM_IMAGE"

# Pull (cheap if cached). If pull fails, keep existing container running.
sudo docker pull "\$DESIRED" >/dev/null 2>&1 || echo "[gpu-vm] DCGM image pull failed — using cache"

running_id=\$(sudo docker inspect dcgm-exporter --format '{{.Image}}' 2>/dev/null || echo none)
desired_id=\$(sudo docker image inspect "\$DESIRED" --format '{{.Id}}' 2>/dev/null || echo none)
status=\$(sudo docker inspect dcgm-exporter --format '{{.State.Status}}' 2>/dev/null || echo none)

if [[ "\$running_id" == "\$desired_id" && "\$status" == "running" ]]; then
    echo "[gpu-vm] dcgm-exporter already running with desired image"
else
    echo "[gpu-vm] (re)deploying dcgm-exporter..."
    sudo docker stop dcgm-exporter 2>/dev/null || true
    sudo docker rm   dcgm-exporter 2>/dev/null || true
    # --net host so the autossh tunnel can reach it via 127.0.0.1:9400
    sudo docker run -d --name dcgm-exporter \
        --restart unless-stopped \
        --gpus all \
        --cap-add SYS_ADMIN \
        --net host \
        "\$DESIRED" >/dev/null
fi

# Verify metrics port is up
for i in \$(seq 1 20); do
    if curl -sS --max-time 3 http://127.0.0.1:9400/metrics 2>/dev/null | grep -q '^DCGM_'; then
        echo "[gpu-vm] dcgm-exporter healthy on :9400"
        exit 0
    fi
    sleep 3
done
echo "[gpu-vm] WARN: dcgm-exporter did not respond on :9400" >&2
REMOTE_EOF

log "GPU VM prereqs OK"
