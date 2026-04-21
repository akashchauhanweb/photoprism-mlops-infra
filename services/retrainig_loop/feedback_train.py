"""
feedback_train.py — Feedback-based fine-tuning API for DevOps
Triggered by DevOps on schedule (e.g. every 48 hours)
Pulls feedback data from:
  - PostgreSQL: query_id, query_text, image_id, clicked
  - Qdrant: image embeddings + image data by image_id
Fine-tunes Qwen2-VL reranker with LoRA
Saves new model weights to MLflow
"""

import logging
import os
import re
import shutil
import subprocess
import time
import threading
from typing import Optional

import mlflow
import torch
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="PhotoPrism Feedback Training API")

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
DOCKER_HUB_USER   = os.environ.get("DOCKER_HUB_USER", "")
DOCKER_HUB_PAT    = os.environ.get("DOCKER_HUB_PAT", "")
RERANKER_API_SRC  = os.environ.get("RERANKER_API_SRC", "/reranker-api")
CONFIG_ENV_PATH   = os.environ.get("CONFIG_ENV_PATH", "/scripts/config.env")
SCRIPTS_DIR       = os.environ.get("SCRIPTS_DIR", "/scripts")

MLFLOW_TRACKING_URI  = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow:5000")
MLFLOW_EXPERIMENT    = os.environ.get("MLFLOW_EXPERIMENT", "photoprism-semantic-search")
POSTGRES_URI         = os.environ.get("POSTGRES_URI", "postgresql://user:password@postgres:5432/photoprism")
QDRANT_URL           = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION    = os.environ.get("QDRANT_COLLECTION", "image_embeddings")
MODEL_NAME           = os.environ.get("MODEL_NAME", "Qwen/Qwen2-VL-2B-Instruct")
LORA_WEIGHTS_PATH    = os.environ.get("LORA_WEIGHTS_PATH", "/tmp/qwen_lora_weights")
MIN_FEEDBACK_SAMPLES = int(os.environ.get("MIN_FEEDBACK_SAMPLES", "100"))
LORA_R               = int(os.environ.get("LORA_R", "8"))
LORA_ALPHA           = int(os.environ.get("LORA_ALPHA", "16"))
LEARNING_RATE        = float(os.environ.get("LEARNING_RATE", "2e-4"))
EPOCHS               = int(os.environ.get("EPOCHS", "1"))

mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)

# Global training state
training_status = {
    "is_training": False,
    "last_trained": None,
    "status": "idle",
    "run_id": None,
    "avg_loss": None
}


# ─────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────

class TrainRequest(BaseModel):
    min_samples: Optional[int] = None
    run_name: Optional[str] = None
    epochs: Optional[int] = None
    learning_rate: Optional[float] = None
    lora_r: Optional[int] = None

class TrainResponse(BaseModel):
    status: str
    message: str
    run_id: Optional[str] = None


# ─────────────────────────────────────────────
# Step 1: Fetch feedback from PostgreSQL
# ─────────────────────────────────────────────

