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
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant.photoprism-platform:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "image_embeddings")
PG_DSN = os.environ["PG_DSN"]

RERANKER_ENABLED = os.environ.get("RERANKER_ENABLED", "false").lower() == "true"
RERANKER_URL = os.environ.get("RERANKER_URL", "")
RERANKER_TOP_K = int(os.environ.get("RERANKER_TOP_K", "10"))
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_ACCESS = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET = os.environ.get("S3_SECRET_KEY", "")
S3_REGION = os.environ.get("S3_REGION", "chi-tacc")

qdrant = QdrantClient(url=QDRANT_URL)

import boto3
from botocore.client import Config
s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS,
    aws_secret_access_key=S3_SECRET,
    region_name=S3_REGION,
    config=Config(signature_version="s3v4"),
) if S3_ENDPOINT else None

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
        r = await client.post(
            f"{CLIP_API_URL}/embed/text",
            json={"text": body.query},
            headers={"X-Internal-Token": INTERNAL_TOKEN},
        )
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

    # Optional rerank via GPU reranker-api
    if RERANKER_ENABLED and RERANKER_URL and hits and s3:
        try:
            docs = []
            url_to_hit = {}
            for h in hits[:body.top_k]:
                if not h.s3_key:
                    continue
                url = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": S3_BUCKET, "Key": h.s3_key},
                    ExpiresIn=300,
                )
                docs.append({"image": url})
                url_to_hit[url] = h

            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    f"{RERANKER_URL}/rerank",
                    json={"query": {"text": body.query}, "documents": docs},
                    headers={"X-Internal-Token": INTERNAL_TOKEN},
                )
                r.raise_for_status()
                reranked = r.json()["results"][:RERANKER_TOP_K]

            new_hits = []
            for r_item in reranked:
                h = url_to_hit.get(r_item["image"])
                if h is None:
                    continue
                new_hits.append(SearchHit(
                    image_id=h.image_id,
                    s3_key=h.s3_key,
                    score=float(r_item["score"]),
                ))
            if new_hits:
                hits = new_hits
                log.info(f"reranked {len(new_hits)} hits")
        except Exception as e:
            log.warning(f"rerank failed, returning ANN order: {e}")

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
                    "INSERT INTO search_results (query_id, rank, image_id, score, clicked) VALUES (%s, %s, %s, %s, 0)",
                    (query_id, rank + 1, h.image_id, h.score),
                )
            conn.commit()
    except Exception as e:
        log.warning(f"analytics insert failed: {e}")

    log.info(f"query_id={query_id} q={body.query!r} hits={len(hits)}")
    return SearchOut(query_id=query_id, hits=hits)


class ClickIn(BaseModel):
    query_id: str
    image_id: str


@app.post("/click")
def record_click(body: ClickIn):
    with psycopg.connect(PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE search_results SET clicked = 1 WHERE query_id = %s AND image_id = %s",
            (body.query_id, body.image_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "result not found")
        conn.commit()
    log.info(f"click recorded query_id={body.query_id} image_id={body.image_id}")
    return {"ok": True}
