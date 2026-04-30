# 15_monitoring_stack.sh — install the cluster monitoring stack (idempotent).
# Components:
#   - kube-prometheus-stack  (Prometheus, Alertmanager, Grafana, node-exporter, kube-state-metrics)
#   - loki                   (logs only — Grafana from this chart is disabled, we use the one from k-p-s)
#   - promtail               (ships pod logs to loki)
#
# Reproducibility notes:
#   - All values come from monitoring/helm-values/*.yaml in the repo.
#   - On first run we detect & uninstall the legacy "loki-stack" release if present
#     (one-time migration; idempotent thereafter).
#   - No Grafana data persists across teardown — dashboards are re-imported by 80_grafana.sh.

MON_NS="monitoring"
KPS_RELEASE="kube-prom"
LOKI_RELEASE="loki"
PROMTAIL_RELEASE="promtail"

KPS_VALUES="$REPO_ROOT/monitoring/helm-values/kube-prometheus-stack.yaml"
LOKI_VALUES="$REPO_ROOT/monitoring/helm-values/loki.yaml"
PROMTAIL_VALUES="$REPO_ROOT/monitoring/helm-values/promtail.yaml"

for f in "$KPS_VALUES" "$LOKI_VALUES" "$PROMTAIL_VALUES"; do
    [[ -f "$f" ]] || die "missing helm values file: $f"
done

# --- helm sanity (kubespray ships helm_enabled=true but doesn't install the binary) ---
if ! command -v helm >/dev/null 2>&1; then
    log "installing helm..."
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash >/dev/null
fi

# --- repos (idempotent) ---
log "ensuring helm repos..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana             https://grafana.github.io/helm-charts             >/dev/null 2>&1 || true
helm repo update >/dev/null

# --- one-time migration: remove legacy loki-stack release if present ---
if helm -n "$MON_NS" list -q 2>/dev/null | grep -qx "loki-stack"; then
    log "migrating: uninstalling legacy 'loki-stack' release..."
    helm -n "$MON_NS" uninstall loki-stack >/dev/null || warn "  loki-stack uninstall non-clean"
    # Kill PVCs from the old release explicitly (helm doesn't delete those)
    kubectl -n "$MON_NS" get pvc -o name 2>/dev/null \
        | grep -E 'loki-stack|storage-loki-stack' \
        | xargs -r kubectl -n "$MON_NS" delete --wait=false 2>/dev/null || true
fi

# --- namespace ---
kubectl get ns "$MON_NS" >/dev/null 2>&1 \
    || kubectl create namespace "$MON_NS" >/dev/null

# --- kube-prometheus-stack ---
log "installing/upgrading kube-prometheus-stack..."
helm upgrade --install "$KPS_RELEASE" prometheus-community/kube-prometheus-stack \
    --namespace "$MON_NS" \
    --version 65.5.1 \
    -f "$KPS_VALUES" \
    --wait --timeout 8m \
    >/dev/null

# --- loki (single-binary, filesystem storage — small, cluster-internal) ---
log "installing/upgrading loki..."
helm upgrade --install "$LOKI_RELEASE" grafana/loki \
    --namespace "$MON_NS" \
    --version 6.16.0 \
    -f "$LOKI_VALUES" \
    --wait --timeout 5m \
    >/dev/null

# --- promtail ---
log "installing/upgrading promtail..."
helm upgrade --install "$PROMTAIL_RELEASE" grafana/promtail \
    --namespace "$MON_NS" \
    --version 6.16.6 \
    -f "$PROMTAIL_VALUES" \
    --wait --timeout 5m \
    >/dev/null

# --- ServiceMonitors and Loki datasource provisioning are applied as plain manifests ---
log "applying servicemonitors + datasource configmap..."
kubectl apply -f "$REPO_ROOT/k8s/platform/monitoring/grafana-loki-datasource.yaml" -f "$REPO_ROOT/k8s/platform/monitoring/servicemonitors.yaml" >/dev/null
kubectl apply -f "$REPO_ROOT/monitoring/alerts.yaml" -f "$REPO_ROOT/monitoring/alertmanager-config.yaml" >/dev/null

# --- DCGM tunnel (GPU VM → in-cluster) ---
# Wired via a Deployment that runs autossh + a Service so Prometheus can scrape it
# at dcgm-tunnel.monitoring:9400. The DCGM exporter itself is started on the GPU
# VM by 65_gpu_vm_prereqs.sh.
if [[ -n "${RERANKER_IP:-}" ]]; then
    log "configuring DCGM reverse-tunnel for $RERANKER_IP..."
    # Sealed secret containing the SSH private key — created on-the-fly here so the
    # GPU IP is never baked into a manifest.
    if ! kubectl -n "$MON_NS" get secret dcgm-tunnel-key >/dev/null 2>&1; then
        kubectl -n "$MON_NS" create secret generic dcgm-tunnel-key \
            --from-file=id_rsa="$RERANKER_SSH_KEY" >/dev/null
        kubectl -n "$MON_NS" patch secret dcgm-tunnel-key \
            -p '{"metadata":{"annotations":{"managed-by":"15_monitoring_stack"}}}' >/dev/null
    fi
    # Render the tunnel manifest with the current IP (re-applied each bring-up)
    sed "s|__RERANKER_IP__|$RERANKER_IP|g; s|__RERANKER_SSH_USER__|${RERANKER_SSH_USER:-cc}|g" \
        "$REPO_ROOT/k8s/platform/monitoring/dcgm-tunnel.yaml.tpl" \
        | kubectl apply -f - >/dev/null
else
    warn "RERANKER_IP empty — DCGM tunnel not deployed; GPU panels will be empty"
    kubectl -n "$MON_NS" delete deployment dcgm-tunnel --ignore-not-found >/dev/null 2>&1 || true
fi

log "monitoring stack OK"
