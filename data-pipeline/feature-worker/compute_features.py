"""
Q2.5 feature-worker
Load CLIP (openai/clip-vit-base-patch32) on CPU, compute 512-d image embeddings
for the first 50 images (from flickr30k-images.zip), and upload to Swift.

Dataset files on HuggingFace (ar10067/flickr30k-images-CFQ):
  unified_dataset.json  - metadata with image_id, queries, split
  flickr30k-images.zip  - all JPEG images named by image_id
"""

import json
import os
import io
import zipfile
from datetime import datetime, timezone

import boto3
from botocore.client import Config
from huggingface_hub import hf_hub_download
from PIL import Image
import torch
from transformers import CLIPProcessor, CLIPModel
from version import resolve_version

BUCKET = "ObjStore_proj24"
S3_ENDPOINT = "https://chi.tacc.chameleoncloud.org:7480"
HF_REPO = "ar10067/flickr30k-images-CFQ"
CLIP_MODEL_NAME = "openai/clip-vit-base-patch32"
NUM_IMAGES = 50


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3"),
    )


def main():
    version, commit_sha = resolve_version()
    prefix = f"embeddings/{version}"
    raw_prefix = f"raw/{version}"
    print(f"Dataset version: {version} → s3://{BUCKET}/{prefix}/")

    print(f"Downloading unified_dataset.json from {HF_REPO}...")
    meta_path = hf_hub_download(
        repo_id=HF_REPO,
        filename="unified_dataset.json",
        repo_type="dataset",
        token=os.environ.get("HF_TOKEN"),
    )
    with open(meta_path) as f:
        records = json.load(f)
    print(f"Loaded {len(records):,} records. Processing first {NUM_IMAGES}.")

    print(f"Downloading flickr30k-images.zip from {HF_REPO}...")
    zip_path = hf_hub_download(
        repo_id=HF_REPO,
        filename="flickr30k-images.zip",
        repo_type="dataset",
        token=os.environ.get("HF_TOKEN"),
    )
    print(f"Downloaded zip to {zip_path}")

    print(f"Loading CLIP model {CLIP_MODEL_NAME} on CPU...")
    model = CLIPModel.from_pretrained(CLIP_MODEL_NAME)
    processor = CLIPProcessor.from_pretrained(CLIP_MODEL_NAME)
    model.eval()
    print("Model loaded.")

    embeddings = []
    subset = records[:NUM_IMAGES]

    with zipfile.ZipFile(zip_path, "r") as zf:
        # Build a set of names inside the zip for fast lookup
        zip_names = set(zf.namelist())

        for i, record in enumerate(subset):
            image_id = record["image_id"]

            # Try to find the image inside the zip (may be at root or in a subdirectory)
            candidates = [
                image_id,
                f"flickr30k-images/{image_id}",
                f"images/{image_id}",
            ]
            img_entry = next((c for c in candidates if c in zip_names), None)

            if img_entry:
                with zf.open(img_entry) as img_file:
                    img = Image.open(io.BytesIO(img_file.read())).convert("RGB")
            else:
                # Fallback: blank image so the pipeline doesn't crash
                print(f"  WARNING: {image_id} not found in zip, using blank image.")
                img = Image.new("RGB", (224, 224), color=(128, 128, 128))

            pixel_values = processor(images=img, return_tensors="pt")["pixel_values"]
            with torch.no_grad():
                vision_outputs = model.vision_model(pixel_values=pixel_values)
                features = model.visual_projection(vision_outputs.pooler_output)
                features = features / features.norm(dim=-1, keepdim=True)

            embedding_vec = features.squeeze(0).tolist()
            assert len(embedding_vec) == 512, f"Expected 512-d, got {len(embedding_vec)}"

            embeddings.append({"image_id": image_id, "embedding": embedding_vec})

            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{NUM_IMAGES} images processed.")

    print(f"Computed {len(embeddings)} embeddings.")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "source_commit_sha": commit_sha,
        "model": CLIP_MODEL_NAME,
        "embedding_dim": 512,
        "num_embeddings": len(embeddings),
        "source": HF_REPO,
        "derived_from": f"s3://{BUCKET}/{raw_prefix}/",
    }

    s3 = get_s3_client()

    embeddings_key = f"{prefix}/embeddings.json"
    print(f"Uploading s3://{BUCKET}/{embeddings_key}...")
    s3.put_object(
        Bucket=BUCKET,
        Key=embeddings_key,
        Body=json.dumps(embeddings).encode("utf-8"),
    )

    manifest_key = f"{prefix}/manifest.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
    )
    print("Done.")


if __name__ == "__main__":
    main()
