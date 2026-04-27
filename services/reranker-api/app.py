import logging
import os
import time as _t
from io import BytesIO
from contextlib import asynccontextmanager
from typing import List

import requests
import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel

from metrics import install_fastapi, ml_counter, ml_histogram, ml_gauge

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","svc":"reranker-api","msg":"%(message)s"}',
)
log = logging.getLogger("reranker-api")

MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen/Qwen2-VL-2B-Instruct")
LORA_PATH = os.environ.get("LORA_PATH", "/app/qwen_lora_weights")
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]

state: dict = {}

# ---- ML signals ----
RERANK_LATENCY = ml_histogram(
    "pp_reranker_total_latency_seconds",
    "End-to-end /rerank latency (image-fetch + model passes)",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
PER_IMAGE_LATENCY = ml_histogram(
    "pp_reranker_per_image_seconds",
    "Per-image scoring latency",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
IMAGE_LOAD_FAIL = ml_counter(
    "pp_reranker_image_load_fail_total",
    "Images that failed to load (HTTP fetch / decode)",
)
DOCS_PER_REQUEST = ml_histogram(
    "pp_reranker_documents_per_request",
    "Number of documents per /rerank call",
    buckets=(1, 2, 5, 10, 20, 50),
)
SCORE_DIST = ml_histogram(
    "pp_reranker_score",
    "Distribution of softmax scores returned",
    buckets=(0.0, 0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0),
)
MODEL_LOADED = ml_gauge(
    "pp_reranker_model_loaded",
    "1 if model+LoRA are loaded, else 0",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from transformers import Qwen2VLForConditionalGeneration, AutoProcessor
    from peft import PeftModel

    log.info(f"loading base {MODEL_NAME}")
    base = Qwen2VLForConditionalGeneration.from_pretrained(
        MODEL_NAME, torch_dtype=torch.bfloat16, device_map="auto"
    )
    log.info(f"applying lora {LORA_PATH}")
    model = PeftModel.from_pretrained(base, LORA_PATH)
    model.eval()
    processor = AutoProcessor.from_pretrained(
        MODEL_NAME, min_pixels=256 * 28 * 28, max_pixels=512 * 28 * 28
    )
    state["model"] = model
    state["processor"] = processor
    MODEL_LOADED.set(1)
    log.info("startup ok")
    yield
    MODEL_LOADED.set(0)


app = FastAPI(lifespan=lifespan)
install_fastapi(app, "reranker-api")


@app.middleware("http")
async def require_internal_token(request: Request, call_next):
    # /metrics joins the open list so Prometheus can scrape unauthenticated
    if request.url.path in ("/health", "/ready", "/metrics"):
        return await call_next(request)
    if request.headers.get("X-Internal-Token") != INTERNAL_TOKEN:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return await call_next(request)


class RerankIn(BaseModel):
    query: dict
    documents: List[dict]
    instruction: str = "Retrieve images relevant to the user's query."
    fps: float = 1.0


class RerankOut(BaseModel):
    results: List[dict]


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/ready")
def ready():
    if "model" not in state:
        raise HTTPException(503, "model not loaded")
    return {"ready": True}


@app.post("/rerank", response_model=RerankOut)
def rerank(body: RerankIn):
    from qwen_vl_utils import process_vision_info

    query_text = body.query.get("text", "")
    if not query_text:
        raise HTTPException(400, "query.text required")

    DOCS_PER_REQUEST.observe(len(body.documents))
    total_start = _t.perf_counter()

    model = state["model"]
    processor = state["processor"]
    device = next(model.parameters()).device

    raw_scores = []
    images_out = []
    with torch.no_grad():
        for doc in body.documents:
            image_ref = doc.get("image", "")
            try:
                if image_ref.startswith("http"):
                    resp = requests.get(image_ref, timeout=10)
                    resp.raise_for_status()
                    image = Image.open(BytesIO(resp.content)).convert("RGB")
                else:
                    image = Image.open(image_ref).convert("RGB")
            except Exception as e:
                log.warning(f"load fail {image_ref}: {e}")
                IMAGE_LOAD_FAIL.inc()
                raw_scores.append(-999.0)
                images_out.append(image_ref)
                continue

            per_start = _t.perf_counter()
            messages = [{"role": "user", "content": [
                {"type": "image", "image": image},
                {"type": "text", "text":
                    f"Does this image match the description: '{query_text}'?\n"
                    f"Answer with a relevance score between 0 and 1."},
            ]}]
            text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            imgs, vids = process_vision_info(messages)
            inputs = processor(text=[text], images=imgs, videos=vids, return_tensors="pt")
            inputs = {k: v.to(device) for k, v in inputs.items()}

            with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                out = model(**inputs)
                raw = out.logits[:, -1, :].mean().item()

            PER_IMAGE_LATENCY.observe(_t.perf_counter() - per_start)
            raw_scores.append(raw)
            images_out.append(image_ref)

    softmax_scores = torch.softmax(torch.tensor(raw_scores), dim=0).tolist()

    results = [
        {"image": img, "score": round(score, 6)}
        for img, score in zip(images_out, softmax_scores)
    ]
    results.sort(key=lambda x: x["score"], reverse=True)

    for r in results:
        SCORE_DIST.observe(r["score"])

    RERANK_LATENCY.observe(_t.perf_counter() - total_start)
    return RerankOut(results=results)
