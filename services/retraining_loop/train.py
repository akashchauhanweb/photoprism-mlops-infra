"""
train.py — Semantic photo retrieval training script
Models:
  - clip_zeroshot: CLIP zero-shot baseline (no training)
  - qwen_reranker: CLIP retrieval + Qwen2-VL-2B reranker with LoRA
Config via: --config config_baseline.yaml or --config config_qwen.yaml
"""

import argparse
import json
import logging
import os
import platform
import subprocess
import time
import zipfile
from pathlib import Path

import mlflow
import numpy as np
from huggingface_hub import HfApi
import torch
import yaml
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Dataset
# ─────────────────────────────────────────────

class Flickr30KDataset(Dataset):
    def __init__(self, hf_repo, preprocess=None, max_samples=None,
                 split="train", query_type="raw", cache_dir="/tmp/flickr30k"):
        from huggingface_hub import hf_hub_download
        self.preprocess = preprocess
        self.query_type = query_type
        self.records = []
        self.images = []

        cache_dir = Path(cache_dir)
        image_dir = cache_dir / "images"

        # Step 1: Download and extract zip if needed
        if not image_dir.exists() or not any(image_dir.glob("*.jpg")):
            log.info(f"Downloading flickr30k-images.zip from {hf_repo}...")
            zip_path = hf_hub_download(
                repo_id=hf_repo,
                filename="flickr30k-images.zip",
                repo_type="dataset",
                local_dir=str(cache_dir)
            )
            log.info(f"Extracting zip to {image_dir}...")
            image_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(str(image_dir))
            log.info("Extraction complete!")
        else:
            log.info(f"Images already extracted at {image_dir}")

        # Step 2: Download unified_dataset.json
        log.info(f"Downloading unified_dataset.json from {hf_repo}...")
        json_path = hf_hub_download(
            repo_id=hf_repo,
            filename="unified_dataset.json",
            repo_type="dataset",
            local_dir=str(cache_dir)
        )
        with open(json_path) as f:
            data = json.load(f)

        # Step 3: Filter by split and load images
        all_train = [r for r in data if r.get("split") == "train"]
        all_test = [r for r in data if r.get("split") == "test"]

        # Create val split from train (last 20%)
        import random
        random.seed(42)
        random.shuffle(all_train)
        val_cutoff = int(len(all_train) * 0.8)

        if split == "train":
            split_data = all_train[:val_cutoff]
        elif split == "val":
            split_data = all_train[val_cutoff:]
        elif split == "test":
            split_data = all_test
        else:
            split_data = all_train

        log.info(f"Split sizes — train: {val_cutoff}, val: {len(all_train)-val_cutoff}, test: {len(all_test)}")

        if max_samples:
            split_data = split_data[:max_samples]

        log.info(f"Loading {len(split_data)} images from disk...")
        for rec in tqdm(split_data, desc="Loading images"):
            # Try direct path first, then nested
            img_path = image_dir / rec["image_id"]
            if not img_path.exists():
                # Try inside a subfolder (in case zip extracted to subfolder)
                matches = list(image_dir.rglob(rec["image_id"]))
                if matches:
                    img_path = matches[0]
                else:
                    log.warning(f"Image not found: {rec['image_id']}")
                    continue
            try:
                image = Image.open(img_path).convert("RGB")
                self.records.append(rec)
                self.images.append(image)
            except Exception as e:
                log.warning(f"Could not load {rec['image_id']}: {e}")

        log.info(f"Loaded {len(self.records)} images")

    def __len__(self):
        return len(self.records)

    def get_query(self, rec):
        queries = rec.get("queries", {})
        if self.query_type == "tags":
            tags = queries.get("tags", [])
            return " ".join(tags) if tags else queries.get("raw", [""])[0]
        elif self.query_type == "caption":
            return queries.get("caption", queries.get("raw", [""])[0])
        return queries.get("raw", [""])[0]

    def __getitem__(self, idx):
        image = self.images[idx]
        if self.preprocess:
            image = self.preprocess(image)
        return image, self.get_query(self.records[idx]), self.records[idx]["image_id"]


