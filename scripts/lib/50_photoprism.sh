# 50_photoprism.sh — apply photoprism (depends on mariadb + ingest webhook secret).

log "applying photoprism..."
kubectl apply -f "$REPO_ROOT/k8s/production/photoprism.yaml"

log "waiting for rollout..."
kubectl -n photoprism-production rollout status deployment/photoprism --timeout=5m

log "photoprism OK"
