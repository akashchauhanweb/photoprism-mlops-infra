#!/usr/bin/env bash
# backup.sh — snapshot persistent state to S3 before teardown.
# Runs on node1. Called by teardown.sh or manually.
#
# Components: photoprism-originals, photoprism-storage, mariadb, qdrant, postgres.
# Each component failure is a loud warning — never aborts the run.
# Writes <component>/YYYYMMDD-HHMM.<ext> and copies to <component>/latest.<ext>.

set -uo pipefail  # NOT -e: we want failures to warn, not abort

readonly GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
log()  { echo -e "${GREEN}[backup]${RESET} $*"; }
warn() { echo -e "${YELLOW}[backup]${RESET} $*"; }
die()  { echo -e "${RED}[backup]${RESET} $*" >&2; exit 1; }
export -f log warn die

readonly REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
readonly TS="$(date +%Y%m%d-%H%M)"
readonly WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

[[ -f "$REPO_ROOT/scripts/config.env" ]] || die "scripts/config.env not found"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/config.env"

# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/lib/_s3_common.sh"

s3_load_creds
rclone_ensure

FAILED=()

# Upload <local> to <component>/<ts>.<ext> and copy to latest.<ext>.
s3_upload() {
    local local_file="$1" comp="$2" ext="$3"
    local dest="$(s3_path "$comp")"
    log "  uploading to ${dest}/${TS}.${ext}"
    "$rclone_bin" copyto "$local_file" "${dest}/${TS}.${ext}" \
        || { warn "  upload of ${comp} timestamped copy FAILED"; return 1; }
    "$rclone_bin" copyto "$local_file" "${dest}/latest.${ext}" \
        || { warn "  upload of ${comp} latest pointer FAILED"; return 1; }
    return 0
}

# ---- 1. photoprism-originals (PVC tarball) ----
backup_originals() {
    log "=== photoprism-originals ==="
    local ns=photoprism-production
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=photoprism -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "no photoprism pod; skipping"; return 1; }
    local out="$WORKDIR/originals.tar.gz"
    log "  tarring /photoprism/originals from pod $pod..."
    kubectl -n "$ns" exec "$pod" -- tar -czf - -C /photoprism originals > "$out" \
        || { warn "tar from pod FAILED"; return 1; }
    log "  size: $(du -h "$out" | cut -f1)"
    s3_upload "$out" photoprism-originals tar.gz
}

# ---- 2. photoprism-storage (PVC tarball) ----
backup_storage() {
    log "=== photoprism-storage ==="
    local ns=photoprism-production
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=photoprism -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "no photoprism pod; skipping"; return 1; }
    local out="$WORKDIR/storage.tar.gz"
    log "  tarring /photoprism/storage from pod $pod..."
    kubectl -n "$ns" exec "$pod" -- tar -czf - -C /photoprism storage > "$out" \
        || { warn "tar from pod FAILED"; return 1; }
    log "  size: $(du -h "$out" | cut -f1)"
    s3_upload "$out" photoprism-storage tar.gz
}

# ---- 3. mariadb (mysqldump) ----
backup_mariadb() {
    log "=== mariadb ==="
    local ns=photoprism-production
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=mariadb -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "no mariadb pod; skipping"; return 1; }
    local user pass db
    user="photoprism"
    db="photoprism"
    pass=$(kubectl -n "$ns" get secret photoprism-secrets -o jsonpath='{.data.mariadb-password}' 2>/dev/null | base64 -d)
    [[ -n "$user" && -n "$pass" && -n "$db" ]] \
        || { warn "mariadb creds missing; skipping"; return 1; }
    local out="$WORKDIR/mariadb.sql.gz"
    log "  dumping db=$db from pod $pod..."
    kubectl -n "$ns" exec "$pod" -- sh -c \
        "mariadb-dump -u'$user' -p'$pass' --single-transaction --routines --triggers '$db'" \
        | gzip > "$out" \
        || { warn "mysqldump FAILED"; return 1; }
    log "  size: $(du -h "$out" | cut -f1)"
    s3_upload "$out" mariadb sql.gz
}

