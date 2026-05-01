"""
Locust 4-hour realistic browser simulation (125-user peak)
Pipeline: browse/scroll → occasional search → rare upload

Models a real user session: the browser passively fetches photos, polls
config and retrain state, and only occasionally performs a semantic search.
Uploads are rare background events.

── Quick start ──────────────────────────────────────────────────────────────
# Headless, shaped 4-hour run (recommended):
  locust -f locust_realistic.py --headless --run-time 4h

# Web UI (open http://localhost:8089, set host + users there):
  locust -f locust_realistic.py

── Load shape (RealisticStepShape) ──────────────────────────────────────────
   0 –  5 min :   5 users  (step 1)
   5 – 10 min :  25 users  (step 2)
  10 – 15 min :  45 users  (step 3)
  15 – 20 min :  80 users  (step 4)
  20 – 25 min : 125 users  (step 5, ramp complete)
  25 min – 3h 50min : 125 users  (sustained peak, 3h 25min)
  3h 50min – 4h     :   0 users  (cool-down)

── Task weights ─────────────────────────────────────────────────────────────
  browse_library   : weight 10  (scroll through photo grid)
  view_library     : weight  8  (mosaic view page load)
  get_config       : weight  3  (app config poll)
  poll_retrain     : weight  3  (ML retrain status poll)
  search_and_click : weight  2  (semantic search + click results)
  upload_image     : weight  1  (background ingestion)
"""

import json
import random
from pathlib import Path

from locust import HttpUser, task, between, LoadTestShape
from locust.clients import HttpSession
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ────────────────────────────────────────────────────────────────────
UPLOAD_HOST  = "http://129.114.26.235:30234"   # primary host (browse/view/config)
SEARCH_HOST  = "http://129.114.26.235:30810"   # secondary (search/click/retrain)
AUTH_TOKEN   = "372b9abc48652ba76e1f2020e699cc7ea14ce7b251186b1b"
USER_ID      = "utdxeie778j5czht"
UPLOAD_ALBUM = "q9xyxqf"
IMAGES_DIR   = Path("/Users/aritro/Aritro/mlops-test/flickr30k-images")
DATASET_PATH = Path("/Users/aritro/Aritro/mlops-test/unified_dataset.json")

TOP_K        = 10
CLICK_TOP_N  = 3
TOTAL_PHOTOS = 3120   # approximate library size for realistic offset randomisation
PAGE_SIZE    = 48     # typical mosaic grid page

UPLOAD_URL = f"{UPLOAD_HOST}/api/v1/users/{USER_ID}/upload/{UPLOAD_ALBUM}"

# Headers shared by all UPLOAD_HOST requests
_SHARED_HEADERS = {
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en",
    "X-Auth-Token":     AUTH_TOKEN,
    "Referer":          f"{UPLOAD_HOST}/library/browse?view=mosaic&order=newest&public=true&quality=3",
    "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "X-Client-Uri":     "/static/build/app.d71a39858005dfc53cdf.js",
    "X-Client-Version": "260427-f16768d96-Linux-AMD64-DEVELOP",
}

_SHARED_COOKIES = {
    "adminer_key": "beceffcbf7d6f1b5a06ca69ae03df710",
    "adminer_sid": "n65j0bva4cuvhv8709asvaolfn",
}

