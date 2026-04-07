"""
Shared version resolution for all pipeline services.

Version priority:
  1. DATASET_VERSION env var if set (explicit override, e.g. for named releases)
  2. Short HuggingFace commit SHA (first 8 chars) — automatic, derived from source data

Using the HF commit SHA as the default means:
  - Same source data → same storage path (idempotent re-runs)
  - Different HF commit → different path (no silent overwrites)
  - Full lineage: the path itself encodes the exact source version
"""

import os
from huggingface_hub import dataset_info

HF_REPO = "ar10067/flickr30k-images-CFQ"


def resolve_version() -> tuple[str, str]:
    """
    Returns (version, commit_sha).
    version is the storage path segment (e.g. "3c62601a" or "v1").
    commit_sha is the full HF SHA (or empty string if overridden by env).
    """
    token = os.environ.get("HF_TOKEN")
    override = os.environ.get("DATASET_VERSION")

    info = dataset_info(HF_REPO, token=token)
    commit_sha = info.sha or ""

    if override:
        print(f"Version override: DATASET_VERSION={override} (HF SHA: {commit_sha[:8]})")
        return override, commit_sha

    short_sha = commit_sha[:8]
    print(f"Version from HF commit SHA: {short_sha} (full: {commit_sha})")
    return short_sha, commit_sha
