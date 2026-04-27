# 99_smoke.sh — quick end-to-end sanity.

log "checking pods..."
bad_platform=$(kubectl -n photoprism-platform get pods --no-headers \
    --field-selector=status.phase!=Succeeded 2>/dev/null \
    | awk '$3 != "Running" && $3 != "Completed" && $3 != "Evicted"' | wc -l)
bad_production=$(kubectl -n photoprism-production get pods --no-headers \
    --field-selector=status.phase!=Succeeded 2>/dev/null \
    | awk '$3 != "Running" && $3 != "Completed" && $3 != "Evicted"' | wc -l)
bad_monitoring=$(kubectl -n monitoring get pods --no-headers \
    --field-selector=status.phase!=Succeeded 2>/dev/null \
    | awk '$3 != "Running" && $3 != "Completed" && $3 != "Evicted"' | wc -l)
[[ "$bad_platform" -eq 0 ]]   || warn "  $bad_platform unhealthy pods in photoprism-platform"
[[ "$bad_production" -eq 0 ]] || warn "  $bad_production unhealthy pods in photoprism-production"
[[ "$bad_monitoring" -eq 0 ]] || warn "  $bad_monitoring unhealthy pods in monitoring"

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
if curl -sS --max-time 5 "http://${RERANKER_IP}:8000/health" 2>/dev/null | grep -q '"ok":true'; then
    log "  reranker OK"
else
    warn "  reranker not healthy"
fi

# ---- Monitoring smoke ----
log "prometheus smoke test..."
prom_resp=$(curl -sS --max-time 5 "http://${NODE1_FLOATING_IP}:30900/-/ready" 2>/dev/null || echo "")
if echo "$prom_resp" | grep -qi "ready\|ok"; then
    log "  prometheus ready"
else
    warn "  prometheus not ready: $prom_resp"
fi

log "checking prometheus targets..."
# Count active scrape targets that are 'up'. Expect at least: 5 platform services + node-exporter(s) + kube-state-metrics
targets_json=$(curl -sS --max-time 10 "http://${NODE1_FLOATING_IP}:30900/api/v1/targets?state=active" 2>/dev/null || echo "")
if [[ -n "$targets_json" ]]; then
    up=$(echo "$targets_json" | jq -r '[.data.activeTargets[] | select(.health=="up")] | length')
    down=$(echo "$targets_json" | jq -r '[.data.activeTargets[] | select(.health!="up")] | length')
    log "  targets up=${up:-?} down=${down:-?}"
    if [[ "${down:-0}" != "0" ]]; then
        echo "$targets_json" | jq -r '.data.activeTargets[] | select(.health!="up") | "    " + .scrapePool + " :: " + (.labels.instance // "?") + " — " + (.lastError // "?")' | head -10
    fi
else
    warn "  could not query prometheus targets API"
fi

log "grafana smoke test..."
grafana_pass=$(kubectl -n monitoring get secret kube-prom-grafana \
    -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d || echo "")
if [[ -n "$grafana_pass" ]]; then
    pass_enc=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote_plus(sys.argv[1]))' "$grafana_pass")
    g_resp=$(curl -sS --max-time 5 "http://admin:${pass_enc}@${NODE1_FLOATING_IP}:30300/api/health" 2>/dev/null || echo "")
    if echo "$g_resp" | grep -q '"database":\s*"ok"'; then
        log "  grafana healthy"
    else
        warn "  grafana health: $g_resp"
    fi
    ds_count=$(curl -sS --max-time 5 "http://admin:${pass_enc}@${NODE1_FLOATING_IP}:30300/api/datasources" 2>/dev/null | jq 'length' || echo "0")
    log "  grafana datasources: ${ds_count}"
    dash_count=$(curl -sS --max-time 5 "http://admin:${pass_enc}@${NODE1_FLOATING_IP}:30300/api/search?type=dash-db" 2>/dev/null | jq 'length' || echo "0")
    log "  grafana dashboards : ${dash_count}"
else
    warn "  grafana password unavailable — skipping"
fi

log "smoke tests done"
