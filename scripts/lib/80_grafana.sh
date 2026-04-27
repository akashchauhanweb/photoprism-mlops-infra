# 80_grafana.sh — import dashboards into Grafana.
# - Discovers Prometheus + Loki datasource UIDs at runtime.
# - Patches every panel's datasource UID by datasource type.
# - Imports all JSON files under monitoring/dashboards/.

GRAFANA_URL="http://${NODE1_FLOATING_IP}:30300"
DASH_DIR="$REPO_ROOT/monitoring/dashboards"
[[ -d "$DASH_DIR" ]] || die "dashboards dir missing: $DASH_DIR"

# kube-prometheus-stack chart name = "kube-prom" (see helm values)
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

    # Patch every panel's datasource.uid by type. Also clear top-level id so
    # Grafana treats it as a fresh import each bring-up.
    local patched
    patched=$(jq \
        --arg prom "${PROM_UID:-NONE}" \
        --arg loki "${LOKI_UID:-NONE}" \
        '
        def patch_ds(prom; loki):
            if (.datasource? // empty) | type == "object" then
                if .datasource.type == "prometheus" then .datasource.uid = prom
                elif .datasource.type == "loki"       then .datasource.uid = loki
                else . end
            else . end;

        # Walk all panels (incl. nested rows/panels) and all targets
        ((.panels // [])[]              |= patch_ds($prom; $loki)) as $_
        | ((.panels // [])[].targets? // [])[]? |= patch_ds($prom; $loki)
        | ((.panels // [])[].panels?  // [])[]? |= patch_ds($prom; $loki)
        | ((.panels // [])[].panels? // [])[]?.targets? // []  |= map(patch_ds($prom; $loki))
        | (.templating.list? // [])[]?  |= patch_ds($prom; $loki)
        | .id = null
        ' "$f")

    local status
    status=$(curl -sS -X POST "http://admin:${PASS_ENC}@${GRAFANA_URL#http://}/api/dashboards/db" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --argjson d "$patched" '{dashboard: $d, overwrite: true}')" \
        | jq -r '. | (.status // "error") + ": " + (.url // .message // "?")')
    log "  → $status"
}

# Import every JSON under monitoring/dashboards/
shopt -s nullglob
for f in "$DASH_DIR"/*.json; do
    import_one "$f"
done
shopt -u nullglob

log "grafana OK"
