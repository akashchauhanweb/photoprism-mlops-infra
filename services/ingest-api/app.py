import hashlib
import hmac
import logging
import os
import time as _t
import uuid
from contextlib import asynccontextmanager

import boto3
import httpx
import psycopg
from botocore.client import Config
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from metrics import install_fastapi, ml_counter, ml_histogram, ml_gauge

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

# ---- ML signals ----
WEBHOOK_OUTCOME = ml_counter(
    "pp_ingest_webhook_total",
    "PhotoPrism webhook outcomes",
    ["outcome"],  # ok | bad_signature | photoprism_fail | s3_fail | enqueue_fail
)
S3_PUT_LATENCY = ml_histogram(
    "pp_ingest_s3_put_latency_seconds",
    "Time to upload original to S3",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)
PHOTOPRISM_DL_LATENCY = ml_histogram(
    "pp_ingest_photoprism_download_latency_seconds",
    "Time to download original from PhotoPrism",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)
INGESTED_BYTES = ml_counter(
    "pp_ingest_bytes_total",
    "Total bytes uploaded to S3 by ingest-api",
)
ENQUEUED = ml_counter(
    "pp_ingest_enqueued_total",
    "Feature jobs enqueued",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with psycopg.connect(PG_DSN) as conn:
        conn.execute("SELECT 1")
    log.info("startup ok")
    yield


app = FastAPI(lifespan=lifespan)
install_fastapi(app, "ingest-api")


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


async def photoprism_login(client: httpx.AsyncClient) -> tuple[str, str]:
    r = await client.post(
        "/api/v1/session",
        json={"username": PHOTOPRISM_USER, "password": PHOTOPRISM_PASSWORD},
    )
    r.raise_for_status()
    body = r.json()
    session_id = r.headers.get("X-Session-Id") or body.get("id")
    download_token = body.get("config", {}).get("downloadToken") or body.get("data", {}).get("config", {}).get("downloadToken", "")
    return session_id, download_token


@app.post("/webhook/photo-imported")
async def photo_imported(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_hmac(raw, sig):
        WEBHOOK_OUTCOME.labels(outcome="bad_signature").inc()
        raise HTTPException(401, "bad signature")

    payload = WebhookPayload.model_validate_json(raw)

    try:
        async with httpx.AsyncClient(base_url=PHOTOPRISM_URL, timeout=30) as client:
            session_id, dl_token = await photoprism_login(client)
            client.headers["X-Session-Id"] = session_id
            _dl_start = _t.perf_counter()
            r = await client.get(f"/api/v1/photos/{payload.photo_uid}/dl", params={"t": dl_token})
            r.raise_for_status()
            image_bytes = r.content
            PHOTOPRISM_DL_LATENCY.observe(_t.perf_counter() - _dl_start)
    except Exception as e:
        WEBHOOK_OUTCOME.labels(outcome="photoprism_fail").inc()
        raise HTTPException(502, f"photoprism download failed: {e}")

    s3_key = f"originals/{payload.photo_uid}"
    try:
        _put_start = _t.perf_counter()
        s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=image_bytes)
        S3_PUT_LATENCY.observe(_t.perf_counter() - _put_start)
        INGESTED_BYTES.inc(len(image_bytes))
    except Exception as e:
        WEBHOOK_OUTCOME.labels(outcome="s3_fail").inc()
        raise HTTPException(502, f"s3 put failed: {e}")

    job_id = str(uuid.uuid4())
    try:
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
        ENQUEUED.inc()
        WEBHOOK_OUTCOME.labels(outcome="ok").inc()
    except Exception as e:
        WEBHOOK_OUTCOME.labels(outcome="enqueue_fail").inc()
        raise HTTPException(500, f"enqueue failed: {e}")

    log.info(f"enqueued photo_uid={payload.photo_uid} job_id={job_id}")
    return {"job_id": job_id, "s3_key": s3_key}
