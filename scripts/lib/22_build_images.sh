# 15_build_images.sh — build custom images via kaniko and push to Docker Hub.
# Idempotent: queries Docker Hub for the desired tag first; only builds if missing.
# Reads source from the public GitHub repo at the configured branch.
#
# Add new images by appending a line to the IMAGES array:
#   "<dockerhub-repo>:<tag>:<subpath-in-repo>"
# Subpath must contain a Dockerfile.

GH_REPO="${GH_REPO:-akashchauhanweb/photoprism-mlops-infra}"
GH_BRANCH="${GH_BRANCH:-main}"

# image entries: "<repo>:<tag>:<subpath>"
IMAGES=(
    "${DOCKER_HUB_USER}/pg-backup:${PG_BACKUP_TAG:-0.1.0}:services/pg-backup"
)

# Returns 0 if tag exists publicly on Docker Hub, 1 otherwise.
dockerhub_tag_exists() {
    local image="$1"          # repo:tag
    local repo="${image%:*}"
    local tag="${image##*:}"
    local code
    code=$(curl -sS -o /dev/null -w "%{http_code}" \
        "https://hub.docker.com/v2/repositories/${repo}/tags/${tag}/")
    [[ "$code" == "200" ]]
}

build_one() {
    local destination="$1" subpath="$2"
    local job_name
    job_name="kaniko-$(echo "${destination##*/}" | tr ':._' '-' | tr '[:upper:]' '[:lower:]')-$(date +%s)"
    local tpl="$REPO_ROOT/k8s/platform/jobs/kaniko-build-job.yaml.tpl"
    local rendered="/tmp/${job_name}.yaml"

    log "  rendering kaniko Job for $destination (subpath=$subpath)..."
    sed \
        -e "s|__JOB_NAME__|${job_name}|g" \
        -e "s|__DOCKER_HUB_USER__|${DOCKER_HUB_USER}|g" \
        -e "s|__GH_REPO__|${GH_REPO}|g" \
        -e "s|__GH_BRANCH__|${GH_BRANCH}|g" \
        -e "s|__SUBPATH__|${subpath}|g" \
        -e "s|__DESTINATION__|${destination}|g" \
        "$tpl" > "$rendered"

    kubectl apply -f "$rendered"
    rm -f "$rendered"

    log "  waiting for build (timeout 10m)..."
    if kubectl -n photoprism-platform wait --for=condition=Complete \
        "job/$job_name" --timeout=10m >/dev/null 2>&1; then
        log "  [$destination] build OK"
        return 0
    fi
    if kubectl -n photoprism-platform wait --for=condition=Failed \
        "job/$job_name" --timeout=10s >/dev/null 2>&1; then
        warn "  [$destination] build FAILED — last 50 lines:"
        kubectl -n photoprism-platform logs "job/$job_name" --tail=50 || true
        return 1
    fi
    warn "  [$destination] build timed out"
    return 1
}

log "checking custom images on Docker Hub (repo=$DOCKER_HUB_USER, branch=$GH_BRANCH)..."

build_failed=0
for entry in "${IMAGES[@]}"; do
    repo_tag="${entry%:*}"            # akashweb/pg-backup:0.1.0
    subpath="${entry##*:}"            # services/pg-backup

    if dockerhub_tag_exists "$repo_tag"; then
        log "  [$repo_tag] already on Docker Hub — skipping"
        continue
    fi

    log "  [$repo_tag] not found — building via kaniko"
    build_one "$repo_tag" "$subpath" || build_failed=$((build_failed + 1))
done

if (( build_failed > 0 )); then
    die "$build_failed image build(s) failed"
fi

log "image builds OK"
