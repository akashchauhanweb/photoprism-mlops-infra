"""
Q2.6 batch-compiler
Build (query, positive_id, negative_id, query_type, source, dataset_version, created_at)
training tuples from two sources:
  1. HuggingFace static dataset (flickr30k_cfq)
  2. Interaction logs from PostgreSQL (clicked = positive, high-rank unclicked = negative)

Split by image_id hash: 70% train / 15% val / 15% test.
Assert zero overlap between train and test image IDs (leakage check).
Export interaction snapshot + splits to Swift under datasets/v{N}/.
"""

import hashlib
import io
import json
import os
import random
from datetime import datetime, timezone

import boto3
import psycopg2
import psycopg2.extras
from botocore.client import Config
from huggingface_hub import hf_hub_download
from version import resolve_version

BUCKET = "ObjStore_proj24"
S3_ENDPOINT = "https://chi.tacc.chameleoncloud.org:7480"
HF_REPO = "ar10067/flickr30k-images-CFQ"

TRAIN_RATIO = 0.70
VAL_RATIO = 0.15


def image_id_split(image_id: str) -> str:
    """Deterministically assign an image to train/val/test by hashing its ID."""
    h = int(hashlib.md5(str(image_id).encode()).hexdigest(), 16)
    bucket = (h % 100) / 100.0
    if bucket < TRAIN_RATIO:
        return "train"
    elif bucket < TRAIN_RATIO + VAL_RATIO:
        return "val"
    else:
        return "test"


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


def iter_queries(record):
    queries = record.get("queries", {})
    for q in queries.get("raw", []):
        if q and q.strip():
            yield q.strip(), "raw"
    tags = [t.strip() for t in queries.get("tags", []) if t and t.strip()]
    if tags:
        yield ", ".join(tags), "tags"
    caption = queries.get("caption", "")
    if caption and caption.strip():
        yield caption.strip(), "caption"


