import asyncio
import os
import contextlib
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

try:
    import requests  # Used to pull ads list from Ad Manager
except Exception:
    requests = None
import sqlite3
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse


app = FastAPI()
AD_MANAGER_URL = os.environ.get("AD_MANAGER_URL")  # e.g., http://<jetson-ip>:5002

# Track connected websocket clients for broadcast messages
_ws_clients = set()

ALLOWED_EXTENSIONS = {
    'images': {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'},
    'videos': {'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv'}
}

def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in (ALLOWED_EXTENSIONS['images'] | ALLOWED_EXTENSIONS['videos'])


def get_db():
    conn = sqlite3.connect("camera_analytics.db")
    conn.row_factory = sqlite3.Row
    return conn

def ensure_db_schema():
    """Ensure auxiliary tables used by the dashboard exist."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ad_plays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id TEXT NOT NULL,
                start_ts REAL NOT NULL,
                end_ts REAL,
                duration_sec REAL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

# Initialize schema on import
ensure_db_schema()


def query_summary(window: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if window.endswith("h"):
        hours = int(window[:-1])
        start = now - timedelta(hours=hours)
    elif window.endswith("d"):
        days = int(window[:-1])
        start = now - timedelta(days=days)
    else:
        start = now - timedelta(hours=1)
    since_ts = start.timestamp()

    conn = get_db()
    cur = conn.cursor()
    # Processing and gender stats from analytics; use NEW gender counts (windowed)
    cur.execute(
        """
        SELECT
            COUNT(*) AS samples,
            COALESCE(SUM(new_unique_faces), 0) AS unique_faces_sum,
            MAX(unique_tracked_persons) AS unique_tracked_persons_max,
            COALESCE(SUM(new_gender_male), 0) AS male_sum,
            COALESCE(SUM(new_gender_female), 0) AS female_sum,
            COALESCE(SUM(new_gender_unknown), 0) AS unknown_sum,
            COALESCE(SUM(processing_time_ms), 0) AS sum_processing_ms
        FROM analytics
        WHERE ts >= ?
        """,
        (since_ts,),
    )
    row = cur.fetchone()

    # Footfall should reflect the selected window: count arrivals in the window
    cur.execute(
        """
        SELECT COUNT(*)
        FROM presence_log
        WHERE start_ts >= ?
        """,
        (since_ts,),
    )
    footfall_sessions = int((cur.fetchone() or [0])[0] or 0)
    # Fallback: if no presence sessions exist in the window, estimate arrivals from analytics for the window
    if footfall_sessions == 0:
        cur.execute(
            """
            SELECT COALESCE(SUM(new_unique_persons), 0)
            FROM analytics
            WHERE ts >= ?
            """,
            (since_ts,),
        )
        footfall_sessions = int((cur.fetchone() or [0])[0] or 0)
    conn.close()

    samples = int(row[0] or 0)
    sum_ms = float(row[6] or 0.0)
    avg_ms = (sum_ms / samples) if samples > 0 else 0.0
    est_fps = (1000.0 / avg_ms) if avg_ms > 0 else 0.0

    return {
        "samples": samples,
        "footfall": int(footfall_sessions or 0),
        "faces": int(row[1] or 0),
        "unique_tracked_persons": int(row[2] or 0),
        "gender": {
            "male": int(row[3] or 0),
            "female": int(row[4] or 0),
            "unknown": int(row[5] or 0),
        },
        "avg_processing_ms": avg_ms,
        "estimated_fps": est_fps,
    }


def query_footfall_series(window: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if window.endswith("h"):
        hours = int(window[:-1])
        start = now - timedelta(hours=hours)
        bucket_seconds = 60
    elif window.endswith("d"):
        days = int(window[:-1])
        start = now - timedelta(days=days)
        bucket_seconds = 300
    else:
        start = now - timedelta(hours=1)
        bucket_seconds = 60
    since_ts = start.timestamp()

    conn = get_db()
    cur = conn.cursor()
    rows: list = []
    try:
        # Prefer presence starts for arrivals per bucket
        cur.execute(
            """
            SELECT CAST(start_ts / ? AS INTEGER) * ? AS bucket, COUNT(*) AS cnt
            FROM presence_log
            WHERE start_ts >= ?
            GROUP BY bucket
            ORDER BY bucket
            """,
            (bucket_seconds, bucket_seconds, since_ts),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    # Fallback: if presence is sparse, use analytics new_unique_persons per bucket
    if not rows:
        cur.execute(
            """
            SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, COALESCE(SUM(new_unique_persons), 0) AS cnt
            FROM analytics
            WHERE ts >= ?
            GROUP BY bucket
            ORDER BY bucket
            """,
            (bucket_seconds, bucket_seconds, since_ts),
        )
        rows = cur.fetchall()
    conn.close()

    # Convert per-bucket counts into a cumulative series that never decreases
    cum = 0
    series = []
    for (b, c) in rows:
        cnt = int(c or 0)
        cum += cnt
        series.append({"t": int(b), "count": cum})
    return {"bucketSeconds": bucket_seconds, "series": series}


def query_gender_series(window: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if window.endswith("h"):
        hours = int(window[:-1])
        start = now - timedelta(hours=hours)
        bucket_seconds = 60
    elif window.endswith("d"):
        days = int(window[:-1])
        start = now - timedelta(days=days)
        bucket_seconds = 300
    else:
        start = now - timedelta(hours=1)
        bucket_seconds = 60
    since_ts = start.timestamp()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT CAST(ts / ? AS INTEGER) * ? AS bucket,
               COALESCE(SUM(new_gender_male), 0) AS m,
               COALESCE(SUM(new_gender_female), 0) AS f,
               COALESCE(SUM(new_gender_unknown), 0) AS u
        FROM analytics
        WHERE ts >= ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        (bucket_seconds, bucket_seconds, since_ts),
    )
    rows = cur.fetchall()
    conn.close()

    series = [
        {"t": int(b), "male": int(m or 0), "female": int(f or 0), "unknown": int(u or 0)}
        for (b, m, f, u) in rows
    ]
    return {"bucketSeconds": bucket_seconds, "series": series}


def query_age_summary(window: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if window.endswith("h"):
        hours = int(window[:-1])
        start = now - timedelta(hours=hours)
    elif window.endswith("d"):
        days = int(window[:-1])
        start = now - timedelta(days=days)
    else:
        start = now - timedelta(hours=1)
    since_ts = start.timestamp()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
          SUM(CASE WHEN age BETWEEN 0 AND 15 THEN 1 ELSE 0 END) AS child,
          SUM(CASE WHEN age BETWEEN 16 AND 40 THEN 1 ELSE 0 END) AS young_adult,
          SUM(CASE WHEN age > 40 THEN 1 ELSE 0 END) AS adult,
          SUM(CASE WHEN age IS NULL OR age < 0 THEN 1 ELSE 0 END) AS unknown
        FROM presence_log
        WHERE end_ts >= ?
        """,
        (since_ts,),
    )
    row = cur.fetchone()
    conn.close()
    return {
        "child": int((row or [0, 0, 0, 0])[0] or 0),
        "young_adult": int((row or [0, 0, 0, 0])[1] or 0),
        "adult": int((row or [0, 0, 0, 0])[2] or 0),
        "unknown": int((row or [0, 0, 0, 0])[3] or 0),
    }


