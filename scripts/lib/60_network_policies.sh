# 60_network_policies.sh — default-deny + per-service allow rules.

log "applying network policies..."
kubectl apply -f "$REPO_ROOT/k8s/platform/network-policies.yaml"

# Ensure stale allow-mlflow (if present from old deploys) is gone
kubectl -n photoprism-platform delete networkpolicy allow-mlflow 2>/dev/null || true

log "network policies OK"