# ─────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────

def recall_at_k(sim_matrix, k):
    n = sim_matrix.shape[0]
    top_k = np.argsort(-sim_matrix, axis=1)[:, :k]
    return sum(1 for i in range(n) if i in top_k[i]) / n


def ndcg_at_k(sim_matrix, k):
    n = sim_matrix.shape[0]
    top_k = np.argsort(-sim_matrix, axis=1)[:, :k]
    scores = []
    for i in range(n):
        dcg = sum(1.0 / np.log2(rank + 2)
                  for rank, j in enumerate(top_k[i]) if j == i)
        scores.append(dcg)
    return float(np.mean(scores))


# ─────────────────────────────────────────────
# CLIP
# ─────────────────────────────────────────────

def load_clip_model(model_name, device):
    import clip
    model, preprocess = clip.load(model_name, device=device)
    model.eval()
    return model, preprocess, clip


@torch.no_grad()
def evaluate_clip_retrieval(model, dataloader, device, clip_module, k_values=(1, 5, 10)):
    image_embeddings, text_embeddings = [], []
    for images, captions, _ in tqdm(dataloader, desc="CLIP encoding"):
        images = images.to(device)
        tokens = clip_module.tokenize(list(captions), truncate=True).to(device)
        img_emb = model.encode_image(images)
        txt_emb = model.encode_text(tokens)
        img_emb = img_emb / img_emb.norm(dim=-1, keepdim=True)
        txt_emb = txt_emb / txt_emb.norm(dim=-1, keepdim=True)
        image_embeddings.append(img_emb.cpu().float().numpy())
        text_embeddings.append(txt_emb.cpu().float().numpy())
    image_embeddings = np.concatenate(image_embeddings)
    text_embeddings = np.concatenate(text_embeddings)
    sim_matrix = text_embeddings @ image_embeddings.T
    metrics = {}
    for k in k_values:
        metrics[f"clip_recall_at_{k}"] = recall_at_k(sim_matrix, k)
        metrics[f"clip_ndcg_at_{k}"] = ndcg_at_k(sim_matrix, k)
    return metrics, image_embeddings, text_embeddings, sim_matrix


# ─────────────────────────────────────────────
# Qwen2-VL Reranker with LoRA
# ─────────────────────────────────────────────