def query_presence_summary(window: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if window.endswith("h"):
        hours = int(window[:-1])
        start = now - timedelta(hours=hours)
    elif window.endswith("d"):
        days = int(window[:-1])
        start = now - timedelta(days=days)
    else:
        start = now - timedelta(hours=1)
    since_ts = start.timestamp()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(SUM(duration_sec), 0.0) AS total_sec,
               COUNT(*) AS sessions
        FROM presence_log
        WHERE end_ts >= ?
        """,
        (since_ts,),
    )
    row = cur.fetchone()

    cur.execute(
        """
        SELECT track_id, COALESCE(SUM(duration_sec), 0.0) AS total_sec
        FROM presence_log
        WHERE end_ts >= ?
        GROUP BY track_id
        ORDER BY total_sec DESC
        LIMIT 5
        """,
        (since_ts,),
    )
    top = [{"track_id": int(r[0]), "total_sec": float(r[1])} for r in cur.fetchall()]
    conn.close()

    return {
        "total_presence_sec": float(row[0] or 0.0),
        "sessions": int(row[1] or 0),
        "top_track_ids": top,
    }


def query_presence_stats(window: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if window.endswith("h"):
        hours = int(window[:-1])
        start = now - timedelta(hours=hours)
    elif window.endswith("d"):
        days = int(window[:-1])
        start = now - timedelta(days=days)
    else:
        start = now - timedelta(hours=1)
    since_ts = start.timestamp()
    now_ts = now.timestamp()

    conn = get_db()
    cur = conn.cursor()

    # Histogram bins
    cur.execute(
        """
        SELECT
          SUM(CASE WHEN duration_sec < 5 THEN 1 ELSE 0 END),
          SUM(CASE WHEN duration_sec >= 5 AND duration_sec < 15 THEN 1 ELSE 0 END),
          SUM(CASE WHEN duration_sec >= 15 AND duration_sec < 30 THEN 1 ELSE 0 END),
          SUM(CASE WHEN duration_sec >= 30 AND duration_sec < 60 THEN 1 ELSE 0 END),
          SUM(CASE WHEN duration_sec >= 60 AND duration_sec < 120 THEN 1 ELSE 0 END),
          SUM(CASE WHEN duration_sec >= 120 THEN 1 ELSE 0 END)
        FROM presence_log
        WHERE start_ts >= ? AND start_ts <= ?
        """,
        (since_ts, now_ts),
    )
    bins_row = cur.fetchone()
    bins = [int(b or 0) for b in bins_row] if bins_row else [0, 0, 0, 0, 0, 0]

    # Hour-of-day average dwell time
    cur.execute(
        """
        SELECT STRFTIME('%H', datetime(start_ts, 'unixepoch')) AS hour,
               AVG(duration_sec) AS avg_sec
        FROM presence_log
        WHERE start_ts >= ? AND start_ts <= ?
        GROUP BY hour
        ORDER BY hour
        """,
        (since_ts, now_ts),
    )
    hour_rows = cur.fetchall()
    by_hour = [{"hour": int(h), "avg_sec": float(a or 0.0)} for (h, a) in hour_rows]

    # Rolling daily mean and 7-day moving average over the last 30 days
    days_back = 30
    start_rolling = now - timedelta(days=days_back)
    cur.execute(
        """
        SELECT DATE(datetime(start_ts, 'unixepoch')) AS d,
               AVG(duration_sec) AS avg_sec
        FROM presence_log
        WHERE start_ts >= ? AND start_ts <= ?
        GROUP BY d
        ORDER BY d
        """,
        (start_rolling.timestamp(), now_ts),
    )
    daily_rows = cur.fetchall()
    conn.close()

    daily = [{"date": d, "avg_sec": float(a or 0.0)} for (d, a) in daily_rows]

    # Compute MA7 in Python
    ma7 = []
    window_vals = []
    for i, item in enumerate(daily):
        window_vals.append(item["avg_sec"])
        if len(window_vals) > 7:
            window_vals.pop(0)
        ma7.append({"date": item["date"], "avg_sec": sum(window_vals) / len(window_vals) if window_vals else 0.0})

    return {
        "histogram": {
            "bins": bins,
            "labels": ["0-5s", "5-15s", "15-30s", "30-60s", "60-120s", ">120s"],
        },
        "by_hour": by_hour,
        "rolling": {
            "daily": daily,
            "ma7": ma7,
        },
    }

def query_ad_stats(window: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if window.endswith("h"):
        hours = int(window[:-1])
        start = now - timedelta(hours=hours)
    elif window.endswith("d"):
        days = int(window[:-1])
        start = now - timedelta(days=days)
    else:
        start = now - timedelta(hours=1)
    since_ts = start.timestamp()
    # Sliding window for viewer counts (distinct tracks recently seen)
    viewer_window_sec = 30.0

    conn = get_db()
    cur = conn.cursor()
    rows = []
    play_map = {}
    try:
        # Saved totals per ad for selected window: sum of new_gender_* since window start
        cur.execute(
            """
            SELECT ad_id,
                   COALESCE(SUM(new_gender_male), 0) AS male,
                   COALESCE(SUM(new_gender_female), 0) AS female,
                   COALESCE(SUM(new_gender_unknown), 0) AS unknown
            FROM analytics
            WHERE ts >= ? AND ad_id IS NOT NULL AND ad_id <> ''
            GROUP BY ad_id
            """,
            (since_ts,)
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    try:
        cur.execute(
            """
            SELECT ad_id,
                   COUNT(*) AS plays,
                   COALESCE(SUM(duration_sec), 0.0) AS total_sec
            FROM ad_plays
            WHERE end_ts >= ?
            GROUP BY ad_id
            """,
            (since_ts,),
        )
        play_map = {r[0]: {"plays": int(r[1] or 0), "total_sec": float(r[2] or 0.0)} for r in cur.fetchall()}
    except Exception:
        play_map = {}
    conn.close()

    stats = []
    for r in rows:
        ad_id = r[0]
        male = int(r[1] or 0)
        female = int(r[2] or 0)
        unknown = int(r[3] or 0)
        stats.append({
            "ad_id": ad_id,
            "viewers": male + female + unknown,
            "male": male,
            "female": female,
            "unknown": unknown,
            "plays": play_map.get(ad_id, {}).get("plays", 0),
            "total_sec": play_map.get(ad_id, {}).get("total_sec", 0.0),
        })
    # Fallback: include analytics-only ads (no presence yet) with viewers estimated from new_unique_persons
    try:
        cur.execute(
            """
            SELECT ad_id, COALESCE(SUM(new_unique_persons), 0) AS viewers
            FROM analytics
            WHERE ts >= ? AND ad_id IS NOT NULL AND ad_id <> ''
            GROUP BY ad_id
            """,
            (since_ts,)
        )
        est = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
        # Merge estimates where viewers are 0
        for s in stats:
            if s["viewers"] == 0:
                s["viewers"] = est.get(s["ad_id"], 0)
    except Exception:
        pass
    # Ensure newly uploaded ads appear even without analytics yet
    try:
        files = []
        # Prefer pulling from Ad Manager if configured
        if AD_MANAGER_URL and requests:
            with contextlib.suppress(Exception):
                resp = requests.get(f"{AD_MANAGER_URL}/api/ads", timeout=2.0)
                j = resp.json() if resp and resp.ok else {}
                if j.get("success"):
                    files = [a.get("filename") for a in j.get("ads", []) or [] if a.get("filename")]
        # Fallback to local advertisement folder
        if not files:
            ads_dir = 'advertisement'
            if os.path.isdir(ads_dir):
                files = sorted([f for f in os.listdir(ads_dir) if allowed_file(f)])
        have = set([s["ad_id"] for s in stats])
        for f in files:
            if f not in have and allowed_file(f):
                stats.append({
                    "ad_id": f,
                    "viewers": 0,
                    "current_viewers": 0,
                    "window_viewers": 0,
                    "male": 0,
                    "female": 0,
                    "unknown": 0,
                    "plays": 0,
                    "total_sec": 0.0,
                })
        # Sort stats by filename for stable UI ordering
        stats = sorted(stats, key=lambda x: str(x.get("ad_id") or ""))
    except Exception:
        pass
    return {"stats": stats}

def query_current_ad() -> Dict[str, Any]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ad_id, start_ts FROM ad_plays
        WHERE end_ts IS NULL
        ORDER BY start_ts DESC
        LIMIT 1
        """
    )
    r = cur.fetchone()
    conn.close()
    if not r:
        return {"ad_id": None}
    return {"ad_id": r[0]}


