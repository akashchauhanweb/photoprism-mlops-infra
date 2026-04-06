#!/bin/bash
set -euo pipefail

echo "============================================"
echo "  PhotoPrism MLOps — Cluster Setup Script"
echo "============================================"
echo ""
echo "Prerequisites:"
echo "  - VMs provisioned via Jupyter notebook"
echo "  - SSH key at ~/.ssh/id_rsa_chameleon"
echo "  - Running this script on node1"
echo ""

# --- Configuration ---
NODES=("192.168.1.11" "192.168.1.12" "192.168.1.13")
CHAMELEON_KEY="$HOME/.ssh/id_rsa_chameleon"
INFRA_REPO="https://github.com/akashchauhanweb/photoprism-mlops-infra.git"

# --- Helper: check and stop on failure ---
check() {
  if [ $? -ne 0 ]; then
    echo ""
    echo "  ✗ FAILED: $1"
    echo "  Stopping here. Fix the issue above and re-run the script."
    exit 1
  else
    echo "  ✓ $1"
  fi
}

# --- Helper: progress bar for long-running commands ---
run_with_progress() {
  local cmd="$1"
  local logfile="$2"
  local label="$3"
  local total="$4"
  local pattern="$5"
  local start_time=$(date +%s)

  # Run command in background, output to log
  eval "$cmd" > "$logfile" 2>&1 &
  local pid=$!

  # Monitor progress
  while kill -0 "$pid" 2>/dev/null; do
    local count
    count=$(grep -c "$pattern" "$logfile" 2>/dev/null) || count=0
    local now=$(date +%s)
    local elapsed=$(( now - start_time ))
    local mins=$(( elapsed / 60 ))
    local secs=$(( elapsed % 60 ))
    local pct=$(( count * 100 / total ))
    if [ "$pct" -gt 99 ]; then pct=99; fi

    printf "\r  %s: ~%3d%% (%d/~%d tasks) — %dm%02ds elapsed   " \
      "$label" "$pct" "$count" "$total" "$mins" "$secs"

    sleep 5
  done

  # Check if command succeeded
  wait "$pid"
  local exit_code=$?

  local now=$(date +%s)
  local elapsed=$(( now - start_time ))
  local mins=$(( elapsed / 60 ))
  local secs=$(( elapsed % 60 ))
  local count
  count=$(grep -c "$pattern" "$logfile" 2>/dev/null) || count=0

  if [ $exit_code -eq 0 ]; then
    printf "\r  %s: 100%% (%d tasks) — %dm%02ds total            \n" \
      "$label" "$count" "$mins" "$secs"
  else
    printf "\r  %s: FAILED after %dm%02ds (%d tasks completed)   \n" \
      "$label" "$mins" "$secs" "$count"
    echo ""
    echo "  Last 30 lines of log:"
    tail -30 "$logfile"
    echo ""
    echo "  Full log at: $logfile"
    echo "  Stopping here. Fix the issue above and re-run the script."
    exit 1
  fi
}

# --- Helper: spinner for medium waits ---
wait_with_spinner() {
  local label="$1"
  local seconds="$2"
  local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
  local i=0
  local end=$(( $(date +%s) + seconds ))

  while [ $(date +%s) -lt $end ]; do
    local remaining=$(( end - $(date +%s) ))
    printf "\r  %s %s (%ds remaining)" "${spin:i%10:1}" "$label" "$remaining"
    sleep 0.3
    i=$(( i + 1 ))
  done
  printf "\r  ✓ %s                              \n" "$label"
}

# --- Preflight Check ---
echo "[0/8] Preflight checks..."
test -f "$CHAMELEON_KEY"
check "Chameleon SSH key found at $CHAMELEON_KEY"

hostname | grep -q "node1"
check "Running on node1"

# Verify we can reach all node IPs at the network level
for ip in "${NODES[@]}"; do
  ping -c 1 -W 2 "$ip" > /dev/null 2>&1
  check "Node $ip is reachable"
done

