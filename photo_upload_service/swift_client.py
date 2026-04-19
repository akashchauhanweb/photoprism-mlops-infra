"""
Chameleon Cloud S3-compatible object storage client.

Credentials come from EC2 credentials downloaded from the Chameleon Horizon
dashboard (Project → API Access → Download EC2 Credentials), sourced as:
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_ENDPOINT_URL, S3_BUCKET_NAME
"""

import os
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

S3_ENDPOINT = os.getenv("S3_ENDPOINT_URL", "https://chi.tacc.chameleoncloud.org:7480")
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "ObjStore_proj24")


def _client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3"),
    )


def upload_bytes(data: bytes, filename: str, content_type: str = "image/jpeg") -> str:
    """Upload raw bytes to Chameleon S3 and return the public object URL."""
    key = f"photos/{uuid.uuid4().hex}_{filename}"
    try:
        _client().put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=data,
            ContentType=content_type,
            ACL="public-read",
        )
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"S3 upload failed: {exc}") from exc
    return f"{S3_ENDPOINT}/{S3_BUCKET}/{key}"
