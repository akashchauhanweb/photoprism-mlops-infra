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

log "restoring sealed-secrets master key from backup..."
# Always restore from backup if the file exists. The controller auto-generates
# a fresh key on first boot, which would diverge from the committed SealedSecrets.
# Deleting the auto-key and applying the backup ensures the cluster uses the
# same key that encrypted the repo's SealedSecrets.
if [[ -f "$SEALED_SECRETS_KEY_BACKUP" ]]; then
    kubectl -n kube-system delete secret \
        -l sealedsecrets.bitnami.com/sealed-secrets-key=active 2>/dev/null || true
    kubectl create -f "$SEALED_SECRETS_KEY_BACKUP"
    log "  backup key restored"
else
    warn "  no backup at $SEALED_SECRETS_KEY_BACKUP; controller will use auto-generated key"
    warn "  existing SealedSecrets in the repo will FAIL to unseal unless resealed"
fi

kubectl -n kube-system rollout restart deployment sealed-secrets-controller
kubectl -n kube-system rollout status  deployment sealed-secrets-controller --timeout=2m

# Nudge any SealedSecrets that failed with the old key to re-reconcile
for ns in photoprism-platform photoprism-production; do
    kubectl -n "$ns" get sealedsecret -o name 2>/dev/null \
        | xargs -r -I{} kubectl -n "$ns" annotate {} \
            reconcile-ts="$(date +%s)" --overwrite >/dev/null || true
done

log "applying sealed secrets..."
for f in \
    "$REPO_ROOT/k8s/platform/postgres-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/objectstore-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/internal-token-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/services/ingest-api-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/services/feature-worker-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/services/search-api-sealed-secret.yaml" \
    "$REPO_ROOT/k8s/platform/services/dockerhub-sealed-secret.yaml" \
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

log "validating SealedSecrets decrypted successfully..."
failed=0
for ns in photoprism-platform photoprism-production; do
    while IFS= read -r name; do
        [[ -z "$name" ]] && continue
        status=$(kubectl -n "$ns" get sealedsecret "$name" \
            -o jsonpath='{.status.conditions[?(@.type=="Synced")].status}' 2>/dev/null)
        reason=$(kubectl -n "$ns" get sealedsecret "$name" \
            -o jsonpath='{.status.conditions[?(@.type=="Synced")].message}' 2>/dev/null)
        if [[ "$status" != "True" ]]; then
            warn "  FAIL $ns/$name — status='${status:-unknown}' reason='${reason:-no status yet}'"
            failed=$((failed + 1))
        fi
    done < <(kubectl -n "$ns" get sealedsecret -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n')
done

if (( failed > 0 )); then
    die "sealed-secrets validation failed: $failed SealedSecret(s) did not decrypt. \
Likely cause: the backup key at \$SEALED_SECRETS_KEY_BACKUP doesn'"'"'t match the one \
used to encrypt the committed YAMLs. Re-seal the secrets or restore the correct key."
fi

log "sealed secrets OK"
