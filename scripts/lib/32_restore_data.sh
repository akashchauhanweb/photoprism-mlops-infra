# 32_restore_data.sh — restore DB dumps (mariadb, postgres per-DB, qdrant) after 30_databases.
# Runs BEFORE 35_mlflow so MLflow connects to a fully-restored mlflow DB.
#
# Postgres restore precedence per DB:
#   1. backups/postgres/<db>/latest.sql.gz       (live CronJob output)
#   2. mlflow-imported/kiran/postgres_dump.sql.gz  (one-time seed; mlflow DB only,
#                                                  used only if mlflow has 0 rows)
#   3. skip                                        (truly first run)
#
# The Kiran dump was taken from PG18; this cluster runs PG16.
# We strip incompatible top-level GUCs (`SET transaction_timeout`) and
# psql meta-commands (`\restrict`, `\unrestrict`) before piping to psql.
#
# Artifacts: on the same first-run path, copies Kiran's mirrored artifacts from
#            mlflow-imported/kiran/ to mlflow/ so MLflow's --default-artifact-root
#            can resolve mlflow-artifacts:/<exp>/<run>/... URIs.

# shellcheck disable=SC1091
source "$LIB_DIR/_s3_common.sh"

s3_load_creds
rclone_ensure

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' RETURN

PG_NS=photoprism-platform
PG_POD=postgres-0
PG_USER=$(kubectl -n "$PG_NS" get secret postgres-credentials \
    -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
[[ -n "$PG_USER" ]] || die "postgres user missing"

# Strip lines that PG16 won't accept from a PG18 dump.
pg_compat_filter() {
    sed -e '/^SET transaction_timeout = /d' \
        -e '/^\\restrict /d' \
        -e '/^\\unrestrict /d'
}

# Returns "0" if DB has zero non-template rows in the named "fingerprint" table.
# Used to decide whether the Kiran fallback should fire for mlflow.
db_is_empty() {
    local db="$1" table="$2"
    local n
    n=$(kubectl -n "$PG_NS" exec "$PG_POD" -- \
        psql -U "$PG_USER" -d "$db" -tAc "SELECT count(*) FROM $table" 2>/dev/null \
        || echo "ERR")
    [[ "$n" == "0" ]]
}

# ---- per-DB postgres restore ----
restore_pg_db() {
    local db="$1" fingerprint_table="$2"
    local comp="postgres/$db"
    local dump="$WORKDIR/${db}.sql.gz"

    if s3_has_latest "$comp" "latest.sql.gz"; then
        log "  [pg/$db] backup found at $(s3_path "$comp")/latest.sql.gz — restoring"
        "$rclone_bin" copyto "$(s3_path "$comp")/latest.sql.gz" "$dump" \
            || { warn "  [pg/$db] download FAILED"; return 1; }
        if gunzip -c "$dump" | pg_compat_filter | \
            kubectl -n "$PG_NS" exec -i "$PG_POD" -- \
            psql -U "$PG_USER" -d "$db" -v ON_ERROR_STOP=1 -q >/dev/null; then
            log "  [pg/$db] restore OK"
        else
            warn "  [pg/$db] psql load FAILED"
        fi
        return 0
    fi
    log "  [pg/$db] no backup at $(s3_path "$comp")/ — skipping"
    return 0
}

# ---- one-time: import Kiran's mlflow dump + artifacts ----
restore_kiran_mlflow() {
    local kiran_dump_key="mlflow-imported/kiran/postgres_dump.sql.gz"
    local kiran_dump_url="${RCLONE_REMOTE}:${S3_BUCKET}/${kiran_dump_key}"

    if ! "$rclone_bin" lsf "$kiran_dump_url" >/dev/null 2>&1; then
        log "  [kiran] no archived dump at $kiran_dump_key — skipping"
        return 0
    fi

    if ! db_is_empty mlflow experiments; then
        log "  [kiran] mlflow DB already populated — skipping seed"
        return 0
    fi

    log "  [kiran] mlflow empty + archived dump exists — seeding"
    local dump="$WORKDIR/kiran-mlflow.sql.gz"
    "$rclone_bin" copyto "$kiran_dump_url" "$dump" \
        || { warn "  [kiran] dump download FAILED"; return 1; }

    if gunzip -c "$dump" | pg_compat_filter | \
        kubectl -n "$PG_NS" exec -i "$PG_POD" -- \
        psql -U "$PG_USER" -d mlflow -v ON_ERROR_STOP=1 -q >/dev/null; then
        log "  [kiran] dump loaded into mlflow DB"
    else
        warn "  [kiran] dump load FAILED"
        return 1
    fi

    log "  [kiran] mirroring artifacts: mlflow-imported/kiran/ -> mlflow/ (idempotent)"
    "$rclone_bin" copy \
        "${RCLONE_REMOTE}:${S3_BUCKET}/mlflow-imported/kiran/" \
        "${RCLONE_REMOTE}:${S3_BUCKET}/mlflow/" \
        --exclude "postgres_dump.sql.gz" \
        --transfers 8 \
        || warn "  [kiran] artifact mirror had errors (non-fatal)"
    log "  [kiran] seed OK"
}

# ---- qdrant ----
restore_qdrant() {
    local comp=qdrant
    if ! s3_has_latest "$comp" "latest.snapshot"; then
        log "  [qdrant] no backup on S3 — skipping (first run)"
        return 0
    fi
    local ns=photoprism-platform
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=qdrant -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "  [qdrant] no pod found; skipping"; return 1; }
    local coll="image_embeddings"
    local snap="$WORKDIR/qdrant.snapshot"

    log "  [qdrant] downloading latest.snapshot..."
    "$rclone_bin" copyto "$(s3_path $comp)/latest.snapshot" "$snap" \
        || { warn "  [qdrant] download FAILED"; return 1; }

    log "  [qdrant] waiting for pod Ready..."
    kubectl -n "$ns" wait --for=condition=Ready pod/"$pod" --timeout=3m \
        || { warn "  [qdrant] pod not ready"; return 1; }

    log "  [qdrant] port-forwarding and uploading snapshot..."
    kubectl -n "$ns" port-forward "$pod" 16333:6333 >/dev/null 2>&1 &
    local pf_pid=$!

    for i in {1..30}; do
        if curl -sS --max-time 2 http://localhost:16333/ >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done

    local resp
    resp=$(curl -sS -X POST \
        -F "snapshot=@${snap}" \
        "http://localhost:16333/collections/${coll}/snapshots/upload?priority=snapshot")
    kill $pf_pid 2>/dev/null

    if echo "$resp" | grep -q '"status":"ok"'; then
        log "  [qdrant] restore OK"
    else
        warn "  [qdrant] recover FAILED: $resp"
    fi
}

