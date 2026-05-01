#!/usr/bin/env python3
"""
Stress test: Upload image → Semantic search with caption → Click top 3 results
"""

import json
import time
import statistics
import threading
import concurrent.futures
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ───────────────────────────────────────────────────────────────────
BASE_URL      = "http://129.114.26.235:30234"
SEARCH_URL    = "http://129.114.26.235:30810"
USER_ID       = "utdxeie778j5czht"
UPLOAD_ALBUM  = "q9xyxqf"
AUTH_TOKEN    = "372b9abc48652ba76e1f2020e699cc7ea14ce7b251186b1b"
IMAGES_DIR    = Path("/Users/aritro/Aritro/mlops-test/flickr30k-images")
DATASET_PATH  = Path("/Users/aritro/Aritro/mlops-test/unified_dataset.json")

TOP_K         = 10    # how many results to request from search
CLICK_TOP_N   = 3     # how many top results to click
WORKERS       = 4     # concurrent threads
MAX_IMAGES    = 50    # set to None to run on the entire dataset

UPLOAD_URL    = f"{BASE_URL}/api/v1/users/{USER_ID}/upload/{UPLOAD_ALBUM}"

HEADERS_BASE = {
    "Accept-Language": "en",
    "Connection":      "keep-alive",
    "Origin":          BASE_URL,
    "Referer":         f"{BASE_URL}/library/browse?view=cards&order=newest&public=true&quality=3",
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "X-Auth-Token":    AUTH_TOKEN,
    "X-Client-Uri":    "/static/build/app.d71a39858005dfc53cdf.js",
    "X-Client-Version":"260427-f16768d96-Linux-AMD64-DEVELOP",
}

HEADERS_SEARCH = {
    "Accept":          "*/*",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Connection":      "keep-alive",
    "Content-Type":    "application/json",
    "Origin":          BASE_URL,
    "Referer":         f"{BASE_URL}/",
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

# ── Metrics ──────────────────────────────────────────────────────────────────
@dataclass
class Metrics:
    lock: threading.Lock = field(default_factory=threading.Lock)

    upload_ok:   int = 0
    upload_fail: int = 0
    upload_times: list = field(default_factory=list)

    search_ok:   int = 0
    search_fail: int = 0
    search_times: list = field(default_factory=list)

    click_ok:    int = 0
    click_fail:  int = 0
    click_times:  list = field(default_factory=list)

    def record(self, category: str, success: bool, elapsed: float):
        with self.lock:
            if category == "upload":
                if success: self.upload_ok += 1; self.upload_times.append(elapsed)
                else:        self.upload_fail += 1
            elif category == "search":
                if success: self.search_ok += 1; self.search_times.append(elapsed)
                else:        self.search_fail += 1
            elif category == "click":
                if success: self.click_ok += 1; self.click_times.append(elapsed)
                else:        self.click_fail += 1

    def _stats(self, times: list) -> dict:
        if not times:
            return {"min": 0, "max": 0, "mean": 0, "p50": 0, "p95": 0}
        s = sorted(times)
        n = len(s)
        return {
            "min":  round(min(s), 3),
            "max":  round(max(s), 3),
            "mean": round(statistics.mean(s), 3),
            "p50":  round(s[int(n * 0.50)], 3),
            "p95":  round(s[int(n * 0.95)], 3),
        }

    def summary(self):
        return {
            "upload": {
                "ok": self.upload_ok, "fail": self.upload_fail,
                "latency_s": self._stats(self.upload_times),
            },
            "search": {
                "ok": self.search_ok, "fail": self.search_fail,
                "latency_s": self._stats(self.search_times),
            },
            "click": {
                "ok": self.click_ok, "fail": self.click_fail,
                "latency_s": self._stats(self.click_times),
            },
        }

metrics = Metrics()

# ── API helpers ───────────────────────────────────────────────────────────────
def upload_image(image_path: Path) -> Optional[str]:
    """POST multipart upload then PUT album confirm. Returns uploaded image_id or None."""
    t0 = time.perf_counter()
    try:
        with open(image_path, "rb") as f:
            resp = requests.post(
                UPLOAD_URL,
                headers={**HEADERS_BASE, "Accept": "application/json, text/plain, */*"},
                files={"files": (image_path.name, f, "image/jpeg")},
                cookies={
                    "adminer_key": "beceffcbf7d6f1b5a06ca69ae03df710",
                    "adminer_sid": "n65j0bva4cuvhv8709asvaolfn",
                },
                verify=False,
                timeout=30,
            )
        resp.raise_for_status()
        data = resp.json()

        # PUT to assign album
        requests.put(
            UPLOAD_URL,
            headers={**HEADERS_BASE,
                     "Accept": "application/json, text/plain, */*",
                     "Content-Type": "application/json"},
            json={"albums": []},
            cookies={
                "adminer_key": "beceffcbf7d6f1b5a06ca69ae03df710",
                "adminer_sid": "n65j0bva4cuvhv8709asvaolfn",
            },
            verify=False,
            timeout=10,
        )

        elapsed = time.perf_counter() - t0
        metrics.record("upload", True, elapsed)

        # extract first image_id from response
        image_id = None
        if isinstance(data, list) and data:
            image_id = data[0].get("id") or data[0].get("image_id")
        elif isinstance(data, dict):
            image_id = data.get("id") or data.get("image_id")

        return image_id

    except Exception as e:
        elapsed = time.perf_counter() - t0
        metrics.record("upload", False, elapsed)
        print(f"  [upload FAIL] {image_path.name}: {e}")
        return None


def semantic_search(query: str) -> Optional[dict]:
    """POST search query. Returns full response dict or None."""
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{SEARCH_URL}/search",
            headers=HEADERS_SEARCH,
            json={"query": query, "top_k": TOP_K},
            verify=False,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.perf_counter() - t0
        metrics.record("search", True, elapsed)
        return data
    except Exception as e:
        elapsed = time.perf_counter() - t0
        metrics.record("search", False, elapsed)
        print(f"  [search FAIL] query='{query[:60]}': {e}")
        return None


def click_result(query_id: str, image_id: str) -> bool:
    """POST click event for a search result."""
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{SEARCH_URL}/click",
            headers=HEADERS_SEARCH,
            json={"query_id": query_id, "image_id": image_id},
            verify=False,
            timeout=10,
        )
        resp.raise_for_status()
        elapsed = time.perf_counter() - t0
        metrics.record("click", True, elapsed)
        return True
    except Exception as e:
        elapsed = time.perf_counter() - t0
        metrics.record("click", False, elapsed)
        print(f"  [click FAIL] query_id={query_id} image_id={image_id}: {e}")
        return False


