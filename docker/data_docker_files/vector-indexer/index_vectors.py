"""
vector-indexer
Read embeddings.json from Swift (s3://ObjStore_proj24/embeddings/v1/embeddings.json),
create a Qdrant collection (512-d cosine) if it does not exist, and upsert all vectors.

Each point:
  - id:      enumerate index (integer)
  - vector:  512-d float list
  - payload: {"image_id": "<filename>"}
"""

import json
import os
import time

import boto3
from botocore.client import Config
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

BUCKET = "ObjStore_proj24"
EMBEDDINGS_KEY = "embeddings/v1/embeddings.json"
S3_ENDPOINT = "https://chi.tacc.chameleoncloud.org:7480"

COLLECTION_NAME = "image_embeddings"
EMBEDDING_DIM = 512
QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_RETRIES = 10
QDRANT_RETRY_DELAY = 3


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3"),
    )


def wait_for_qdrant(client: QdrantClient) -> None:
    for attempt in range(1, QDRANT_RETRIES + 1):
        try:
            client.get_collections()
            print(f"Qdrant ready (attempt {attempt}).")
            return
        except Exception as exc:
            print(f"Qdrant not ready (attempt {attempt}/{QDRANT_RETRIES}): {exc}")
            if attempt == QDRANT_RETRIES:
                raise RuntimeError(
                    f"Qdrant at {QDRANT_HOST}:{QDRANT_PORT} did not become ready."
                ) from exc
            time.sleep(QDRANT_RETRY_DELAY)


def ensure_collection(client: QdrantClient) -> None:
    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION_NAME in existing:
        print(f"Collection '{COLLECTION_NAME}' already exists, skipping creation.")
        return
    print(f"Creating collection '{COLLECTION_NAME}' ({EMBEDDING_DIM}-d, cosine)...")
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
    )
    print("Collection created.")


def main():
    print(f"Fetching s3://{BUCKET}/{EMBEDDINGS_KEY}...")
    s3 = get_s3_client()
    response = s3.get_object(Bucket=BUCKET, Key=EMBEDDINGS_KEY)
    embeddings = json.loads(response["Body"].read().decode("utf-8"))
    print(f"Loaded {len(embeddings)} embeddings.")

    print(f"Connecting to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}...")
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    wait_for_qdrant(client)

    ensure_collection(client)

    points = [
        PointStruct(
            id=idx,
            vector=record["embedding"],
            payload={"image_id": record["image_id"]},
        )
        for idx, record in enumerate(embeddings)
    ]

    print(f"Upserting {len(points)} points into '{COLLECTION_NAME}'...")
    client.upsert(collection_name=COLLECTION_NAME, points=points)
    print(f"Done. {len(points)} vectors indexed.")


if __name__ == "__main__":
    main()
