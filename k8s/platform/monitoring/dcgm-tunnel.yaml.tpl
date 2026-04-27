# DCGM reverse-SSH tunnel.
# Reads SSH key from secret 'dcgm-tunnel-key' (created at bring-up time, IP is parameter).
# Connects to the GPU VM's port 9400 (DCGM exporter) and exposes it as a ClusterIP
# Service `dcgm-tunnel.monitoring:9400` for Prometheus to scrape.
#
# Also tunnels the reranker's /metrics on port 8003 → reranker-tunnel.monitoring:8003.
#
# Replaced at runtime: __RERANKER_IP__, __RERANKER_SSH_USER__
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dcgm-tunnel
  namespace: monitoring
  labels: { app: dcgm-tunnel }
spec:
  replicas: 1
  strategy: { type: Recreate }
  selector: { matchLabels: { app: dcgm-tunnel } }
  template:
    metadata:
      labels: { app: dcgm-tunnel }
    spec:
      containers:
        - name: autossh
          image: jnovack/autossh:2.0.1
          env:
            - { name: SSH_HOSTUSER, value: "__RERANKER_SSH_USER__" }
            - { name: SSH_HOSTNAME, value: "__RERANKER_IP__" }
            - { name: SSH_HOSTPORT, value: "22" }
            # Local-listening forwards (in-cluster pod) → remote service on GPU VM
            - { name: SSH_TUNNEL_PORT, value: "9400" }
            - { name: SSH_TUNNEL_HOST, value: "127.0.0.1" }
            - { name: SSH_TUNNEL_REMOTE, value: "9400" }
            - { name: SSH_MODE, value: "-L" }
            - { name: AUTOSSH_GATETIME, value: "0" }
          ports:
            - { name: dcgm, containerPort: 9400 }
          volumeMounts:
            - { name: ssh-key, mountPath: /id_rsa, subPath: id_rsa, readOnly: true }
          # autossh image expects key at /id_rsa
          resources:
            requests: { cpu: 20m, memory: 32Mi }
            limits:   { cpu: 100m, memory: 128Mi }
        - name: autossh-reranker
          image: jnovack/autossh:2.0.1
          env:
            - { name: SSH_HOSTUSER, value: "__RERANKER_SSH_USER__" }
            - { name: SSH_HOSTNAME, value: "__RERANKER_IP__" }
            - { name: SSH_HOSTPORT, value: "22" }
            - { name: SSH_TUNNEL_PORT, value: "8000" }
            - { name: SSH_TUNNEL_HOST, value: "127.0.0.1" }
            - { name: SSH_TUNNEL_REMOTE, value: "8000" }
            - { name: SSH_MODE, value: "-L" }
            - { name: AUTOSSH_GATETIME, value: "0" }
          ports:
            - { name: metrics, containerPort: 8000 }
          volumeMounts:
            - { name: ssh-key, mountPath: /id_rsa, subPath: id_rsa, readOnly: true }
          resources:
            requests: { cpu: 20m, memory: 32Mi }
            limits:   { cpu: 100m, memory: 128Mi }
      volumes:
        - name: ssh-key
          secret:
            secretName: dcgm-tunnel-key
            defaultMode: 0600
---
apiVersion: v1
kind: Service
metadata:
  name: dcgm-tunnel
  namespace: monitoring
  labels: { app: dcgm-tunnel }
spec:
  selector: { app: dcgm-tunnel }
  ports:
    - { name: dcgm,    port: 9400, targetPort: 9400 }
---
apiVersion: v1
kind: Service
metadata:
  name: reranker-tunnel
  namespace: monitoring
  labels: { app: reranker-tunnel }
spec:
  selector: { app: dcgm-tunnel }   # same pod, second container
  ports:
    - { name: metrics, port: 8000, targetPort: 8000 }
