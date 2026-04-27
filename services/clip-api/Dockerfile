# Build context: services/  (so _shared/metrics.py is reachable).
FROM python:3.11-slim

WORKDIR /app
COPY clip-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download CLIP
RUN python -c "from transformers import CLIPModel, CLIPProcessor; \
    CLIPModel.from_pretrained('openai/clip-vit-base-patch32'); \
    CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')"

COPY _shared/metrics.py /app/metrics.py
COPY clip-api/app.py .

ENV TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1

EXPOSE 8001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
