# 10_security_groups.sh — ensure required Chameleon SGs exist (idempotent).
# Each SG is created only if missing; rules are added only if missing.

declare -A SG_RULES=(
    [allow-ssh]="22"
    [allow-30234]="30234"        # photoprism
    [allow-30633-proj24]="30633" # qdrant
    [allow-30300]="30300"        # grafana
    [allow-30443]="30443"        # k8s dashboard
    [allow-30810]="30810"        # search-api
    [allow-30532]="30532"        # postgres NodePort (feedback-trainer on GPU node)
)

log "reconciling security groups..."
existing_sgs=$(openstack --os-cloud "$OS_CLOUD" security group list -f value -c Name)

for sg_name in "${!SG_RULES[@]}"; do
    port="${SG_RULES[$sg_name]}"
    if ! grep -qx "$sg_name" <<<"$existing_sgs"; then
        log "  creating SG $sg_name (TCP $port)"
        openstack --os-cloud "$OS_CLOUD" security group create "$sg_name" \
            --description "auto: TCP $port" >/dev/null
    fi
    # Ensure the ingress rule exists (ignore if already present)
    openstack --os-cloud "$OS_CLOUD" security group rule create "$sg_name" \
        --protocol tcp --dst-port "$port" --remote-ip 0.0.0.0/0 \
        --ingress >/dev/null 2>&1 || true
done

log "attaching SGs to node1 port..."
# Find the port that owns the floating IP
port_id=$(openstack --os-cloud "$OS_CLOUD" port list --fixed-ip ip-address=192.168.1.11 \
    -f value -c ID | head -1)
if [[ -n "$port_id" ]]; then
    for sg_name in "${!SG_RULES[@]}"; do
        openstack --os-cloud "$OS_CLOUD" port set "$port_id" \
            --security-group "$sg_name" >/dev/null 2>&1 || true
    done
    log "  attached ${#SG_RULES[@]} SGs to node1 port"
else
    warn "could not find node1 port (192.168.1.11) — SGs created but not attached"
fi

log "security groups OK"
