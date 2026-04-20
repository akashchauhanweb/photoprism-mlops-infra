import io
import logging
import os
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel
from transformers import CLIPModel, CLIPProcessor

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","svc":"clip-api","msg":"%(message)s"}',
)
log = logging.getLogger("clip-api")

MODEL_NAME = os.environ.get("CLIP_MODEL", "openai/clip-vit-base-patch32")
EMBED_DIM = 512

state: dict = {}


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

INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]

@app.middleware("http")
async def require_internal_token(request, call_next):
    if request.url.path in ("/health", "/ready"):
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
    try:
        img = Image.open(io.BytesIO(await file.read())).convert("RGB")
    except Exception as e:
        raise HTTPException(400, f"invalid image: {e}")

    processor = state["processor"]
    model = state["model"]
    pixel_values = processor(images=img, return_tensors="pt")["pixel_values"]
    with torch.no_grad():
        v = model.vision_model(pixel_values=pixel_values)
        feat = model.visual_projection(v.pooler_output)
        feat = feat / feat.norm(dim=-1, keepdim=True)
    return EmbedOut(embedding=feat.squeeze(0).tolist(), dim=EMBED_DIM)


@app.post("/embed/text", response_model=EmbedOut)
def embed_text(body: TextIn):
    processor = state["processor"]
    model = state["model"]
    inputs = processor(text=[body.text], return_tensors="pt", padding=True, truncation=True)
    with torch.no_grad():
        t = model.text_model(**inputs)
        feat = model.text_projection(t.pooler_output)
        feat = feat / feat.norm(dim=-1, keepdim=True)
    return EmbedOut(embedding=feat.squeeze(0).tolist(), dim=EMBED_DIM)
