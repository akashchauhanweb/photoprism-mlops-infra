# PhotoPrism MLOps Infrastructure

Infrastructure as Code for a PhotoPrism-based semantic photo search platform on Chameleon Cloud (KVM@TACC + CHI@UC GPU).

## Architecture

- **K8s cluster** on KVM@TACC: 3 VMs (node1 = control plane + jump host with floating IP; node2/3 = workers), provisioned via Terraform, bootstrapped with kubespray `release-2.26`
- **GPU VM** on CHI@UC: hosts reranker + feedback-trainer as Docker containers
- **Object store**: S3-compatible (Chameleon Swift via S3 API) — used for photo originals and backups
- **Namespaces**: `photoprism-production` (PhotoPrism + MariaDB), `photoprism-platform` (Postgres + Qdrant + ML services)

## Services

### In-cluster
| Service | Namespace | NodePort | Purpose |
|---|---|---|---|
| PhotoPrism | photoprism-production | 30234 | Photo management app (custom fork with semantic search + click tracking) |
| MariaDB | photoprism-production | — | PhotoPrism metadata DB |
| Postgres | photoprism-platform | 30532 | Feature-jobs queue + click/feedback logs |
| Qdrant | photoprism-platform | 30633 | CLIP vector DB (`image_embeddings`) |
| clip-api | photoprism-platform | — | CLIP ViT-B/32 image+text embeddings |
| ingest-api | photoprism-platform | — | PhotoPrism webhook receiver → S3 + feature job queue |
| search-api | photoprism-platform | 30810 | Text query → CLIP → Qdrant ANN → reranker |
| feature-worker | photoprism-platform | — | Polls feature_jobs, embeds, writes to Qdrant |

### Platform (monitoring + secrets)
| Service | NodePort | Purpose |
|---|---|---|
| Grafana + Loki | 30300 | Logs + dashboards |
| K8s Dashboard | 30443 | Cluster UI |
| Sealed Secrets | — | Encrypted secrets in git |

### On GPU VM (CHI@UC)
| Service | Port | Purpose |
|---|---|---|
| reranker-api | 8000 | Qwen2-VL-2B + LoRA reranker |
| feedback-trainer | 8002 | Retrains LoRA from Postgres feedback |

## Persistent state

All stateful components are backed up to S3 on teardown and restored on next bring-up:
- `photoprism-originals` PVC (uploaded photos)
- `photoprism-storage` PVC (thumbs, sidecars, index)
- MariaDB (PhotoPrism metadata)
- Postgres (feature-jobs, feedback, clicks)
- Qdrant (CLIP embeddings)

S3 layout: `<bucket>/backups/<component>/{YYYYMMDD-HHMM,latest}.<ext>`

## Prerequisites (on your Mac)

- `~/.ssh/id_rsa_chameleon` — registered on Chameleon
- `~/.ssh/id_proj24_gpu` — registered on Chameleon (for GPU VM)
- `~/.ssh/sealed-secrets-key-backup.yaml` — master key backup
- `clouds.yaml` for KVM@TACC (application credentials)

## Bring-up

### 1. Provision infrastructure (Chameleon Jupyter)
Run `provision/0_reserve_and_provision.ipynb`. It prints next-step commands with the floating IP filled in.

### 2. Copy files to node1 (from your Mac)
```bash
scp -i ~/.ssh/id_rsa_chameleon \
    ~/.ssh/id_rsa_chameleon ~/.ssh/id_proj24_gpu \
    ~/.ssh/sealed-secrets-key-backup.yaml \
    cc@<FLOATING_IP>:~/.ssh/

ssh -i ~/.ssh/id_rsa_chameleon cc@<FLOATING_IP> 'mkdir -p ~/.config/openstack'
scp -i ~/.ssh/id_rsa_chameleon clouds.yaml cc@<FLOATING_IP>:~/.config/openstack/clouds.yaml
```

### 3. On node1 — inter-node SSH + kubespray
Follow the printed instructions from the notebook. Summary:
```bash
chmod 600 ~/.ssh/*
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -q -N ""
cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/id_rsa.pub | ssh -i ~/.ssh/id_rsa_chameleon cc@192.168.1.12 "cat >> ~/.ssh/authorized_keys"
cat ~/.ssh/id_rsa.pub | ssh -i ~/.ssh/id_rsa_chameleon cc@192.168.1.13 "cat >> ~/.ssh/authorized_keys"

# disable broken IPv6, install deps, run kubespray in tmux (~20 min)
# then install kubeseal, helm, local-path-provisioner, metrics-server, CoreDNS patch
# then install Sealed Secrets controller + loki-stack
```

### 4. Prepare GPU VM (first time per lease only)
Needed because Chameleon GPU images ship without Docker or the NVIDIA container toolkit.
```bash
# Install Docker + NVIDIA container toolkit
ssh -i ~/.ssh/id_proj24_gpu cc@<GPU_IP> 'curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker cc'

ssh -i ~/.ssh/id_proj24_gpu cc@<GPU_IP> '
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed "s#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g" | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt-get update -q
  sudo apt-get install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
'

# Verify
ssh -i ~/.ssh/id_proj24_gpu cc@<GPU_IP> "docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi | head -5"
```

