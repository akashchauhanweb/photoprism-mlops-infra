"""
Q2.5 online-feature-worker
Long-running daemon that polls the PostgreSQL feature_jobs table for pending
jobs, computes a 512-d CLIP embedding for each image, and upserts the vector
into Qdrant — making the image searchable without blocking the upload path.

Job lifecycle: pending → processing → done | failed
"""

import io
import os
import time

import boto3
import psycopg2
import psycopg2.extras
from botocore.client import Config
from PIL import Image
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

BUCKET = "ObjStore_proj24"
S3_ENDPOINT = "https://chi.tacc.chameleoncloud.org:7480"
CLIP_MODEL_NAME = "openai/clip-vit-base-patch32"
COLLECTION_NAME = "image_embeddings"
EMBEDDING_DIM = 512
POLL_INTERVAL = 5  # seconds between polls when queue is empty
QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))


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
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3"),
    )


def ensure_collection(qdrant: QdrantClient) -> None:
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION_NAME not in existing:
        print(f"Creating Qdrant collection '{COLLECTION_NAME}' ({EMBEDDING_DIM}-d cosine)...")
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )


def compute_embedding(model, processor, img: Image.Image) -> list[float]:
    import torch
    pixel_values = processor(images=img, return_tensors="pt")["pixel_values"]
    with torch.no_grad():
        vision_outputs = model.vision_model(pixel_values=pixel_values)
        features = model.visual_projection(vision_outputs.pooler_output)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.squeeze(0).tolist()


def process_job(cur, job_id, image_id, s3_key, s3, model, processor, qdrant):
    # Download image from S3
    response = s3.get_object(Bucket=BUCKET, Key=s3_key)
    img = Image.open(io.BytesIO(response["Body"].read())).convert("RGB")

    # Compute embedding
    embedding = compute_embedding(model, processor, img)
    assert len(embedding) == EMBEDDING_DIM, f"Expected {EMBEDDING_DIM}-d, got {len(embedding)}"

    # Upsert into Qdrant — use hash of image_id as integer point ID
    point_id = abs(hash(image_id)) % (2**53)
    qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(
            id=point_id,
            vector=embedding,
            payload={"image_id": image_id, "s3_key": s3_key},
        )],
    )

    # Mark done
    cur.execute(
        "UPDATE feature_jobs SET status='done', completed_at=NOW() WHERE job_id=%s",
        (job_id,),
    )
    print(f"  [done] {image_id} → Qdrant point {point_id}")


def main():
    print("Loading CLIP model (CPU)...")
    import torch
    from transformers import CLIPModel, CLIPProcessor
    model = CLIPModel.from_pretrained(CLIP_MODEL_NAME)
    processor = CLIPProcessor.from_pretrained(CLIP_MODEL_NAME)
    model.eval()
    print("CLIP model ready.")

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
                    # Claim one pending job atomically — safe for multiple workers
                    cur.execute("""
                        SELECT job_id, image_id, s3_key
                        FROM feature_jobs
                        WHERE status = 'pending'
                        ORDER BY created_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    """)
                    row = cur.fetchone()

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
                        process_job(cur, job_id, image_id, s3_key, s3, model, processor, qdrant)
                    except Exception as exc:
                        cur.execute(
                            "UPDATE feature_jobs SET status='failed', completed_at=NOW(), error=%s WHERE job_id=%s",
                            (str(exc), job_id),
                        )
                        print(f"  [failed] {image_id}: {exc}")

            conn.close()

        except psycopg2.OperationalError as exc:
            print(f"Postgres connection error: {exc}. Retrying in {POLL_INTERVAL}s...")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
