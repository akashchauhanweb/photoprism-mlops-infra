# 80_grafana.sh — import dashboards into Grafana.
GRAFANA_URL="http://${NODE1_FLOATING_IP}:30300"
DASH_DIR="$REPO_ROOT/monitoring/dashboards"
[[ -d "$DASH_DIR" ]] || die "dashboards dir missing: $DASH_DIR"

log "fetching Grafana admin password..."
PASS=$(kubectl -n monitoring get secret kube-prom-grafana \
       -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)
[[ -n "$PASS" ]] || die "could not get Grafana admin password (kube-prom-grafana secret missing — was 15_monitoring_stack run?)"
PASS_ENC=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote_plus(sys.argv[1]))' "$PASS")

log "discovering datasource UIDs..."
DS_JSON=$(curl -sS "http://admin:${PASS_ENC}@${GRAFANA_URL#http://}/api/datasources")
PROM_UID=$(jq -r '.[] | select(.type=="prometheus") | .uid' <<<"$DS_JSON" | head -1)
LOKI_UID=$(jq -r '.[] | select(.type=="loki")        | .uid' <<<"$DS_JSON" | head -1)
[[ -n "$PROM_UID" ]] || warn "  no Prometheus datasource found in Grafana"
[[ -n "$LOKI_UID" ]] || warn "  no Loki datasource found in Grafana"
log "  prometheus UID: ${PROM_UID:-<missing>}"
log "  loki UID:       ${LOKI_UID:-<missing>}"

import_one() {
    local f="$1"
    local title
    title=$(jq -r '.title // "(no title)"' "$f")
    log "importing dashboard: $title  ($(basename "$f"))"

    # Recursive walk: replace every datasource UID by type
    local patched
    patched=$(jq \
        --arg prom "${PROM_UID:-NONE}" \
        --arg loki "${LOKI_UID:-NONE}" \
        '
        def fix:
            if type == "object" then
                (if .datasource? != null and (.datasource | type) == "object" then
                    if .datasource.type == "prometheus" then .datasource.uid = $prom
                    elif .datasource.type == "loki"    then .datasource.uid = $loki
                    else . end
                 else . end)
                | with_entries(.value |= fix)
            elif type == "array" then map(fix)
            else . end;
        fix | .id = null
        ' "$f")

    local resp
    resp=$(curl -sS -X POST "http://admin:${PASS_ENC}@${GRAFANA_URL#http://}/api/dashboards/db" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --argjson d "$patched" '{dashboard: $d, overwrite: true}')")
    local status=$(jq -r '.status // "error"' <<<"$resp")
    local url=$(jq -r '.url // .message // "?"' <<<"$resp")
    log "  → $status: $url"
}

shopt -s nullglob
for f in "$DASH_DIR"/*.json; do
    import_one "$f"
done
shopt -u nullglob

log "grafana OK"
