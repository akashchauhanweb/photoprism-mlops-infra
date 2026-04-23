# 80_grafana.sh — import PhotoPrism dashboard to Grafana.
# Loki datasource UID is discovered at runtime; JSON is patched then POSTed.

GRAFANA_URL="http://${NODE1_FLOATING_IP}:30300"
DASH_FILE="$REPO_ROOT/monitoring/dashboards/photoprism-services.json"
[[ -f "$DASH_FILE" ]] || die "dashboard JSON missing: $DASH_FILE"

log "fetching Grafana admin password..."
PASS=$(kubectl -n monitoring get secret loki-stack-grafana \
       -o jsonpath='{.data.admin-password}' | base64 -d)
[[ -n "$PASS" ]] || die "could not get Grafana admin password"
PASS_ENC=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote_plus(sys.argv[1]))' "$PASS")

log "discovering Loki datasource UID..."
DS_UID=$(curl -sS "http://admin:${PASS_ENC}@${GRAFANA_URL#http://}/api/datasources" \
         | jq -r '.[] | select(.type=="loki") | .uid' | head -1)
[[ -n "$DS_UID" ]] || die "no Loki datasource found in Grafana"
log "  Loki UID: $DS_UID"

log "patching dashboard JSON and importing..."
patched=$(jq --arg uid "$DS_UID" '
    (.panels[].datasource.uid? // empty) |= $uid
    | .id = null
' "$DASH_FILE")

curl -sS -X POST "http://admin:${PASS_ENC}@${GRAFANA_URL#http://}/api/dashboards/db" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --argjson d "$patched" '{dashboard: $d, overwrite: true}')" \
    | jq -r '. | "  " + .status + ": " + .url' || warn "dashboard import unclear"

log "grafana OK"
