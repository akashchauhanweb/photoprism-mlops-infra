import io
import logging
import os
import time as _t
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel
from transformers import CLIPModel, CLIPProcessor

from metrics import install_fastapi, ml_counter, ml_histogram

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","svc":"clip-api","msg":"%(message)s"}',
)
log = logging.getLogger("clip-api")

MODEL_NAME = os.environ.get("CLIP_MODEL", "openai/clip-vit-base-patch32")
EMBED_DIM = 512

state: dict = {}

# ---- ML signals ----
EMBED_LATENCY = ml_histogram(
    "pp_clip_embed_latency_seconds",
    "CLIP embedding latency",
    ["modality"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
EMBED_TOTAL = ml_counter(
    "pp_clip_embed_total",
    "CLIP embedding calls",
    ["modality", "outcome"],
)
IMAGE_BYTES_IN = ml_histogram(
    "pp_clip_image_bytes",
    "Size of image payloads received for embedding",
    buckets=(1024, 10*1024, 100*1024, 1024*1024, 5*1024*1024, 20*1024*1024),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"loading {MODEL_NAME}")
    model = CLIPModel.from_pretrained(MODEL_NAME)
    processor = CLIPProcessor.from_pretrained(MODEL_NAME)
    model.eval()
    state["model"] = model
    state["processor"] = processor
    log.info("startup ok")
    yield


app = FastAPI(lifespan=lifespan)
install_fastapi(app, "clip-api")

INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]

@app.middleware("http")
async def require_internal_token(request, call_next):
    if request.url.path in ("/health", "/ready", "/metrics"):
        return await call_next(request)
    if request.headers.get("X-Internal-Token") != INTERNAL_TOKEN:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return await call_next(request)

class TextIn(BaseModel):
    text: str


class EmbedOut(BaseModel):
    embedding: list[float]
    dim: int


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/ready")
def ready():
    if "model" not in state:
        raise HTTPException(503, "model not loaded")
    return {"ready": True}


@app.post("/embed/image", response_model=EmbedOut)
async def embed_image(file: UploadFile = File(...)):
    start = _t.perf_counter()
    try:
        raw = await file.read()
        IMAGE_BYTES_IN.observe(len(raw))
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as e:
            EMBED_TOTAL.labels(modality="image", outcome="error").inc()
            raise HTTPException(400, f"invalid image: {e}")

        processor = state["processor"]
        model = state["model"]
        pixel_values = processor(images=img, return_tensors="pt")["pixel_values"]
        with torch.no_grad():
            v = model.vision_model(pixel_values=pixel_values)
            feat = model.visual_projection(v.pooler_output)
            feat = feat / feat.norm(dim=-1, keepdim=True)
        EMBED_TOTAL.labels(modality="image", outcome="ok").inc()
        return EmbedOut(embedding=feat.squeeze(0).tolist(), dim=EMBED_DIM)
    finally:
        EMBED_LATENCY.labels(modality="image").observe(_t.perf_counter() - start)


@app.post("/embed/text", response_model=EmbedOut)
def embed_text(body: TextIn):
    start = _t.perf_counter()
    try:
        processor = state["processor"]
        model = state["model"]
        inputs = processor(text=[body.text], return_tensors="pt", padding=True, truncation=True)
        with torch.no_grad():
            t = model.text_model(**inputs)
            feat = model.text_projection(t.pooler_output)
            feat = feat / feat.norm(dim=-1, keepdim=True)
        EMBED_TOTAL.labels(modality="text", outcome="ok").inc()
        return EmbedOut(embedding=feat.squeeze(0).tolist(), dim=EMBED_DIM)
    except HTTPException:
        EMBED_TOTAL.labels(modality="text", outcome="error").inc()
        raise
    except Exception as e:
        EMBED_TOTAL.labels(modality="text", outcome="error").inc()
        raise HTTPException(500, f"embed failed: {e}")
    finally:
        EMBED_LATENCY.labels(modality="text").observe(_t.perf_counter() - start)
