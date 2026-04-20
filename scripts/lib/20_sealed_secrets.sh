# 20_sealed_secrets.sh — restore SS controller key, apply sealed secrets.

log "installing sealed-secrets controller (if missing)..."
if ! kubectl get ns kube-system >/dev/null 2>&1; then
    die "kube-system namespace not present"
fi

if ! kubectl get deployment -n kube-system sealed-secrets-controller >/dev/null 2>&1; then
    log "  installing controller from upstream release"
    kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.27.1/controller.yaml
fi

log "ensuring namespaces exist..."
kubectl create ns photoprism-platform   --dry-run=client -o yaml | kubectl apply -f -
kubectl create ns photoprism-production --dry-run=client -o yaml | kubectl apply -f -

log "restoring sealed-secrets master key..."
# Apply; on fresh cluster this creates, on existing cluster skip if same key already present.
if kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key=active \
    -o name 2>/dev/null | grep -q .; then
    log "  sealed-secrets key already present, skipping restore"
else
    kubectl create -f "$SEALED_SECRETS_KEY_BACKUP"
fi

kubectl -n kube-system rollout restart deployment sealed-secrets-controller
kubectl -n kube-system rollout status  deployment sealed-secrets-controller --timeout=2m

log "applying sealed secrets..."
for f in \
    "$REPO_ROOT/k8s/platform/postgres-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/objectstore-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/internal-token-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/services/ingest-api-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/services/feature-worker-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/services/search-api-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/production/sealed-secret.yaml" \
    "$REPO_ROOT/k8s/production/photoprism-webhook-sealed-secret.yaml"; do
    [[ -f "$f" ]] || die "missing sealed secret: $f"
    kubectl apply -f "$f"
done

log "waiting for secrets to materialize..."
for i in {1..30}; do
    plat_count=$(kubectl -n photoprism-platform   get secret -o name 2>/dev/null | wc -l)
    prod_count=$(kubectl -n photoprism-production get secret -o name 2>/dev/null | wc -l)
    if [[ "${plat_count:-0}" -ge 6 && "${prod_count:-0}" -ge 2 ]]; then
        log "  platform=$plat_count, production=$prod_count secrets"
        break
    fi
    sleep 2
done

log "sealed secrets OK"
