# 85_dashboard.sh — install Kubernetes Dashboard + long-lived admin token.

DASH_VERSION="v2.7.0"

log "installing Kubernetes Dashboard ${DASH_VERSION}..."
kubectl apply -f "https://raw.githubusercontent.com/kubernetes/dashboard/${DASH_VERSION}/aio/deploy/recommended.yaml"

log "patching dashboard service to NodePort 30443..."
kubectl -n kubernetes-dashboard patch svc kubernetes-dashboard \
    --type='merge' \
    -p '{"spec":{"type":"NodePort","ports":[{"port":443,"targetPort":8443,"nodePort":30443}]}}'

log "creating admin ServiceAccount + ClusterRoleBinding..."
kubectl -n kubernetes-dashboard apply -f - <<YAML
apiVersion: v1
kind: ServiceAccount
metadata:
  name: dashboard-admin
  namespace: kubernetes-dashboard
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: dashboard-admin
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: dashboard-admin
  namespace: kubernetes-dashboard
---
apiVersion: v1
kind: Secret
metadata:
  name: dashboard-admin-token
  namespace: kubernetes-dashboard
  annotations:
    kubernetes.io/service-account.name: dashboard-admin
type: kubernetes.io/service-account-token
YAML

log "waiting for dashboard pods..."
kubectl -n kubernetes-dashboard rollout status deployment/kubernetes-dashboard --timeout=2m
kubectl -n kubernetes-dashboard rollout status deployment/dashboard-metrics-scraper --timeout=2m

log "waiting for token to be populated..."
for i in {1..30}; do
    TOKEN=$(kubectl -n kubernetes-dashboard get secret dashboard-admin-token \
            -o jsonpath='{.data.token}' 2>/dev/null | base64 -d)
    [[ -n "$TOKEN" ]] && break
    sleep 2
done
[[ -n "$TOKEN" ]] || die "dashboard token never populated"

log "dashboard OK"
log "  URL:   https://${NODE1_FLOATING_IP}:30443"
log "  Token: $TOKEN"
