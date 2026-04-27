apiVersion: batch/v1
kind: Job
metadata:
  name: __JOB_NAME__
  namespace: photoprism-platform
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 600
  template:
    metadata:
      labels: { app: kaniko-build }
    spec:
      restartPolicy: Never
      initContainers:
      - name: build-docker-config
        image: busybox:1.36
        command:
        - sh
        - -c
        - |
          set -e
          AUTH=$(echo -n "${DOCKER_HUB_USER}:${DOCKER_HUB_PAT}" | base64 | tr -d '\n')
          cat > /kaniko/.docker/config.json <<EOF
          {"auths":{"https://index.docker.io/v1/":{"auth":"$AUTH"}}}
          EOF
        env:
        - { name: DOCKER_HUB_USER, value: "__DOCKER_HUB_USER__" }
        - { name: DOCKER_HUB_PAT, valueFrom: { secretKeyRef: { name: dockerhub-secret, key: DOCKER_HUB_PAT } } }
        volumeMounts:
        - { name: docker-config, mountPath: /kaniko/.docker }
      containers:
      - name: kaniko
        image: gcr.io/kaniko-project/executor:v1.23.2
        args:
        - --context=git://github.com/__GH_REPO__.git#refs/heads/__GH_BRANCH__
        - --context-sub-path=__SUBPATH__
        - --dockerfile=Dockerfile
        - --destination=__DESTINATION__
        - --cache=false
        - --single-snapshot
        - --use-new-run
        resources:
          requests: { cpu: 200m, memory: 512Mi }
          limits:   { cpu: "2",  memory: 4Gi }
        volumeMounts:
        - { name: docker-config, mountPath: /kaniko/.docker }
      volumes:
      - { name: docker-config, emptyDir: {} }
