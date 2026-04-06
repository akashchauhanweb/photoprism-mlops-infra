# PhotoPrism MLOps Infrastructure

Infrastructure as Code for deploying a PhotoPrism-based ML photo search system on Chameleon Cloud (KVM@TACC).

## Architecture

- **3 VMs** on KVM@TACC (node1, node2, node3) connected via private network (192.168.1.0/24)
- **Kubernetes** cluster deployed via kubespray (2 control planes, 3 workers)
- **Floating IP** on node1 (jump host, only public-facing node)

## Services

| Service | Namespace | Port | NodePort | Purpose |
|---------|-----------|------|----------|---------|
| PhotoPrism | photoprism-production | 2342 | 30234 | Photo management app |
| MariaDB | photoprism-production | 3306 | — | PhotoPrism database |
| MLFlow | photoprism-platform | 5000 | 30500 | Experiment tracking |
| Qdrant | photoprism-platform | 6333 | 30633 | Vector database |

## Bring-Up Sequence

### Step 1: Provision Infrastructure (Chameleon Jupyter)
1. Upload `clouds.yaml` to `/work/clouds.yaml` on Chameleon Jupyter
2. Open and run `0_reserve_and_provision.ipynb` — creates lease, VMs, network, floating IP via Terraform
3. Note the floating IP from the output

### Step 2: SSH into node1 (Local Terminal)
```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@<FLOATING_IP>
```

### Step 3: Set up inter-node SSH
```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -q -N ""
cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/id_rsa.pub | ssh -i ~/.ssh/id_rsa_chameleon cc@192.168.1.12 "cat >> ~/.ssh/authorized_keys"
cat ~/.ssh/id_rsa.pub | ssh -i ~/.ssh/id_rsa_chameleon cc@192.168.1.13 "cat >> ~/.ssh/authorized_keys"
```

### Step 4: Prepare nodes
```bash
for ip in 192.168.1.11 192.168.1.12 192.168.1.13; do
  ssh cc@$ip "sudo systemctl stop firewalld; sudo systemctl mask firewalld" 2>/dev/null
  ssh cc@$ip "sudo mkdir -p /etc/docker && sudo tee /etc/docker/daemon.json > /dev/null <<CONF
{
  \"registry-mirrors\": [\"http://kvm-dyn-129-114-25-246.tacc.chameleoncloud.org:5000\"],
  \"insecure-registries\": [\"registry.kube-system.svc.cluster.local:5000\", \"kvm-dyn-129-114-25-246.tacc.chameleoncloud.org:5000\"]
}
CONF"
done
```

### Step 5: Install Kubernetes
```bash
git clone --branch release-2.26 https://github.com/kubernetes-sigs/kubespray
sudo apt update && sudo apt -y install virtualenv
virtualenv -p python3 myenv
source myenv/bin/activate
cd kubespray
pip3 install -r requirements.txt
pip3 install ruamel.yaml
cp -r inventory/sample inventory/mycluster
sed -i "s/container_manager: containerd/container_manager: docker/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/metrics_server_enabled: false/metrics_server_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/dashboard_enabled: false/dashboard_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/helm_enabled: false/helm_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/registry_enabled: false/registry_enabled: true/" inventory/mycluster/group_vars/all/all.yml
sed -i "s/local_path_provisioner_enabled: false/local_path_provisioner_enabled: true/" inventory/mycluster/group_vars/all/all.yml
declare -a IPS=(192.168.1.11 192.168.1.12 192.168.1.13)
CONFIG_FILE=inventory/mycluster/hosts.yaml python3 contrib/inventory_builder/inventory.py ${IPS[@]}
ansible-playbook -i inventory/mycluster/hosts.yaml --become --become-user=root cluster.yml
```

### Step 6: Post-K8s configuration
```bash
sudo cp -R /root/.kube /home/cc/.kube
sudo chown -R cc:cc /home/cc/.kube

# Install local-path-provisioner for PersistentVolumeClaims
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.30/deploy/local-path-storage.yaml
kubectl patch storageclass local-path -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'

# Install metrics server for resource monitoring
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type='json' -p='[{"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--cert-dir=/tmp","--secure-port=10250","--kubelet-preferred-address-types=InternalIP","--kubelet-use-node-status-port","--metric-resolution=15s","--kubelet-insecure-tls"]}]'

# Fix DNS for external resolution
for ip in 192.168.1.11 192.168.1.12 192.168.1.13; do
  ssh cc@$ip "sudo sysctl -w net.ipv6.conf.ens3.disable_ipv6=1"
  ssh cc@$ip "sudo resolvectl dns ens4 127.0.0.1"
done

# Patch CoreDNS
kubectl -n kube-system patch configmap coredns --type merge -p '{
  "data": {
    "Corefile": ".:53 {\n    errors\n    health\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n      pods insecure\n      fallthrough in-addr.arpa ip6.arpa\n    }\n    prometheus :9153\n    forward . 8.8.8.8 1.1.1.1 {\n      prefer_udp\n      max_concurrent 1000\n    }\n    cache 30\n    loop\n    reload\n    loadbalance\n}\n"
  }
}'
kubectl -n kube-system rollout restart deployment coredns
```

### Step 7: Create namespaces and deploy services
```bash
kubectl create namespace photoprism-production
kubectl create namespace photoprism-staging
kubectl create namespace photoprism-canary
kubectl create namespace photoprism-platform

git clone https://github.com/akashchauhanweb/photoprism-mlops-infra.git
kubectl apply -f photoprism-mlops-infra/k8s/production/
kubectl apply -f photoprism-mlops-infra/k8s/platform/
```

### Step 8: Verify
```bash
kubectl get nodes
kubectl get pods --all-namespaces | grep photoprism
kubectl top pods --all-namespaces | grep -E "photoprism|mlflow|qdrant|mariadb"
```

## Access (after deployment)
- **PhotoPrism:** `http://<FLOATING_IP>:30234` (admin / photoprism-admin)
- **MLFlow:** `http://<FLOATING_IP>:30500`
- **Qdrant:** `http://<FLOATING_IP>:30633/dashboard/`

## Teardown
1. On Chameleon Jupyter, run the teardown cells in `0_reserve_and_provision.ipynb`
2. This destroys VMs, network, and floating IP
3. Persistent data on block storage volumes survives for next session

## Repository Structure
```
photoprism-mlops-infra/
├── tf/kvm/                    # Terraform IaC for Chameleon
│   ├── main.tf                # VMs, network, ports, floating IP
│   ├── variables.tf           # Configurable parameters
│   ├── data.tf                # Existing shared resources
│   ├── provider.tf            # OpenStack provider config
│   ├── versions.tf            # Terraform version constraints
│   └── outputs.tf             # Floating IP output
├── k8s/
│   ├── production/            # PhotoPrism + MariaDB
│   │   ├── photoprism.yaml
│   │   └── mariadb.yaml
│   ├── platform/              # MLFlow + Qdrant
│   │   ├── mlflow.yaml
│   │   └── qdrant.yaml
│   ├── staging/               # (for system integration phase)
│   └── canary/                # (for system integration phase)
├── 0_reserve_and_provision.ipynb  # Provisioning notebook
└── README.md
```
