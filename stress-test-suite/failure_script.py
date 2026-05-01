#!/usr/bin/env python3
"""
Load escalation test — keeps doubling concurrency until the service breaks.

Stages: 1 → 2 → 4 → 8 → 16 → 32 → 64 → 128 → ...
At each stage, BATCH_SIZE pipelines fire simultaneously.
A stage is declared FAILED when:
  - error rate  > FAIL_ERROR_RATE  (default 20 %)
  - OR p95 latency > FAIL_P95_S    (default 10 s)
  - OR ConnectionRefused on any request

Run:
  python3 failure_script.py            # search+click only (fast)
  python3 failure_script.py --upload   # full pipeline incl. image upload
"""

import json
import sys
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

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL     = "http://129.114.26.235:30234"
SEARCH_URL   = "http://129.114.26.235:30810"
AUTH_TOKEN   = "372b9abc48652ba76e1f2020e699cc7ea14ce7b251186b1b"
USER_ID      = "utdxeie778j5czht"
UPLOAD_ALBUM = "q9xyxqf"
IMAGES_DIR   = Path("/Users/aritro/Aritro/mlops-test/flickr30k-images")
DATASET_PATH = Path("/Users/aritro/Aritro/mlops-test/unified_dataset.json")

UPLOAD_URL   = f"{BASE_URL}/api/v1/users/{USER_ID}/upload/{UPLOAD_ALBUM}"

INITIAL_WORKERS  = 5
MAX_WORKERS      = 256
BATCH_SIZE       = 20      # pipelines per stage
TOP_K            = 10
CLICK_TOP_N      = 3

FAIL_ERROR_RATE  = 0.20    # >20 % errors  → failed
FAIL_P95_S       = 200.0    # p95 > 10 s    → failed
TIMEOUT_S        = 200

INCLUDE_UPLOAD   = "--upload" in sys.argv

