apiVersion: v1
kind: ServiceAccount
metadata:
  name: pg-backup
  namespace: photoprism-platform
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: pg-backup
  namespace: photoprism-platform
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["create", "get"]
- apiGroups: [""]
  resources: ["secrets"]
  resourceNames: ["postgres-credentials"]
  verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: pg-backup
  namespace: photoprism-platform
subjects:
- kind: ServiceAccount
  name: pg-backup
  namespace: photoprism-platform
roleRef:
  kind: Role
  name: pg-backup
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: pg-backup
  namespace: photoprism-platform
spec:
  schedule: "__SCHEDULE__"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 2
  failedJobsHistoryLimit: 3
  startingDeadlineSeconds: 300
  jobTemplate:
    spec:
      backoffLimit: 1
      ttlSecondsAfterFinished: 3600
      template:
        metadata:
          labels: { app: pg-backup }
        spec:
          serviceAccountName: pg-backup
          restartPolicy: Never
          containers:
          - name: pg-backup
            image: akashweb/pg-backup:__TAG__
            imagePullPolicy: IfNotPresent
            env:
            - { name: NAMESPACE,        value: photoprism-platform }
            - { name: BACKUP_PREFIX,    value: backups }
            - { name: RETENTION_HOURS,  value: "__RETENTION_HOURS__" }
            envFrom:
            - secretRef: { name: objectstore-credentials }
            resources:
              requests: { cpu: 50m,  memory: 128Mi }
              limits:   { cpu: 500m, memory: 512Mi }
