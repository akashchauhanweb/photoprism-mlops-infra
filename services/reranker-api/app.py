import logging
import os
from io import BytesIO
from contextlib import asynccontextmanager
from typing import List

import requests
import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","svc":"reranker-api","msg":"%(message)s"}',
)
log = logging.getLogger("reranker-api")

MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen/Qwen2-VL-2B-Instruct")
LORA_PATH = os.environ.get("LORA_PATH", "/app/qwen_lora_weights")
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]

state: dict = {}


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
    log.info("startup ok")
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def require_internal_token(request: Request, call_next):
    if request.url.path in ("/health", "/ready"):
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

    model = state["model"]
    processor = state["processor"]
    device = next(model.parameters()).device

    results = []
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
                results.append({"image": image_ref, "score": 0.0})
                continue

            messages = [{"role": "user", "content": [
                {"type": "image", "image": image},
                {"type": "text", "text":
                    f"Does this image match: '{query_text}'? Return score 0-1."},
            ]}]
            text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            imgs, vids = process_vision_info(messages)
            inputs = processor(text=[text], images=imgs, videos=vids, return_tensors="pt")
            inputs = {k: v.to(device) for k, v in inputs.items()}

            with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                out = model(**inputs)
                score = torch.sigmoid(out.logits[:, -1, :].mean()).item()

            results.append({"image": image_ref, "score": round(score, 6)})

    results.sort(key=lambda x: x["score"], reverse=True)
    return RerankOut(results=results)
