"""
Locust 4-hour overnight stress test (25-user peak)
Pipeline: semantic search → click top 3  (+ optional image upload)

Peak capped at 25 users — 40 users still saturates the H100.

── Quick start ──────────────────────────────────────────────────────────────
# Headless, shaped 4-hour run (recommended):
  locust -f locustfile.py --headless --run-time 4h

# Web UI (open http://localhost:8089, set host + users there):
  locust -f locustfile.py

── Load shape (OvernightStepShape) ─────────────────────────────────────────
   0 –  5 min :   5 users  (step 1)
   5 – 10 min :  10 users  (step 2)
  10 – 15 min :  15 users  (step 3)
  15 – 20 min :  20 users  (step 4)
  20 – 25 min :  25 users  (step 5, ramp complete)
  25 min – 3h 50min : 25 users  (sustained peak, 3h 25min)
  3h 50min – 4h     :  0 users  (cool-down)

── Task weights ─────────────────────────────────────────────────────────────
  search_and_click : weight 8  (dominant workload)
  upload_image     : weight 1  (background ingestion, only if images exist)
"""

import json
import random
import os
from pathlib import Path

from locust import HttpUser, task, between, events, LoadTestShape
from locust.clients import HttpSession
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_HOST  = "http://129.114.26.235:30810"   # primary host for HttpUser
UPLOAD_HOST  = "http://129.114.26.235:30234"
AUTH_TOKEN   = "372b9abc48652ba76e1f2020e699cc7ea14ce7b251186b1b"
USER_ID      = "utdxeie778j5czht"
UPLOAD_ALBUM = "q9xyxqf"
IMAGES_DIR   = Path("/Users/aritro/Aritro/mlops-test/flickr30k-images")
DATASET_PATH = Path("/Users/aritro/Aritro/mlops-test/unified_dataset.json")

TOP_K        = 10
CLICK_TOP_N  = 3

UPLOAD_URL   = f"{UPLOAD_HOST}/api/v1/users/{USER_ID}/upload/{UPLOAD_ALBUM}"

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

# ── Dataset loaded once at module level (shared across all workers) ───────────
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
class SearchUser(HttpUser):
    """
    Simulates a user browsing an image library:
      - frequently searches with natural-language captions
      - clicks the most relevant results
      - occasionally uploads a new image
    """
    host      = SEARCH_HOST
    wait_time = between(1, 4)   # seconds between tasks

    # separate session for the upload service (different host)
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

    # ── Tasks ─────────────────────────────────────────────────────────────────

    @task(8)
    def search_and_click(self):
        """Pick a random caption, search, then click the top results."""
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
        hits     = data.get("hits", [])[:CLICK_TOP_N]

        for hit in hits:
            self._click(query_id, hit["image_id"])

    @task(1)
    def upload_image(self):
        """Upload a random image from the local flickr30k set."""
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

            # confirm album assignment
            self._upload_session.put(
                UPLOAD_URL,
                json={"albums": []},
                headers={**UPLOAD_HEADERS, "Content-Type": "application/json"},
                cookies=UPLOAD_COOKIES,
                name="PUT /upload (album)",
            )
        except OSError:
            pass  # file vanished between listing and open

    # ── Helpers ───────────────────────────────────────────────────────────────

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


# ── 4-hour overnight stress shape (40-user peak) ──────────────────────────────
class OvernightStepShape(LoadTestShape):
    """
    5 steps of 5 users each (5 min per step), then 3h 25min at 25 users,
    followed by a 10-minute cool-down.  Total: 4 hours.

    Cumulative time  Users  Stage
    ───────────────  ─────  ──────────────────
         0 –   300      5   step 1
       300 –   600     10   step 2
       600 –   900     15   step 3
       900 –  1200     20   step 4
      1200 –  1500     25   step 5 (ramp complete)
      1500 – 13800     25   sustained peak (3h 25min)
     13800 – 14400      0   cool-down (10 min)
    """

    stages = [
        {"duration":   300, "users":  5, "spawn_rate": 2},   # step 1
        {"duration":   600, "users": 10, "spawn_rate": 2},   # step 2
        {"duration":   900, "users": 15, "spawn_rate": 2},   # step 3
        {"duration":  1200, "users": 20, "spawn_rate": 2},   # step 4
        {"duration":  1500, "users": 25, "spawn_rate": 2},   # step 5
        {"duration": 13800, "users": 25, "spawn_rate": 2},   # sustained peak
        {"duration": 14400, "users":  0, "spawn_rate": 10},  # cool-down
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None  # stop after 4 hours
