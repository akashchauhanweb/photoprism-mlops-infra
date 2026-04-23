# _s3_common.sh — shared S3/rclone helpers for backup.sh and restore modules.
# Sourced, not executed. Uses log/warn/die from bring_up.sh.

readonly BACKUP_PREFIX="${BACKUP_PREFIX:-backups}"
readonly RCLONE_REMOTE="${RCLONE_REMOTE:-chi}"

s3_load_creds() {
    log "loading S3 creds from objectstore-credentials secret..."
    local ns="photoprism-platform"
    S3_BUCKET=$(kubectl -n "$ns" get secret objectstore-credentials \
        -o jsonpath='{.data.S3_BUCKET}' | base64 -d)
    S3_ENDPOINT=$(kubectl -n "$ns" get secret objectstore-credentials \
        -o jsonpath='{.data.S3_ENDPOINT}' | base64 -d)
    S3_ACCESS_KEY=$(kubectl -n "$ns" get secret objectstore-credentials \
        -o jsonpath='{.data.S3_ACCESS_KEY}' | base64 -d)
    S3_SECRET_KEY=$(kubectl -n "$ns" get secret objectstore-credentials \
        -o jsonpath='{.data.S3_SECRET_KEY}' | base64 -d)
    S3_REGION=$(kubectl -n "$ns" get secret objectstore-credentials \
        -o jsonpath='{.data.S3_REGION}' | base64 -d)
    [[ -n "$S3_BUCKET" && -n "$S3_ENDPOINT" && -n "$S3_ACCESS_KEY" \
        && -n "$S3_SECRET_KEY" ]] \
        || die "objectstore-credentials secret incomplete"
    export S3_BUCKET S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY S3_REGION
}

rclone_ensure() {
    rclone_bin="$(command -v rclone || true)"
    if [[ -z "$rclone_bin" ]]; then
        log "installing rclone on node1..."
        curl -fsSL https://rclone.org/install.sh | sudo bash >/dev/null \
            || die "rclone install failed"
        rclone_bin="$(command -v rclone)"
    fi

    local cfg="$HOME/.config/rclone/rclone.conf"
    mkdir -p "$(dirname "$cfg")"
    cat > "$cfg" <<RCLONE_EOF
[$RCLONE_REMOTE]
type = s3
provider = Other
env_auth = false
access_key_id = $S3_ACCESS_KEY
secret_access_key = $S3_SECRET_KEY
endpoint = $S3_ENDPOINT
region = ${S3_REGION:-}
acl = private
RCLONE_EOF
    chmod 600 "$cfg"
    export rclone_bin
}

s3_path() {
    local comp="$1"
    echo "${RCLONE_REMOTE}:${S3_BUCKET}/${BACKUP_PREFIX}/${comp}"
}

s3_has_latest() {
    local comp="$1"
    local pattern="${2:-latest.*}"
    "$rclone_bin" lsf "$(s3_path "$comp")/" --include "$pattern" 2>/dev/null \
        | grep -q .
}
