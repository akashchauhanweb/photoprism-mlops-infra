import logging
import os
import uuid
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from qdrant_client import QdrantClient

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","svc":"search-api","msg":"%(message)s"}',
)
log = logging.getLogger("search-api")

CLIP_API_URL = os.environ.get("CLIP_API_URL", "http://clip-api.photoprism-platform:8001")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant.photoprism-platform:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "image_embeddings")
PG_DSN = os.environ["PG_DSN"]

RERANKER_ENABLED = os.environ.get("RERANKER_ENABLED", "false").lower() == "true"
RERANKER_URL = os.environ.get("RERANKER_URL", "")

qdrant = QdrantClient(url=QDRANT_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with psycopg.connect(PG_DSN) as conn:
        conn.execute("SELECT 1")
    qdrant.get_collection(COLLECTION)
    log.info(f"startup ok (reranker={'on' if RERANKER_ENABLED else 'off'})")
    yield


app = FastAPI(lifespan=lifespan)
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchIn(BaseModel):
    query: str
    top_k: int = 20


class SearchHit(BaseModel):
    image_id: str
    s3_key: str | None = None
    score: float


class SearchOut(BaseModel):
    query_id: str
    hits: list[SearchHit]


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/ready")
def ready():
    try:
        with psycopg.connect(PG_DSN, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        qdrant.get_collection(COLLECTION)
        return {"ready": True}
    except Exception as e:
        raise HTTPException(503, f"not ready: {e}")


@app.post("/search", response_model=SearchOut)
async def search(body: SearchIn):
    # 1. Embed the query via clip-api
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{CLIP_API_URL}/embed/text", json={"text": body.query})
        r.raise_for_status()
        vector = r.json()["embedding"]

    # 2. ANN search in Qdrant
    results = qdrant.search(
        collection_name=COLLECTION,
        query_vector=vector,
        limit=body.top_k,
    )

    hits = [
        SearchHit(
            image_id=str(p.payload.get("image_id", "")),
            s3_key=p.payload.get("s3_key"),
            score=float(p.score),
        )
        for p in results
    ]

    # 3. Log to Postgres for analytics
    query_id = str(uuid.uuid4())
    try:
        with psycopg.connect(PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO search_queries (query_id, query_text, query_type) VALUES (%s, %s, %s)",
                (query_id, body.query, "raw"),
            )
            for rank, h in enumerate(hits):
                cur.execute(
                    "INSERT INTO search_results (query_id, rank, image_id, score) VALUES (%s, %s, %s, %s)",
                    (query_id, rank + 1, h.image_id, h.score),
                )
            conn.commit()
    except Exception as e:
        log.warning(f"analytics insert failed: {e}")

    log.info(f"query_id={query_id} q={body.query!r} hits={len(hits)}")
    return SearchOut(query_id=query_id, hits=hits)
