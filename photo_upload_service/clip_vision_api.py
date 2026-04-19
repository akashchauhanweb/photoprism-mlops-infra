import io
import os
import torch
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
from PIL import Image
from transformers import CLIPProcessor, CLIPVisionModelWithProjection

MODEL_ID = os.getenv("CLIP_MODEL_ID", "openai/clip-vit-base-patch32")

_model = None
_processor = None


def load_model():
    global _processor
    processor = CLIPProcessor.from_pretrained(MODEL_ID)
    model = CLIPVisionModelWithProjection.from_pretrained(
        MODEL_ID, torch_dtype=torch.float16, use_safetensors=True
    )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device).eval()
    _processor = processor
    print(f"CLIP Vision model loaded on {device}.")
    return model


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    _model = load_model()
    yield
    _model = None


app = FastAPI(
    title="CLIP Vision Encoder API",
    description="Encode images into CLIP embedding space (ViT-B/32).",
    version="1.0.0",
    lifespan=lifespan,
)


class EmbedResponse(BaseModel):
    embedding: list[float]
    dim: int


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": _model is not None}


@app.post("/embed", response_model=EmbedResponse)
async def embed_image(file: UploadFile = File(...)):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded.")

    raw = await file.read()
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Cannot decode image: {exc}")

    inputs = _processor(images=image, return_tensors="pt")
    inputs = {k: v.to(_model.device) for k, v in inputs.items()}

    with torch.no_grad():
        emb = _model(**inputs).image_embeds[0]  # [dim]
        emb = emb / emb.norm()                  # L2-normalise → cosine-ready

    return EmbedResponse(embedding=emb.float().cpu().tolist(), dim=emb.shape[0])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("clip_vision_api:app", host="0.0.0.0", port=8001, reload=False)
