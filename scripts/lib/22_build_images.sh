# 22_build_images.sh — build custom images via kaniko and push to Docker Hub.
# Idempotent: queries Docker Hub for the desired tag first; only builds if missing.
# Reads source from the public GitHub repo at the configured branch.
#
# Image entry format:
#   "<dockerhub-repo>:<tag>:<context-subpath>:<dockerfile-relative-to-context>"
# (4 colons-separated fields). The legacy 3-field form is also accepted:
#   "<dockerhub-repo>:<tag>:<context-subpath>"
# in which case <dockerfile> defaults to "Dockerfile".

GH_REPO="${GH_REPO:-akashchauhanweb/photoprism-mlops-infra}"
GH_BRANCH="${GH_BRANCH:-main}"

# image entries
IMAGES=(
    "${DOCKER_HUB_USER}/pg-backup:${PG_BACKUP_TAG:-0.1.1}:services/pg-backup"
    # Instrumented services — build context is services/ so we can COPY _shared/metrics.py
    "${DOCKER_HUB_USER}/search-api:${SEARCH_API_TAG:-0.4.1}:services:search-api/Dockerfile"
    "${DOCKER_HUB_USER}/clip-api:${CLIP_API_TAG:-0.2.0}:services:clip-api/Dockerfile"
    "${DOCKER_HUB_USER}/ingest-api:${INGEST_API_TAG:-0.2.0}:services:ingest-api/Dockerfile"
    "${DOCKER_HUB_USER}/feature-worker:${FEATURE_WORKER_TAG:-0.4.0}:services:feature-worker/Dockerfile"
)

dockerhub_tag_exists() {
    local image="$1"
    local repo="${image%:*}"
    local tag="${image##*:}"
    local code
    code=$(curl -sS -o /dev/null -w "%{http_code}" \
        "https://hub.docker.com/v2/repositories/${repo}/tags/${tag}/")
    [[ "$code" == "200" ]]
}

build_one() {
    local destination="$1" subpath="$2" dockerfile="${3:-Dockerfile}"
    local job_name
    job_name="kaniko-$(echo "${destination##*/}" | tr ':._' '-' | tr '[:upper:]' '[:lower:]')-$(date +%s)"
    local tpl="$REPO_ROOT/k8s/platform/jobs/kaniko-build-job.yaml.tpl"
    local rendered="/tmp/${job_name}.yaml"

    log "  rendering kaniko Job for $destination (subpath=$subpath, dockerfile=$dockerfile)..."
    sed \
        -e "s|__JOB_NAME__|${job_name}|g" \
        -e "s|__DOCKER_HUB_USER__|${DOCKER_HUB_USER}|g" \
        -e "s|__GH_REPO__|${GH_REPO}|g" \
        -e "s|__GH_BRANCH__|${GH_BRANCH}|g" \
        -e "s|__SUBPATH__|${subpath}|g" \
        -e "s|__DOCKERFILE__|${dockerfile}|g" \
        -e "s|__DESTINATION__|${destination}|g" \
        "$tpl" > "$rendered"

    kubectl apply -f "$rendered"
    rm -f "$rendered"

    log "  waiting for build (timeout 15m)..."
    if kubectl -n photoprism-platform wait --for=condition=Complete \
        "job/$job_name" --timeout=15m >/dev/null 2>&1; then
        log "  [$destination] build OK"
        return 0
    fi
    if kubectl -n photoprism-platform wait --for=condition=Failed \
        "job/$job_name" --timeout=10s >/dev/null 2>&1; then
        warn "  [$destination] build FAILED — last 80 lines:"
        kubectl -n photoprism-platform logs "job/$job_name" --tail=80 || true
        return 1
    fi
    warn "  [$destination] build timed out"
    return 1
}

log "checking custom images on Docker Hub (repo=$DOCKER_HUB_USER, branch=$GH_BRANCH)..."

build_failed=0
for entry in "${IMAGES[@]}"; do
    # Split on ':' but the repo:tag also contains ':'. We expect 3 or 4 fields where
    # field 1 is repo:tag (always 1 colon), field 2 is subpath, field 3 (optional)
    # is dockerfile path relative to subpath.
    IFS=':' read -r -a parts <<<"$entry"
    case ${#parts[@]} in
        3) repo_tag="${parts[0]}:${parts[1]}"; subpath="${parts[2]}"; dockerfile="Dockerfile" ;;
        4) repo_tag="${parts[0]}:${parts[1]}"; subpath="${parts[2]}"; dockerfile="${parts[3]}" ;;
        *) warn "  skipping malformed entry: $entry"; continue ;;
    esac

    if dockerhub_tag_exists "$repo_tag"; then
        log "  [$repo_tag] already on Docker Hub — skipping"
        continue
    fi

    log "  [$repo_tag] not found — building via kaniko"
    build_one "$repo_tag" "$subpath" "$dockerfile" || build_failed=$((build_failed + 1))
done

if (( build_failed > 0 )); then
    die "$build_failed image build(s) failed"
fi

log "image builds OK"
