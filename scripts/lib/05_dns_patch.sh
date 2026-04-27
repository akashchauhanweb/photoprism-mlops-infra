# 05_dns_patch.sh — patch kubespray's nodelocaldns to use public DNS for external queries.
# Default kubespray config forwards `.:53` to /etc/resolv.conf which on Chameleon
# points at unreliable upstreams. We force it to 8.8.8.8/1.1.1.1.
# Idempotent: re-applying the same patch is a no-op rollout.

log "checking nodelocaldns external forward..."
current=$(kubectl -n kube-system get configmap nodelocaldns \
    -o jsonpath='{.data.Corefile}' \
    | grep -oE 'forward \. [^ ]+ ?[^ ]*' \
    | tail -1)

if [[ "$current" == *"8.8.8.8"* && "$current" == *"1.1.1.1"* ]]; then
    log "  already patched (forward $current) — skipping"
    return 0
fi

log "  current external forward: '$current' — patching to 8.8.8.8 1.1.1.1"
kubectl -n kube-system get configmap nodelocaldns -o yaml \
    | sed 's|forward \. /etc/resolv\.conf|forward . 8.8.8.8 1.1.1.1|' \
    | kubectl apply -f - >/dev/null

log "  restarting nodelocaldns daemonset..."
kubectl -n kube-system rollout restart daemonset nodelocaldns
kubectl -n kube-system rollout status daemonset nodelocaldns --timeout=2m \
    || warn "  nodelocaldns rollout slow; continuing"

log "DNS patch OK"
