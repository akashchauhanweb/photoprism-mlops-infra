FROM python:3.11-slim

RUN apt-get update && apt-get install -y curl unzip && \
    curl -fsSL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o rclone.zip && \
    unzip rclone.zip && mv rclone-*-linux-amd64/rclone /usr/local/bin/ && \
    rm -rf rclone.zip rclone-*-linux-amd64 && \
    apt-get remove -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY rclone.conf /root/.config/rclone/rclone.conf

RUN pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    torch \
    transformers \
    Pillow \
    boto3 \
    psycopg2-binary \
    qdrant-client

COPY online-feature-worker/worker.py .
COPY online-feature-worker/submit_job.py .
COPY online-feature-worker/batch_submit.py .

CMD ["python", "worker.py"]