def fetch_feedback(min_samples: int):
    """
    Fetch clicked/not-clicked feedback from PostgreSQL.

    Table schema:
    CREATE TABLE feedback (
        query_id    SERIAL PRIMARY KEY,
        query_text  TEXT NOT NULL,
        image_id    TEXT NOT NULL,
        clicked     BOOLEAN NOT NULL,
        timestamp   TIMESTAMP DEFAULT NOW()
    );

    Returns list of (query_text, image_id, label) tuples.
    label = 1.0 if clicked, 0.0 if not clicked.
    """
    try:
        import psycopg2
        log.info(f"Connecting to PostgreSQL: {POSTGRES_URI}")
        conn = psycopg2.connect(POSTGRES_URI)
        cur = conn.cursor()

        # Fetch recent feedback — last 48 hours, joined from actual schema
        cur.execute("""
            SELECT q.query_text, r.image_id, r.clicked
            FROM search_results r
            JOIN search_queries q ON r.query_id = q.query_id
            WHERE q.timestamp > NOW() - INTERVAL '48 hours'
            ORDER BY q.timestamp DESC
            LIMIT %s
        """, (min_samples * 5,))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        log.info(f"Fetched {len(rows)} feedback rows from PostgreSQL")

        if len(rows) < min_samples:
            log.warning(f"Only {len(rows)} feedback rows — need {min_samples} to train")
            return None

        # Convert to training pairs
        pairs = [(row[0], row[1], 1.0 if row[2] == 1 else 0.0) for row in rows]
        positives = sum(1 for _, _, l in pairs if l == 1.0)
        negatives = sum(1 for _, _, l in pairs if l == 0.0)
        log.info(f"Training pairs: {len(pairs)} ({positives} positive, {negatives} negative)")
        return pairs

    except Exception as e:
        log.error(f"PostgreSQL error: {e}")
        raise HTTPException(status_code=500, detail=f"PostgreSQL error: {e}")


# ─────────────────────────────────────────────
# Step 2: Fetch images from Qdrant
# ─────────────────────────────────────────────

def fetch_images(image_ids):
    """
    Fetch images from Qdrant by image_id.
    Images stored in Qdrant payload as base64 or URL.
    Returns dict: {image_id: PIL Image}
    """
    try:
        import base64
        import requests as req
        from io import BytesIO
        from PIL import Image
        from qdrant_client import QdrantClient
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue

        log.info(f"Connecting to Qdrant: {QDRANT_URL}")
        client = QdrantClient(url=QDRANT_URL)
        image_lookup = {}

        for image_id in image_ids:
            try:
                results, _ = client.scroll(
                    collection_name=QDRANT_COLLECTION,
                    scroll_filter=Filter(
                        must=[FieldCondition(
                            key="image_id",
                            match=MatchValue(value=image_id)
                        )]
                    ),
                    limit=1,
                    with_payload=True,
                    with_vectors=False
                )

                if not results:
                    log.warning(f"Image {image_id} not found in Qdrant")
                    continue

                payload = results[0].payload

                # Try base64 first, then URL
                if "image_base64" in payload:
                    img_data = base64.b64decode(payload["image_base64"])
                    image = Image.open(BytesIO(img_data)).convert("RGB")
                    image_lookup[image_id] = image

                elif "image_url" in payload:
                    resp = req.get(payload["image_url"], timeout=10)
                    image = Image.open(BytesIO(resp.content)).convert("RGB")
                    image_lookup[image_id] = image

                elif "image_path" in payload:
                    image = Image.open(payload["image_path"]).convert("RGB")
                    image_lookup[image_id] = image

                else:
                    log.warning(f"No image data found for {image_id} in Qdrant payload")

            except Exception as e:
                log.warning(f"Could not load image {image_id} from Qdrant: {e}")
                continue

        log.info(f"Loaded {len(image_lookup)}/{len(image_ids)} images from Qdrant")
        return image_lookup

    except Exception as e:
        log.error(f"Qdrant error: {e}")
        raise HTTPException(status_code=500, detail=f"Qdrant error: {e}")


# ─────────────────────────────────────────────
# Post-training helpers: tag bump, build, push, redeploy
# ─────────────────────────────────────────────

def _bump_patch(tag: str) -> str:
    parts = tag.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _read_makefile_tag(makefile: str) -> str:
    m = re.search(r"^TAG\s*\?=\s*(\S+)", open(makefile).read(), re.MULTILINE)
    if not m:
        raise RuntimeError("TAG not found in Makefile")
    return m.group(1)


def _update_makefile_tag(makefile: str, new_tag: str):
    text = open(makefile).read()
    text = re.sub(r"^(TAG\s*\?=\s*)\S+", rf"\g<1>{new_tag}", text, flags=re.MULTILINE)
    open(makefile, "w").write(text)


