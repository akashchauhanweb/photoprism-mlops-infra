#!/bin/bash
set -e

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

# --- Preflight Check ---
if [ ! -f "$CHAMELEON_KEY" ]; then
  echo "ERROR: $CHAMELEON_KEY not found."
  echo "Copy it from your local machine first:"
  echo "  scp -i ~/.ssh/id_rsa_chameleon ~/.ssh/id_rsa_chameleon cc@<FLOATING_IP>:~/.ssh/id_rsa_chameleon"
  exit 1
fi

# --- Step 1: SSH Key Setup ---
echo "[1/8] Setting up inter-node SSH..."
if [ ! -f "$HOME/.ssh/id_rsa" ]; then
  ssh-keygen -t rsa -b 4096 -f "$HOME/.ssh/id_rsa" -q -N ""
fi
grep -qF "$(cat ~/.ssh/id_rsa.pub)" ~/.ssh/authorized_keys 2>/dev/null || cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys
for ip in "${NODES[@]:1}"; do
  cat ~/.ssh/id_rsa.pub | ssh -i "$CHAMELEON_KEY" -o StrictHostKeyChecking=no cc@$ip "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
  echo "  Key copied to $ip"
done
# Verify
for ip in "${NODES[@]}"; do
  result=$(ssh -o StrictHostKeyChecking=no cc@$ip hostname 2>/dev/null)
  echo "  $ip → $result"
done

# --- Step 2: Disable Firewall ---
echo ""
echo "[2/8] Disabling firewall on all nodes..."
for ip in "${NODES[@]}"; do
  ssh cc@$ip "sudo systemctl stop firewalld 2>/dev/null; sudo systemctl mask firewalld 2>/dev/null" || true
  echo "  Firewall disabled on $ip"
done

# --- Step 3: Configure Docker ---
echo ""
echo "[3/8] Configuring Docker daemon on all nodes..."
for ip in "${NODES[@]}"; do
  ssh cc@$ip 'sudo mkdir -p /etc/docker && sudo tee /etc/docker/daemon.json > /dev/null <<DOCKEREOF
{
  "registry-mirrors": ["http://kvm-dyn-129-114-25-246.tacc.chameleoncloud.org:5000"],
  "insecure-registries": ["registry.kube-system.svc.cluster.local:5000", "kvm-dyn-129-114-25-246.tacc.chameleoncloud.org:5000"]
}
DOCKEREOF'
  echo "  Docker configured on $ip"
done

# --- Step 4: Kill unattended-upgrades ---
echo ""
echo "[4/8] Stopping unattended-upgrades on all nodes..."
for ip in "${NODES[@]}"; do
  ssh cc@$ip "sudo systemctl stop unattended-upgrades 2>/dev/null; sudo systemctl disable unattended-upgrades 2>/dev/null; sudo killall -9 unattended-upgr 2>/dev/null" || true
  echo "  Stopped on $ip"
done

# --- Step 5: Install and run kubespray ---
echo ""
echo "[5/8] Installing Kubernetes via kubespray (this takes 20-30 minutes)..."
cd ~
if [ ! -d "kubespray" ]; then
  git clone --branch release-2.26 https://github.com/kubernetes-sigs/kubespray
fi
sudo apt-get update -qq && sudo apt-get -y -qq install virtualenv > /dev/null 2>&1

if [ ! -d "myenv" ]; then
  virtualenv -p python3 myenv
fi
source ~/myenv/bin/activate
cd ~/kubespray
pip3 install -q -r requirements.txt
pip3 install -q ruamel.yaml

cp -r inventory/sample inventory/mycluster 2>/dev/null || true

# Configure kubespray options
sed -i "s/container_manager: containerd/container_manager: docker/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/metrics_server_enabled: false/metrics_server_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/dashboard_enabled: false/dashboard_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/helm_enabled: false/helm_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/registry_enabled: false/registry_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/local_path_provisioner_enabled: false/local_path_provisioner_enabled: true/" inventory/mycluster/group_vars/all/all.yml

# Build inventory
declare -a IPS=(192.168.1.11 192.168.1.12 192.168.1.13)
CONFIG_FILE=inventory/mycluster/hosts.yaml python3 contrib/inventory_builder/inventory.py ${IPS[@]}

# Run kubespray
ansible-playbook -i inventory/mycluster/hosts.yaml --become --become-user=root cluster.yml

