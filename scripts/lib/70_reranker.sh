# 70_reranker.sh — ensure reranker-api container is running on GPU VM with the
# latest published image. Always pulls; redeploys if remote digest changed.

if [[ -z "${RERANKER_IP:-}" ]]; then
    warn "RERANKER_IP empty — skipping reranker deployment"
    return 0
fi

SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i $RERANKER_SSH_KEY $RERANKER_SSH_USER@$RERANKER_IP"
IMAGE="${DOCKER_HUB_USER}/reranker-api"
DESIRED_IMAGE="${IMAGE}:latest"

log "fetching INTERNAL_TOKEN from cluster..."
TOKEN=$(kubectl -n photoprism-platform get secret internal-token \
    -o jsonpath='{.data.INTERNAL_TOKEN}' | base64 -d)
[[ -n "$TOKEN" ]] || die "could not read INTERNAL_TOKEN from cluster"

log "pulling $DESIRED_IMAGE on GPU VM..."
$SSH "docker pull $DESIRED_IMAGE" >/dev/null \
    || die "docker pull failed (Docker Hub unreachable, or image not pushed yet)"

# Compare the digest of the running container's image vs the freshly pulled one.
running_id=$($SSH "docker inspect reranker-api --format '{{.Image}}' 2>/dev/null || echo none")
latest_id=$($SSH "docker image inspect $DESIRED_IMAGE --format '{{.Id}}' 2>/dev/null || echo none")
status=$($SSH "docker inspect reranker-api --format '{{.State.Status}}' 2>/dev/null || echo none")

if [[ "$running_id" == "$latest_id" && "$status" == "running" ]]; then
    log "  already running with the latest digest ($latest_id) — skipping"
else
    log "  redeploying: running_id='$running_id' status='$status' latest_id='$latest_id'"
    $SSH "docker stop reranker-api 2>/dev/null; docker rm reranker-api 2>/dev/null; true"
    $SSH "docker run -d --name reranker-api --gpus all --restart unless-stopped \
          -p 8000:8000 -e INTERNAL_TOKEN='$TOKEN' $DESIRED_IMAGE"
    log "  waiting for reranker to become healthy (up to 180s)..."
fi

deadline=$((SECONDS + 180))
healthy=0
while (( SECONDS < deadline )); do
    if curl -sS --max-time 5 "http://${RERANKER_IP}:8000/health" 2>/dev/null | grep -q '"ok":true'; then
        healthy=1
        break
    fi
    sleep 3
done
if (( healthy == 1 )); then
    log "  reranker healthy"
else
    warn "  reranker did not respond within 180s — check 'docker logs reranker-api' on $RERANKER_IP"
fi

log "reranker OK"
