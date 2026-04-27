# 33_postgres_backup_cron.sh — periodic pg_dumpall to S3 (live backup).
# CronJob runs every POSTGRES_BACKUP_SCHEDULE in photoprism-platform.
# Writes backups/postgres/<ts>.sql.gz and refreshes backups/postgres/latest.sql.gz,
# which is the same key 32_restore_data.sh consumes.

SCHEDULE="${POSTGRES_BACKUP_SCHEDULE:-*/5 * * * *}"
RETENTION_HOURS="${POSTGRES_BACKUP_RETENTION_HOURS:-24}"
TAG="${PG_BACKUP_TAG:-0.1.0}"

tpl="$REPO_ROOT/k8s/platform/postgres-backup-cronjob.yaml.tpl"
rendered="/tmp/postgres-backup-cronjob-$$.yaml"

log "rendering pg-backup CronJob (schedule='$SCHEDULE' retention=${RETENTION_HOURS}h tag=$TAG)..."
sed \
    -e "s|__SCHEDULE__|${SCHEDULE}|g" \
    -e "s|__RETENTION_HOURS__|${RETENTION_HOURS}|g" \
    -e "s|__TAG__|${TAG}|g" \
    "$tpl" > "$rendered"

kubectl apply -f "$rendered"
rm -f "$rendered"

log "verifying CronJob..."
kubectl -n photoprism-platform get cronjob pg-backup >/dev/null \
    || die "pg-backup CronJob not found after apply"

log "pg-backup CronJob OK"
