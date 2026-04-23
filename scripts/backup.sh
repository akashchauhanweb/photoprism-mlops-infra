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
    user=$(kubectl -n "$ns" get secret mariadb-credentials -o jsonpath='{.data.MYSQL_USER}' 2>/dev/null | base64 -d)
    pass=$(kubectl -n "$ns" get secret mariadb-credentials -o jsonpath='{.data.MYSQL_PASSWORD}' 2>/dev/null | base64 -d)
    db=$(kubectl -n "$ns" get secret mariadb-credentials -o jsonpath='{.data.MYSQL_DATABASE}' 2>/dev/null | base64 -d)
    [[ -n "$user" && -n "$pass" && -n "$db" ]] \
        || { warn "mariadb creds missing; skipping"; return 1; }
    local out="$WORKDIR/mariadb.sql.gz"
    log "  dumping db=$db from pod $pod..."
    kubectl -n "$ns" exec "$pod" -- sh -c \
        "mysqldump -u'$user' -p'$pass' --single-transaction --routines --triggers '$db'" \
        | gzip > "$out" \
        || { warn "mysqldump FAILED"; return 1; }
    log "  size: $(du -h "$out" | cut -f1)"
    s3_upload "$out" mariadb sql.gz
}

# ---- 4. qdrant (snapshot API) ----
backup_qdrant() {
    log "=== qdrant ==="
    local ns=photoprism-platform
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=qdrant -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "no qdrant pod; skipping"; return 1; }
    local coll="image_embeddings"
    log "  creating snapshot for collection=$coll..."
    local snap_name
    snap_name=$(kubectl -n "$ns" exec "$pod" -- sh -c \
        "wget -qO- --post-data='' http://localhost:6333/collections/${coll}/snapshots" \
        | jq -r '.result.name' 2>/dev/null)
    [[ -n "$snap_name" && "$snap_name" != "null" ]] \
        || { warn "qdrant snapshot create FAILED"; return 1; }
    log "  snapshot: $snap_name"
    local out="$WORKDIR/qdrant.snapshot"
    log "  downloading snapshot..."
    kubectl -n "$ns" exec "$pod" -- sh -c \
        "wget -qO- http://localhost:6333/collections/${coll}/snapshots/${snap_name}" \
        > "$out" \
        || { warn "qdrant snapshot download FAILED"; return 1; }
    # Clean up snapshot on qdrant disk
    kubectl -n "$ns" exec "$pod" -- sh -c \
        "wget -q --method=DELETE http://localhost:6333/collections/${coll}/snapshots/${snap_name} -O /dev/null" \
        >/dev/null 2>&1 || true
    log "  size: $(du -h "$out" | cut -f1)"
    s3_upload "$out" qdrant snapshot
}

# ---- 5. postgres (pg_dump) ----
backup_postgres() {
    log "=== postgres ==="
    local ns=photoprism-platform
    local pod
    pod=$(kubectl -n "$ns" get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [[ -n "$pod" ]] || { warn "no postgres pod; skipping"; return 1; }
    local user db
    user=$(kubectl -n "$ns" get secret postgres-credentials -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
    db=$(kubectl -n "$ns" get secret postgres-credentials -o jsonpath='{.data.POSTGRES_DB}' | base64 -d)
    [[ -n "$user" && -n "$db" ]] || { warn "postgres creds missing; skipping"; return 1; }
    local out="$WORKDIR/postgres.sql.gz"
    log "  dumping db=$db from pod $pod..."
    kubectl -n "$ns" exec "$pod" -- sh -c "pg_dump -U '$user' '$db'" \
        | gzip > "$out" \
        || { warn "pg_dump FAILED"; return 1; }
    log "  size: $(du -h "$out" | cut -f1)"
    s3_upload "$out" postgres sql.gz
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
