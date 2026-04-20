# 30_databases.sh — postgres, mariadb, qdrant.

log "creating postgres-init ConfigMap from canonical init.sql..."
kubectl -n photoprism-platform create configmap postgres-init \
    --from-file=init.sql="$REPO_ROOT/docker/data_docker_files/postgres/init.sql" \
    --dry-run=client -o yaml | kubectl apply -f -

log "applying postgres (MLOps DB, platform ns)..."
kubectl apply -f "$REPO_ROOT/k8s/platform/postgres.yaml"

log "applying qdrant (vector DB, platform ns)..."
kubectl apply -f "$REPO_ROOT/k8s/platform/qdrant.yaml"

log "applying mariadb (photoprism app DB, production ns)..."
kubectl apply -f "$REPO_ROOT/k8s/production/mariadb.yaml"

log "waiting for databases..."
kubectl -n photoprism-platform   rollout status statefulset/postgres --timeout=3m
kubectl -n photoprism-platform   rollout status deployment/qdrant    --timeout=3m
kubectl -n photoprism-production rollout status deployment/mariadb   --timeout=3m


log "databases OK"