def _update_config_env_tag(config_env: str, new_tag: str):
    text = open(config_env).read()
    text = re.sub(r'^(RERANKER_API_TAG=).*', rf'\g<1>"{new_tag}"', text, flags=re.MULTILINE)
    open(config_env, "w").write(text)


def _docker_build_push(image: str, tag: str, src_dir: str):
    if not DOCKER_HUB_USER or not DOCKER_HUB_PAT:
        raise RuntimeError("DOCKER_HUB_USER / DOCKER_HUB_PAT not set in env")
    result = subprocess.run(
        ["docker", "login", "-u", DOCKER_HUB_USER, "--password-stdin"],
        input=DOCKER_HUB_PAT, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker login failed: {result.stderr}")
    full_tag = f"{image}:{tag}"
    subprocess.run(["docker", "build", "--network", "host", "-t", full_tag, src_dir], check=True)
    subprocess.run(["docker", "push", full_tag], check=True)
    log.info(f"Pushed {full_tag}")


def _redeploy_via_script():
    reranker_script = os.path.join(SCRIPTS_DIR, "lib", "70_reranker.sh")
    if not os.path.isfile(reranker_script):
        log.warning(f"70_reranker.sh not found at {reranker_script} — skipping redeploy")
        return
    # Source config.env so the script picks up the updated RERANKER_API_TAG
    cmd = f"set -a && source {CONFIG_ENV_PATH} && set +a && bash {reranker_script}"
    subprocess.run(cmd, shell=True, check=True, executable="/bin/bash")
    log.info("70_reranker.sh completed — reranker redeployed")


# ─────────────────────────────────────────────
# Step 3: Fine-tune Qwen2-VL with LoRA
# ─────────────────────────────────────────────

def run_finetuning(pairs, image_lookup, config: dict):
    """
    Fine-tune Qwen2-VL reranker on feedback data.
    Saves LoRA weights to MLflow.
    """
    global training_status

    from transformers import Qwen2VLForConditionalGeneration, AutoProcessor
    from peft import get_peft_model, LoraConfig, TaskType
    from torch.optim import AdamW
    from qwen_vl_utils import process_vision_info

    training_status["is_training"] = True
    training_status["status"] = "training"

    try:
        mlflow.set_experiment(MLFLOW_EXPERIMENT)

        with mlflow.start_run(run_name=config.get("run_name", f"feedback-finetune-{int(time.time())}")) as run:

            # Log all config
            mlflow.log_param("training_type", "feedback_finetuning")
            mlflow.log_param("num_pairs", len(pairs))
            mlflow.log_param("num_images", len(image_lookup))
            mlflow.log_param("model_name", MODEL_NAME)
            mlflow.log_param("lora_r", config["lora_r"])
            mlflow.log_param("lora_alpha", config["lora_r"] * 2)
            mlflow.log_param("learning_rate", config["lr"])
            mlflow.log_param("epochs", config["epochs"])
            mlflow.log_param("min_feedback_samples", config["min_samples"])
            mlflow.log_param("gpu_name", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu")
            mlflow.log_param("gpu_count", torch.cuda.device_count())

            device = "cuda" if torch.cuda.is_available() else "cpu"
            log.info(f"Training on device: {device}")

            # Load base model
            log.info(f"Loading {MODEL_NAME}...")
            base_model = Qwen2VLForConditionalGeneration.from_pretrained(
                MODEL_NAME,
                torch_dtype=torch.bfloat16,
                device_map="auto"
            )

            # Apply LoRA
            lora_config = LoraConfig(
                task_type=TaskType.CAUSAL_LM,
                r=config["lora_r"],
                lora_alpha=config["lora_r"] * 2,
                lora_dropout=0.05,
                target_modules=["q_proj", "v_proj", "k_proj", "o_proj"],
                bias="none"
            )
            model = get_peft_model(base_model, lora_config)
            trainable, total = model.get_nb_trainable_parameters()
            log.info(f"Trainable params: {trainable:,} / {total:,} ({100*trainable/total:.3f}%)")
            mlflow.log_param("trainable_params", trainable)

            # Load processor
            processor = AutoProcessor.from_pretrained(
                MODEL_NAME,
                min_pixels=256*28*28,
                max_pixels=512*28*28
            )

            optimizer = AdamW(model.parameters(), lr=config["lr"])
            model.train()

            # Training loop
            t0 = time.time()
            for epoch in range(config["epochs"]):
                epoch_t0 = time.time()
                total_loss = 0
                step = 0
                skipped = 0

                for query_text, image_id, label in pairs:
                    if image_id not in image_lookup:
                        skipped += 1
                        continue

                    image = image_lookup[image_id]

                    # Format input
                    messages = [{"role": "user", "content": [
                        {"type": "image", "image": image},
                        {"type": "text", "text": (
                            f"Does this image match the description: '{query_text}'?\n"
                            f"Answer with a relevance score between 0 and 1."
                        )}
                    ]}]

                    text = processor.apply_chat_template(
                        messages, tokenize=False, add_generation_prompt=True)
                    image_inputs, video_inputs = process_vision_info(messages)
                    inputs = processor(
                        text=[text], images=image_inputs, videos=video_inputs,
                        padding=True, truncation=True,
                        max_length=512, return_tensors="pt"
                    )
                    inputs = {k: v.to(device) for k, v in inputs.items()}
                    label_t = torch.tensor(label, dtype=torch.float32).to(device)

                    # Forward + backward
                    optimizer.zero_grad()
                    with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                        outputs = model(**inputs)
                        logits = outputs.logits[:, -1, :].mean(dim=-1)
                        loss = torch.nn.functional.binary_cross_entropy_with_logits(
                            logits, label_t.unsqueeze(0)
                        )

                    loss.backward()
                    optimizer.step()

                    total_loss += loss.item()
                    step += 1

                    # Log every 10 steps
                    if step % 10 == 0:
                        mlflow.log_metric("train_loss", loss.item(),
                                          step=epoch * len(pairs) + step)
                        log.info(f"Epoch {epoch+1} Step {step}/{len(pairs)-skipped} "
                                 f"Loss: {loss.item():.4f}")

                # Epoch metrics
                epoch_time = time.time() - epoch_t0
                avg_loss = total_loss / max(step, 1)
                mlflow.log_metric("epoch_loss", avg_loss, step=epoch)
                mlflow.log_metric("epoch_time_sec", epoch_time, step=epoch)
                log.info(f"Epoch {epoch+1} complete — avg loss: {avg_loss:.4f}, "
                         f"time: {epoch_time/60:.1f} mins, skipped: {skipped}")

            # Total training metrics
            train_time = time.time() - t0
            gpu_count = torch.cuda.device_count()
            est_cost = (train_time / 3600) * 3.0 * gpu_count
            mlflow.log_metric("total_train_time_sec", train_time)
            mlflow.log_metric("total_train_time_min", train_time / 60)
            mlflow.log_metric("estimated_gpu_cost_usd", est_cost)

            # Save LoRA weights to disk + MLflow
            log.info(f"Saving LoRA weights to {LORA_WEIGHTS_PATH}...")
            model.save_pretrained(LORA_WEIGHTS_PATH)
            mlflow.log_artifact(LORA_WEIGHTS_PATH, artifact_path="qwen_lora_weights")
            log.info(f"Model saved to MLflow run {run.info.run_id}")

            # Post-training: rebuild reranker-api image + push + redeploy
            if DOCKER_HUB_USER and os.path.isdir(RERANKER_API_SRC):
                makefile_path = os.path.join(RERANKER_API_SRC, "Makefile")
                weights_dest  = os.path.join(RERANKER_API_SRC, "qwen_lora_weights")

                shutil.copytree(LORA_WEIGHTS_PATH, weights_dest, dirs_exist_ok=True)
                log.info(f"Copied LoRA weights → {weights_dest}")

                old_tag = _read_makefile_tag(makefile_path)
                new_tag = _bump_patch(old_tag)
                _update_makefile_tag(makefile_path, new_tag)
                if os.path.isfile(CONFIG_ENV_PATH):
                    _update_config_env_tag(CONFIG_ENV_PATH, new_tag)
                mlflow.log_param("reranker_image_tag", new_tag)
                log.info(f"Tag bumped: {old_tag} → {new_tag}")

                image = f"{DOCKER_HUB_USER}/reranker-api"
                _docker_build_push(image, new_tag, RERANKER_API_SRC)
                _redeploy_via_script()
            else:
                log.info("DOCKER_HUB_USER or RERANKER_API_SRC not set — skipping image rebuild")

            training_status = {
                "is_training": False,
                "last_trained": time.time(),
                "status": "completed",
                "run_id": run.info.run_id,
                "avg_loss": avg_loss
            }

            return run.info.run_id

    except Exception as e:
        log.error(f"Fine-tuning failed: {e}")
        training_status = {
            "is_training": False,
            "last_trained": None,
            "status": f"failed: {str(e)}",
            "run_id": None,
            "avg_loss": None
        }
        raise


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "training_status": training_status,
        "mlflow_uri": MLFLOW_TRACKING_URI,
        "postgres_uri": POSTGRES_URI,
        "qdrant_url": QDRANT_URL
    }


@app.get("/training/status")
def get_training_status():
    """Check if training is running — for DevOps monitoring."""
    return training_status


@app.post("/train", response_model=TrainResponse)
def trigger_training(request: TrainRequest, background_tasks: BackgroundTasks):
    """
    Trigger feedback-based fine-tuning.
    Called by DevOps on schedule (e.g. every 48 hours).

    Steps:
    1. Fetch feedback from PostgreSQL
    2. Fetch images from Qdrant
    3. Fine-tune Qwen2-VL with LoRA in background
    4. Save weights to MLflow
    """
    # Check if already training
    if training_status["is_training"]:
        raise HTTPException(
            status_code=409,
            detail="Training already in progress. Check /training/status"
        )

    # Build config from request + env defaults
    config = {
        "min_samples": request.min_samples or MIN_FEEDBACK_SAMPLES,
        "run_name":    request.run_name or f"feedback-finetune-{int(time.time())}",
        "epochs":      request.epochs or EPOCHS,
        "lr":          request.learning_rate or LEARNING_RATE,
        "lora_r":      request.lora_r or LORA_R,
    }

    # Step 1: Fetch feedback from PostgreSQL
    log.info(f"Fetching feedback (min {config['min_samples']} samples)...")
    pairs = fetch_feedback(config["min_samples"])

    if pairs is None:
        return TrainResponse(
            status="skipped",
            message=f"Not enough feedback data (need {config['min_samples']} samples). Training skipped."
        )

    # Step 2: Fetch images from Qdrant
    image_ids = list(set(p[1] for p in pairs))
    log.info(f"Fetching {len(image_ids)} unique images from Qdrant...")
    image_lookup = fetch_images(image_ids)

    if len(image_lookup) == 0:
        return TrainResponse(
            status="failed",
            message="Could not load any images from Qdrant"
        )

    # Step 3: Run fine-tuning in background
    def background_train():
        try:
            run_finetuning(pairs, image_lookup, config)
        except Exception as e:
            log.error(f"Background training failed: {e}")

    background_tasks.add_task(background_train)

    return TrainResponse(
        status="started",
        message=f"Fine-tuning started — {len(pairs)} feedback pairs, {len(image_lookup)} images. Check /training/status for progress."
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
