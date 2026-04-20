# 00_prereqs.sh — verify tools + cluster + secrets backup.
# Sourced by bring_up.sh; uses log/warn/die and env vars.

log "checking CLI tools..."
for cmd in kubectl kubeseal openstack docker ssh scp jq curl base64; do
    command -v "$cmd" >/dev/null || die "missing CLI: $cmd"
done

log "checking kubectl connectivity..."
kubectl cluster-info >/dev/null 2>&1 || die "kubectl cannot reach the cluster"

log "checking sealed-secrets key backup..."
[[ -f "$SEALED_SECRETS_KEY_BACKUP" ]] \
    || die "sealed-secrets key backup missing at $SEALED_SECRETS_KEY_BACKUP"

log "checking GPU VM SSH access..."
[[ -f "$RERANKER_SSH_KEY" ]] || die "GPU VM SSH key missing at $RERANKER_SSH_KEY"
ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
    -i "$RERANKER_SSH_KEY" \
    "$RERANKER_SSH_USER@$RERANKER_IP" "echo ok" >/dev/null \
    || die "cannot SSH to GPU VM $RERANKER_IP"

log "checking OpenStack CLI..."
openstack --os-cloud "$OS_CLOUD" security group list >/dev/null 2>&1 \
    || die "OpenStack CLI cannot list security groups (check ~/.config/openstack/clouds.yaml and OS_CLOUD)"

log "prerequisites OK"
