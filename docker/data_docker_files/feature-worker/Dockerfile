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
    datasets \
    boto3 \
    huggingface_hub \
    torch \
    transformers \
    Pillow

COPY version.py .
COPY feature-worker/compute_features.py .

CMD ["python", "compute_features.py"]
