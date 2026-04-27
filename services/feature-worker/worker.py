"""
Q2.5 online-feature-worker
Long-running daemon that polls the PostgreSQL feature_jobs table for pending
jobs, computes a 512-d CLIP embedding for each image, and upserts the vector
into Qdrant — making the image searchable without blocking the upload path.

Job lifecycle: pending → processing → done | failed
"""

import os
import time

import boto3
import httpx
import psycopg2
import psycopg2.extras
from botocore.client import Config
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from metrics import install_worker, ml_counter, ml_histogram, ml_gauge

BUCKET = os.environ["S3_BUCKET"]
S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_REGION = os.environ.get("S3_REGION", "chi-tacc")
CLIP_API_URL = os.environ.get("CLIP_API_URL", "http://clip-api.photoprism-platform:8001")
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]
COLLECTION_NAME = "image_embeddings"
EMBEDDING_DIM = 512
POLL_INTERVAL = 5  # seconds between polls when queue is empty
QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))

# ---- ML signals ----
JOBS_PROCESSED = ml_counter(
    "pp_feature_worker_jobs_total",
    "Feature jobs processed",
    ["outcome"],  # done | failed
)
JOB_LATENCY = ml_histogram(
    "pp_feature_worker_job_latency_seconds",
    "End-to-end job processing latency (S3 fetch + CLIP embed + Qdrant upsert)",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
S3_FETCH_LATENCY = ml_histogram(
    "pp_feature_worker_s3_fetch_seconds",
    "S3 GET latency",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
EMBED_RTT = ml_histogram(
    "pp_feature_worker_embed_rtt_seconds",
    "Round-trip to clip-api /embed/image",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
QUEUE_DEPTH = ml_gauge(
    "pp_feature_worker_queue_pending",
    "Number of feature_jobs rows in pending status",
)
QUEUE_FAILED = ml_gauge(
    "pp_feature_worker_queue_failed",
    "Number of feature_jobs rows in failed status",
)


def get_pg_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", 5432)),
        dbname=os.environ.get("POSTGRES_DB", "mlops"),
        user=os.environ.get("POSTGRES_USER", "mlops"),
        password=os.environ.get("POSTGRES_PASSWORD", "mlops"),
    )


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name=S3_REGION,
        config=Config(signature_version="s3v4"),
    )


def ensure_collection(qdrant: QdrantClient) -> None:
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION_NAME not in existing:
        print(f"Creating Qdrant collection '{COLLECTION_NAME}' ({EMBEDDING_DIM}-d cosine)...")
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )


def compute_embedding(image_bytes: bytes) -> list[float]:
    start = time.perf_counter()
    r = httpx.post(
        f"{CLIP_API_URL}/embed/image",
        files={"file": ("image.jpg", image_bytes, "image/jpeg")},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=30,
    )
    r.raise_for_status()
    EMBED_RTT.observe(time.perf_counter() - start)
    return r.json()["embedding"]


def process_job(cur, job_id, image_id, s3_key, s3, qdrant):
    job_start = time.perf_counter()
    # Download image from S3
    s3_start = time.perf_counter()
    response = s3.get_object(Bucket=BUCKET, Key=s3_key)
    image_bytes = response["Body"].read()
    S3_FETCH_LATENCY.observe(time.perf_counter() - s3_start)

    # Compute embedding
    embedding = compute_embedding(image_bytes)
    assert len(embedding) == EMBEDDING_DIM, f"Expected {EMBEDDING_DIM}-d, got {len(embedding)}"

    # Upsert into Qdrant
    point_id = abs(hash(image_id)) % (2**53)
    qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(
            id=point_id,
            vector=embedding,
            payload={"image_id": image_id, "s3_key": s3_key},
        )],
    )

    cur.execute(
        "UPDATE feature_jobs SET status='done', completed_at=NOW() WHERE job_id=%s",
        (job_id,),
    )
    JOB_LATENCY.observe(time.perf_counter() - job_start)
    print(f"  [done] {image_id} → Qdrant point {point_id}")


def refresh_queue_gauges(cur):
    cur.execute("SELECT status, COUNT(*) FROM feature_jobs GROUP BY status")
    counts = {row[0]: row[1] for row in cur.fetchall()}
    QUEUE_DEPTH.set(counts.get("pending", 0))
    QUEUE_FAILED.set(counts.get("failed", 0))


def main():
    install_worker("feature-worker", METRICS_PORT)
    print(f"metrics exporter listening on :{METRICS_PORT}")

    s3 = get_s3_client()

    print(f"Connecting to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}...")
    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    ensure_collection(qdrant)

    print(f"Polling feature_jobs every {POLL_INTERVAL}s...")
    while True:
        try:
            conn = get_pg_conn()
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("""
                        SELECT job_id, image_id, s3_key
                        FROM feature_jobs
                        WHERE status = 'pending'
                        ORDER BY created_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    """)
                    row = cur.fetchone()

                    refresh_queue_gauges(cur)

                    if row is None:
                        time.sleep(POLL_INTERVAL)
                        continue

                    job_id, image_id, s3_key = row["job_id"], row["image_id"], row["s3_key"]
                    cur.execute(
                        "UPDATE feature_jobs SET status='processing', started_at=NOW() WHERE job_id=%s",
                        (job_id,),
                    )
                    print(f"Processing job {job_id}: {image_id} ({s3_key})")

                    try:
                        process_job(cur, job_id, image_id, s3_key, s3, qdrant)
                        JOBS_PROCESSED.labels(outcome="done").inc()
                    except Exception as exc:
                        cur.execute(
                            "UPDATE feature_jobs SET status='failed', completed_at=NOW(), error=%s WHERE job_id=%s",
                            (str(exc), job_id),
                        )
                        JOBS_PROCESSED.labels(outcome="failed").inc()
                        print(f"  [failed] {image_id}: {exc}")

            conn.close()

        except psycopg2.OperationalError as exc:
            print(f"Postgres connection error: {exc}. Retrying in {POLL_INTERVAL}s...")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
