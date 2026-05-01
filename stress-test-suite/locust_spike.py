"""
Locust spike / breaking-point test (5 → 200 users)
Pipeline: semantic search → click top 3  (+ optional image upload)

Ramps aggressively from 5 to 200 users in ~14 minutes (2-min steps),
then holds at 200 for 30 minutes to observe failure modes and recovery.
Expect GPU saturation and MLflow drops well before 200 — the goal is to
find the exact cliff edge and see whether the system self-recovers.

── Quick start ──────────────────────────────────────────────────────────────
# Headless (~1 hour total):
  locust -f locust_spike.py --headless --run-time 1h

# Web UI (open http://localhost:8089):
  locust -f locust_spike.py

── Load shape (SpikeShape) ──────────────────────────────────────────────────
   0 –  2 min :   5 users  (baseline)
   2 –  4 min :  25 users
   4 –  6 min :  50 users
   6 –  8 min :  75 users
   8 – 10 min : 100 users
  10 – 12 min : 150 users
  12 – 14 min : 200 users  (peak)
  14 – 44 min : 200 users  (sustained — observe failure + recovery)
  44 – 54 min :   0 users  (cool-down)

── Task weights ─────────────────────────────────────────────────────────────
  search_and_click : weight 8  (heaviest GPU workload — worst-case stress)
  upload_image     : weight 1  (background ingestion)
"""

import json
import random
from pathlib import Path

from locust import HttpUser, task, between, LoadTestShape
from locust.clients import HttpSession
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_HOST  = "http://129.114.26.235:30810"
UPLOAD_HOST  = "http://129.114.26.235:30234"
AUTH_TOKEN   = "372b9abc48652ba76e1f2020e699cc7ea14ce7b251186b1b"
USER_ID      = "utdxeie778j5czht"
UPLOAD_ALBUM = "q9xyxqf"
IMAGES_DIR   = Path("/Users/aritro/Aritro/mlops-test/flickr30k-images")
DATASET_PATH = Path("/Users/aritro/Aritro/mlops-test/unified_dataset.json")

TOP_K       = 10
CLICK_TOP_N = 3

UPLOAD_URL = f"{UPLOAD_HOST}/api/v1/users/{USER_ID}/upload/{UPLOAD_ALBUM}"

UPLOAD_HEADERS = {
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en",
    "X-Auth-Token":     AUTH_TOKEN,
    "Origin":           UPLOAD_HOST,
    "Referer":          f"{UPLOAD_HOST}/library/browse",
    "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "X-Client-Uri":     "/static/build/app.d71a39858005dfc53cdf.js",
    "X-Client-Version": "260427-f16768d96-Linux-AMD64-DEVELOP",
}

UPLOAD_COOKIES = {
    "adminer_key": "beceffcbf7d6f1b5a06ca69ae03df710",
    "adminer_sid": "n65j0bva4cuvhv8709asvaolfn",
}

# ── Dataset ───────────────────────────────────────────────────────────────────
_dataset: list = []
_image_files: list = []

def _load_dataset():
    global _dataset, _image_files
    with open(DATASET_PATH) as f:
        _dataset = json.load(f)
    if IMAGES_DIR.exists():
        _image_files = [p for p in IMAGES_DIR.iterdir() if p.suffix.lower() == ".jpg"]
    print(f"[locust] dataset={len(_dataset)} entries  images={len(_image_files)} files")

_load_dataset()


# ── User class ────────────────────────────────────────────────────────────────
class SpikeUser(HttpUser):
    """
    Search-heavy user — maximises GPU pressure to surface the breaking point
    as quickly as possible during the spike ramp.
    """
    host      = SEARCH_HOST
    wait_time = between(1, 3)   # tight pacing to maximise load per user

    _upload_session: HttpSession | None = None

    def on_start(self):
        self.client.verify = False
        if _image_files:
            self._upload_session = HttpSession(
                base_url=UPLOAD_HOST,
                request_event=self.environment.events.request,
                user=self,
            )
            self._upload_session.verify = False

    @task(8)
    def search_and_click(self):
        entry   = random.choice(_dataset)
        caption = entry["queries"]["caption"]

        with self.client.post(
            "/search",
            json={"query": caption, "top_k": TOP_K},
            headers={"Content-Type": "application/json"},
            name="/search",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"search HTTP {resp.status_code}")
                return
            try:
                data = resp.json()
            except Exception:
                resp.failure("search: invalid JSON")
                return
            resp.success()

        query_id = data.get("query_id", "")
        for hit in data.get("hits", [])[:CLICK_TOP_N]:
            self._click(query_id, hit["image_id"])

    @task(1)
    def upload_image(self):
        if not _image_files or self._upload_session is None:
            return

        image_path = random.choice(_image_files)
        try:
            with open(image_path, "rb") as f:
                with self._upload_session.post(
                    UPLOAD_URL,
                    files={"files": (image_path.name, f, "image/jpeg")},
                    headers=UPLOAD_HEADERS,
                    cookies=UPLOAD_COOKIES,
                    name="POST /upload",
                    catch_response=True,
                ) as resp:
                    if resp.status_code not in (200, 201):
                        resp.failure(f"upload HTTP {resp.status_code}")
                        return
                    resp.success()

            self._upload_session.put(
                UPLOAD_URL,
                json={"albums": []},
                headers={**UPLOAD_HEADERS, "Content-Type": "application/json"},
                cookies=UPLOAD_COOKIES,
                name="PUT /upload (album)",
            )
        except OSError:
            pass

    def _click(self, query_id: str, image_id: str):
        with self.client.post(
            "/click",
            json={"query_id": query_id, "image_id": image_id},
            headers={"Content-Type": "application/json"},
            name="/click",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"click HTTP {resp.status_code}")
            else:
                resp.success()


# ── Spike shape: 5 → 200 users in 14 min, hold 30 min, cool-down 10 min ──────
class SpikeShape(LoadTestShape):
    """
    Aggressive 2-minute steps to surface the GPU/MLflow breaking point fast.

    Cumulative time  Users  Stage
    ───────────────  ─────  ──────────────────────────────
         0 –   120      5   baseline
       120 –   240     25   ramp ×5
       240 –   360     50   ramp ×2
       360 –   480     75   ramp ×1.5
       480 –   600    100   ramp ×1.33
       600 –   720    150   ramp ×1.5
       720 –   840    200   peak
       840 –  2640    200   sustained (30 min — observe failure + recovery)
      2640 –  3240      0   cool-down (10 min)
    """

    stages = [
        {"duration":   120, "users":   5, "spawn_rate": 5},    # baseline
        {"duration":   240, "users":  25, "spawn_rate": 10},   # ×5 jump
        {"duration":   360, "users":  50, "spawn_rate": 15},   # +25
        {"duration":   480, "users":  75, "spawn_rate": 15},   # +25
        {"duration":   600, "users": 100, "spawn_rate": 15},   # +25
        {"duration":   720, "users": 150, "spawn_rate": 25},   # +50
        {"duration":   840, "users": 200, "spawn_rate": 25},   # +50  peak
        {"duration":  264, "users": 200, "spawn_rate": 25},   # sustained
        {"duration":  324, "users":   0, "spawn_rate": 50},   # cool-down
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None
