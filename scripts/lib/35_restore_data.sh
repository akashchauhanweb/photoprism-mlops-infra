# 35_restore_data.sh — restore DB dumps (qdrant, postgres) after 30_databases.
# First run: no backup on S3 → skip. Subsequent run: download latest and load.

# shellcheck disable=SC1091
source "$LIB_DIR/_s3_common.sh"

s3_load_creds
rclone_ensure

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' RETURN

# ---- postgres ----
restore_postgres() {
    local comp=postgres
    if ! s3_has_latest "$comp" "latest.sql.gz"; then
        log "  [postgres] no backup on S3 — skipping (first run)"
        return 0
    fi
    local ns=photoprism-platform
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "  [postgres] no pod found; skipping"; return 1; }
    local user db
    user=$(kubectl -n "$ns" get secret postgres-credentials -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
    db=$(kubectl -n "$ns" get secret postgres-credentials -o jsonpath='{.data.POSTGRES_DB}' | base64 -d)
    local dump="$WORKDIR/postgres.sql.gz"

    log "  [postgres] downloading latest.sql.gz..."
    "$rclone_bin" copyto "$(s3_path $comp)/latest.sql.gz" "$dump" \
        || { warn "  [postgres] download FAILED"; return 1; }

    log "  [postgres] waiting for pod Ready..."
    kubectl -n "$ns" wait --for=condition=Ready pod/"$pod" --timeout=3m \
        || { warn "  [postgres] pod not ready"; return 1; }

    log "  [postgres] loading dump into db=$db..."
    if gunzip -c "$dump" | kubectl -n "$ns" exec -i "$pod" -- \
        psql -U "$user" -d "$db" -v ON_ERROR_STOP=1 -q >/dev/null; then
        log "  [postgres] restore OK"
    else
        warn "  [postgres] psql load FAILED"
    fi
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

    # Use /snapshots/upload (multipart) — supports direct snapshot file upload
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
restore_postgres
restore_qdrant

log "data restore OK"