# =============================================
# Step 1: SSH Key Setup
# =============================================
echo ""
echo "[1/8] Setting up inter-node SSH..."
if [ ! -f "$HOME/.ssh/id_rsa" ]; then
  ssh-keygen -t rsa -b 4096 -f "$HOME/.ssh/id_rsa" -q -N ""
fi
grep -qF "$(cat ~/.ssh/id_rsa.pub)" ~/.ssh/authorized_keys 2>/dev/null || cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys
for ip in "${NODES[@]:1}"; do
  cat ~/.ssh/id_rsa.pub | ssh -i "$CHAMELEON_KEY" -o StrictHostKeyChecking=no cc@$ip "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
done

# Autocheck: verify SSH works to all nodes
for ip in "${NODES[@]}"; do
  result=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 cc@$ip hostname 2>/dev/null)
  test -n "$result"
  check "SSH to $ip → $result"
done

# =============================================
# Step 2: Disable Firewall
# =============================================
echo ""
echo "[2/8] Disabling firewall on all nodes..."
for ip in "${NODES[@]}"; do
  ssh cc@$ip "sudo systemctl stop firewalld 2>/dev/null; sudo systemctl mask firewalld 2>/dev/null" || true
done
check "Firewalls disabled"

# =============================================
# Step 3: Kill unattended-upgrades
# =============================================
echo ""
echo "[3/8] Stopping unattended-upgrades on all nodes..."
for ip in "${NODES[@]}"; do
  ssh cc@$ip "sudo systemctl stop unattended-upgrades 2>/dev/null; sudo systemctl disable unattended-upgrades 2>/dev/null; sudo killall -9 unattended-upgr 2>/dev/null" || true
done

# Autocheck: verify dpkg lock is free on all nodes
for ip in "${NODES[@]}"; do
  ssh cc@$ip "sudo fuser /var/lib/dpkg/lock-frontend 2>/dev/null" && {
    echo "  ✗ FAILED: dpkg lock still held on $ip — waiting 30s and retrying..."
    sleep 30
    ssh cc@$ip "sudo killall -9 unattended-upgr apt apt-get dpkg 2>/dev/null" || true
    sleep 5
    ssh cc@$ip "sudo fuser /var/lib/dpkg/lock-frontend 2>/dev/null" && {
      echo "  ✗ FAILED: dpkg lock still held on $ip after retry. Stopping."
      exit 1
    }
  } || true
done
check "Package locks clear on all nodes"

# =============================================
# Step 4: Install and run kubespray
# =============================================
echo ""
echo "[4/8] Installing Kubernetes via kubespray (this takes 20-30 minutes)..."
cd ~
if [ ! -d "kubespray" ]; then
  git clone --branch release-2.26 https://github.com/kubernetes-sigs/kubespray
  check "Kubespray cloned"
else
  echo "  ✓ Kubespray already present"
fi

sudo apt-get update -qq && sudo apt-get -y -qq install virtualenv > /dev/null 2>&1
check "virtualenv installed"

if [ ! -d "myenv" ]; then
  virtualenv -p python3 myenv
fi
source ~/myenv/bin/activate
cd ~/kubespray
pip3 install -q -r requirements.txt
pip3 install -q ruamel.yaml
check "Kubespray dependencies installed"

cp -r inventory/sample inventory/mycluster 2>/dev/null || true

# Configure kubespray — use containerd (default), enable platform features
sed -i "s/metrics_server_enabled: false/metrics_server_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/dashboard_enabled: false/dashboard_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/helm_enabled: false/helm_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/registry_enabled: false/registry_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/local_path_provisioner_enabled: false/local_path_provisioner_enabled: true/" inventory/mycluster/group_vars/all/all.yml
check "Kubespray inventory configured"

# Build inventory
declare -a IPS=(192.168.1.11 192.168.1.12 192.168.1.13)
CONFIG_FILE=inventory/mycluster/hosts.yaml python3 contrib/inventory_builder/inventory.py ${IPS[@]}
check "Kubespray hosts.yaml generated"

