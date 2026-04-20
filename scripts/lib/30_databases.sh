# 30_databases.sh — postgres, mariadb, qdrant.

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

log "initializing postgres schema..."
# feature_jobs + search tables. Idempotent (IF NOT EXISTS).
kubectl -n photoprism-platform exec -i postgres-0 -- \
    psql -U photoprism -d photoprism_mlops <<'SQL'
CREATE TABLE IF NOT EXISTS image_metadata (
    image_id TEXT PRIMARY KEY,
    split TEXT NOT NULL,
    source TEXT,
    dataset_version TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS feature_jobs (
    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    image_id TEXT REFERENCES image_metadata(image_id),
    s3_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS search_queries (
    query_id UUID PRIMARY KEY,
    query_text TEXT NOT NULL,
    top_k INT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS search_results (
    query_id UUID REFERENCES search_queries(query_id),
    rank INT NOT NULL,
    image_id TEXT NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (query_id, rank)
);
CREATE TABLE IF NOT EXISTS feedback_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_id UUID,
    image_id TEXT,
    event_type TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
SQL

log "databases OK"
