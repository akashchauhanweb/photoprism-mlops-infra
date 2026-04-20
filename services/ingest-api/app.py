import hashlib
import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager

import boto3
import httpx
import psycopg
from botocore.client import Config
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","svc":"ingest-api","msg":"%(message)s"}',
)
log = logging.getLogger("ingest-api")

S3_ACCESS = os.environ["S3_ACCESS_KEY"]
S3_SECRET = os.environ["S3_SECRET_KEY"]
S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_REGION = os.environ["S3_REGION"]
S3_BUCKET = os.environ["S3_BUCKET"]

PG_DSN = os.environ["PG_DSN"]

PHOTOPRISM_URL = os.environ["PHOTOPRISM_URL"]
PHOTOPRISM_USER = os.environ["PHOTOPRISM_USER"]
PHOTOPRISM_PASSWORD = os.environ["PHOTOPRISM_PASSWORD"]

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS,
    aws_secret_access_key=S3_SECRET,
    region_name=S3_REGION,
    config=Config(signature_version="s3v4"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with psycopg.connect(PG_DSN) as conn:
        conn.execute("SELECT 1")
    log.info("startup ok")
    yield


app = FastAPI(lifespan=lifespan)


class WebhookPayload(BaseModel):
    photo_uid: str
    original_name: str
    file_hash: str


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/ready")
def ready():
    try:
        with psycopg.connect(PG_DSN, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        s3.head_bucket(Bucket=S3_BUCKET)
        return {"ready": True}
    except Exception as e:
        raise HTTPException(503, f"not ready: {e}")


def verify_hmac(raw: bytes, signature: str) -> bool:
    expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def photoprism_session() -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=PHOTOPRISM_URL, timeout=30)
    r = await client.post(
        "/api/v1/session",
        json={"username": PHOTOPRISM_USER, "password": PHOTOPRISM_PASSWORD},
    )
    r.raise_for_status()
    token = r.headers.get("X-Session-Id") or r.json().get("id")
    client.headers["X-Session-Id"] = token
    return client


@app.post("/webhook/photo-imported")
async def photo_imported(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_hmac(raw, sig):
        raise HTTPException(401, "bad signature")

    payload = WebhookPayload.model_validate_json(raw)

    async with await photoprism_session() as client:
        r = await client.get(f"/api/v1/photos/{payload.photo_uid}/dl")
        r.raise_for_status()
        image_bytes = r.content

    s3_key = f"originals/{payload.photo_uid}"
    s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=image_bytes)

    job_id = str(uuid.uuid4())
    with psycopg.connect(PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO image_metadata (image_id, split, source, dataset_version)
            VALUES (%s, 'val', 'photoprism', 'v1')
            ON CONFLICT (image_id) DO NOTHING
            """,
            (payload.photo_uid,),
        )
        cur.execute(
            """
            INSERT INTO feature_jobs (job_id, image_id, s3_key, status)
            VALUES (%s, %s, %s, 'pending')
            """,
            (job_id, payload.photo_uid, s3_key),
        )
        conn.commit()

    log.info(f"enqueued photo_uid={payload.photo_uid} job_id={job_id}")
    return {"job_id": job_id, "s3_key": s3_key}
