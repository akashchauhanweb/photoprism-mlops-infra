# 16_autoscaling_alerting.sh — install prometheus-adapter (custom-metrics for HPA)
# and apply HPAs + Alertmanager rules + Slack receiver.
#
# Idempotent. Requires kube-prometheus-stack (15_monitoring_stack) already installed.

ADAPTER_RELEASE="prom-adapter"
ADAPTER_VALUES="$REPO_ROOT/monitoring/helm-values/prometheus-adapter.yaml"
ALERTS_FILE="$REPO_ROOT/monitoring/alerts.yaml"
HPA_DIR="$REPO_ROOT/k8s/platform/autoscaling"
ALERTMGR_CONFIG="$REPO_ROOT/monitoring/alertmanager-config.yaml"

for f in "$ADAPTER_VALUES" "$ALERTS_FILE" "$ALERTMGR_CONFIG"; do
    [[ -f "$f" ]] || die "missing file: $f"
done
[[ -d "$HPA_DIR" ]] || die "missing dir: $HPA_DIR"

# --- prometheus-adapter ---
log "installing/upgrading prometheus-adapter..."
helm upgrade --install "$ADAPTER_RELEASE" prometheus-community/prometheus-adapter \
    --namespace monitoring \
    --version 4.11.0 \
    -f "$ADAPTER_VALUES" \
    --wait --timeout 5m \
    >/dev/null

# --- AlertmanagerConfig — Slack receiver ---
# The webhook URL lives in a sealed secret named "alertmanager-slack" in monitoring ns.
# If the secret is absent, alerts will still fire but routing to Slack will fail.
if ! kubectl -n monitoring get secret alertmanager-slack >/dev/null 2>&1; then
    warn "  monitoring/alertmanager-slack secret absent — alerts will fire to Alertmanager but won't reach Slack"
    warn "  to enable Slack: kubectl create secret generic alertmanager-slack --from-literal=webhook-url='<URL>' -n monitoring"
fi

log "applying alert rules + alertmanager config..."
kubectl apply -f "$ALERTS_FILE" >/dev/null
kubectl apply -f "$ALERTMGR_CONFIG" >/dev/null

# --- HPAs ---
log "applying HPAs..."
kubectl apply -f "$HPA_DIR/" >/dev/null

# --- Sanity: prometheus-adapter exposing custom metrics? ---
log "verifying prometheus-adapter custom metrics API..."
for i in $(seq 1 12); do
    if kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1" >/dev/null 2>&1; then
        log "  external.metrics.k8s.io API available"
        break
    fi
    sleep 5
done

log "autoscaling + alerting OK"
