# 60_network_policies.sh — default-deny + per-service allow rules.

log "applying network policies..."
kubectl apply -f "$REPO_ROOT/k8s/platform/network-policies.yaml"


log "network policies OK"
