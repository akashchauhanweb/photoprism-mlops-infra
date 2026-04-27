# 30_databases.sh — postgres, mariadb, qdrant.
# Ensures the user DBs exist after Postgres pod is Ready. Idempotent —
# safe to re-run on a populated cluster. Independent of init.sql, which only
# fires on a virgin data dir.

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

log "ensuring user databases exist (photoprism_mlops, mlflow)..."
PG_USER=$(kubectl -n photoprism-platform get secret postgres-credentials \
    -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
[[ -n "$PG_USER" ]] || die "postgres user missing from secret"

# pg_isready first — rollout-status returns Ready before listener is bound.
for i in {1..30}; do
    if kubectl -n photoprism-platform exec postgres-0 -- \
        pg_isready -U "$PG_USER" >/dev/null 2>&1; then break; fi
    sleep 2
done

# Idempotent CREATE DATABASE via SELECT-then-CREATE (CREATE DATABASE IF NOT
# EXISTS doesn't exist in postgres). Connect to 'postgres' (always exists),
# never to a user DB which may not exist yet.
for db in photoprism_mlops mlflow; do
    exists=$(kubectl -n photoprism-platform exec postgres-0 -- \
        psql -U "$PG_USER" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname='$db'")
    if [[ "$exists" == "1" ]]; then
        log "  [$db] already exists"
    else
        log "  [$db] creating..."
        kubectl -n photoprism-platform exec postgres-0 -- \
            psql -U "$PG_USER" -d postgres -c "CREATE DATABASE $db" >/dev/null
    fi
done

# Apply init.sql against photoprism_mlops every run (it's idempotent — uses
# CREATE TABLE IF NOT EXISTS / CREATE EXTENSION IF NOT EXISTS). Postgres only
# auto-runs /docker-entrypoint-initdb.d/ on a virgin PGDATA, so we cannot
# rely on that path on any restart with a preserved volume.
log "  applying init.sql to photoprism_mlops (idempotent)..."
kubectl -n photoprism-platform exec -i postgres-0 -- \
    psql -U "$PG_USER" -d photoprism_mlops -v ON_ERROR_STOP=1 -q \
    < "$REPO_ROOT/docker/data_docker_files/postgres/init.sql" >/dev/null \
    || warn "  init.sql apply had errors (non-fatal)"

log "databases OK"
