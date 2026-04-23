# 40_services.sh — ingest-api, clip-api, search-api, feature-worker.
# search-api gets RERANKER_URL set at runtime (not baked in YAML).

log "applying clip-api..."
kubectl apply -f "$REPO_ROOT/k8s/platform/services/clip-api.yaml"

log "applying ingest-api..."
kubectl apply -f "$REPO_ROOT/k8s/platform/services/ingest-api.yaml"

log "applying search-api..."
kubectl apply -f "$REPO_ROOT/k8s/platform/services/search-api.yaml"

log "applying feature-worker..."
kubectl apply -f "$REPO_ROOT/k8s/platform/services/feature-worker.yaml"

log "setting RERANKER_URL at runtime..."
kubectl -n photoprism-platform set env deployment/search-api \
    RERANKER_URL="http://${RERANKER_IP}:8000" \
    RETRAIN_URL="http://${RERANKER_IP}:8002"

log "waiting for rollouts..."
kubectl -n photoprism-platform rollout status deployment/clip-api        --timeout=3m
kubectl -n photoprism-platform rollout status deployment/ingest-api      --timeout=3m
kubectl -n photoprism-platform rollout status deployment/search-api      --timeout=3m
kubectl -n photoprism-platform rollout status deployment/feature-worker  --timeout=3m

log "services OK"
