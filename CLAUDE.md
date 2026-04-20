# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Infrastructure-as-Code for a PhotoPrism-based ML photo search platform on Chameleon Cloud (KVM@TACC). The system combines a photo management app (PhotoPrism) with a semantic search pipeline backed by CLIP embeddings and Qdrant.

## Repository Layout

```
tf/kvm/              # Terraform: 3-VM OpenStack cluster (node1=jump host, node2/3=workers)
k8s/
  production/        # PhotoPrism + MariaDB manifests
  platform/          # MLFlow, Qdrant, PostgreSQL, and platform sealed secrets
scripts/
  bring_up.sh        # Cluster bring-up orchestrator (run on node1 after kubespray)
  config.env.example # Required config — copy to config.env and fill in
  lib/               # Numbered modules sourced by bring_up.sh (00–99)
services/
  clip-api/          # FastAPI: CLIP image+text embeddings (openai/clip-vit-base-patch32)
  ingest-api/        # FastAPI: PhotoPrism webhook receiver, enqueues feature jobs
  feature-worker/    # Long-running daemon: polls feature_jobs, embeds → Qdrant
  search-api/        # FastAPI: text query → CLIP → Qdrant ANN → optional rerank
  reranker-api/      # FastAPI: Qwen2-VL-2B + LoRA reranker (GPU VM, CHI@UC)
  _shared/           # Middleware templates copied into each service image at build time
provision/
  0_reserve_and_provision.ipynb  # Chameleon Jupyter: Terraform-based VM provisioning
monitoring/dashboards/           # Grafana dashboard JSON
docker/                          # Training and data Docker files
```

## Bring-Up Flow

**Step 1 (Chameleon Jupyter):** Run `provision/0_reserve_and_provision.ipynb` — creates lease, 3 VMs, private network, floating IP via Terraform.

**Step 2–5 (node1 terminal):** SSH into `cc@<FLOATING_IP>`, set up inter-node SSH, disable firewalld, configure Docker daemon for registry mirror, install Kubernetes via kubespray (release-2.26).

**Step 6 (node1):** Post-K8s setup — copy kubeconfig, install local-path-provisioner as default StorageClass, patch metrics-server for insecure TLS, fix IPv6/DNS/CoreDNS.

**Step 7 (node1):** Create namespaces, clone this repo, then run `scripts/bring_up.sh`.

```bash
# Full bring-up (from repo root on node1)
cp scripts/config.env.example scripts/config.env  # fill in values
bash scripts/bring_up.sh

# Run a single module
bash scripts/bring_up.sh --step 40_services
```

`bring_up.sh` sources modules in order: `00_prereqs → 10_security_groups → 20_sealed_secrets → 30_databases → 40_services → 50_photoprism → 60_network_policies → 70_reranker → 80_grafana → 85_dashboard → 99_smoke`

## Service Development

Each service under `services/` has a `Makefile` with `build`, `push`, `all` targets:

```bash
# Build and push a service image
cd services/clip-api
make TAG=0.1.2 all           # builds akashweb/clip-api:0.1.2 and pushes

# Build only
make TAG=0.1.2 build
```

Image tags are pinned in `scripts/config.env` (e.g. `CLIP_API_TAG=0.1.1`). After building a new image, update the tag there and re-run the relevant `bring_up.sh` step.

`_shared/` middleware is **copied** (not imported) into each service image at build time — changes must be manually propagated to each Dockerfile.

## Sealed Secrets

All Kubernetes secrets are stored as `SealedSecret` objects (Bitnami sealed-secrets v0.27.1). The master key backup lives at `$SEALED_SECRETS_KEY_BACKUP` (default `~/.ssh/sealed-secrets-key-backup.yaml`).

When redeploying on a new cluster, module `20_sealed_secrets.sh` always restores the backup key before applying sealed secrets — the controller's auto-generated key is deleted to prevent divergence.

To reseal a new secret:
```bash
kubectl create secret generic my-secret --dry-run=client -o yaml \
  | kubeseal --controller-namespace kube-system -o yaml > k8s/.../my-sealed-secret.yaml
```

## Architecture — Semantic Search Pipeline

```
PhotoPrism webhook
      │ POST /webhook/photo-imported
      ▼
  ingest-api (port 8004)
      │ downloads original from PhotoPrism, stores in S3,
      │ inserts row into feature_jobs (PostgreSQL)
      ▼
  feature-worker (polls every 5s)
      │ reads image from S3 → POST /embed/image to clip-api
      │ upserts 512-d vector into Qdrant collection "image_embeddings"
      ▼
  search-api (port 8010)  ◄── user query
      │ POST /embed/text to clip-api → ANN search in Qdrant
      │ optionally reranks top-K via reranker-api (GPU, CHI@UC)
      │ logs query + results to PostgreSQL
      └─► returns ranked SearchHit list
```

- **CLIP model:** `openai/clip-vit-base-patch32`, 512-d cosine vectors
- **Reranker:** `Qwen2-VL-2B-Instruct` + LoRA weights, toggled via `RERANKER_ENABLED=true`
- **Internal auth:** all service-to-service calls require `X-Internal-Token` header (from `internal-token-sealed-secret`)
- **Object store:** S3-compatible (Chameleon Swift), originals stored under `originals/<photo_uid>`

## Infrastructure Details

- **Kubernetes:** kubespray 2.26, container runtime: Docker (not containerd), `local-path` default StorageClass
- **Networking:** node1 = jump host with floating IP; nodes communicate on `192.168.1.0/24`; all NodePorts exposed on node1
- **Namespaces:** `photoprism-production` (PhotoPrism + MariaDB), `photoprism-platform` (Qdrant, PostgreSQL, MLFlow, ML services)
- **Terraform:** OpenStack provider, state is local (not remote) — `tf/kvm/`

## Access URLs (after deploy)

| Service | URL | Credentials |
|---------|-----|-------------|
| PhotoPrism | `http://<FLOATING_IP>:30234` | admin / photoprism-admin |
| Search API | `http://<FLOATING_IP>:30810/search` | — |
| Qdrant | `http://<FLOATING_IP>:30633/dashboard/` | — |
| MLFlow | `http://<FLOATING_IP>:30500` | — |
| Grafana | `http://<FLOATING_IP>:30300` | admin / (from secret) |
| K8s Dashboard | `https://<FLOATING_IP>:30443` | bearer token (from secret) |

## Key Config Variables (`scripts/config.env`)

| Variable | Purpose |
|----------|---------|
| `RERANKER_IP` | GPU VM IP (CHI@UC fixed lease) |
| `DOCKER_HUB_USER` | Docker Hub org for pushing images |
| `*_TAG` | Pinned image tags for each service |
| `SEALED_SECRETS_KEY_BACKUP` | Path to sealed-secrets master key backup |
| `OS_CLOUD` | OpenStack cloud alias from `clouds.yaml` |
