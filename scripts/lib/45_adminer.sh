# 45_adminer.sh — Adminer DB UI (Postgres + MariaDB browser)
log "deploying Adminer..."
kubectl apply -f "$REPO_ROOT/k8s/platform/adminer/"
kubectl -n photoprism-platform rollout status deployment/adminer --timeout=120s
log "Adminer ready at http://${NODE1_FLOATING_IP}:30801"
