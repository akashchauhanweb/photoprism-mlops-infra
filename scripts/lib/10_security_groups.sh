# 10_security_groups.sh — ensure required Chameleon SGs exist (idempotent).
# Each SG is created only if missing; rules are added only if missing.

declare -A SG_RULES=(
    [allow-ssh-proj24]="22"
    [allow-30234-proj24]="30234"        # photoprism
    [allow-30633-proj24]="30633" # qdrant
    [allow-30300-proj24]="30300"        # grafana
    [allow-30443-proj24]="30443"        # k8s dashboard
    [allow-30810-proj24]="30810"        # search-api
    [allow-30532-proj24]="30532"        # postgres NodePort (feedback-trainer on GPU node)
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
# Derive node1's private IP from the control-plane node at runtime (not hardcoded)
NODE1_PRIVATE_IP=$(kubectl get node -l 'node-role.kubernetes.io/control-plane' \
    -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)
if [[ -z "$NODE1_PRIVATE_IP" ]]; then
    warn "could not derive node1 private IP from kubectl — SGs created but not attached"
else
    port_id=$(openstack --os-cloud "$OS_CLOUD" port list \
        --fixed-ip ip-address="$NODE1_PRIVATE_IP" -f value -c ID | head -1)
    if [[ -n "$port_id" ]]; then
        for sg_name in "${!SG_RULES[@]}"; do
            openstack --os-cloud "$OS_CLOUD" port set "$port_id" \
                --security-group "$sg_name" >/dev/null 2>&1 || true
        done
        log "  attached ${#SG_RULES[@]} SGs to node1 port ($NODE1_PRIVATE_IP)"
    else
        warn "could not find node1 port ($NODE1_PRIVATE_IP) — SGs created but not attached"
    fi
fi

log "security groups OK"
