# DCGM + reranker reverse-port-forwarding tunnel.
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
        - name: tunnel-dcgm
          image: kroniak/ssh-client:3.21
          command: ["/bin/sh","-c"]
          args:
            - |
              set -e
              cp /keys/id_rsa /tmp/id_rsa && chmod 600 /tmp/id_rsa
              while true; do
                ssh -i /tmp/id_rsa \
                  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                  -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
                  -o ExitOnForwardFailure=yes \
                  -N -L 0.0.0.0:9400:127.0.0.1:9400 \
                  __RERANKER_SSH_USER__@__RERANKER_IP__ || true
                echo "ssh exited, retrying in 5s..."
                sleep 5
              done
          ports: [{ name: dcgm, containerPort: 9400 }]
          volumeMounts:
            - { name: ssh-key, mountPath: /keys, readOnly: true }
          resources:
            requests: { cpu: 20m, memory: 32Mi }
            limits:   { cpu: 100m, memory: 128Mi }
        - name: tunnel-reranker
          image: kroniak/ssh-client:3.21
          command: ["/bin/sh","-c"]
          args:
            - |
              set -e
              cp /keys/id_rsa /tmp/id_rsa && chmod 600 /tmp/id_rsa
              while true; do
                ssh -i /tmp/id_rsa \
                  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                  -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
                  -o ExitOnForwardFailure=yes \
                  -N -L 0.0.0.0:8000:127.0.0.1:8000 \
                  __RERANKER_SSH_USER__@__RERANKER_IP__ || true
                echo "ssh exited, retrying in 5s..."
                sleep 5
              done
          ports: [{ name: metrics, containerPort: 8000 }]
          volumeMounts:
            - { name: ssh-key, mountPath: /keys, readOnly: true }
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
    - { name: dcgm, port: 9400, targetPort: 9400 }
---
apiVersion: v1
kind: Service
metadata:
  name: reranker-tunnel
  namespace: monitoring
  labels: { app: reranker-tunnel }
spec:
  selector: { app: dcgm-tunnel }
  ports:
    - { name: metrics, port: 8000, targetPort: 8000 }
