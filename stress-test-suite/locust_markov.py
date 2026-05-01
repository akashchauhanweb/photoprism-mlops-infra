"""
Locust Markov-chain browser simulation (5 → 200 users, 4-hour stress test)
Pipeline: stateful user sessions driven by transition probabilities

Instead of randomly picking from weighted tasks each tick, each user holds
a current "state" and transitions to the next state based on a probability
matrix — much closer to how real users actually navigate the app.

── Quick start ──────────────────────────────────────────────────────────────
# Headless, 4-hour stress test:
  locust -f locust_markov.py --headless --run-time 4h

# Web UI:
  locust -f locust_markov.py

── Markov states ────────────────────────────────────────────────────────────
  browse   GET /api/v1/photos          (scrolling the grid)
  view     GET /api/v1/photos/view     (mosaic page load)
  search   POST /search                (semantic query)
  click    POST /click × top-N         (clicking search results)
  config   GET /api/v1/config          (passive page-load poll)
  retrain  GET /retrain-state          (passive ML status poll)
  upload   POST /upload + PUT /upload  (rare ingestion)

── Transition matrix ────────────────────────────────────────────────────────
  from \ to   browse  view  search  click  config  retrain  upload
  browse        40%   20%    15%     –      10%     10%      5%
  view          40%   25%    20%     –       –      10%      5%
  search        20%    –     20%    60%      –       –        –
  click         50%   10%    30%     –       –       –      10%
  config        60%   30%    10%     –       –       –        –
  retrain       70%   10%    20%     –       –       –        –
  upload        70%   20%    10%     –       –       –        –

── Load shape (MarkovStepShape) ─────────────────────────────────────────────
  6 steps of 30 s each ramp to 200 users (3 min total ramp), then hold
  for ~3 h 47 min to stress the system, then 10 min cool-down.

    0 s –  30 s :   5 users  (step 1)
   30 s –  60 s :  25 users  (step 2)
   60 s –  90 s :  50 users  (step 3)
   90 s – 120 s : 100 users  (step 4)
  120 s – 150 s : 150 users  (step 5)
  150 s – 180 s : 200 users  (step 6, ramp complete)
  180 s – 13800 s: 200 users  (sustained peak, ~3 h 47 min)
  13800 s – 14400 s:  0 users  (cool-down, 10 min)
"""

import json
import random
import threading
import time
from pathlib import Path

from locust import HttpUser, task, between, LoadTestShape
from locust.clients import HttpSession
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ────────────────────────────────────────────────────────────────────
UPLOAD_HOST  = "http://129.114.26.235:30234"
SEARCH_HOST  = "http://129.114.26.235:30810"
AUTH_TOKEN   = "372b9abc48652ba76e1f2020e699cc7ea14ce7b251186b1b"
USER_ID      = "utdxeie778j5czht"
UPLOAD_ALBUM = "q9xyxqf"
IMAGES_DIR   = Path("/Users/aritro/Aritro/mlops-test/flickr30k-images")
DATASET_PATH = Path("/Users/aritro/Aritro/mlops-test/unified_dataset.json")

TOP_K        = 10
CLICK_TOP_N  = 3
TOTAL_PHOTOS = 3120
PAGE_SIZE    = 48

UPLOAD_URL = f"{UPLOAD_HOST}/api/v1/users/{USER_ID}/upload/{UPLOAD_ALBUM}"

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

# ── Upload rate gate (1 upload per 15–20 min across ALL users) ───────────────
_upload_lock      = threading.Lock()
_next_upload_at   = 0.0   # epoch seconds; 0 = allow immediately on first call

def _claim_upload_slot() -> bool:
    """Return True and advance the window if an upload slot is available."""
    global _next_upload_at
    with _upload_lock:
        if time.monotonic() < _next_upload_at:
            return False
        _next_upload_at = time.monotonic() + 60  # 1 min
        return True

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