def train_qwen_reranker(cfg, sim_matrix, dataset, device):
    from transformers import Qwen2VLForConditionalGeneration, AutoProcessor
    from peft import get_peft_model, LoraConfig, TaskType
    from torch.optim import AdamW
    from qwen_vl_utils import process_vision_info

    model_name = cfg["reranker"]["model_name"]
    log.info(f"Loading {model_name}...")

    base_model = Qwen2VLForConditionalGeneration.from_pretrained(
        model_name, torch_dtype=torch.bfloat16, device_map="auto")

    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=cfg["reranker"].get("lora_r", 8),
        lora_alpha=cfg["reranker"].get("lora_alpha", 16),
        lora_dropout=cfg["reranker"].get("lora_dropout", 0.05),
        target_modules=["q_proj", "v_proj", "k_proj", "o_proj"],
        bias="none"
    )
    model = get_peft_model(base_model, lora_config)
    model.print_trainable_parameters()

    processor = AutoProcessor.from_pretrained(
        model_name, min_pixels=256*28*28, max_pixels=512*28*28)

    # Build training pairs
    n = len(dataset.records)
    pairs = []
    for i in range(n):
        query = dataset.records[i]["queries"]["raw"][0]
        pairs.append((query, dataset.images[i], 1.0))
        for j in np.argsort(-sim_matrix[i]):
            if j != i:
                pairs.append((query, dataset.images[int(j)], 0.0))
                break

    log.info(f"Built {len(pairs)} training pairs")

    def make_input(query, image):
        messages = [{"role": "user", "content": [
            {"type": "image", "image": image},
            {"type": "text", "text": f"Does this image match: '{query}'? Score 0-1."}
        ]}]
        text = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
        image_inputs, video_inputs = process_vision_info(messages)
        return processor(text=[text], images=image_inputs, videos=video_inputs,
                         padding=True, truncation=True,
                         max_length=cfg["reranker"].get("max_length", 512),
                         return_tensors="pt")
    
        reranker.save_pretrained("/tmp/qwen_lora_weights")


    optimizer = AdamW(model.parameters(), lr=float(cfg["reranker"].get("lr", 2e-4)))
    epochs = cfg["reranker"].get("epochs", 2)
    batch_size = cfg["reranker"].get("batch_size", 2)
    t0 = time.time()
    model.train()

    for epoch in range(epochs):
        total_loss = 0
        epoch_t0 = time.time()
        for step in tqdm(range(0, len(pairs), batch_size),
                         desc=f"Epoch {epoch+1}/{epochs}"):
            batch_pairs = pairs[step:step+batch_size]
            optimizer.zero_grad()
            batch_loss = 0
            for query, image, label in batch_pairs:
                inputs = make_input(query, image)
                inputs = {k: v.to(device) for k, v in inputs.items()}
                label_t = torch.tensor(label, dtype=torch.float32).to(device)
                with torch.cuda.amp.autocast(dtype=torch.bfloat16):
                    outputs = model(**inputs)
                    logits = outputs.logits[:, -1, :].mean(dim=-1)
                    loss = torch.nn.functional.binary_cross_entropy_with_logits(
                        logits, label_t.unsqueeze(0))
                loss.backward()
                batch_loss += loss.item()
            optimizer.step()
            total_loss += batch_loss
            if step % 20 == 0:
                mlflow.log_metric("train_loss", batch_loss,
                                   step=epoch * len(pairs) + step)
        avg = total_loss / max(len(pairs) / batch_size, 1)
        epoch_time = time.time() - epoch_t0
        mlflow.log_metric("epoch_loss", avg, step=epoch)
        mlflow.log_metric("epoch_time_sec", epoch_time, step=epoch)
        mlflow.log_metric("epoch_samples_per_sec", len(pairs) / epoch_time, step=epoch)
        log.info(f"Epoch {epoch+1} avg loss: {avg:.4f} time: {epoch_time:.1f}s")

    return model, processor, time.time() - t0


@torch.no_grad()
def evaluate_with_reranker(model, processor, sim_matrix, dataset, device,
                            top_k=10, k_values=(1, 5, 10)):
    from qwen_vl_utils import process_vision_info
    n = len(dataset.records)
    reranked_sim = sim_matrix.copy()
    model.eval()

    for i in tqdm(range(n), desc="Reranking"):
        query = dataset.records[i]["queries"]["raw"][0]
        top_k_idx = np.argsort(-sim_matrix[i])[:top_k]
        for j in top_k_idx:
            messages = [{"role": "user", "content": [
                {"type": "image", "image": dataset.images[int(j)]},
                {"type": "text", "text": f"Does this image match: '{query}'? Score 0-1."}
            ]}]
            text = processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)
            image_inputs, _ = process_vision_info(messages)
            inputs = processor(text=[text], images=image_inputs, return_tensors="pt")
            inputs = {k: v.to(device) for k, v in inputs.items()}
            with torch.cuda.amp.autocast(dtype=torch.bfloat16):
                outputs = model(**inputs)
                score = torch.sigmoid(outputs.logits[:, -1, :].mean()).item()
            reranked_sim[i][int(j)] = score

    metrics = {}
    for k in k_values:
        metrics[f"reranked_recall_at_{k}"] = recall_at_k(reranked_sim, k)
        metrics[f"reranked_ndcg_at_{k}"] = ndcg_at_k(reranked_sim, k)
    return metrics


# ─────────────────────────────────────────────
# Environment info
# ─────────────────────────────────────────────

