# 25_restore_volumes.sh — restore PVC tarballs (originals, storage) before DB module.
# First run: no backup on S3 → skip, let 50_photoprism create empty PVCs as usual.
# Subsequent run after teardown: backups exist → pre-create PVCs, restore via Job.

# shellcheck disable=SC1091
source "$LIB_DIR/_s3_common.sh"

s3_load_creds
rclone_ensure

# objectstore-credentials secret lives in photoprism-platform, but PVCs are in
# photoprism-production. We need the secret in both namespaces for the Job.
ensure_secret_in_production() {
    if ! kubectl -n photoprism-production get secret objectstore-credentials >/dev/null 2>&1; then
        log "  copying objectstore-credentials into photoprism-production..."
        kubectl -n photoprism-platform get secret objectstore-credentials -o json \
            | jq 'del(.metadata.ownerReferences, .metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp) | .metadata.namespace="photoprism-production"' \
            | kubectl apply -f -
    fi
}

# Apply PVC definition from photoprism.yaml but ONLY the PVCs (not Deployment/Service).
apply_photoprism_pvcs() {
    log "  pre-creating photoprism PVCs..."
    # Extract just the two PVC documents from photoprism.yaml
    awk '
        BEGIN { RS="---\n"; ORS="---\n" }
        /kind: PersistentVolumeClaim/ { print }
    ' "$REPO_ROOT/k8s/production/photoprism.yaml" | kubectl apply -f -
}

restore_pvc() {
    local comp="$1" pvc="$2" mount="$3"

    if ! s3_has_latest "$comp" "latest.tar.gz"; then
        log "  [$comp] no backup on S3 — skipping (first run)"
        return 0
    fi

    log "  [$comp] backup found — restoring into PVC $pvc..."
    local job_name="restore-${comp}"
    local tpl="$REPO_ROOT/k8s/platform/jobs/restore-pvc-job.yaml.tpl"
    local rendered="/tmp/${job_name}-$$.yaml"

    sed \
        -e "s|__NS__|photoprism-production|g" \
        -e "s|__JOB_NAME__|${job_name}|g" \
        -e "s|__PVC__|${pvc}|g" \
        -e "s|__MOUNT__|${mount}|g" \
        -e "s|__BACKUP_PREFIX__|${BACKUP_PREFIX}|g" \
        -e "s|__COMPONENT__|${comp}|g" \
        "$tpl" > "$rendered"

    kubectl delete job -n photoprism-production "$job_name" --ignore-not-found >/dev/null
    kubectl apply -f "$rendered"
    rm -f "$rendered"

    log "  [$comp] waiting for restore Job (timeout 10m)..."
    if kubectl -n photoprism-production wait --for=condition=complete \
        job/"$job_name" --timeout=10m; then
        log "  [$comp] restore OK"
    else
        warn "  [$comp] restore Job did NOT complete — check: kubectl -n photoprism-production logs job/$job_name"
    fi
}

log "=== volume restore (pre-DB) ==="
ensure_secret_in_production
apply_photoprism_pvcs

# (component, pvc-name, mount-path) — mount must match where tar was created
restore_pvc photoprism-originals photoprism-originals-pvc /photoprism/originals
restore_pvc photoprism-storage   photoprism-storage-pvc   /photoprism/storage

log "volume restore OK"