# --- Step 6: Post-K8s Configuration ---
echo ""
echo "[6/8] Post-K8s configuration..."

# kubectl config
sudo cp -R /root/.kube /home/cc/.kube
sudo chown -R cc:cc /home/cc/.kube

# Docker group
for ip in "${NODES[@]}"; do
  ssh cc@$ip "sudo usermod -aG docker cc"
done

# Disable IPv6 and fix DNS
for ip in "${NODES[@]}"; do
  ssh cc@$ip "sudo sysctl -w net.ipv6.conf.ens3.disable_ipv6=1" > /dev/null
  ssh cc@$ip "sudo resolvectl dns ens4 127.0.0.1" > /dev/null
done

# Patch CoreDNS to use external resolvers
kubectl -n kube-system patch configmap coredns --type merge -p '{
  "data": {
    "Corefile": ".:53 {\n    errors\n    health\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n      pods insecure\n      fallthrough in-addr.arpa ip6.arpa\n    }\n    prometheus :9153\n    forward . 8.8.8.8 1.1.1.1 {\n      prefer_udp\n      max_concurrent 1000\n    }\n    cache 30\n    loop\n    reload\n    loadbalance\n}\n"
  }
}'
kubectl -n kube-system rollout restart deployment coredns
kubectl -n kube-system rollout status deployment/coredns --timeout=120s

# Install local-path-provisioner for PersistentVolumeClaims
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.30/deploy/local-path-storage.yaml
sleep 10
kubectl patch storageclass local-path -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'

# Install metrics server for resource monitoring
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
sleep 5
kubectl -n kube-system patch deployment metrics-server --type='json' -p='[{"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--cert-dir=/tmp","--secure-port=10250","--kubelet-preferred-address-types=InternalIP","--kubelet-use-node-status-port","--metric-resolution=15s","--kubelet-insecure-tls"]}]'

echo "  Waiting for metrics server to stabilize..."
sleep 30

# --- Step 7: Create Namespaces and Deploy Services ---
echo ""
echo "[7/8] Creating namespaces and deploying services..."
kubectl create namespace photoprism-production 2>/dev/null || true
kubectl create namespace photoprism-staging 2>/dev/null || true
kubectl create namespace photoprism-canary 2>/dev/null || true
kubectl create namespace photoprism-platform 2>/dev/null || true

# Clone or update infra repo
cd ~
if [ -d "photoprism-mlops-infra" ]; then
  cd photoprism-mlops-infra && git pull && cd ~
else
  git clone "$INFRA_REPO"
fi

kubectl apply -f ~/photoprism-mlops-infra/k8s/production/
kubectl apply -f ~/photoprism-mlops-infra/k8s/platform/

echo "  Waiting for pods to start..."
kubectl -n photoprism-production wait --for=condition=ready pod -l app=mariadb --timeout=120s 2>/dev/null || true
kubectl -n photoprism-production wait --for=condition=ready pod -l app=photoprism --timeout=120s 2>/dev/null || true
kubectl -n photoprism-platform wait --for=condition=ready pod -l app=mlflow --timeout=120s 2>/dev/null || true
kubectl -n photoprism-platform wait --for=condition=ready pod -l app=qdrant --timeout=120s 2>/dev/null || true

# --- Step 8: Verify ---
echo ""
echo "[8/8] Verification..."
echo ""
echo "--- Nodes ---"
kubectl get nodes
echo ""
echo "--- Pods ---"
kubectl get pods --all-namespaces | grep -E "photoprism|mlflow|qdrant|mariadb"
echo ""
echo "--- Resource Usage ---"
kubectl top pods --all-namespaces 2>/dev/null | grep -E "photoprism|mlflow|qdrant|mariadb" || echo "  Metrics not ready yet — run 'kubectl top pods --all-namespaces' in a minute"
echo ""
echo "--- Services ---"
kubectl get svc --all-namespaces | grep -E "photoprism|mlflow|qdrant|mariadb"

echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "Access services at:"
echo "  PhotoPrism: http://<FLOATING_IP>:30234  (admin / photoprism-admin)"
echo "  MLFlow:     http://<FLOATING_IP>:30500"
echo "  Qdrant:     http://<FLOATING_IP>:30633/dashboard/"
echo ""
echo "To tear down, run the teardown cells in the Jupyter notebook."
