import json
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Optional

import faiss
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# CLIP ViT-B/32 projects to 512-d; override via env if using a different model.
EMBED_DIM = int(os.getenv("FAISS_DIM", "512"))
INDEX_PATH = os.getenv("FAISS_INDEX_PATH", "faiss.index")
META_PATH = os.getenv("FAISS_META_PATH", "faiss_meta.json")

_lock = threading.Lock()
_index: Optional[faiss.Index] = None
_meta: dict[int, dict] = {}   # external int id → arbitrary metadata
_next_id: int = 0


def _build_index(dim: int) -> faiss.Index:
    base = faiss.IndexFlatIP(dim)          # inner-product = cosine for unit vecs
    return faiss.IndexIDMap(base)          # allows add_with_ids + remove_ids


def load_index():
    global _index, _meta, _next_id
    if os.path.exists(INDEX_PATH) and os.path.exists(META_PATH):
        _index = faiss.read_index(INDEX_PATH)
        with open(META_PATH) as f:
            raw = json.load(f)
        _meta = {int(k): v for k, v in raw.items()}
        _next_id = max(_meta.keys(), default=-1) + 1
        print(f"FAISS index loaded: {_index.ntotal} vectors, dim={_index.d}.")
    else:
        _index = _build_index(EMBED_DIM)
        _meta = {}
        _next_id = 0
        print(f"FAISS index created fresh, dim={EMBED_DIM}.")


def _save():
    faiss.write_index(_index, INDEX_PATH)
    with open(META_PATH, "w") as f:
        json.dump({str(k): v for k, v in _meta.items()}, f)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_index()
    yield


app = FastAPI(
    title="FAISS Vector Store API",
    description="ANN index (IndexFlatIP + IndexIDMap) for CLIP embeddings.",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class AddRequest(BaseModel):
    embedding: list[float]
    metadata: dict[str, Any] = {}


class AddResponse(BaseModel):
    id: int
    total: int


class SearchRequest(BaseModel):
    embedding: list[float]
    top_k: int = 10


class SearchHit(BaseModel):
    id: int
    score: float
    metadata: dict[str, Any]


class SearchResponse(BaseModel):
    hits: list[SearchHit]


class StatsResponse(BaseModel):
    total: int
    dim: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_unit_vec(embedding: list[float]) -> np.ndarray:
    vec = np.array(embedding, dtype=np.float32)[np.newaxis, :]  # (1, d)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "total": _index.ntotal if _index else 0}


@app.get("/stats", response_model=StatsResponse)
def stats():
    return StatsResponse(total=_index.ntotal, dim=_index.d)


@app.post("/add", response_model=AddResponse)
def add(request: AddRequest):
    global _next_id
    if len(request.embedding) != _index.d:
        raise HTTPException(
            status_code=422,
            detail=f"Expected dim={_index.d}, got {len(request.embedding)}.",
        )
    vec = _to_unit_vec(request.embedding)
    with _lock:
        eid = _next_id
        _next_id += 1
        ids = np.array([eid], dtype=np.int64)
        _index.add_with_ids(vec, ids)
        _meta[eid] = request.metadata
        _save()
    return AddResponse(id=eid, total=_index.ntotal)


@app.post("/search", response_model=SearchResponse)
def search(request: SearchRequest):
    if len(request.embedding) != _index.d:
        raise HTTPException(
            status_code=422,
            detail=f"Expected dim={_index.d}, got {len(request.embedding)}.",
        )
    if _index.ntotal == 0:
        return SearchResponse(hits=[])

    k = min(request.top_k, _index.ntotal)
    vec = _to_unit_vec(request.embedding)
    scores, ids = _index.search(vec, k)  # (1, k)

    hits = []
    for score, eid in zip(scores[0], ids[0]):
        if eid == -1:
            continue
        hits.append(SearchHit(id=int(eid), score=float(score), metadata=_meta.get(int(eid), {})))
    return SearchResponse(hits=hits)


@app.delete("/remove/{item_id}")
def remove(item_id: int):
    if item_id not in _meta:
        raise HTTPException(status_code=404, detail=f"ID {item_id} not found.")
    with _lock:
        # faiss >= 1.7.2: IDSelectorBatch accepts a numpy int64 array directly
        ids_arr = np.array([item_id], dtype=np.int64)
        _index.remove_ids(faiss.IDSelectorBatch(ids_arr))
        del _meta[item_id]
        _save()
    return {"removed": item_id, "total": _index.ntotal}


@app.post("/save")
def save():
    with _lock:
        _save()
    return {"saved": True, "total": _index.ntotal}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("faiss_api:app", host="0.0.0.0", port=8003, reload=False)
