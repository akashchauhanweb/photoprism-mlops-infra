"""
Q2.4 data-generator
Simulate user search traffic using queries from the dataset.
Generates (query, ranked_ids, clicked_id, timestamp) interaction events
for 120 seconds at ~5-20 RPS (Poisson), writing each event to PostgreSQL
(search_queries, search_results, feedback_events tables).
Also exports an interactions_snapshot.jsonl to Swift for batch compiler use.
"""

import json
import os
import io
import random
import string
import time
import uuid
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

def random_run_id(n=8) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))


DURATION_SECONDS = 120
TARGET_RPS = 10          # mean of Poisson distribution
RANKED_LIST_SIZE = 10


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


def build_query_pool(records):
    """Build pool from test split only to avoid training data contamination."""
    pool = []
    for record in records:
        if record.get("split") != "test":
            continue
        image_id = record["image_id"]
        queries = record.get("queries", {})

        for q in queries.get("raw", []):
            if q and q.strip():
                pool.append({"query": q.strip(), "image_id": image_id, "query_type": "raw"})

        tags = queries.get("tags", [])
        if tags:
            tag_query = ", ".join(t.strip() for t in tags if t.strip())
            if tag_query:
                pool.append({"query": tag_query, "image_id": image_id, "query_type": "tags"})

        caption = queries.get("caption", "")
        if caption and caption.strip():
            pool.append({"query": caption.strip(), "image_id": image_id, "query_type": "caption"})

    return pool


def simulate_ranked_list(positive_id, all_ids, k=RANKED_LIST_SIZE):
    candidates = random.sample([i for i in all_ids if i != positive_id], min(k - 1, len(all_ids) - 1))
    ranked = candidates + [positive_id]
    random.shuffle(ranked)
    return ranked


def click_weights(n):
    """Position-biased click: p=0.6 at rank 1, ~0.35 at rank 2, decaying."""
    base = 0.6
    return [base * (0.58 ** i) for i in range(n)]


def write_event_to_pg(cur, event):
    """Insert one search event into the three interaction tables."""
    query_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO search_queries (query_id, query_text, query_type, timestamp) VALUES (%s, %s, %s, %s)",
        (query_id, event["query"], event["query_type"], event["timestamp"]),
    )
    psycopg2.extras.execute_batch(
        cur,
        "INSERT INTO search_results (query_id, image_id, rank, score) VALUES (%s, %s, %s, %s)",
        [(query_id, img_id, rank + 1, None) for rank, img_id in enumerate(event["ranked_ids"])],
    )
    clicked_rank = event["ranked_ids"].index(event["clicked_id"]) + 1
    cur.execute(
        "INSERT INTO feedback_events (query_id, image_id, event_type, rank, timestamp) VALUES (%s, %s, %s, %s, %s)",
        (query_id, event["clicked_id"], "click", clicked_rank, event["timestamp"]),
    )
    return query_id


def main():
    dataset_version, commit_sha = resolve_version()
    run_id = random_run_id()
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

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
    query_pool = build_query_pool(records)
    print(f"Query pool size (test split only): {len(query_pool):,}")

    conn = get_pg_conn()
    events = []

    print(f"Simulating traffic for {DURATION_SECONDS}s at ~{TARGET_RPS} RPS (Poisson)...")
    start_time = time.monotonic()
    deadline = start_time + DURATION_SECONDS

    try:
        with conn.cursor() as cur:
            while time.monotonic() < deadline:
                tick_start = time.monotonic()

                entry = random.choice(query_pool)
                ranked_ids = simulate_ranked_list(entry["image_id"], all_image_ids)
                weights = click_weights(len(ranked_ids))
                clicked_id = random.choices(ranked_ids, weights=weights, k=1)[0]
                ts = datetime.now(timezone.utc)

                event = {
                    "query": entry["query"],
                    "query_type": entry["query_type"],
                    "ranked_ids": ranked_ids,
                    "clicked_id": clicked_id,
                    "timestamp": ts,
                }
                write_event_to_pg(cur, event)
                events.append({**event, "timestamp": ts.isoformat()})

                # Poisson inter-arrival: sleep exponential(1/TARGET_RPS)
                elapsed = time.monotonic() - tick_start
                inter_arrival = random.expovariate(TARGET_RPS)
                sleep_time = inter_arrival - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        conn.commit()
    finally:
        conn.close()

    print(f"Generated {len(events):,} interaction events → PostgreSQL.")

    # Export snapshot to Swift for batch compiler
    buf = io.BytesIO()
    for event in events:
        buf.write((json.dumps(event) + "\n").encode("utf-8"))
    buf.seek(0)

    s3 = get_s3_client()
    snapshot_key = f"interactions/{run_id}/{run_ts}/interactions_snapshot.jsonl"
    print(f"Uploading snapshot s3://{BUCKET}/{snapshot_key}...")
    s3.put_object(Bucket=BUCKET, Key=snapshot_key, Body=buf.read())

    meta = {
        "run_id": run_id,
        "run_timestamp": run_ts,
        "dataset_version": dataset_version,
        "source_commit_sha": commit_sha,
        "duration_seconds": DURATION_SECONDS,
        "target_rps": TARGET_RPS,
        "rps_distribution": "poisson",
        "query_pool": "test_split_only",
        "event_count": len(events),
        "snapshot_key": snapshot_key,
    }
    meta_key = f"interactions/{run_id}/{run_ts}/meta.json"
    s3.put_object(Bucket=BUCKET, Key=meta_key, Body=json.dumps(meta, indent=2).encode("utf-8"))
    print("Done.")


if __name__ == "__main__":
    main()
