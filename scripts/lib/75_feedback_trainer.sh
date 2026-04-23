# 75_feedback_trainer.sh — ensure feedback-trainer container is running on GPU VM.
# Idempotent: skip if correct image is already running.
# Mirrors 70_reranker.sh pattern.

if [[ -z "${RERANKER_IP:-}" ]]; then
    warn "RERANKER_IP empty — skipping feedback-trainer deployment"
    return 0
fi

readonly SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i $RERANKER_SSH_KEY $RERANKER_SSH_USER@$RERANKER_IP"
readonly DESIRED_IMAGE="${DOCKER_HUB_USER}/feedback-trainer:${FEEDBACK_TRAINER_TAG}"

log "fetching postgres credentials from cluster..."
PG_USER=$(kubectl -n photoprism-platform get secret postgres-credentials \
    -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
PG_PASS=$(kubectl -n photoprism-platform get secret postgres-credentials \
    -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
PG_DB=$(kubectl -n photoprism-platform get secret postgres-credentials \
    -o jsonpath='{.data.POSTGRES_DB}' | base64 -d)
[[ -n "$PG_USER" && -n "$PG_PASS" && -n "$PG_DB" ]] \
    || die "could not read postgres credentials"

# Connect via node1 private IP + NodePort (30532) — reachable from GPU VM's
# floating interface since we opened allow-30532 SG on the sharednet port.
readonly PG_HOST="${NODE1_FLOATING_IP}"
readonly POSTGRES_URI="postgresql://${PG_USER}:${PG_PASS}@${PG_HOST}:30532/${PG_DB}"
readonly QDRANT_URL="http://${NODE1_FLOATING_IP}:30633"

# MLflow: running on her GPU VM at :8000 (not deployed in our cluster).
readonly MLFLOW_TRACKING_URI="http://${RERANKER_IP}:8000"

log "checking feedback-trainer container on $RERANKER_IP..."
current=$($SSH "docker inspect feedback-trainer --format '{{.Config.Image}}' 2>/dev/null || echo none")
status=$($SSH "docker inspect feedback-trainer --format '{{.State.Status}}' 2>/dev/null || echo none")

if [[ "$current" == "$DESIRED_IMAGE" && "$status" == "running" ]]; then
    log "  already running with image $DESIRED_IMAGE — skipping"
else
    log "  (re)deploying: current='$current' status='$status' desired='$DESIRED_IMAGE'"
    $SSH "docker stop feedback-trainer 2>/dev/null; docker rm feedback-trainer 2>/dev/null; true"
    $SSH "docker pull $DESIRED_IMAGE"
    $SSH "docker run -d --name feedback-trainer --gpus all --restart unless-stopped \
          -p 8002:8001 \
          -e POSTGRES_URI='$POSTGRES_URI' \
          -e QDRANT_URL='$QDRANT_URL' \
          -e MLFLOW_TRACKING_URI='$MLFLOW_TRACKING_URI' \
          -e DOCKER_HUB_USER='$DOCKER_HUB_USER' \
          $DESIRED_IMAGE"
    log "  waiting for feedback-trainer startup (30s)..."
    sleep 30
fi

log "verifying feedback-trainer health..."
if curl -sS --max-time 5 "http://${RERANKER_IP}:8002/health" | grep -q '"status":"ok"'; then
    log "  feedback-trainer healthy"
else
    warn "  feedback-trainer did not respond — check 'docker logs feedback-trainer' on $RERANKER_IP"
fi

log "feedback-trainer OK"
