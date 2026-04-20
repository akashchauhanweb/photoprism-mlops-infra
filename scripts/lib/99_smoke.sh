# 99_smoke.sh — quick end-to-end sanity.

log "checking pods..."
bad_platform=$(kubectl -n photoprism-platform get pods --no-headers \
    --field-selector=status.phase!=Succeeded 2>/dev/null \
    | awk '$3 != "Running" && $3 != "Completed" && $3 != "Evicted"' | wc -l)
bad_production=$(kubectl -n photoprism-production get pods --no-headers \
    --field-selector=status.phase!=Succeeded 2>/dev/null \
    | awk '$3 != "Running" && $3 != "Completed" && $3 != "Evicted"' | wc -l)
[[ "$bad_platform" -eq 0 ]] || warn "  $bad_platform unhealthy pods in photoprism-platform"
[[ "$bad_production" -eq 0 ]] || warn "  $bad_production unhealthy pods in photoprism-production"

log "search-api smoke test..."
resp=$(curl -sS --max-time 10 -X POST \
    "http://${NODE1_FLOATING_IP}:30810/search" \
    -H "Content-Type: application/json" \
    -d '{"query":"test","top_k":1}' || echo "")
if echo "$resp" | grep -q '"query_id"'; then
    log "  search-api OK"
else
    warn "  search-api returned: $resp"
fi

log "reranker smoke test..."
if curl -sS --max-time 5 "http://${RERANKER_IP}:8000/health" | grep -q '"ok":true'; then
    log "  reranker OK"
else
    warn "  reranker not healthy"
fi

log "smoke tests done"