# Run kubespray with progress tracking
# ~650 TASK lines is typical for a 3-node kubespray run
KUBESPRAY_LOG="/tmp/kubespray_install.log"
run_with_progress \
  "ansible-playbook -i inventory/mycluster/hosts.yaml --become --become-user=root cluster.yml" \
  "$KUBESPRAY_LOG" \
  "Kubespray" \
  650 \
  "^TASK"
check "Kubespray completed (full log: $KUBESPRAY_LOG)"

# =============================================
# Step 5: Post-K8s Configuration
# =============================================
echo ""
echo "[5/8] Post-K8s configuration..."

# kubectl config
sudo cp -R /root/.kube /home/cc/.kube
sudo chown -R cc:cc /home/cc/.kube

# Autocheck: kubectl works
kubectl get nodes > /dev/null 2>&1
check "kubectl configured and cluster reachable"

# Autocheck: all nodes Ready
NOT_READY=$(kubectl get nodes --no-headers | grep -v " Ready " | wc -l)
test "$NOT_READY" -eq 0
check "All 3 nodes are Ready"

# Docker group (only if docker is the runtime)
for ip in "${NODES[@]}"; do
  ssh cc@$ip "getent group docker && sudo usermod -aG docker cc || true" 2>/dev/null
done

# Disable IPv6 and fix DNS
for ip in "${NODES[@]}"; do
  ssh cc@$ip "sudo sysctl -w net.ipv6.conf.ens3.disable_ipv6=1" > /dev/null
  ssh cc@$ip "sudo resolvectl dns ens4 127.0.0.1" > /dev/null
done
check "IPv6 disabled and DNS fixed on all nodes"

# Patch CoreDNS to use external resolvers
kubectl -n kube-system patch configmap coredns --type merge -p '{
  "data": {
    "Corefile": ".:53 {\n    errors\n    health\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n      pods insecure\n      fallthrough in-addr.arpa ip6.arpa\n    }\n    prometheus :9153\n    forward . 8.8.8.8 1.1.1.1 {\n      prefer_udp\n      max_concurrent 1000\n    }\n    cache 30\n    loop\n    reload\n    loadbalance\n}\n"
  }
}'
kubectl -n kube-system rollout restart deployment coredns
kubectl -n kube-system rollout status deployment/coredns --timeout=120s
check "CoreDNS patched and restarted"

# Install local-path-provisioner for PersistentVolumeClaims
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.30/deploy/local-path-storage.yaml
wait_with_spinner "Waiting for local-path-provisioner" 10
kubectl patch storageclass local-path -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
check "local-path-provisioner installed and set as default StorageClass"

# Install metrics server for resource monitoring
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
wait_with_spinner "Waiting for metrics server apply" 5
kubectl -n kube-system patch deployment metrics-server --type='json' -p='[{"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--cert-dir=/tmp","--secure-port=10250","--kubelet-preferred-address-types=InternalIP","--kubelet-use-node-status-port","--metric-resolution=15s","--kubelet-insecure-tls"]}]'
wait_with_spinner "Waiting for metrics server to stabilize" 30

# Autocheck: metrics server running
kubectl -n kube-system get deployment metrics-server -o jsonpath='{.status.readyReplicas}' | grep -q "1"
check "Metrics server is running"

# =============================================
# Step 6: Create Namespaces
# =============================================
echo ""
echo "[6/8] Creating namespaces..."
kubectl create namespace photoprism-production 2>/dev/null || true
kubectl create namespace photoprism-staging 2>/dev/null || true
kubectl create namespace photoprism-canary 2>/dev/null || true
kubectl create namespace photoprism-platform 2>/dev/null || true

# Autocheck: namespaces exist
for ns in photoprism-production photoprism-staging photoprism-canary photoprism-platform; do
  kubectl get namespace "$ns" > /dev/null 2>&1
  check "Namespace $ns exists"
done

# =============================================
# Step 7: Deploy Services
# =============================================
echo ""
echo "[7/8] Deploying services..."
cd ~
if [ -d "photoprism-mlops-infra" ]; then
  cd photoprism-mlops-infra && git pull && cd ~
