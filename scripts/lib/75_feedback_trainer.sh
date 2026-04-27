# 75_feedback_trainer.sh — ensure feedback-trainer container is running on GPU VM.
# Idempotent: skip if correct image is already running.
# Mirrors 70_reranker.sh pattern.

if [[ -z "${RERANKER_IP:-}" ]]; then
    warn "RERANKER_IP empty — skipping feedback-trainer deployment"
    return 0
fi

SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i $RERANKER_SSH_KEY $RERANKER_SSH_USER@$RERANKER_IP"
IMAGE="${DOCKER_HUB_USER}/feedback-trainer"
DESIRED_IMAGE="${IMAGE}:latest"

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
PG_HOST="${NODE1_FLOATING_IP}"
PG_USER_ENC=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote_plus(sys.argv[1]))' "$PG_USER")
PG_PASS_ENC=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote_plus(sys.argv[1]))' "$PG_PASS")
POSTGRES_URI="postgresql://${PG_USER_ENC}:${PG_PASS_ENC}@${PG_HOST}:30532/${PG_DB}"
QDRANT_URL="http://${NODE1_FLOATING_IP}:30633"

# MLflow: running on her GPU VM at :8000 (not deployed in our cluster).
MLFLOW_TRACKING_URI="http://${NODE1_FLOATING_IP}:30500"

log "syncing reranker-api + scripts/ to GPU VM for rebuild path..."
rsync -az -e "ssh -o BatchMode=yes -i $RERANKER_SSH_KEY" \
    "$REPO_ROOT/services/reranker-api/" "$RERANKER_SSH_USER@$RERANKER_IP:/tmp/reranker-api/" \
    || warn "  reranker-api rsync failed (rebuild flow will be broken)"
rsync -az -e "ssh -o BatchMode=yes -i $RERANKER_SSH_KEY" \
    "$REPO_ROOT/scripts/" "$RERANKER_SSH_USER@$RERANKER_IP:/tmp/photoprism-scripts/" \
    || warn "  scripts rsync failed"

log "loading docker hub PAT from cluster secret..."
DOCKER_HUB_PAT=$(kubectl -n photoprism-platform get secret dockerhub-secret \
    -o jsonpath='{.data.DOCKER_HUB_PAT}' 2>/dev/null | base64 -d)

log "loading S3 creds from objectstore-credentials secret..."
S3_BUCKET=$(kubectl -n photoprism-platform get secret objectstore-credentials -o jsonpath='{.data.S3_BUCKET}' | base64 -d)
S3_ENDPOINT=$(kubectl -n photoprism-platform get secret objectstore-credentials -o jsonpath='{.data.S3_ENDPOINT}' | base64 -d)
S3_ACCESS_KEY=$(kubectl -n photoprism-platform get secret objectstore-credentials -o jsonpath='{.data.S3_ACCESS_KEY}' | base64 -d)
S3_SECRET_KEY=$(kubectl -n photoprism-platform get secret objectstore-credentials -o jsonpath='{.data.S3_SECRET_KEY}' | base64 -d)
S3_REGION=$(kubectl -n photoprism-platform get secret objectstore-credentials -o jsonpath='{.data.S3_REGION}' | base64 -d)

log "checking feedback-trainer container on $RERANKER_IP..."
$SSH "docker pull $DESIRED_IMAGE" >/dev/null \
    || die "docker pull failed (Docker Hub unreachable, or image not pushed yet)"

running_id=$($SSH "docker inspect feedback-trainer --format '{{.Image}}' 2>/dev/null || echo none")
latest_id=$($SSH "docker image inspect $DESIRED_IMAGE --format '{{.Id}}' 2>/dev/null || echo none")
status=$($SSH "docker inspect feedback-trainer --format '{{.State.Status}}' 2>/dev/null || echo none")

if [[ "$running_id" == "$latest_id" && "$status" == "running" ]]; then
    log "  already running with the latest digest ($latest_id) — skipping"
else
    log "  redeploying: running_id='$running_id' status='$status' latest_id='$latest_id'"
    $SSH "docker stop feedback-trainer 2>/dev/null; docker rm feedback-trainer 2>/dev/null; true"
    $SSH "docker run -d --name feedback-trainer --gpus all --restart unless-stopped \
          -p 8002:8001 \
          -v /var/run/docker.sock:/var/run/docker.sock \
          -v /tmp/reranker-api:/reranker-api \
          -v /tmp/photoprism-scripts:/scripts \
          -e DOCKER_HUB_PAT='$DOCKER_HUB_PAT' \
          -e POSTGRES_URI='$POSTGRES_URI' \
          -e QDRANT_URL='$QDRANT_URL' \
          -e MLFLOW_TRACKING_URI='$MLFLOW_TRACKING_URI' \
          -e MIN_FEEDBACK_SAMPLES='${MIN_FEEDBACK_SAMPLES:-100}' \
          -e S3_BUCKET='$S3_BUCKET' \
          -e S3_ENDPOINT='$S3_ENDPOINT' \
          -e S3_ACCESS_KEY='$S3_ACCESS_KEY' \
          -e S3_SECRET_KEY='$S3_SECRET_KEY' \
          -e S3_REGION='$S3_REGION' \
          -e MLFLOW_S3_ENDPOINT_URL='$S3_ENDPOINT' \
          -e AWS_ACCESS_KEY_ID='$S3_ACCESS_KEY' \
          -e AWS_SECRET_ACCESS_KEY='$S3_SECRET_KEY' \
          -e AWS_DEFAULT_REGION='$S3_REGION' \
          -e DOCKER_HUB_USER='$DOCKER_HUB_USER' \
          $DESIRED_IMAGE"
    log "  waiting for feedback-trainer to become healthy (up to 120s)..."
fi

deadline=$((SECONDS + 120))
healthy=0
while (( SECONDS < deadline )); do
    if curl -sS --max-time 5 "http://${RERANKER_IP}:8002/health" 2>/dev/null | grep -q '"status":"ok"'; then
        healthy=1
        break
    fi
    sleep 3
done
if (( healthy == 1 )); then
    log "  feedback-trainer healthy"
else
    warn "  feedback-trainer did not respond within 120s — check 'docker logs feedback-trainer' on $RERANKER_IP"
fi

log "feedback-trainer OK"
