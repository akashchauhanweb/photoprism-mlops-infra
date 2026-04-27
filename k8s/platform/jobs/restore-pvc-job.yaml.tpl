# Templated — substitute __NS__, __JOB_NAME__, __PVC__, __MOUNT__,
# __S3_ENDPOINT__, __S3_BUCKET__, __S3_KEY__, __S3_SECRET__, __S3_REGION__,
# __BACKUP_PREFIX__, __COMPONENT__ before kubectl apply.
apiVersion: batch/v1
kind: Job
metadata:
  name: __JOB_NAME__
  namespace: __NS__
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 300
  template:
    spec:
      nodeSelector:
        kubernetes.io/hostname: node1
      restartPolicy: Never
      containers:
        - name: restore
          image: rclone/rclone:1.68
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -eu
              mkdir -p /root/.config/rclone
              cat > /root/.config/rclone/rclone.conf <<RCONF
              [chi]
              type = s3
              provider = Other
              env_auth = false
              access_key_id = $(S3_ACCESS_KEY)
              secret_access_key = $(S3_SECRET_KEY)
              endpoint = $(S3_ENDPOINT)
              region = $(S3_REGION)
              acl = private
              RCONF
              SRC="chi:$(S3_BUCKET)/__BACKUP_PREFIX__/__COMPONENT__/latest.tar.gz"
              echo "[restore] downloading $SRC ..."
              rclone copyto "$SRC" /tmp/restore.tar.gz
              echo "[restore] untarring to __MOUNT__ ..."
              # Strip the top-level dir so contents land directly in __MOUNT__
              tar -xzf /tmp/restore.tar.gz -C __MOUNT__ --strip-components=1
              echo "[restore] done"
              ls -la __MOUNT__ | head -20
          env:
            - { name: S3_ACCESS_KEY, valueFrom: { secretKeyRef: { name: objectstore-credentials, key: S3_ACCESS_KEY } } }
            - { name: S3_SECRET_KEY, valueFrom: { secretKeyRef: { name: objectstore-credentials, key: S3_SECRET_KEY } } }
            - { name: S3_ENDPOINT,   valueFrom: { secretKeyRef: { name: objectstore-credentials, key: S3_ENDPOINT   } } }
            - { name: S3_BUCKET,     valueFrom: { secretKeyRef: { name: objectstore-credentials, key: S3_BUCKET     } } }
            - { name: S3_REGION,     valueFrom: { secretKeyRef: { name: objectstore-credentials, key: S3_REGION     } } }
          volumeMounts:
            - name: target
              mountPath: __MOUNT__
      volumes:
        - name: target
          persistentVolumeClaim:
            claimName: __PVC__
