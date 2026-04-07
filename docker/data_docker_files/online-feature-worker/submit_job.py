"""
Submit a feature computation job to the PostgreSQL job queue.

Usage:
    python submit_job.py --image_id 2513260012.jpg --s3_key raw/v1/images/2513260012.jpg

The online-feature-worker daemon will pick it up, compute the CLIP embedding,
and upsert it into Qdrant.
"""

import argparse
import os

import psycopg2


def get_pg_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", 5432)),
        dbname=os.environ.get("POSTGRES_DB", "mlops"),
        user=os.environ.get("POSTGRES_USER", "mlops"),
        password=os.environ.get("POSTGRES_PASSWORD", "mlops"),
    )


def main():
    parser = argparse.ArgumentParser(description="Submit a feature computation job.")
    parser.add_argument("--image_id", required=True, help="Image filename e.g. 2513260012.jpg")
    parser.add_argument("--s3_key", required=True, help="S3 key e.g. raw/v1/images/2513260012.jpg")
    args = parser.parse_args()

    conn = get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feature_jobs (image_id, s3_key) VALUES (%s, %s) RETURNING job_id",
                (args.image_id, args.s3_key),
            )
            job_id = cur.fetchone()[0]
        conn.commit()
        print(f"Submitted job {job_id}: {args.image_id} ({args.s3_key})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
