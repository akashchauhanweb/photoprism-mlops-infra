#!/usr/bin/env bash
# seal_dockerhub.sh — create and seal the Docker Hub PAT secret.
# Run once (or after rotating the PAT) from the repo root on node1.
#
# Usage:
#   bash scripts/lib/seal_dockerhub.sh
#
# Requires: kubectl, kubeseal, and a valid KUBECONFIG pointing at the cluster.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
DEST="$REPO_ROOT/k8s/platform/services/dockerhub-sealed-secret.yaml"
NS="photoprism-platform"

# Prompt for PAT (hidden input)
read -r -s -p "Docker Hub PAT: " DOCKER_HUB_PAT
echo

[[ -n "$DOCKER_HUB_PAT" ]] || { echo "No PAT entered — aborting." >&2; exit 1; }

kubectl create secret generic dockerhub-secret \
    --namespace "$NS" \
    --from-literal=DOCKER_HUB_PAT="$DOCKER_HUB_PAT" \
    --dry-run=client -o yaml \
  | kubeseal --format yaml > "$DEST"

unset DOCKER_HUB_PAT

echo "Sealed secret written to $DEST"
echo "Commit it:  git add $DEST && git commit -m 'Secrets: seal Docker Hub PAT'"
