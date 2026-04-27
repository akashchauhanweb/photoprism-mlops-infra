import logging
import os
import time as _t
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from io import BytesIO
from typing import List, Optional

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
# Optional default cap if caller omits top_k. Caller (search-api) is
# expected to pass top_k explicitly; this is a safety net.
DEFAULT_TOP_K = int(os.environ.get("RERANKER_DEFAULT_TOP_K", "10"))

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
    "Number of documents per /rerank call (after top_k cap)",
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
    from transformers import Qwen2VLForConditionalGeneration, AutoProcessor, BitsAndBytesConfig
    from peft import PeftModel

    log.info(f"loading base {MODEL_NAME} (int8)")
    bnb = BitsAndBytesConfig(load_in_8bit=True)
    base = Qwen2VLForConditionalGeneration.from_pretrained(
        MODEL_NAME, quantization_config=bnb, device_map="auto"
    )
    log.info(f"applying lora {LORA_PATH}")
    model = PeftModel.from_pretrained(base, LORA_PATH)
    model.eval()
    # Smaller pixel budget — fewer vision tokens per image, faster inference.
    processor = AutoProcessor.from_pretrained(
        MODEL_NAME, min_pixels=128 * 28 * 28, max_pixels=256 * 28 * 28
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
    # Caller controls how many to score. If omitted, falls back to env default.
    top_k: Optional[int] = None


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

    # Determine effective top-K: caller > default.
    effective_k = body.top_k if (body.top_k and body.top_k > 0) else DEFAULT_TOP_K
    documents = body.documents[:effective_k]
    if len(body.documents) > effective_k:
        log.info(f"truncating {len(body.documents)} docs to top_k={effective_k}")

    DOCS_PER_REQUEST.observe(len(documents))
    total_start = _t.perf_counter()

    model = state["model"]
    processor = state["processor"]
    device = next(model.parameters()).device

    # ---- Parallel image fetch (pure I/O — overlaps S3 round-trips) ----
    def _fetch(doc):
        image_ref = doc.get("image", "")
        try:
            if image_ref.startswith("http"):
                resp = requests.get(image_ref, timeout=10)
                resp.raise_for_status()
                return image_ref, Image.open(BytesIO(resp.content)).convert("RGB"), None
            return image_ref, Image.open(image_ref).convert("RGB"), None
        except Exception as e:
            log.warning(f"load fail {image_ref}: {e}")
            return image_ref, None, e

    if documents:
        with ThreadPoolExecutor(max_workers=len(documents)) as pool:
            fetched = list(pool.map(_fetch, documents))
    else:
        fetched = []

    raw_scores: List[float] = []
    images_out: List[str] = []
    with torch.no_grad():
        for image_ref, image, err in fetched:
            if err is not None:
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

    # Softmax across ALL candidates — proper probability distribution
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
