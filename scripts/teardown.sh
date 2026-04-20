#!/usr/bin/env bash
# PhotoPrism MLOps — full teardown.
# Deletes K8s namespaces, the sealed-secrets master key, and the GPU reranker container.
# Runs on node1.

set -euo pipefail

readonly GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
log()  { echo -e "${GREEN}[teardown]${RESET} $*"; }
warn() { echo -e "${YELLOW}[teardown]${RESET} $*"; }
die()  { echo -e "${RED}[teardown]${RESET} $*" >&2; exit 1; }

readonly REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ---- Load config ----
[[ -f "$REPO_ROOT/scripts/config.env" ]] \
    || die "scripts/config.env not found."
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/config.env"

# ---- Confirmation ----
cat <<EOF
${RED}============================================${RESET}
  WARNING — full teardown
${RED}============================================${RESET}
This will DELETE:
  - K8s namespaces: photoprism-platform, photoprism-production, monitoring
  - Sealed-secrets master key in kube-system
  - Reranker container on GPU VM (${RERANKER_IP})

All data in Postgres, MariaDB, Qdrant, and PhotoPrism storage will be lost.

EOF
read -r -p "Type 'destroy' to continue: " ans
[[ "$ans" == "destroy" ]] || die "aborted"

# ---- Stop GPU reranker ----
log "stopping reranker on ${RERANKER_IP}..."
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i "$RERANKER_SSH_KEY" \
    "$RERANKER_SSH_USER@$RERANKER_IP" \
    "docker stop reranker-api 2>/dev/null; docker rm reranker-api 2>/dev/null; true" \
    || warn "  could not reach GPU VM (already down?)"

# ---- Delete namespaces ----
log "deleting K8s namespaces..."
for ns in photoprism-platform photoprism-production monitoring; do
    kubectl delete namespace "$ns" --wait=false 2>/dev/null || true
done

# ---- Wait until namespaces are gone ----
log "waiting for namespace deletion (up to 3m)..."
for i in {1..90}; do
    if ! kubectl get ns photoprism-platform photoprism-production monitoring >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

# ---- Delete sealed-secrets key ----
log "deleting sealed-secrets master key..."
kubectl -n kube-system delete secret \
    -l sealedsecrets.bitnami.com/sealed-secrets-key=active 2>/dev/null || true

log "teardown complete"
log "to reprovision: bash scripts/bring_up.sh"