def load_interaction_tuples(all_image_ids: list, created_at: str, version: str) -> list:
    """
    Query PostgreSQL for interaction logs.
    Clicked image = positive. A high-rank (top-3) unclicked image = hard negative.
    Returns list of training tuple dicts.
    """
    tuples = []
    try:
        conn = get_pg_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT
                    sq.query_id,
                    sq.query_text,
                    sq.query_type,
                    fe.image_id   AS clicked_id,
                    fe.rank       AS click_rank
                FROM feedback_events fe
                JOIN search_queries sq ON sq.query_id = fe.query_id
                WHERE fe.event_type = 'click'
            """)
            feedback_rows = cur.fetchall()

            if not feedback_rows:
                print("  No interaction data in PostgreSQL yet — skipping interaction tuples.")
                return []

            # For each click, find a hard negative: top-3 result that was NOT clicked
            for row in feedback_rows:
                query_id = row["query_id"]
                cur.execute("""
                    SELECT image_id FROM search_results
                    WHERE query_id = %s AND image_id != %s AND rank <= 3
                    ORDER BY rank
                    LIMIT 1
                """, (query_id, row["clicked_id"]))
                neg_row = cur.fetchone()
                neg_id = neg_row["image_id"] if neg_row else random.choice(all_image_ids)

                tuples.append({
                    "query": row["query_text"],
                    "positive_id": row["clicked_id"],
                    "negative_id": neg_id,
                    "query_type": row["query_type"],
                    "source": "interaction_log",
                    "dataset_version": version,
                    "created_at": created_at,
                })

        conn.close()
        print(f"  Loaded {len(tuples):,} interaction-derived tuples from PostgreSQL.")
    except Exception as exc:
        print(f"  WARNING: Could not load interaction data from PostgreSQL: {exc}")
        print("  Continuing with static dataset tuples only.")

    return tuples


def export_interaction_snapshot(tuples: list, prefix: str, s3) -> None:
    """Export interaction-derived tuples to Swift for auditability."""
    if not tuples:
        return
    buf = io.BytesIO()
    for t in tuples:
        buf.write((json.dumps(t, default=str) + "\n").encode("utf-8"))
    buf.seek(0)
    key = f"{prefix}/interactions_snapshot.jsonl"
    print(f"Uploading interaction snapshot s3://{BUCKET}/{key}...")
    s3.put_object(Bucket=BUCKET, Key=key, Body=buf.read())


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        choices=["static", "interactions", "static-interactions"],
        default="static-interactions",
        help="Data source: static (HF only), interactions (Postgres only), or both (default)",
    )
    args = parser.parse_args()

    version, commit_sha = resolve_version()
    prefix = f"datasets/{version}"
    raw_prefix = f"raw/{version}"
    created_at = datetime.now(timezone.utc).isoformat()
    print(f"Dataset version: {version} → s3://{BUCKET}/{prefix}/  [source: {args.source}]")

    print(f"Downloading unified_dataset.json from {HF_REPO}...")
    local_path = hf_hub_download(
        repo_id=HF_REPO,
        filename="unified_dataset.json",
        repo_type="dataset",
        token=os.environ.get("HF_TOKEN"),
    )
    with open(local_path) as f:
        records = json.load(f)
    print(f"Loaded {len(records):,} records.")

    all_image_ids = [r["image_id"] for r in records]
    splits: dict[str, list] = {"train": [], "val": [], "test": []}

    # --- Source 1: Static HuggingFace dataset ---
    if args.source in ("static", "static-interactions"):
        print("Building tuples from static dataset...")
        for record in records:
            pos_id = record["image_id"]
            split = image_id_split(pos_id)
            for query_text, query_type in iter_queries(record):
                neg_id = pos_id
                while neg_id == pos_id:
                    neg_id = random.choice(all_image_ids)
                splits[split].append({
                    "query": query_text,
                    "positive_id": pos_id,
                    "negative_id": neg_id,
                    "query_type": query_type,
                    "source": "flickr30k_cfq",
                    "dataset_version": version,
                    "created_at": created_at,
                })

    # --- Source 2: Interaction logs from PostgreSQL ---
    interaction_tuples = []
    if args.source in ("interactions", "static-interactions"):
        print("Loading interaction-derived tuples from PostgreSQL...")
        interaction_tuples = load_interaction_tuples(all_image_ids, created_at, version)
        for t in interaction_tuples:
            split = image_id_split(t["positive_id"])
            splits[split].append(t)

    # --- Leakage check ---
    train_ids = {t["positive_id"] for t in splits["train"]}
    test_ids = {t["positive_id"] for t in splits["test"]}
    overlap = train_ids & test_ids
    assert len(overlap) == 0, (
        f"Leakage detected: {len(overlap)} image IDs appear in both train and test."
    )
    print("Leakage check passed: zero overlap between train and test image IDs.")
    for split_name, tuples in splits.items():
        print(f"  {split_name}: {len(tuples):,} tuples")

    s3 = get_s3_client()

    # --- Export interaction snapshot ---
    export_interaction_snapshot(interaction_tuples, prefix, s3)

    # --- Upload splits ---
    for split_name, tuples in splits.items():
        buf = io.BytesIO()
        for t in tuples:
            buf.write((json.dumps(t, default=str) + "\n").encode("utf-8"))
        buf.seek(0)
        key = f"{prefix}/{split_name}.jsonl"
        print(f"Uploading s3://{BUCKET}/{key}...")
        s3.put_object(Bucket=BUCKET, Key=key, Body=buf.read())

    manifest = {
        "created_at": created_at,
        "version": version,
        "source_commit_sha": commit_sha,
        "source": HF_REPO,
        "derived_from": f"s3://{BUCKET}/{raw_prefix}/",
        "split_method": "md5(image_id) % 100",
        "split_ratios": {
            "train": TRAIN_RATIO,
            "val": VAL_RATIO,
            "test": round(1 - TRAIN_RATIO - VAL_RATIO, 2),
        },
        "counts": {k: len(v) for k, v in splits.items()},
        "data_source": args.source,
        "interaction_tuples": len(interaction_tuples),
        "leakage_check": "passed",
    }
    manifest_key = f"{prefix}/manifest.json"
    print(f"Uploading s3://{BUCKET}/{manifest_key}...")
    s3.put_object(
        Bucket=BUCKET,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
    )
    print("Done.")


if __name__ == "__main__":
    main()