# ── Dataset loaded once at module level ───────────────────────────────────────
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
class RealisticBrowserUser(HttpUser):
    """
    Simulates a real browser session against the photo library:
      - scrolls through the photo grid (dominant)
      - loads mosaic view pages
      - periodically polls app config and retrain state
      - occasionally searches and clicks results
      - rarely uploads an image
    """
    host      = UPLOAD_HOST        # most traffic goes to the library service
    wait_time = between(2, 6)      # realistic reading/viewing pauses

    _search_session: HttpSession | None = None  # for SEARCH_HOST endpoints

    def on_start(self):
        self.client.verify = False
        self._search_session = HttpSession(
            base_url=SEARCH_HOST,
            request_event=self.environment.events.request,
            user=self,
        )
        self._search_session.verify = False

    # ── Passive: browsing / scrolling ────────────────────────────────────────

    @task(10)
    def browse_library(self):
        """Fetch a page of photos as if the user scrolled to that offset."""
        offset = random.randrange(0, max(TOTAL_PHOTOS - PAGE_SIZE, PAGE_SIZE), PAGE_SIZE)
        self.client.get(
            "/api/v1/photos",
            params={
                "count":   PAGE_SIZE,
                "offset":  offset,
                "merged":  "true",
                "order":   "newest",
                "reverse": "false",
                "public":  "true",
                "quality": 3,
                # unused filters kept empty to match real browser request
                "country": "", "camera": 0, "lens": 0, "label": "",
                "latlng":  "", "year": 0, "month": 0, "color": "",  "q": "",
            },
            headers=_SHARED_HEADERS,
            cookies=_SHARED_COOKIES,
            name="GET /api/v1/photos (browse)",
        )

    @task(8)
    def view_library(self):
        """Load the mosaic view page at a random scroll position."""
        offset = random.randrange(0, max(TOTAL_PHOTOS - PAGE_SIZE, PAGE_SIZE), PAGE_SIZE)
        self.client.get(
            "/api/v1/photos/view",
            params={
                "count":   PAGE_SIZE,
                "offset":  offset,
                "merged":  "true",
                "order":   "newest",
                "reverse": "false",
                "public":  "true",
                "quality": 3,
                "country": "", "camera": 0, "lens": 0, "label": "",
                "latlng":  "", "year": 0, "month": 0, "color": "", "q": "",
            },
            headers=_SHARED_HEADERS,
            cookies=_SHARED_COOKIES,
            name="GET /api/v1/photos/view",
        )

    # ── Passive: config / state polls ────────────────────────────────────────

    @task(3)
    def get_config(self):
        """App config fetch — happens on every page load in the browser."""
        self.client.get(
            "/api/v1/config",
            headers=_SHARED_HEADERS,
            cookies=_SHARED_COOKIES,
            name="GET /api/v1/config",
        )

    @task(3)
    def poll_retrain_state(self):
        """Poll the ML backend for ongoing retrain / indexing status."""
        self._search_session.get(
            "/retrain-state",
            headers={
                "Accept":          "*/*",
                "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
                "Origin":          UPLOAD_HOST,
                "Referer":         f"{UPLOAD_HOST}/",
                "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
            name="GET /retrain-state",
        )

    # ── Active: search + click ────────────────────────────────────────────────

    @task(2)
    def search_and_click(self):
        """Semantic search with a random caption, then click the top results."""
        entry   = random.choice(_dataset)
        caption = entry["queries"]["caption"]

        with self._search_session.post(
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

    # ── Active: upload ────────────────────────────────────────────────────────

    @task(1)
    def upload_image(self):
        """Upload a random image from the local flickr30k set."""
        if not _image_files:
            return

        image_path = random.choice(_image_files)
        try:
            with open(image_path, "rb") as f:
                with self.client.post(
                    UPLOAD_URL,
                    files={"files": (image_path.name, f, "image/jpeg")},
                    headers=_SHARED_HEADERS,
                    cookies=_SHARED_COOKIES,
                    name="POST /upload",
                    catch_response=True,
                ) as resp:
                    if resp.status_code not in (200, 201):
                        resp.failure(f"upload HTTP {resp.status_code}")
                        return
                    resp.success()

            self.client.put(
                UPLOAD_URL,
                json={"albums": []},
                headers={**_SHARED_HEADERS, "Content-Type": "application/json"},
                cookies=_SHARED_COOKIES,
                name="PUT /upload (album)",
            )
        except OSError:
            pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _click(self, query_id: str, image_id: str):
        with self._search_session.post(
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


# ── 4-hour realistic load shape (125-user peak) ───────────────────────────────
class RealisticStepShape(LoadTestShape):
    """
    Aggressive 5-step ramp (5 min per step) to 125 users, then 3h 25min
    sustained, followed by a 10-minute cool-down.  Total: 4 hours.

    Cumulative time  Users  Stage
    ───────────────  ─────  ──────────────────
         0 –   300      5   step 1
       300 –   600     25   step 2  (+20)
       600 –   900     45   step 3  (+20)
       900 –  1200     80   step 4  (+35)
      1200 –  1500    125   step 5  (+45, ramp complete)
      1500 – 13800    125   sustained peak (3h 25min)
     13800 – 14400      0   cool-down (10 min)
    """

    # durations are CUMULATIVE — each must be strictly greater than the previous
    stages = [
        {"duration":   100, "users":   5, "spawn_rate": 5},   # step 1
        {"duration":   200, "users":  25, "spawn_rate": 5},   # step 2
        {"duration":   300, "users":  45, "spawn_rate": 5},   # step 3
        {"duration":   400, "users":  80, "spawn_rate": 5},   # step 4
        {"duration":   500, "users": 100, "spawn_rate": 5},   # step 5
        {"duration":   600, "users": 120, "spawn_rate": 5},   # step 6
        {"duration":   700, "users": 140, "spawn_rate": 5},   # step 7
        {"duration":   800, "users": 160, "spawn_rate": 5},   # step 8
        {"duration":   900, "users": 180, "spawn_rate": 5},   # step 9
        {"duration":  1000, "users": 200, "spawn_rate": 5},   # step 10
        {"duration":  1100, "users": 220, "spawn_rate": 5},   # step 11
        {"duration":  1200, "users": 240, "spawn_rate": 5},   # step 12
        {"duration":  1300, "users": 260, "spawn_rate": 5},   # step 13
        {"duration":  1400, "users": 280, "spawn_rate": 5},   # step 14
        {"duration":  1500, "users": 300, "spawn_rate": 5},   # step 15
        {"duration":  1600, "users": 320, "spawn_rate": 5},   # step 16
        {"duration":  1700, "users": 340, "spawn_rate": 5},   # step 17
        {"duration":  1800, "users": 360, "spawn_rate": 5},   # step 18
        {"duration":  1900, "users": 380, "spawn_rate": 5},   # step 19
        {"duration":  2000, "users": 400, "spawn_rate": 5},   # step 20
        {"duration":  2100, "users": 420, "spawn_rate": 5},   # step 21
        {"duration":  2200, "users": 440, "spawn_rate": 5},   # step 22
        {"duration":  2300, "users": 460, "spawn_rate": 5},   # step 23
        {"duration":  2400, "users": 480, "spawn_rate": 5},   # step 24
        {"duration":  2500, "users": 500, "spawn_rate": 5},   # step 25 (ramp complete)
        {"duration":  2600, "users": 550, "spawn_rate": 5},
        {"duration":  2700, "users": 600, "spawn_rate": 5},
        {"duration":  2800, "users": 650, "spawn_rate": 5},
        {"duration":  2900, "users": 700, "spawn_rate": 5},
        {"duration":  3000, "users": 750, "spawn_rate": 5},
        {"duration":  3100, "users": 800, "spawn_rate": 5},
        {"duration":  3200, "users": 850, "spawn_rate": 5},
        {"duration":  3300, "users": 900, "spawn_rate": 5},
        {"duration":  3400, "users": 950, "spawn_rate": 5},
        {"duration":  3500, "users": 1000, "spawn_rate": 5},
        {"duration":  3600, "users": 1050, "spawn_rate": 5},
        {"duration":  3700, "users": 1100, "spawn_rate": 5},
        {"duration":  3800, "users": 1150, "spawn_rate": 5},
        {"duration":  3900, "users": 1200, "spawn_rate": 5},
        {"duration":  4000, "users": 1225, "spawn_rate": 5},
        {"duration":  4100, "users": 100, "spawn_rate": 5},
        {"duration":  4200, "users": 10, "spawn_rate": 5},
        {"duration":  4300, "users": 200, "spawn_rate": 5},
        {"duration":  8400, "users": 1000, "spawn_rate": 5},
        {"duration": 13800, "users": 1200, "spawn_rate": 5},   # sustained peak (~3h)
        {"duration": 14400, "users":   0, "spawn_rate": 20},  # cool-down (10 min)
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None  # stop after 4 hours
