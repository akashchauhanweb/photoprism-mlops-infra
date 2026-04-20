FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download CLIP so runtime has no dependency on huggingface.co
RUN python -c "from transformers import CLIPModel, CLIPProcessor; \
    CLIPModel.from_pretrained('openai/clip-vit-base-patch32'); \
    CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')"

COPY worker.py .

ENV TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1

CMD ["python", "-u", "worker.py"]
