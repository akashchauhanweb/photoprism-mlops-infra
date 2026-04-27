# 35_mlflow.sh — MLflow tracking server.
# DB existence is owned by 30_databases (mlflow DB ensured there).
# Restore from S3 (per-DB backup OR Kiran-imported) is owned by 32_restore_data.
# This module just deploys the server and waits for rollout.

log "applying mlflow..."
kubectl apply -f "$REPO_ROOT/k8s/platform/mlflow.yaml"

log "waiting for mlflow rollout..."
kubectl -n photoprism-platform rollout status deployment/mlflow --timeout=3m \
    || warn "  mlflow rollout slow; continuing"

log "mlflow OK"