# ── Per-image pipeline ────────────────────────────────────────────────────────
def run_pipeline(entry: dict, idx: int, total: int):
    image_id  = entry["image_id"]
    caption   = entry["queries"]["caption"]
    image_path = IMAGES_DIR / image_id

    print(f"[{idx+1}/{total}] {image_id}")

    # 1. Upload
    if image_path.exists():
        upload_image(image_path)
    else:
        print(f"  [skip upload] image not found: {image_path.name}")

    # 2. Semantic search with caption
    result = semantic_search(caption)
    if not result:
        return

    query_id = result.get("query_id")
    hits     = result.get("hits", [])
    top_hits = hits[:CLICK_TOP_N]
    print(f"  search → query_id={query_id}  hits={len(hits)}  top={[h['image_id'] for h in top_hits]}")

    # 3. Click top N results
    for hit in top_hits:
        click_result(query_id, hit["image_id"])


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    with open(DATASET_PATH) as f:
        dataset = json.load(f)

    if MAX_IMAGES is not None:
        dataset = dataset[:MAX_IMAGES]

    total = len(dataset)
    print(f"Stress test: {total} images | {WORKERS} workers | top_k={TOP_K} | click_top={CLICK_TOP_N}\n")

    wall_start = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [
            pool.submit(run_pipeline, entry, idx, total)
            for idx, entry in enumerate(dataset)
        ]
        concurrent.futures.wait(futures)

    wall_elapsed = time.perf_counter() - wall_start

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    summary = metrics.summary()
    for op, stats in summary.items():
        lat = stats["latency_s"]
        print(f"\n{op.upper()}")
        print(f"  ok={stats['ok']}  fail={stats['fail']}")
        print(f"  latency (s): min={lat['min']}  mean={lat['mean']}  p50={lat['p50']}  p95={lat['p95']}  max={lat['max']}")

    print(f"\nTotal wall time: {wall_elapsed:.1f}s  |  throughput: {total/wall_elapsed:.2f} images/s")


if __name__ == "__main__":
    main()
