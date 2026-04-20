# 70_reranker.sh — ensure reranker-api container is running on GPU VM.
# Idempotent: skip if correct image is already running.

readonly SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i $RERANKER_SSH_KEY $RERANKER_SSH_USER@$RERANKER_IP"
readonly DESIRED_IMAGE="${DOCKER_HUB_USER}/reranker-api:${RERANKER_API_TAG}"

log "fetching INTERNAL_TOKEN from cluster..."
TOKEN=$(kubectl -n photoprism-platform get secret internal-token \
    -o jsonpath='{.data.INTERNAL_TOKEN}' | base64 -d)
[[ -n "$TOKEN" ]] || die "could not read INTERNAL_TOKEN from cluster"

log "checking reranker container on $RERANKER_IP..."
current=$($SSH "docker inspect reranker-api --format '{{.Config.Image}}' 2>/dev/null || echo none")
status=$($SSH "docker inspect reranker-api --format '{{.State.Status}}' 2>/dev/null || echo none")

if [[ "$current" == "$DESIRED_IMAGE" && "$status" == "running" ]]; then
    log "  already running with image $DESIRED_IMAGE — skipping"
else
    log "  (re)deploying: current='$current' status='$status' desired='$DESIRED_IMAGE'"
    $SSH "docker stop reranker-api 2>/dev/null; docker rm reranker-api 2>/dev/null; true"
    $SSH "docker pull $DESIRED_IMAGE"
    $SSH "docker run -d --name reranker-api --gpus all --restart unless-stopped \
          -p 8000:8000 -e INTERNAL_TOKEN='$TOKEN' $DESIRED_IMAGE"
    log "  waiting for reranker startup (60s)..."
    sleep 60
fi

log "verifying reranker health..."
if curl -sS --max-time 5 "http://${RERANKER_IP}:8000/health" | grep -q '"ok":true'; then
    log "  reranker healthy"
else
    warn "  reranker did not respond — check 'docker logs reranker-api' on $RERANKER_IP"
fi

log "reranker OK"
