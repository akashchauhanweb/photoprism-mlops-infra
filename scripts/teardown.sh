#!/usr/bin/env bash
# PhotoPrism MLOps — full teardown.
# Deletes K8s namespaces, the sealed-secrets master key, and the GPU containers.
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
  - Helm releases : kube-prom, loki, promtail (in monitoring ns)
  - Sealed-secrets master key in kube-system
  - Reranker + feedback-trainer + dcgm-exporter containers on GPU VM (${RERANKER_IP})

A backup will be taken to S3 first (originals, storage, mariadb, qdrant, postgres).
On failure of any component, teardown proceeds anyway with a loud warning.

EOF
read -r -p "Type 'destroy' to continue: " ans
[[ "$ans" == "destroy" ]] || die "aborted"

# ---- Snapshot persistent state to S3 BEFORE destroying anything ----
log "running backup.sh before teardown..."
if bash "$REPO_ROOT/scripts/backup.sh"; then
    log "backup OK"
else
    warn "backup script reported errors — proceeding with teardown anyway"
fi

# ---- Stop GPU containers ----
if [[ -n "${RERANKER_IP:-}" ]]; then
    log "stopping GPU containers on ${RERANKER_IP}..."
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i "$RERANKER_SSH_KEY" \
        "$RERANKER_SSH_USER@$RERANKER_IP" \
        "docker stop reranker-api feedback-trainer dcgm-exporter 2>/dev/null; \
         docker rm   reranker-api feedback-trainer dcgm-exporter 2>/dev/null; true" \
        || warn "  could not reach GPU VM (already down?)"
else
    warn "RERANKER_IP empty — skipping GPU container cleanup"
fi

# ---- Uninstall helm releases (monitoring stack) ----
# We do this BEFORE namespace deletion so helm cleans up CRDs / leftover PVCs cleanly.
if command -v helm >/dev/null 2>&1; then
    log "uninstalling helm releases in monitoring namespace..."
    for rel in kube-prom loki promtail loki-stack; do
        if helm -n monitoring list -q 2>/dev/null | grep -qx "$rel"; then
            helm -n monitoring uninstall "$rel" >/dev/null 2>&1 \
                && log "  uninstalled $rel" \
                || warn "  $rel uninstall non-clean"
        fi
    done
    # PVCs from helm releases (helm doesn't auto-remove these)
    kubectl -n monitoring get pvc -o name 2>/dev/null \
        | xargs -r kubectl -n monitoring delete --wait=false 2>/dev/null || true
fi

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