# ---- mariadb ----
restore_mariadb() {
    local comp=mariadb
    if ! s3_has_latest "$comp" "latest.sql.gz"; then
        log "  [mariadb] no backup on S3 — skipping (first run)"
        return 0
    fi
    local ns=photoprism-production
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=mariadb -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "  [mariadb] no pod found; skipping"; return 1; }
    local pass
    pass=$(kubectl -n "$ns" get secret photoprism-secrets -o jsonpath='{.data.mariadb-password}' | base64 -d)
    local dump="$WORKDIR/mariadb.sql.gz"

    log "  [mariadb] downloading latest.sql.gz..."
    "$rclone_bin" copyto "$(s3_path $comp)/latest.sql.gz" "$dump" \
        || { warn "  [mariadb] download FAILED"; return 1; }

    log "  [mariadb] waiting for pod Ready..."
    kubectl -n "$ns" wait --for=condition=Ready pod/"$pod" --timeout=3m \
        || { warn "  [mariadb] pod not ready"; return 1; }

    log "  [mariadb] waiting for mysqld socket..."
    for i in {1..30}; do
        if kubectl -n "$ns" exec "$pod" -- sh -c "mariadb -u photoprism -p'$pass' -e 'SELECT 1' >/dev/null 2>&1"; then
            break
        fi
        sleep 2
    done

    log "  [mariadb] loading dump into db=photoprism..."
    if gunzip -c "$dump" | kubectl -n "$ns" exec -i "$pod" -- \
        sh -c "mariadb -u photoprism -p'$pass' photoprism" >/dev/null; then
        log "  [mariadb] restore OK"
    else
        warn "  [mariadb] mariadb load FAILED"
    fi
}

log "=== data restore (post-DB) ==="
restore_mariadb
restore_pg_db photoprism_mlops image_metadata
restore_pg_db mlflow            experiments
restore_kiran_mlflow
restore_qdrant

log "data restore OK"
