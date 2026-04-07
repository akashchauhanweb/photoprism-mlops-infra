"""
Bulk-submit feature computation jobs for N images from image_metadata.

Usage:
    python batch_submit.py --count 100

Reads image_id and dataset_version from PostgreSQL image_metadata,
constructs the S3 key, and inserts all jobs in a single transaction.
The online-feature-worker daemon will process them asynchronously.
"""

import argparse
import os

import psycopg2
import psycopg2.extras


def get_pg_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", 5432)),
        dbname=os.environ.get("POSTGRES_DB", "mlops"),
        user=os.environ.get("POSTGRES_USER", "mlops"),
        password=os.environ.get("POSTGRES_PASSWORD", "mlops"),
    )


def main():
    parser = argparse.ArgumentParser(description="Bulk-submit feature jobs.")
    parser.add_argument("--count", type=int, default=100, help="Number of images to submit (default: 100)")
    args = parser.parse_args()

    conn = get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Fetch N images from metadata, skip any already queued
            cur.execute("""
                SELECT m.image_id, m.dataset_version
                FROM image_metadata m
                WHERE NOT EXISTS (
                    SELECT 1 FROM feature_jobs f WHERE f.image_id = m.image_id
                )
                LIMIT %s
            """, (args.count,))
            rows = cur.fetchall()

            if not rows:
                print("No new images to submit — all already queued.")
                return

            jobs = [
                (row["image_id"], f"raw/{row['dataset_version']}/images/{row['image_id']}")
                for row in rows
            ]

            psycopg2.extras.execute_batch(
                cur,
                "INSERT INTO feature_jobs (image_id, s3_key) VALUES (%s, %s)",
                jobs,
            )

        conn.commit()
        print(f"Submitted {len(jobs)} jobs to feature_jobs queue.")
        for image_id, s3_key in jobs[:5]:
            print(f"  {image_id} → {s3_key}")
        if len(jobs) > 5:
            print(f"  ... and {len(jobs) - 5} more.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