# Removed dummy data seeding and clearing endpoints per request


@app.get("/")
async def index():
    return HTMLResponse(HTML)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    window = "1h"
    try:
        # Initial push
        payload = {
            "type": "snapshot",
            "summary": query_summary(window),
            "footfall": query_footfall_series(window),
            "gender": query_gender_series(window),
            "age": query_age_summary(window),
            "presence": query_presence_summary(window),
            "presence_stats": query_presence_stats(window),
            "window": window,
        }
        await ws.send_json(payload)

        # Listen for window change from client and periodically push updates
        async def sender():
            while True:
                await asyncio.sleep(2.0)
                upd = {
                    "type": "update",
                    "summary": query_summary(window),
                    "footfall": query_footfall_series(window),
                    "gender": query_gender_series(window),
                    "age": query_age_summary(window),
                    "presence": query_presence_summary(window),
                    "presence_stats": query_presence_stats(window),
                }
                await ws.send_json(upd)

        sender_task = asyncio.create_task(sender())
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "set_window":
                    new_w = str(msg.get("window") or "1h")
                    window = new_w
        finally:
            sender_task.cancel()
            with contextlib.suppress(Exception):
                await sender_task
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            _ws_clients.discard(ws)

@app.get("/api/ad_stats")
async def api_ad_stats(window: str = "1h"):
    return query_ad_stats(window)

