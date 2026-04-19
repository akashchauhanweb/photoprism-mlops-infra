"""
Upload Service  (port 8004)

Pipeline: image file → CLIP Vision API → FAISS API /add
"""

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel

VISION_URL = os.getenv("CLIP_VISION_URL", "http://localhost:8001")
FAISS_URL  = os.getenv("FAISS_URL",       "http://localhost:8003")

_http: httpx.AsyncClient = None
TIMEOUT = httpx.Timeout(30.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http
    _http = httpx.AsyncClient(timeout=TIMEOUT)
    yield
    await _http.aclose()


app = FastAPI(
    title="Image Upload API",
    description="Encodes an image via CLIP Vision and stores it in FAISS.",
    version="1.0.0",
    lifespan=lifespan,
)


class UploadResponse(BaseModel):
    id: int
    total: int
    filename: str


async def _service_post(url: str, **kwargs):
    try:
        resp = await _http.post(url, **kwargs)
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach {url}: {exc}")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream {url} returned {resp.status_code}: {resp.text}",
        )
    return resp.json()


@app.get("/health")
async def health():
    statuses = {}
    for name, base in [("vision", VISION_URL), ("faiss", FAISS_URL)]:
        try:
            r = await _http.get(f"{base}/health", timeout=5)
            statuses[name] = r.json()
        except Exception as exc:
            statuses[name] = {"error": str(exc)}
    return {"status": "ok", "upstreams": statuses}


@app.post("/upload", response_model=UploadResponse)
async def upload_image(file: UploadFile = File(...)):
    raw = await file.read()

    vision_resp = await _service_post(
        f"{VISION_URL}/embed",
        files={"file": (file.filename, raw, file.content_type or "image/jpeg")},
    )
    embedding: list[float] = vision_resp["embedding"]

    faiss_resp = await _service_post(
        f"{FAISS_URL}/add",
        json={"embedding": embedding, "metadata": {"filename": file.filename}},
    )

    return UploadResponse(id=faiss_resp["id"], total=faiss_resp["total"], filename=file.filename)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("upload_api:app", host="0.0.0.0", port=8004, reload=False)