# ---- 4. qdrant (snapshot API via port-forward; pod has no wget/curl) ----
backup_qdrant() {
    log "=== qdrant ==="
    local ns=photoprism-platform
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=qdrant -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "no qdrant pod; skipping"; return 1; }
    local coll="image_embeddings"

    # Port-forward qdrant to local 16333 in background
    kubectl -n "$ns" port-forward "$pod" 16333:6333 >/dev/null 2>&1 &
    local pf_pid=$!
    trap "kill $pf_pid 2>/dev/null" RETURN
    sleep 2

    log "  creating snapshot for collection=$coll..."
    local snap_name
    snap_name=$(curl -sS -X POST "http://localhost:16333/collections/${coll}/snapshots" \
        | jq -r '.result.name' 2>/dev/null)
    [[ -n "$snap_name" && "$snap_name" != "null" ]] \
        || { warn "qdrant snapshot create FAILED"; kill $pf_pid 2>/dev/null; return 1; }
    log "  snapshot: $snap_name"

    local out="$WORKDIR/qdrant.snapshot"
    log "  downloading snapshot..."
    curl -sS -o "$out" "http://localhost:16333/collections/${coll}/snapshots/${snap_name}" \
        || { warn "qdrant snapshot download FAILED"; kill $pf_pid 2>/dev/null; return 1; }

    # Clean up snapshot on qdrant disk
    curl -sS -X DELETE "http://localhost:16333/collections/${coll}/snapshots/${snap_name}" \
        >/dev/null 2>&1 || true

    kill $pf_pid 2>/dev/null
    log "  size: $(du -h "$out" | cut -f1)"
    s3_upload "$out" qdrant snapshot
}

# ---- 5. postgres (per-DB pg_dump) ----
# One dump per user database, written to <prefix>/postgres/<db>/<ts>.sql.gz
# and <prefix>/postgres/<db>/latest.sql.gz. Skips template DBs and 'postgres'.
# --no-owner --no-acl: avoids ROLE drops on restore (which broke pg_dumpall).
backup_postgres() {
    log "=== postgres ==="
    local ns=photoprism-platform
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "no postgres pod; skipping"; return 1; }
    local user
    user=$(kubectl -n "$ns" get secret postgres-credentials -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
    [[ -n "$user" ]] || { warn "postgres creds missing; skipping"; return 1; }

    # Discover user DBs
    local dbs
    dbs=$(kubectl -n "$ns" exec "$pod" -- psql -U "$user" -d postgres -tAc \
        "SELECT datname FROM pg_database WHERE datistemplate = false AND datname <> 'postgres'" \
        2>/dev/null | tr -d '\r')
    [[ -n "$dbs" ]] || { warn "no user DBs found; skipping"; return 1; }

    local rc=0
    while IFS= read -r db; do
        [[ -z "$db" ]] && continue
        log "  [pg/$db] pg_dump from pod $pod..."
        local out="$WORKDIR/pg-${db}.sql.gz"
        if ! kubectl -n "$ns" exec "$pod" -- sh -c \
                "pg_dump --clean --if-exists --no-owner --no-acl -U '$user' '$db'" \
                | gzip > "$out"; then
            warn "  [pg/$db] pg_dump FAILED"
            rc=1
            continue
        fi
        log "    size: $(du -h "$out" | cut -f1)"
        s3_upload "$out" "postgres/$db" sql.gz || rc=1
    done <<< "$dbs"
    return $rc
}

log "backup timestamp: $TS"
log "work dir:         $WORKDIR"
log "target:           s3://${S3_BUCKET}/${BACKUP_PREFIX}/"
echo

for fn in backup_originals backup_storage backup_mariadb backup_qdrant backup_postgres; do
    if ! "$fn"; then FAILED+=("$fn"); fi
    echo
done

# Write meta marker
echo "$TS" > "$WORKDIR/last-backup.txt"
"$rclone_bin" copyto "$WORKDIR/last-backup.txt" \
    "${RCLONE_REMOTE}:${S3_BUCKET}/${BACKUP_PREFIX}/_meta/last-backup.txt" \
    >/dev/null 2>&1 || warn "meta marker upload failed"

echo "==============================================="
if [[ ${#FAILED[@]} -eq 0 ]]; then
    log "backup complete — all 5 components OK"
else
    warn "backup finished with ${#FAILED[@]} FAILED component(s): ${FAILED[*]}"
    warn "proceeding anyway (teardown will continue)"
fi
echo "==============================================="
