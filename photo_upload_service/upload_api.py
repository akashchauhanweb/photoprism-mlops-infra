"""
Upload Service  (port 8004)

Pipeline: image file
  → Chameleon S3 (object storage)
  → CLIP Vision API /embed (port 8001)
  → Qdrant /collections/photos (vector store)
  → PhotoPrism REST API (photo management)
"""

import os
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

import swift_client

VISION_URL = os.getenv("CLIP_VISION_URL", "http://localhost:8001")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "photos")
PHOTOPRISM_URL = os.getenv("PHOTOPRISM_URL", "http://localhost:2342")
PHOTOPRISM_USERNAME = os.getenv("PHOTOPRISM_USERNAME", "admin")
PHOTOPRISM_PASSWORD = os.getenv("PHOTOPRISM_PASSWORD", "")

TIMEOUT = httpx.Timeout(60.0)

_http: httpx.AsyncClient = None
_qdrant: QdrantClient = None
_pp_token: str = ""
_pp_user_uid: str = ""


async def _photoprism_login(client: httpx.AsyncClient) -> tuple[str, str]:
    """Authenticate with PhotoPrism and return (bearer_token, user_uid)."""
    try:
        resp = await client.post(
            f"{PHOTOPRISM_URL}/api/v1/session",
            json={"username": PHOTOPRISM_USERNAME, "password": PHOTOPRISM_PASSWORD},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("id", ""), data.get("user", {}).get("UID", "u000000000000001")
    except Exception as exc:
        print(f"PhotoPrism login failed (uploads will be skipped): {exc}")
        return "", ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http, _qdrant, _pp_token, _pp_user_uid
    _http = httpx.AsyncClient(timeout=TIMEOUT)
    _qdrant = QdrantClient(url=QDRANT_URL)
    if not _qdrant.collection_exists(QDRANT_COLLECTION):
        _qdrant.create_collection(
            QDRANT_COLLECTION,
            vectors_config=VectorParams(size=512, distance=Distance.COSINE),
        )
    _pp_token, _pp_user_uid = await _photoprism_login(_http)
    yield
    await _http.aclose()


app = FastAPI(
    title="Image Upload API",
    description="Stores images in Chameleon S3, indexes embeddings in Qdrant, and registers in PhotoPrism.",
    version="2.0.0",
    lifespan=lifespan,
)


class UploadResponse(BaseModel):
    id: str
    swift_url: str
    filename: str
    dim: int


async def _post(url: str, **kwargs) -> dict:
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


def _photoprism_headers() -> dict:
    if _pp_token:
        return {"Authorization": f"Bearer {_pp_token}"}
    return {}


async def _register_in_photoprism(raw: bytes, filename: str, content_type: str) -> None:
    """Upload file to PhotoPrism staging then trigger import. Errors are logged but non-fatal."""
    if not _pp_token:
        print("PhotoPrism registration skipped: not authenticated.")
        return
    token = uuid.uuid4().hex
    upload_url = f"{PHOTOPRISM_URL}/api/v1/users/{_pp_user_uid}/upload/{token}"
    try:
        await _http.post(
            upload_url,
            files={"files": (filename, raw, content_type or "image/jpeg")},
            headers=_photoprism_headers(),
            timeout=30,
        )
        await _http.put(
            upload_url,
            json={},
            headers=_photoprism_headers(),
            timeout=30,
        )
    except Exception as exc:
        # Non-fatal: S3 and Qdrant already succeeded.
        print(f"PhotoPrism registration skipped: {exc}")


@app.get("/health")
async def health():
    statuses: dict = {}
    try:
        r = await _http.get(f"{VISION_URL}/health", timeout=5)
        statuses["vision"] = r.json()
    except Exception as exc:
        statuses["vision"] = {"error": str(exc)}
    try:
        info = _qdrant.get_collection(QDRANT_COLLECTION)
        statuses["qdrant"] = {"status": "ok", "vectors": info.vectors_count}
    except Exception as exc:
        statuses["qdrant"] = {"error": str(exc)}
    return {"status": "ok", "upstreams": statuses}


@app.post("/upload", response_model=UploadResponse)
async def upload_image(file: UploadFile = File(...)):
    raw = await file.read()
    content_type = file.content_type or "image/jpeg"

    # 1. Upload to Chameleon S3.
    try:
        swift_url = swift_client.upload_bytes(raw, file.filename, content_type)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # 2. Generate CLIP embedding.
    vision_resp = await _post(
        f"{VISION_URL}/embed",
        files={"file": (file.filename, raw, content_type)},
    )
    embedding: list[float] = vision_resp["embedding"]
    dim: int = vision_resp["dim"]

    # 3. Store in Qdrant.
    point_id = uuid.uuid4().hex
    _qdrant.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[
            PointStruct(
                id=point_id,
                vector=embedding,
                payload={"filename": file.filename, "swift_url": swift_url},
            )
        ],
    )

    # 4. Register in PhotoPrism (best-effort).
    await _register_in_photoprism(raw, file.filename, content_type)

    return UploadResponse(id=point_id, swift_url=swift_url, filename=file.filename, dim=dim)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("upload_api:app", host="0.0.0.0", port=8004, reload=False)
