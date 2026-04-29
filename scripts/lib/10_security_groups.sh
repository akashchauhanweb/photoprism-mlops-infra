# 10_security_groups.sh — ensure required Chameleon SGs exist (idempotent).
# Each SG is created only if missing; rules are added only if missing.

declare -A SG_RULES=(
    [allow-ssh-proj24]="22"
    [allow-30234-proj24]="30234"        # photoprism
    [allow-30633-proj24]="30633"        # qdrant
    [allow-30300-proj24]="30300"        # grafana
    [allow-30443-proj24]="30443"        # k8s dashboard
    [allow-30810-proj24]="30810"        # search-api
    [allow-30532-proj24]="30532"        # postgres NodePort (feedback-trainer on GPU node)
    [allow-30500-proj24]="30500"        # mlflow
    [allow-30900-proj24]="30900"        # prometheus
    [allow-30903-proj24]="30903"        # alertmanager
    [allow-30801-proj24]="30801"        # adminer DB UI    [allow-8000-proj24]="8000"          # GPU VM: reranker-api
    [allow-8002-proj24]="8002"          # GPU VM: feedback-trainer
)

# SGs to attach to the GPU VM port (subset of SG_RULES — only inbound paths into GPU VM)
GPU_SGS=(allow-ssh-proj24 allow-8000-proj24 allow-8002-proj24)

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
    # Find the port carrying the public/floating IP, NOT the private one.
    # The sharednet1 port is the one Security Groups apply to for external traffic.
    port_id=$(openstack --os-cloud "$OS_CLOUD" port list --server node1-proj24 \
        -f value -c ID -c "Fixed IP Addresses" \
        | awk '/sharednet|10\.|172\.|129\./ && !/192\.168\./ {print $1; exit}')
    if [[ -z "$port_id" ]]; then
        # Fallback: the port whose name starts with "sharednet"
        port_id=$(openstack --os-cloud "$OS_CLOUD" port list --server node1-proj24 \
            -f value -c ID -c Name | awk '/sharednet/ {print $1; exit}')
    fi
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

log "attaching SGs to GPU VM port..."
if [[ -z "${RERANKER_IP:-}" || "$RERANKER_IP" == "<FILL_IN>" ]]; then
    warn "  RERANKER_IP not set — skipping GPU SG attachment"
else
    gpu_port_id=$(openstack --os-cloud "$OS_CLOUD" floating ip list \
        --floating-ip-address "$RERANKER_IP" \
        -f value -c "Port" 2>/dev/null | head -1)
    if [[ -z "$gpu_port_id" ]]; then
        warn "  could not resolve port for GPU VM floating IP $RERANKER_IP — SGs not attached"
    else
        for sg_name in "${GPU_SGS[@]}"; do
            openstack --os-cloud "$OS_CLOUD" port set "$gpu_port_id" \
                --security-group "$sg_name" >/dev/null 2>&1 || true
        done
        log "  attached ${#GPU_SGS[@]} SGs to GPU VM port ($RERANKER_IP)"
    fi
fi

log "security groups OK"