def get_env_info():
    info = {
        "python_version": platform.python_version(),
        "pytorch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "platform": platform.platform(),
    }
    if torch.cuda.is_available():
        info["gpu_name"] = torch.cuda.get_device_name(0)
        info["gpu_count"] = torch.cuda.device_count()
        info["cuda_version"] = torch.version.cuda
        info["vram_gb"] = round(
            torch.cuda.get_device_properties(0).total_memory / 1e9, 2)
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"],
                                capture_output=True, text=True)
        info["git_sha"] = result.stdout.strip()[:8]
    except Exception:
        info["git_sha"] = "unknown"
    return info


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="config_baseline.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info(f"Using device: {device}")

    hf_token = os.environ.get("HF_TOKEN")
    if hf_token:
        os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token  # compat for older datasets lib

    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI") or cfg["mlflow"]["tracking_uri"]
    mlflow.set_tracking_uri(tracking_uri)
    log.info(f"MLflow tracking URI: {tracking_uri}")
    mlflow.set_experiment(cfg["mlflow"]["experiment_name"])

    env_info = get_env_info()

    with mlflow.start_run(run_name=cfg["run_name"]):
        [mlflow.log_param(f"clip_{k}", v) for k, v in cfg.get("clip", {}).items()]
        if cfg.get("model") == "qwen_reranker":
            [mlflow.log_param(f"reranker_{k}", v) for k, v in cfg.get("reranker", {}).items()]
        mlflow.log_param("model_type", cfg["model"])
        mlflow.log_param("max_eval_samples", cfg.get("max_eval_samples", "all"))
        mlflow.log_param("query_type", cfg.get("query_type", "raw"))
        [mlflow.log_param(k, v) for k, v in env_info.items()]

        log.info(f"Loading CLIP: {cfg['clip']['model_name']}")
        t0 = time.time()
        clip_model, preprocess, clip_module = load_clip_model(
            cfg["clip"]["model_name"], device)
        mlflow.log_metric("clip_load_time_sec", time.time() - t0)

        # Load train split for reranker training
        train_dataset = Flickr30KDataset(
            hf_repo=cfg["data"]["hf_repo"],
            preprocess=preprocess,
            max_samples=cfg.get("max_train_samples"),
            split="train",
            query_type=cfg.get("query_type", "raw"),
            cache_dir=cfg["data"].get("cache_dir", "/tmp/flickr30k"),
        )

        # Load test split for evaluation
        test_dataset = Flickr30KDataset(
            hf_repo=cfg["data"]["hf_repo"],
            preprocess=preprocess,
            max_samples=cfg.get("max_eval_samples"),
            split="test",
            query_type=cfg.get("query_type", "raw"),
            cache_dir=cfg["data"].get("cache_dir", "/tmp/flickr30k"),
        )

        # Use test set for CLIP evaluation
        dataset = test_dataset
        dataloader = DataLoader(dataset,
                                batch_size=cfg["clip"].get("batch_size", 32),
                                shuffle=False, num_workers=0)
        mlflow.log_param("train_dataset_size", len(train_dataset))
        mlflow.log_param("eval_dataset_size", len(test_dataset))

        log.info("Evaluating CLIP zero-shot retrieval...")
        t0 = time.time()
        clip_metrics, image_embeddings, text_embeddings, sim_matrix = \
            evaluate_clip_retrieval(clip_model, dataloader, device, clip_module)
        mlflow.log_metrics(clip_metrics)
        mlflow.log_metric("clip_eval_time_sec", time.time() - t0)
        log.info(f"CLIP Metrics: {clip_metrics}")

        np.save("/tmp/image_embeddings.npy", image_embeddings)
        np.save("/tmp/text_embeddings.npy", text_embeddings)
        mlflow.log_artifact("/tmp/image_embeddings.npy")
        mlflow.log_artifact("/tmp/text_embeddings.npy")

        if cfg["model"] == "qwen_reranker":
            log.info("Fine-tuning Qwen2-VL reranker with LoRA...")
            # Train reranker on train split
            train_clip_metrics, train_img_emb, train_txt_emb, train_sim = evaluate_clip_retrieval(
                clip_model, DataLoader(train_dataset, batch_size=cfg["clip"].get("batch_size", 32),
                shuffle=False, num_workers=0), device, clip_module)
            log.info(f"Train split CLIP metrics: {train_clip_metrics}")
            reranker, processor, train_time = train_qwen_reranker(
                cfg, train_sim, train_dataset, device)
            mlflow.log_metric("reranker_train_time_sec", train_time)
            mlflow.log_metric("reranker_train_time_min", train_time / 60)
            # Estimated GPU cost (H100 ~$3/hr on cloud)
            estimated_cost_usd = (train_time / 3600) * 3.0 * env_info.get("gpu_count", 1)
            mlflow.log_metric("estimated_gpu_cost_usd", estimated_cost_usd)
            log.info(f"Training complete. Time: {train_time/60:.1f} mins. Est. cost: ${estimated_cost_usd:.2f}")
            # reranker.save_pretrained("/tmp/qwen_lora_weights")
            # mlflow.log_artifact("/tmp/qwen_lora_weights")
            reranker.save_pretrained("/tmp/qwen_lora_weights")
            # Change to plural 'log_artifacts' and optionally specify a destination folder in MLflow
            mlflow.log_artifacts("/tmp/qwen_lora_weights", artifact_path="qwen_lora_weights")
            hf_repo = cfg.get("hf_hub", {}).get("repo_id")
            if hf_token and hf_repo:
                try:
                    api = HfApi(token=hf_token)
                    api.create_repo(repo_id=hf_repo, repo_type="model", exist_ok=True)
                    api.upload_folder(
                        folder_path="/tmp/qwen_lora_weights",
                        repo_id=hf_repo,
                        repo_type="model",
                        commit_message=f"LoRA weights - {cfg['run_name']}",
                    )
                    log.info(f"Pushed LoRA weights to HF Hub: {hf_repo}")
                except Exception as e:
                    log.error(f"Failed to push LoRA weights to HF Hub: {e}")
            log.info("Evaluating with reranker...")
            reranked_metrics = evaluate_with_reranker(
                reranker, processor, sim_matrix, dataset, device)
            mlflow.log_metrics(reranked_metrics)
            log.info(f"Reranked Metrics: {reranked_metrics}")

            # ── Model quality gates ──────────────────────────────────────
            # Only register model in MLflow Model Registry if it passes
            # quality thresholds. Justified by:
            # - recall@1 > 0.70: must meaningfully beat random (CLIP baseline = 0.673)
            # - recall@10 > 0.90: top-10 results must contain correct image most of the time
            RECALL_AT_1_THRESHOLD = 0.70
            RECALL_AT_10_THRESHOLD = 0.90

            recall_at_1 = reranked_metrics.get("reranked_recall_at_1", 0)
            recall_at_10 = reranked_metrics.get("reranked_recall_at_10", 0)

            mlflow.log_param("quality_gate_recall_at_1_threshold", RECALL_AT_1_THRESHOLD)
            mlflow.log_param("quality_gate_recall_at_10_threshold", RECALL_AT_10_THRESHOLD)

            if recall_at_1 >= RECALL_AT_1_THRESHOLD and recall_at_10 >= RECALL_AT_10_THRESHOLD:
                log.info(f"Model passed quality gates (recall@1={recall_at_1:.3f} >= {RECALL_AT_1_THRESHOLD}, "
                         f"recall@10={recall_at_10:.3f} >= {RECALL_AT_10_THRESHOLD})")
                mlflow.log_param("quality_gate_passed", True)
                try:
                    model_uri = f"runs:/{mlflow.active_run().info.run_id}/qwen_lora_weights"
                    mv = mlflow.register_model(model_uri, "photoprism-reranker")
                    log.info(f"Model registered in MLflow Model Registry as version {mv.version}")
                    mlflow.log_param("registered_model_version", mv.version)
                except Exception as e:
                    log.warning(f"Could not register model: {e}")
            else:
                log.warning(f"Model FAILED quality gates (recall@1={recall_at_1:.3f} < {RECALL_AT_1_THRESHOLD} "
                            f"or recall@10={recall_at_10:.3f} < {RECALL_AT_10_THRESHOLD}) — NOT registered")
                mlflow.log_param("quality_gate_passed", False)

        log.info("Run complete.")


if __name__ == "__main__":
    main()
