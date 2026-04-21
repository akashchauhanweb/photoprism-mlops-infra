# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Semantic Photo Retrieval MLOps project** that evaluates image-text matching on the Flickr30K dataset. It supports two model variants:
- `clip_zeroshot` — CLIP zero-shot baseline (no training, pure evaluation)
- `qwen_reranker` — CLIP retrieval + Qwen2-VL-2B reranker with LoRA fine-tuning

## Running the Project

```bash
# Install Python dependencies
pip install -r requirements.txt
pip install git+https://github.com/openai/CLIP.git

# Run baseline (CLIP zero-shot)
python train.py --config config_baseline.yaml

# Run CLIP + Qwen reranker (v1: ViT-B/32, query=text, LoRA r=8)
python train.py --config config_qwen.yaml

# Run CLIP + Qwen reranker (v2: ViT-B/16, query=tags, LoRA r=16)
python train.py --config config_qwen_v2.yaml
```

## Docker

```bash
# Build
docker build -f Dockerfile.train -t mlops-train:latest .

# Run (requires GPU)
docker run --gpus all mlops-train:latest --config config_baseline.yaml
```

Base image: `pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime`

## Configuration

All configs are YAML files passed via `--config`. Key fields:

| Field | Description |
|---|---|
| `model` | `clip_zeroshot` or `qwen_reranker` |
| `clip_model` | CLIP backbone (`ViT-B/32` or `ViT-B/16`) |
| `max_samples` | Limit evaluation set size (useful for testing) |
| `query_type` | How to build text queries: `raw`, `tags`, or `caption` |
| `lora_r` / `lora_alpha` | LoRA rank and scaling for Qwen fine-tuning |
| `mlflow_experiment` | MLflow experiment name for tracking |

## Architecture

All logic lives in `train.py`. The execution pipeline:

1. **Load config** from YAML
2. **Initialize CLIP** (ViT-B/32 or ViT-B/16)
3. **Load Flickr30K** from HuggingFace Hub — cached to `/tmp/flickr30k`; split 80/20 for train/val
4. **CLIP zero-shot evaluation** — encodes images + text, computes cosine similarity, evaluates Recall@k and NDCG@k (k=1,5,10)
5. **[qwen_reranker only] Fine-tune Qwen2-VL-2B** with LoRA on binary classification (image matches query: yes/no), using hard negatives from CLIP top-10
6. **[qwen_reranker only] Rerank** CLIP top-10 results with the fine-tuned Qwen model
7. **Log to MLflow** — hyperparameters, metrics, embeddings, LoRA weights, GPU cost estimate

## Experiment Tracking

MLflow is used for all experiment tracking. Logged artifacts include:
- All hyperparameters from config
- Recall@1/5/10 and NDCG@1/5/10 (before and after reranking)
- Training loss per epoch
- CLIP embeddings and LoRA weights
- Environment info (GPU, CUDA, PyTorch versions, git SHA)
- Estimated GPU cost (H100 @ $3/hr)
