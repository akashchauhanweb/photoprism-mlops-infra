# pg-backup — periodic pg_dumpall to S3 via rclone, run from a CronJob.
# Image is intentionally small; uses kubectl exec into the postgres pod so the
# DB password never leaves that pod.

FROM alpine:3.20

ARG KUBECTL_VERSION=v1.29.4
ARG RCLONE_VERSION=v1.66.0

RUN apk add --no-cache bash curl ca-certificates gzip coreutils tzdata unzip \
 && curl -fsSLo /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && curl -fsSLo /tmp/rclone.zip \
        "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-amd64.zip" \
 && unzip -j /tmp/rclone.zip "*/rclone" -d /usr/local/bin/ \
 && chmod +x /usr/local/bin/rclone \
 && rm -f /tmp/rclone.zip

COPY backup.sh /backup.sh
RUN chmod +x /backup.sh

ENTRYPOINT ["/backup.sh"]