Set the GPU IP in `scripts/config.env` on node1:
```bash
cd ~/photoprism-mlops-infra
cp scripts/config.env.example scripts/config.env  # auto-done by bring_up.sh if missing
sed -i 's|^RERANKER_IP=.*|RERANKER_IP="<GPU_IP>"|' scripts/config.env
```

If the GPU isn't ready yet, leave `RERANKER_IP=""` — modules 70 and 75 skip cleanly with a warning.

### 5. Run bring-up
```bash
cd ~/photoprism-mlops-infra
bash scripts/bring_up.sh
```

Modules run in order:
```
00_prereqs → 10_security_groups → 20_sealed_secrets
25_restore_volumes   ← restores originals + storage from S3 (skips on first run)
30_databases         ← MariaDB, Postgres, Qdrant
35_restore_data      ← restores MariaDB, Postgres, Qdrant dumps (skips on first run)
40_services          ← clip-api, ingest-api, search-api, feature-worker
50_photoprism → 60_network_policies → 70_reranker → 75_feedback_trainer
80_grafana → 85_dashboard → 99_smoke
```

Run a single module: `bash scripts/bring_up.sh --step 40_services`

### 6. Verify
```bash
kubectl get pods -A | grep photoprism
FIP=$(curl -s https://api.ipify.org)
curl -sS -o /dev/null -w "PhotoPrism: %{http_code}\n"  http://$FIP:30234/api/v1/status
curl -sS -o /dev/null -w "Search API: %{http_code}\n"  http://$FIP:30810/health
curl -sS -o /dev/null -w "Qdrant:     %{http_code}\n"  http://$FIP:30633/
ssh -i ~/.ssh/id_proj24_gpu cc@<GPU_IP> "docker ps"
```

## Teardown

```bash
cd ~/photoprism-mlops-infra
bash scripts/teardown.sh
```

Order:
1. Prompts for confirmation (type `destroy`)
2. Runs `backup.sh` — snapshots all 5 stateful components to S3
3. Stops reranker + feedback-trainer on GPU VM
4. Deletes K8s namespaces
5. Deletes sealed-secrets master key in kube-system

Then, on Chameleon Jupyter, run teardown cells in `0_reserve_and_provision.ipynb` to destroy VMs + release floating IP + delete lease.

## Manual backup (without teardown)
```bash
bash scripts/backup.sh
```

## Access URLs
| Service | URL | Credentials |
|---|---|---|
| PhotoPrism | `http://<FLOATING_IP>:30234` | admin / photoprism-admin |
| Search API | `http://<FLOATING_IP>:30810/search` | — |
| Qdrant | `http://<FLOATING_IP>:30633/dashboard/` | — |
| Grafana | `http://<FLOATING_IP>:30300` | admin / (from `loki-stack-grafana` secret) |
| K8s Dashboard | `https://<FLOATING_IP>:30443` | bearer token (from `dashboard-admin-token`) |
| Reranker (internal) | `http://<GPU_IP>:8000/health` | — |
| Feedback (internal) | `http://<GPU_IP>:8002/health` | — |

## Image tags

Pinned in `scripts/config.env.example`. Rebuild with `cd services/<name> && make TAG=X.Y.Z all`, then bump the tag in `config.env`.

## Repository layout
```
tf/kvm/                       # Terraform: 3-VM cluster + network + floating IP
provision/                    # Chameleon Jupyter notebook for lease + Terraform
k8s/
  production/                 # PhotoPrism + MariaDB
  platform/                   # Postgres, Qdrant, services, sealed secrets
  platform/jobs/              # Templated Jobs (e.g. PVC restore)
scripts/
  bring_up.sh                 # Orchestrator
  teardown.sh                 # Backup + destroy
  backup.sh                   # Standalone backup to S3
  config.env.example          # Copy to config.env, fill in per-lease values
  lib/                        # Numbered modules sourced by bring_up.sh
services/                     # FastAPI apps + feature-worker + retraining_loop
docker/                       # Training + data docker files
monitoring/dashboards/        # Grafana dashboards
```

## Known gotchas

- **Stale OpenStack ports** (status `DOWN`) cause Terraform 409. Fix from notebook:
  `openstack port list --status DOWN -f value -c ID | xargs -r -n1 openstack port delete`
- **IPv6 on `ens3`** must be disabled (Chameleon's upstream IPv6 doesn't route; Go/apt/CoreDNS hang).
- **Kubespray `helm_enabled: true`** doesn't actually install the binary — use `get-helm-3` after.
- **Qdrant and MariaDB containers** lack `wget` and `curl`; backup/restore uses `kubectl port-forward` + local tools instead.
- **`objectstore-credentials` secret** in `photoprism-production` must have `ownerReferences` stripped when copied from `photoprism-platform` or Kubernetes GC deletes it.
