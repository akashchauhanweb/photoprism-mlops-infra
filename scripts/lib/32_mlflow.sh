# 32_mlflow.sh — MLflow tracking server.
# Backend: postgres (mlflow DB, created by init.sql).
# Artifacts: S3 (mlflow/ prefix in objectstore bucket).

log "ensuring mlflow database exists in postgres..."
for i in {1..30}; do
    if kubectl -n photoprism-platform exec postgres-0 -- pg_isready -U postgres >/dev/null 2>&1; then break; fi
    sleep 2
done
kubectl -n photoprism-platform exec postgres-0 -- psql -U photoprism -d photoprism_mlops -tAc \
    "SELECT 1 FROM pg_database WHERE datname='mlflow'" | grep -q 1 \
    || kubectl -n photoprism-platform exec postgres-0 -- psql -U photoprism -d photoprism_mlops -c "CREATE DATABASE mlflow" >/dev/null

log "applying mlflow..."
kubectl apply -f "$REPO_ROOT/k8s/platform/mlflow.yaml"

log "waiting for mlflow rollout..."
kubectl -n photoprism-platform rollout status deployment/mlflow --timeout=3m \
    || warn "  mlflow rollout slow; continuing"

log "mlflow OK"
