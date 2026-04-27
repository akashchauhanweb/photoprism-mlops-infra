#!/usr/bin/env bash
# pg-backup — runs inside CronJob pod every tick.
# 1. discover user DBs by querying postgres pod
# 2. per-DB: pg_dump --clean --if-exists --no-owner --no-acl | gzip
# 3. upload to s3://$S3_BUCKET/$BACKUP_PREFIX/postgres/<db>/<ts>.sql.gz
# 4. copy to .../postgres/<db>/latest.sql.gz (consumed by 32_restore_data.sh)
# 5. prune timestamped dumps older than RETENTION_HOURS

set -euo pipefail

NS="${NAMESPACE:-photoprism-platform}"
PREFIX="${BACKUP_PREFIX:-backups}"
RETENTION_HOURS="${RETENTION_HOURS:-24}"
REMOTE="chi"

: "${S3_BUCKET:?S3_BUCKET unset}"
: "${S3_ENDPOINT:?S3_ENDPOINT unset}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY unset}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY unset}"

log()  { echo "[pg-backup] $*"; }
warn() { echo "[pg-backup] WARN: $*" >&2; }
die()  { echo "[pg-backup] ERROR: $*" >&2; exit 1; }

# --- rclone config (written fresh each run; ephemeral pod) ---
mkdir -p /root/.config/rclone
cat > /root/.config/rclone/rclone.conf <<EOF
[$REMOTE]
type = s3
provider = Other
env_auth = false
access_key_id = $S3_ACCESS_KEY
secret_access_key = $S3_SECRET_KEY
endpoint = $S3_ENDPOINT
acl = private
EOF

# --- find postgres pod + user ---
POD=$(kubectl -n "$NS" get pod -l app=postgres \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
[[ -n "$POD" ]] || die "no postgres pod in $NS"

PG_USER=$(kubectl -n "$NS" get secret postgres-credentials \
            -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
[[ -n "$PG_USER" ]] || die "postgres user secret empty"

# --- discover user DBs (excludes templates and 'postgres') ---
DBS=$(kubectl -n "$NS" exec "$POD" -- psql -U "$PG_USER" -d postgres -tAc \
    "SELECT datname FROM pg_database WHERE datistemplate = false AND datname <> 'postgres'" \
    | tr -d '\r')
[[ -n "$DBS" ]] || die "no user DBs found"

TS=$(date -u +%Y%m%d-%H%M)
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

OVERALL_RC=0
while IFS= read -r DB; do
    [[ -z "$DB" ]] && continue
    OUT="$WORKDIR/${DB}.sql.gz"
    DEST="${REMOTE}:${S3_BUCKET}/${PREFIX}/postgres/${DB}"

    log "[$DB] dumping (pod=$POD ts=$TS)..."
    if ! kubectl -n "$NS" exec "$POD" -- sh -c \
        "pg_dump --clean --if-exists --no-owner --no-acl -U '$PG_USER' '$DB'" \
        | gzip > "$OUT"; then
        warn "[$DB] pg_dump failed"
        OVERALL_RC=1
        continue
    fi
    log "  size: $(du -h "$OUT" | cut -f1)"

    log "[$DB] uploading to ${DEST}/${TS}.sql.gz ..."
    if ! rclone copyto "$OUT" "${DEST}/${TS}.sql.gz"; then
        warn "[$DB] timestamped upload failed"
        OVERALL_RC=1
        continue
    fi
    if ! rclone copyto "$OUT" "${DEST}/latest.sql.gz"; then
        warn "[$DB] latest pointer upload failed"
        OVERALL_RC=1
        continue
    fi

    log "[$DB] pruning dumps older than ${RETENTION_HOURS}h..."
    rclone delete "$DEST" \
        --include "*.sql.gz" \
        --exclude "latest.sql.gz" \
        --min-age "${RETENTION_HOURS}h" \
        || warn "[$DB] prune step had errors (non-fatal)"
done <<< "$DBS"

exit $OVERALL_RC
