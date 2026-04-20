FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    git libgl1-mesa-glx libglib2.0-0 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Qwen2-VL base so runtime needs no internet
RUN python -c "from transformers import Qwen2VLForConditionalGeneration, AutoProcessor; \
    Qwen2VLForConditionalGeneration.from_pretrained('Qwen/Qwen2-VL-2B-Instruct'); \
    AutoProcessor.from_pretrained('Qwen/Qwen2-VL-2B-Instruct')"

COPY qwen_lora_weights/ /app/qwen_lora_weights/
COPY app.py .

ENV TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
