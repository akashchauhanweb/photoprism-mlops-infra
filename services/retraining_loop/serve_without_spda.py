"""
serve.py — Reranking API for serving team
Loads latest fine-tuned Qwen2-VL weights from MLflow
Exposes POST /rerank endpoint

Input format (matches VLM_input.json):
{
    "instruction": "...",
    "query": {"text": "my son at the beach"},
    "documents": [{"image": "/path/to/img.jpg"}, ...],
    "fps": 1.0
}

Output format (matches VLM_output.json):
{
    "results": [{"image": "/path/to/img.jpg", "score": 0.92}, ...]
}
"""

import logging
import os
import torch
from pathlib import Path
from typing import List
from io import BytesIO

import mlflow
import requests
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="PhotoPrism Reranker Serving API")

# ─────────────────────────────────────────────
# Config from environment variables
# ─────────────────────────────────────────────
MLFLOW_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow:5000")
MLFLOW_EXPERIMENT   = os.environ.get("MLFLOW_EXPERIMENT", "photoprism-semantic-search")
MODEL_NAME          = os.environ.get("MODEL_NAME", "Qwen/Qwen2-VL-2B-Instruct")
LORA_WEIGHTS_PATH   = os.environ.get("LORA_WEIGHTS_PATH", "/tmp/qwen_lora_weights")

mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)

# Global model state
_model = None
_processor = None


# ─────────────────────────────────────────────
# Request / Response Models
# ─────────────────────────────────────────────

class RerankRequest(BaseModel):
    instruction: str = "Retrieve images relevant to the user's query."
    query: dict           # {"text": "..."}
    documents: List[dict] # [{"image": "/path/img.jpg"}, ...]
    fps: float = 1.0
    top_k: int = 5        # return only top K results

class RerankResponse(BaseModel):
    results: List[dict]   # [{"image": "...", "score": 0.92}, ...]


# ─────────────────────────────────────────────
# Load model from MLflow
# ─────────────────────────────────────────────

def load_model():
    global _model, _processor

    if _model is not None:
        return _model, _processor

    from transformers import Qwen2VLForConditionalGeneration, AutoProcessor
    from peft import PeftModel

    log.info(f"Loading base model: {MODEL_NAME}")
    base_model = Qwen2VLForConditionalGeneration.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        attn_implementation="sdpa"
    )

    # Download latest LoRA weights from MLflow
    lora_path = Path(LORA_WEIGHTS_PATH)
    if not lora_path.exists():
        log.info("Downloading latest LoRA weights from MLflow...")
        try:
            client = mlflow.tracking.MlflowClient()
            experiment = client.get_experiment_by_name(MLFLOW_EXPERIMENT)
            if experiment is None:
                raise ValueError(f"Experiment '{MLFLOW_EXPERIMENT}' not found in MLflow")

            runs = client.search_runs(
                experiment_ids=[experiment.experiment_id],
                order_by=["start_time DESC"],
                max_results=10
            )

            # Find most recent run that has LoRA weights
            downloaded = False
            for run in runs:
                try:
                    mlflow.artifacts.download_artifacts(
                        run_id=run.info.run_id,
                        artifact_path="qwen_lora_weights",
                        dst_path="/tmp"
                    )
                    log.info(f"Downloaded LoRA weights from run {run.info.run_id}")
                    downloaded = True
                    break
                except Exception:
                    continue

            if not downloaded:
                log.warning("No LoRA weights found in MLflow — using base model")

        except Exception as e:
            log.warning(f"Could not download LoRA from MLflow: {e} — using base model")

    # Apply LoRA if weights exist
    if lora_path.exists():
        log.info(f"Applying LoRA weights from {lora_path}")
        _model = PeftModel.from_pretrained(base_model, str(lora_path))
    else:
        log.warning("No LoRA weights available — using base Qwen2-VL model")
        _model = base_model

    _processor = AutoProcessor.from_pretrained(
        MODEL_NAME,
        min_pixels=256*28*28,
        max_pixels=512*28*28
    )

    _model.eval()
    log.info("Model ready for inference")
    return _model, _processor


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

@app.on_event("startup")
def startup():
    log.info("Loading model on startup...")
    load_model()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_loaded": _model is not None,
        "mlflow_uri": MLFLOW_TRACKING_URI
    }


@app.get("/model/latest")
def get_latest_model():
    """Get info about latest model from MLflow — for serving team."""
    try:
        client = mlflow.tracking.MlflowClient()
        experiment = client.get_experiment_by_name(MLFLOW_EXPERIMENT)
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        runs = client.search_runs(
            experiment_ids=[experiment.experiment_id],
            order_by=["start_time DESC"],
            max_results=1
        )
        if not runs:
            raise HTTPException(status_code=404, detail="No runs found")

        run = runs[0]
        return {
            "run_id": run.info.run_id,
            "run_name": run.info.run_name,
            "status": run.info.status,
            "metrics": run.data.metrics,
            "params": run.data.params,
            "lora_weights_uri": f"{run.info.artifact_uri}/qwen_lora_weights"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/rerank", response_model=RerankResponse)
def rerank(request: RerankRequest):
    """
    Rerank candidate images using fine-tuned Qwen2-VL.
    Called by serving team.
    """
    from qwen_vl_utils import process_vision_info

    query_text = request.query.get("text", "")
    if not query_text:
        raise HTTPException(status_code=400, detail="query.text is required")

    model, processor = load_model()
    device = next(model.parameters()).device
    raw_scores = []
    result_images = []

    with torch.no_grad():
        for doc in request.documents:
            image_path = doc.get("image", "")

            # Load image
            try:
                if image_path.startswith("http"):
                    resp = requests.get(image_path, timeout=5)
                    image = Image.open(BytesIO(resp.content)).convert("RGB")
                else:
                    image = Image.open(image_path).convert("RGB")
            except Exception as e:
                log.warning(f"Could not load image {image_path}: {e}")
                raw_scores.append(-999.0)
                result_images.append(image_path)
                continue

            # Format input for Qwen2-VL
            messages = [{"role": "user", "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": (
                    f"Does this image match the description: '{query_text}'?\n"
                    f"Answer with a relevance score between 0 and 1."
                )}
            ]}]

            text = processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)
            image_inputs, video_inputs = process_vision_info(messages)
            inputs = processor(
                text=[text], images=image_inputs, videos=video_inputs,
                return_tensors="pt"
            )
            inputs = {k: v.to(device) for k, v in inputs.items()}

            with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                outputs = model(**inputs)
                raw_score = outputs.logits[:, -1, :].mean().item()

            raw_scores.append(raw_score)
            result_images.append(image_path)

    # Apply softmax across all document scores for proper probability distribution
    scores_tensor = torch.tensor(raw_scores)
    softmax_scores = torch.softmax(scores_tensor, dim=0).tolist()

    results = [
        {"image": img, "score": round(score, 6)}
        for img, score in zip(result_images, softmax_scores)
    ]

    # Sort by score descending and return top_k
    results.sort(key=lambda x: x["score"], reverse=True)
    results = results[:request.top_k]
    return RerankResponse(results=results)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