# ── Markov user ───────────────────────────────────────────────────────────────
class MarkovBrowserUser(HttpUser):
    """
    Each user maintains a current state and picks the next state by sampling
    from that state's transition distribution — no global task scheduler.
    """
    host      = UPLOAD_HOST
    wait_time = between(1, 5)

    # Transition table: state → [(next_state, weight), ...]
    # Weights are relative (not required to sum to 100).
    _TRANSITIONS: dict[str, list[tuple[str, int]]] = {
        "browse":  [("browse", 40), ("view", 20), ("search", 15),
                    ("config", 10), ("retrain", 10), ("upload",  5)],
        "view":    [("browse", 40), ("view",   25), ("search", 20),
                    ("retrain", 10), ("upload",  5)],
        "search":  [("click",  60), ("search", 20), ("browse", 20)],
        "click":   [("browse", 50), ("search", 30), ("view",   10), ("upload", 10)],
        "config":  [("browse", 60), ("view",   30), ("search", 10)],
        "retrain": [("browse", 70), ("view",   10), ("search", 20)],
        "upload":  [("browse", 70), ("view",   20), ("search", 10)],
    }

    _search_session: HttpSession | None = None

    # stored between search → click states
    _pending_query_id: str
    _pending_hits: list

    def on_start(self):
        self.client.verify = False
        self._markov_state = "browse"
        self._pending_query_id = ""
        self._pending_hits = []
        self._search_session = HttpSession(
            base_url=SEARCH_HOST,
            request_event=self.environment.events.request,
            user=self,
        )
        self._search_session.verify = False

    # ── Single task — the state machine tick ──────────────────────────────────

    @task
    def step(self):
        getattr(self, f"_do_{self._markov_state}")()
        self._markov_state = self._sample_next()

    # ── State handlers ────────────────────────────────────────────────────────

    def _do_browse(self):
        offset = random.randrange(0, max(TOTAL_PHOTOS - PAGE_SIZE, PAGE_SIZE), PAGE_SIZE)
        self.client.get(
            "/api/v1/photos",
            params={
                "count": PAGE_SIZE, "offset": offset, "merged": "true",
                "order": "newest", "reverse": "false", "public": "true", "quality": 3,
                "country": "", "camera": 0, "lens": 0, "label": "",
                "latlng": "", "year": 0, "month": 0, "color": "", "q": "",
            },
            headers=_SHARED_HEADERS,
            cookies=_SHARED_COOKIES,
            name="GET /api/v1/photos (browse)",
        )

    def _do_view(self):
        offset = random.randrange(0, max(TOTAL_PHOTOS - PAGE_SIZE, PAGE_SIZE), PAGE_SIZE)
        self.client.get(
            "/api/v1/photos/view",
            params={
                "count": PAGE_SIZE, "offset": offset, "merged": "true",
                "order": "newest", "reverse": "false", "public": "true", "quality": 3,
                "country": "", "camera": 0, "lens": 0, "label": "",
                "latlng": "", "year": 0, "month": 0, "color": "", "q": "",
            },
            headers=_SHARED_HEADERS,
            cookies=_SHARED_COOKIES,
            name="GET /api/v1/photos/view",
        )

    def _do_search(self):
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
                self._markov_state = "browse"   # failed search → fall back to browsing
                return
            try:
                data = resp.json()
            except Exception:
                resp.failure("search: invalid JSON")
                self._markov_state = "browse"
                return
            resp.success()

        self._pending_query_id = data.get("query_id", "")
        self._pending_hits = data.get("hits", [])[:CLICK_TOP_N]

        # no results → nothing to click, skip ahead to browse
        if not self._pending_hits:
            self._markov_state = "browse"

    def _do_click(self):
        # guard: click state entered without a prior successful search
        if not self._pending_hits:
            self._markov_state = "browse"
            return

        for hit in self._pending_hits:
            with self._search_session.post(
                "/click",
                json={"query_id": self._pending_query_id, "image_id": hit["image_id"]},
                headers={"Content-Type": "application/json"},
                name="/click",
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"click HTTP {resp.status_code}")
                else:
                    resp.success()

        self._pending_hits = []   # consumed — next click needs a fresh search

    def _do_config(self):
        self.client.get(
            "/api/v1/config",
            headers=_SHARED_HEADERS,
            cookies=_SHARED_COOKIES,
            name="GET /api/v1/config",
        )

    def _do_retrain(self):
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

    def _do_upload(self):
        if not _image_files or not _claim_upload_slot():
            # no images, or another user already owns this window — skip quietly
            self._markov_state = "browse"
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

    def _sample_next(self) -> str:
        options = self._TRANSITIONS[self._markov_state]
        states, weights = zip(*options)
        return random.choices(states, weights=weights, k=1)[0]


# ── Load shape ────────────────────────────────────────────────────────────────
class MarkovStepShape(LoadTestShape):
    """
    6 steps of 30 s each → 200 users (3 min ramp), then ~3 h 47 min
    sustained at 200, then 10 min cool-down. Total: 4 hours.

    Cumulative time  Users   Step size  Stage
    ───────────────  ──────  ─────────  ──────────────────
          0 –    30      5      +5     step 1
         30 –    60     25     +20     step 2
         60 –    90     50     +25     step 3
         90 –   120    100     +50     step 4
        120 –   150    150     +50     step 5
        150 –   180    200     +50     step 6 (ramp complete)
        180 – 13800    200             sustained peak (~3 h 47 min)
      13800 – 14400      0             cool-down (10 min)
    """

    stages = [
        {"duration":    30, "users":   5, "spawn_rate":   5},  # step 1
        {"duration":    60, "users":  25, "spawn_rate":   5},  # step 2  +20
        {"duration":    90, "users":  2, "spawn_rate":   5},  # step 3  +25
        {"duration":   120, "users": 30, "spawn_rate":  10},  # step 4  +50
        {"duration": 13800, "users": 60, "spawn_rate":  10},  # sustained peak
        {"duration": 14400, "users":   0, "spawn_rate":  50},  # cool-down
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None