@app.get("/api/current_ad")
async def api_current_ad():
    return query_current_ad()

@app.post("/api/reset")
async def api_reset():
    conn = get_db()
    try:
        cur = conn.cursor()
        # Clear analytics, presence, ad plays, and recent tracks
        with contextlib.suppress(Exception):
            cur.execute("DELETE FROM analytics")
        with contextlib.suppress(Exception):
            cur.execute("DELETE FROM presence_log")
        with contextlib.suppress(Exception):
            cur.execute("DELETE FROM ad_plays")
        with contextlib.suppress(Exception):
            cur.execute("DELETE FROM recent_tracks")
        conn.commit()
        return {"status": "ok", "message": "All dashboard data cleared."}
    finally:
        conn.close()

@app.post("/api/notify_ad_change")
async def api_notify_ad_change():
    # Broadcast a lightweight WS event so clients refresh ads immediately
    dead = []
    for client in list(_ws_clients):
        try:
            await client.send_json({"type": "ad_changed"})
        except Exception:
            dead.append(client)
    for d in dead:
        with contextlib.suppress(Exception):
            _ws_clients.discard(d)
    return {"status": "ok"}


HTML = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Camera Analytics -  Dashboard </title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
      body { font-family: Arial, sans-serif; margin: 16px; background: #0f1116; color: #e6e6e6; }
      .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
      .card { background: #151823; border-radius: 10px; padding: 16px; }
      h1 { margin: 0 0 12px 0; font-size: 22px; }
      h2 { margin: 0 0 8px 0; font-size: 18px; }
      .toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
      select { background: #0c0f16; color: #e6e6e6; border: 1px solid #2a2f3a; padding: 6px 10px; border-radius: 6px; }
      .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
      .kpi { background: #0c0f16; border-radius: 8px; padding: 12px; text-align: center; }
      .kpi .num { font-size: 26px; font-weight: 700; color: #4bd1ff; }
      .kpi .label { font-size: 12px; color: #9aa4b2; }
      .note { font-size: 12px; color: #9aa4b2; margin-top: 6px; }
      canvas { max-height: 360px; }
      /* Layout rows */
      .row { display: grid; gap: 16px; }
      .row-1col { grid-template-columns: 1fr; }
      .row-2col { grid-template-columns: repeat(2, minmax(320px, 1fr)); }
      .row-3col { grid-template-columns: repeat(3, minmax(280px, 1fr)); }
      /* Medium chart height for specific plots */
      .canvas-medium { max-height: 280px; }
      .list { list-style: none; padding-left: 0; }
      .list li { padding: 6px 8px; background: #0c0f16; margin-bottom: 6px; border-radius: 6px; }
      /* Ad performance */
      .ad-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
      .ad-card { background: linear-gradient(135deg, #141a24 0%, #0c1017 100%); border: 1px solid #222838; border-radius: 10px; padding: 14px; }
      .ad-title { font-size: 16px; font-weight: 700; margin-bottom: 6px; color: #8ad1ff; }
      .chip { display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 12px; margin-right: 6px; }
      .chip-primary { background: rgba(75,209,255,0.15); color: #4bd1ff; border: 1px solid #2aa7d1; }
      .chip-male { background: rgba(75,209,255,0.12); color: #4bd1ff; border: 1px solid #2aa7d1; }
      .chip-female { background: rgba(255,111,177,0.12); color: #ff6fb1; border: 1px solid #d85593; }
      .chip-unknown { background: rgba(154,164,178,0.12); color: #9aa4b2; border: 1px solid #7f8895; }
    </style>
  </head>
  <body>
    <div class="toolbar">
      <h1>Dual Dashboard (Live)</h1>
      <div style="display:flex;gap:8px;align-items:center;">
        <label>Window:</label>
        <select id="window">
          <option value="1h" selected>Last 1h</option>
          <option value="6h">Last 6h</option>
          <option value="24h">Last 24h</option>
          <option value="7d">Last 7d</option>
        </select>
        <button id="resetBtn" style="background:#c62828;color:#fff;border:none;padding:6px 10px;border-radius:6px;cursor:pointer;">Reset Data</button>
      </div>
    </div>
    <!-- Row 1: Key metrics full width (12 columns equivalent) -->
    <div class="row row-1col">
      <div class="card">
        <h2>Key Metrics</h2>
        <div class="kpis">
          <div class="kpi"><div class="num" id="kpi-footfall">0</div><div class="label">Footfall</div></div>
          <div class="kpi"><div class="num" id="kpi-avg">0</div><div class="label">Average Presence Time (s)</div></div>
          <div class="kpi"><div class="num" id="kpi-fps">0</div><div class="label">System Speed (FPS)</div></div>
        </div>
      </div>
    </div>

    <!-- Row 2: Gender and Age (medium height) in one complete row -->
    <div class="row row-2col" style="margin-top:16px;">
      <div class="card">
        <h2>Gender Distribution</h2>
        <canvas id="genderPie" class="canvas-medium"></canvas>
        <p class="note">Breakdown of detected genders in the selected period.</p>
      </div>
      <div class="card">
        <h2>Age Distribution</h2>
        <canvas id="agePie" class="canvas-medium"></canvas>
        <p class="note">Breakdown of estimated age groups in the selected period.</p>
      </div>
    </div>

    <!-- Row 3: The other three plots in a single row -->
    <div class="row row-3col" style="margin-top:16px;">
      <div class="card">
        <h2>Visitors Over Time</h2>
        <canvas id="footfallLine"></canvas>
        <p class="note">Counts of  visitors detected in each time block.</p>
      </div>
      <div class="card">
        <h2>Visit Length Distribution</h2>
        <canvas id="presenceHist"></canvas>
        <p class="note">How many visits fall into each length band.</p>
      </div>
      <div class="card">
        <h2>Peak Hours</h2>
        <canvas id="hourTrend"></canvas>
        <p class="note">Peak hours in front of the camera.</p>
      </div>
    </div>

    <div class="card" style="margin-top:16px;">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <h2>Ad Performance</h2>
        <div class="note" id="currentAd">Current Ad: —</div>
      </div>
      <div class="ad-grid" id="adGrid"></div>
    </div>

    <script>
      const wnd = document.getElementById('window');
      let ws;
      let footfallChart, genderPieChart, agePieChart, presenceHistChart, hourTrendChart;

      function fmtTs(t){
        const d = new Date(t * 1000);
        return d.toLocaleTimeString();
      }

      function ensureCharts(){
        if (!footfallChart){
          footfallChart = new Chart(document.getElementById('footfallLine'), {
            type: 'line',
            data: { labels: [], datasets: [{ label: 'Visitors', data: [], borderColor: '#4bd1ff', backgroundColor: 'rgba(75,209,255,0.25)', fill: true, tension: 0.3 }] },
            options: { plugins: { legend: { labels: { color: '#e6e6e6' } } }, scales: { x: { ticks: { color: '#9aa4b2' } }, y: { ticks: { color: '#9aa4b2' } } } }
          });
        }
        if (!genderPieChart){
          genderPieChart = new Chart(document.getElementById('genderPie'), {
            type: 'pie',
            data: { labels: ['Male', 'Female', 'Unknown'], datasets: [{ data: [0,0,0], backgroundColor: ['#4bd1ff','#ff6fb1','#9aa4b2'] }] },
            options: { plugins: { legend: { labels: { color: '#e6e6e6' } } } }
          });
        }
        if (!agePieChart){
          agePieChart = new Chart(document.getElementById('agePie'), {
                type: 'pie',
                data: {
                    labels: ['Child (0-15)', 'Young Adult (16-40)', 'Adult (41+)', 'Unknown'],
                    datasets: [{ data: [0,0,0,0], backgroundColor: ['#ffd54f','#4bd1ff','#a2ff6f','#9aa4b2'] }]
                },
                options: { plugins: { legend: { labels: { color: '#e6e6e6' } } } }
                });
        }
        if (!presenceHistChart){
          presenceHistChart = new Chart(document.getElementById('presenceHist'), {
            type: 'bar',
            data: { labels: [], datasets: [{ label: 'Visits', data: [], backgroundColor: 'rgba(75,209,255,0.5)', borderColor: '#4bd1ff' }] },
            options: { plugins: { legend: { labels: { color: '#e6e6e6' } } }, scales: { x: { ticks: { color: '#9aa4b2' } }, y: { ticks: { color: '#9aa4b2' } } } }
          });
        }
        if (!hourTrendChart){
          hourTrendChart = new Chart(document.getElementById('hourTrend'), {
            type: 'line',
            data: { labels: [], datasets: [{ label: 'Avg Presence Time (sec)', data: [], borderColor: '#a2ff6f', backgroundColor: 'rgba(162,255,111,0.25)', fill: true, tension: 0.3 }] },
            options: { plugins: { legend: { labels: { color: '#e6e6e6' } } }, scales: { x: { ticks: { color: '#9aa4b2' } }, y: { ticks: { color: '#9aa4b2' } } } }
          });
        }
      }
    function renderAgePie(a){
        const vals = [a.child||0, a.young_adult||0, a.adult||0, a.unknown||0];
        agePieChart.data.datasets[0].data = vals;
        agePieChart.update();
        }
      function updateKPIs(sum, presence){
        const total = Math.round(presence.total_presence_sec||0);
        const sessions = Math.round(presence.sessions||0);
        const avg = sessions > 0 ? Math.round(total / sessions) : 0;
        document.getElementById('kpi-footfall').textContent = sum.footfall||0;
        document.getElementById('kpi-avg').textContent = avg;
        document.getElementById('kpi-fps').textContent = (sum.estimated_fps||0).toFixed(1);
      }

      function renderFootfall(series){
        const labels = series.map(p => fmtTs(p.t));
        const data = series.map(p => p.count);
        footfallChart.data.labels = labels;
        footfallChart.data.datasets[0].data = data;
        footfallChart.update();
      }

      function renderGenderPie(g){
        genderPieChart.data.datasets[0].data = [g.male||0, g.female||0, g.unknown||0];
        genderPieChart.update();
      }

      agePieChart = new Chart(document.getElementById('agePie'), {
            type: 'pie',
            data: {
                labels: ['Child (0-15)', 'Young Adult (16-40)', 'Adult (41+)', 'Unknown'],
                datasets: [{ data: [0,0,0,0], backgroundColor: ['#ffd54f','#4bd1ff','#a2ff6f','#9aa4b2'] }]
            },
            options: { plugins: { legend: { labels: { color: '#e6e6e6' } } } }
            });

      // Simplified: focus on presence stats only

      function renderPresenceStats(stats){
        // Histogram
        const labels = (stats.histogram && stats.histogram.labels) || [];
        const bins = (stats.histogram && stats.histogram.bins) || [];
        presenceHistChart.data.labels = labels;
        presenceHistChart.data.datasets[0].data = bins;
        presenceHistChart.update();

        // Hour-of-day
        const hours = (stats.by_hour || []).map(h => h.hour);
        const hourData = (stats.by_hour || []).map(h => (h.avg_sec||0));
        hourTrendChart.data.labels = hours.map(h => `${h}:00`);
        hourTrendChart.data.datasets[0].data = hourData;
        hourTrendChart.update();
      }

      function fmtDuration(sec){
        const s = Math.round(sec||0);
        const m = Math.floor(s/60);
        const r = s%60;
        return `${m}m ${r}s`;
      }

      function renderAdStats(list){
        const grid = document.getElementById('adGrid');
        grid.innerHTML = '';
        (list||[]).forEach(item => {
          const div = document.createElement('div');
          div.className = 'ad-card';
          div.innerHTML = `
            <div class="ad-title">${item.ad_id || 'Unknown Ad'}</div>
            <div style="margin-bottom:8px;">
              <span class="chip chip-primary">Visitors: ${item.viewers||0}</span>
              <span class="chip chip-male">Male: ${item.male||0}</span>
              <span class="chip chip-female">Female: ${item.female||0}</span>
              <span class="chip chip-unknown">Unknown: ${item.unknown||0}</span>
            </div>
            <div class="note">Plays: ${item.plays||0} • Total Time: ${fmtDuration(item.total_sec||0)}</div>
          `;
          grid.appendChild(div);
        });
      }

      function connect(){
        ensureCharts();
        ws = new WebSocket(`ws://${location.host}/ws`);
        ws.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'snapshot' || msg.type === 'update'){
              updateKPIs(msg.summary, msg.presence);
              renderFootfall(msg.footfall.series||[]);
              const gsum = msg.summary.gender||{male:0,female:0,unknown:0};
              renderGenderPie(gsum);
              if (msg.age){
                renderAgePie(msg.age);
              }
              if (msg.presence_stats){
                renderPresenceStats(msg.presence_stats);
              }
            } else if (msg.type === 'ad_changed') {
              // Immediately refresh ads when Ad Manager notifies a change
              refreshAds();
            }
          } catch (_) {}
        };
        ws.onclose = () => setTimeout(connect, 1000);
      }

      wnd.addEventListener('change', () => {
        const v = wnd.value;
        ws && ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: 'set_window', window: v }));
      });

      // Dummy actions removed per request

      async function refreshAds(){
        try {
          const w = wnd.value;
          const ads = await fetch(`/api/ad_stats?window=${w}`);
          const list = (await ads.json()).stats||[];
          renderAdStats(list);
          const cur = await fetch(`/api/current_ad`);
          const curJ = await cur.json();
          document.getElementById('currentAd').textContent = `Current Ad: ${curJ.ad_id || '—'}`;
        } catch (_) {}
      }

      async function resetData(){
        try {
          await fetch('/api/reset', { method: 'POST' });
          // Brief delay to allow reload of charts from empty state
          setTimeout(() => {
            refreshAds();
          }, 250);
        } catch (_) {}
      }

      setInterval(refreshAds, 3000);
      refreshAds();

      document.getElementById('resetBtn').addEventListener('click', resetData);

      connect();
    </script>
  </body>
</html>
"""