HEADERS_SEARCH = {
    "Accept":          "*/*",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Connection":      "keep-alive",
    "Content-Type":    "application/json",
    "Origin":          BASE_URL,
    "Referer":         f"{BASE_URL}/",
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

HEADERS_UPLOAD = {
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en",
    "Connection":       "keep-alive",
    "X-Auth-Token":     AUTH_TOKEN,
    "Origin":           BASE_URL,
    "Referer":          f"{BASE_URL}/library/browse",
    "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "X-Client-Uri":     "/static/build/app.d71a39858005dfc53cdf.js",
    "X-Client-Version": "260427-f16768d96-Linux-AMD64-DEVELOP",
}

COOKIES = {
    "adminer_key": "beceffcbf7d6f1b5a06ca69ae03df710",
    "adminer_sid": "n65j0bva4cuvhv8709asvaolfn",
}

# ── Shared progress counter ───────────────────────────────────────────────────
_progress_lock    = threading.Lock()
_progress_done    = 0
_progress_total   = 0
_progress_ok      = 0
_progress_fail    = 0


def _tick(success: bool):
    global _progress_done, _progress_ok, _progress_fail
    with _progress_lock:
        _progress_done += 1
        if success:
            _progress_ok += 1
        else:
            _progress_fail += 1
        print(
            f"\r  pipelines: {_progress_done}/{_progress_total}  "
            f"ok={_progress_ok}  fail={_progress_fail}   ",
            end="", flush=True,
        )


def _reset_progress(total: int):
    global _progress_done, _progress_total, _progress_ok, _progress_fail
    _progress_done  = 0
    _progress_total = total
    _progress_ok    = 0
    _progress_fail  = 0


# ── Result types ──────────────────────────────────────────────────────────────
@dataclass
class OpResult:
    op: str
    success: bool
    latency: float
    status_code: Optional[int] = None
    error: Optional[str] = None


@dataclass
class StageResult:
    concurrency: int
    results: list = field(default_factory=list)

    @property
    def total(self):      return len(self.results)
    @property
    def ok(self):         return sum(1 for r in self.results if r.success)
    @property
    def fail(self):       return self.total - self.ok
    @property
    def error_rate(self): return self.fail / self.total if self.total else 1.0
    @property
    def latencies(self):  return sorted(r.latency for r in self.results if r.success)

    def p(self, pct):
        s = self.latencies
        if not s: return float("inf")
        idx = min(int(len(s) * pct / 100), len(s) - 1)
        return s[idx]

    def mean(self):
        s = self.latencies
        return statistics.mean(s) if s else float("inf")

    def failed(self):
        return (
            self.error_rate > FAIL_ERROR_RATE
            or self.p(95) > FAIL_P95_S
            or any(r.error == "ConnectionRefused" for r in self.results)
        )

    def errors_by_type(self):
        counts: dict = {}
        for r in self.results:
            if not r.success:
                key = r.error or f"HTTP {r.status_code}"
                counts[key] = counts.get(key, 0) + 1
        return counts

    def row(self):
        status = "FAIL ✗" if self.failed() else "OK   ✓"
        return (
            f"  {self.concurrency:>7} | "
            f"{self.ok:>3}/{self.total:<4}| "
            f"{self.error_rate*100:>5.1f}% | "
            f"{self.mean():>7.2f}s | "
            f"{self.p(95):>7.2f}s | "
            f"{self.p(100):>7.2f}s | "
            f"{status}"
        )


# ── Single-request helpers ────────────────────────────────────────────────────
def do_upload(image_path: Path) -> OpResult:
    t0 = time.perf_counter()
    try:
        with open(image_path, "rb") as f:
            r = requests.post(
                UPLOAD_URL,
                headers=HEADERS_UPLOAD,
                files={"files": (image_path.name, f, "image/jpeg")},
                cookies=COOKIES,
                verify=False,
                timeout=TIMEOUT_S,
            )
        r.raise_for_status()
        requests.put(
            UPLOAD_URL,
            headers={**HEADERS_UPLOAD, "Content-Type": "application/json"},
            json={"albums": []},
            cookies=COOKIES,
            verify=False,
            timeout=TIMEOUT_S,
        )
        return OpResult("upload", True, time.perf_counter() - t0, r.status_code)
    except requests.exceptions.ConnectionError:
        return OpResult("upload", False, time.perf_counter() - t0, error="ConnectionRefused")
    except requests.exceptions.Timeout:
        return OpResult("upload", False, time.perf_counter() - t0, error="Timeout")
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else None
        return OpResult("upload", False, time.perf_counter() - t0,
                        status_code=code, error=f"HTTP {code}")
    except Exception as e:
        return OpResult("upload", False, time.perf_counter() - t0, error=type(e).__name__)


def do_search(query: str) -> OpResult:
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{SEARCH_URL}/search",
            headers=HEADERS_SEARCH,
            json={"query": query, "top_k": TOP_K},
            verify=False,
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        data = r.json()
        # stash payload in error field to pass it up without a separate return type
        return OpResult("search", True, time.perf_counter() - t0, r.status_code,
                        error=json.dumps(data))
    except requests.exceptions.ConnectionError:
        return OpResult("search", False, time.perf_counter() - t0, error="ConnectionRefused")
    except requests.exceptions.Timeout:
        return OpResult("search", False, time.perf_counter() - t0, error="Timeout")
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else None
        return OpResult("search", False, time.perf_counter() - t0,
                        status_code=code, error=f"HTTP {code}")
    except Exception as e:
        return OpResult("search", False, time.perf_counter() - t0, error=type(e).__name__)


def do_click(query_id: str, image_id: str) -> OpResult:
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{SEARCH_URL}/click",
            headers=HEADERS_SEARCH,
            json={"query_id": query_id, "image_id": image_id},
            verify=False,
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        return OpResult("click", True, time.perf_counter() - t0, r.status_code)
    except requests.exceptions.ConnectionError:
        return OpResult("click", False, time.perf_counter() - t0, error="ConnectionRefused")
    except requests.exceptions.Timeout:
        return OpResult("click", False, time.perf_counter() - t0, error="Timeout")
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else None
        return OpResult("click", False, time.perf_counter() - t0,
                        status_code=code, error=f"HTTP {code}")
    except Exception as e:
        return OpResult("click", False, time.perf_counter() - t0, error=type(e).__name__)


# ── Full pipeline for one dataset entry ──────────────────────────────────────
def run_entry(entry: dict) -> list[OpResult]:
    results = []
    caption    = entry["queries"]["caption"]
    image_path = IMAGES_DIR / entry["image_id"]

    if INCLUDE_UPLOAD and image_path.exists():
        results.append(do_upload(image_path))

    search_res = do_search(caption)
    results.append(search_res)

    if search_res.success:
        try:
            data     = json.loads(search_res.error)
            query_id = data.get("query_id", "")
            hits     = data.get("hits", [])[:CLICK_TOP_N]
            for hit in hits:
                results.append(do_click(query_id, hit["image_id"]))
        except (json.JSONDecodeError, AttributeError):
            pass

    return results


# ── Stage runner ──────────────────────────────────────────────────────────────
def run_stage(entries: list, concurrency: int) -> StageResult:
    stage = StageResult(concurrency=concurrency)
    lock  = threading.Lock()
    _reset_progress(BATCH_SIZE)

    def task(entry):
        ops     = run_entry(entry)
        success = all(o.success for o in ops)
        with lock:
            stage.results.extend(ops)
        _tick(success)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(task, e) for e in entries[:BATCH_SIZE]]
        concurrent.futures.wait(futures)

    print()  # newline after progress bar
    return stage


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    mode = "upload + search + click" if INCLUDE_UPLOAD else "search + click (use --upload for full pipeline)"

    with open(DATASET_PATH) as f:
        dataset = json.load(f)

    print("=" * 75)
    print("LOAD ESCALATION TEST — doubling concurrency until service failure")
    print(f"mode: {mode}")
    print(f"batch={BATCH_SIZE}/stage | fail>{FAIL_ERROR_RATE*100:.0f}% err OR p95>{FAIL_P95_S}s | timeout={TIMEOUT_S}s")
    print("=" * 75)
    print(f"  {'workers':>7} | {'ok/total':>8} | {'err%':>6} | "
          f"{'mean':>8} | {'p95':>8} | {'max':>8} | status")
    print("-" * 75)

    all_stages  = []
    concurrency = INITIAL_WORKERS

    while concurrency <= MAX_WORKERS:
        start   = (len(all_stages) * BATCH_SIZE) % len(dataset)
        entries = (dataset * 2)[start: start + BATCH_SIZE]

        print(f"  → running stage: {concurrency} worker(s)...", flush=True)
        stage = run_stage(entries, concurrency)
        all_stages.append(stage)
        print(stage.row(), flush=True)

        if stage.failed():
            errs = stage.errors_by_type()
            print(f"\n  !! SERVICE FAILED at concurrency={concurrency}")
            print(f"     error breakdown: {errs}")
            break

        concurrency *= 2
    else:
        print(f"\n  Reached MAX_WORKERS={MAX_WORKERS} without triggering failure.")

    print("\n" + "=" * 75)
    stable = [s for s in all_stages if not s.failed()]
    failed = [s for s in all_stages if s.failed()]
    if stable:
        best = stable[-1]
        print(f"  Last STABLE concurrency : {best.concurrency} workers")
        print(f"  At that load            : mean={best.mean():.2f}s  "
              f"p95={best.p(95):.2f}s  err={best.error_rate*100:.1f}%")
    if failed:
        f0 = failed[0]
        print(f"  First FAILURE           : {f0.concurrency} workers  "
              f"err={f0.error_rate*100:.1f}%  p95={f0.p(95):.2f}s")
        errs = f0.errors_by_type()
        print(f"  Failure causes          : {errs}")
    print("=" * 75)


if __name__ == "__main__":
    main()