else
  git clone "$INFRA_REPO"
fi
check "Infra repo ready"

kubectl apply -f ~/photoprism-mlops-infra/k8s/production/
check "Production manifests applied (PhotoPrism + MariaDB)"

kubectl apply -f ~/photoprism-mlops-infra/k8s/platform/
check "Platform manifests applied (MLFlow + Qdrant)"

echo "  Waiting for pods to become ready..."

SERVICES=("photoprism-production:mariadb:MariaDB" "photoprism-production:photoprism:PhotoPrism" "photoprism-platform:qdrant:Qdrant" "photoprism-platform:mlflow:MLFlow")
TOTAL=${#SERVICES[@]}
DONE=0

for svc in "${SERVICES[@]}"; do
  IFS=':' read -r ns app label <<< "$svc"
  DONE=$(( DONE + 1 ))
  
  # Spinner while waiting for this pod
  local_start=$(date +%s)
  while true; do
    status=$(kubectl -n "$ns" get pods -l app="$app" --no-headers 2>/dev/null | awk '{print $3}' | head -1)
    ready=$(kubectl -n "$ns" get pods -l app="$app" --no-headers 2>/dev/null | awk '{print $2}' | head -1)
    elapsed=$(( $(date +%s) - local_start ))
    
    if [ "$status" = "Running" ] && [ "$ready" = "1/1" ]; then
      printf "\r  ✓ [%d/%d] %s is Running (%ds)                    \n" "$DONE" "$TOTAL" "$label" "$elapsed"
      break
    elif [ $elapsed -gt 180 ]; then
      printf "\r  ✗ [%d/%d] %s timed out after 180s (status: %s)   \n" "$DONE" "$TOTAL" "$label" "$status"
      echo "  Pod logs:"
      kubectl -n "$ns" logs -l app="$app" --tail=20 2>/dev/null || true
      echo ""
      echo "  Stopping here. Fix the issue above and re-run the script."
      exit 1
    else
      spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
      idx=$(( elapsed % 10 ))
      printf "\r  %s [%d/%d] Waiting for %s... (status: %s, %ds)" "${spin:idx:1}" "$DONE" "$TOTAL" "$label" "$status" "$elapsed"
      sleep 3
    fi
  done
done

# =============================================
# Step 8: Final Verification
# =============================================
echo ""
echo "[8/8] Final verification..."
echo ""

echo "--- Nodes ---"
kubectl get nodes
echo ""

# Autocheck: all nodes Ready (final)
NOT_READY=$(kubectl get nodes --no-headers | grep -v " Ready " | wc -l)
test "$NOT_READY" -eq 0
check "All nodes Ready"

echo ""
echo "--- Pods ---"
kubectl get pods --all-namespaces | grep -E "photoprism|mlflow|qdrant|mariadb"
echo ""

# Autocheck: no crashed pods
CRASHED=$(kubectl get pods --all-namespaces --no-headers | grep -E "photoprism|mlflow|qdrant|mariadb" | grep -v "Running" | wc -l)
test "$CRASHED" -eq 0
check "All service pods are Running"

echo ""
echo "--- Services ---"
kubectl get svc --all-namespaces | grep -E "photoprism|mlflow|qdrant|mariadb"
echo ""

echo "--- Resource Usage ---"
kubectl top pods --all-namespaces 2>/dev/null | grep -E "photoprism|mlflow|qdrant|mariadb" || echo "  Metrics not ready yet — run 'kubectl top pods --all-namespaces' in a minute"

echo ""
echo "============================================"
echo "  ✓ Setup complete! All checks passed."
echo "============================================"
echo ""
echo "Access services at:"
echo "  PhotoPrism: http://<FLOATING_IP>:30234  (admin / photoprism-admin)"
echo "  MLFlow:     http://<FLOATING_IP>:30500"
echo "  Qdrant:     http://<FLOATING_IP>:30633/dashboard/"
echo ""
echo "To tear down, run the teardown cells in the Jupyter notebook."