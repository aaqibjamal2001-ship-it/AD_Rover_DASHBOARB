import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import contextlib


app = FastAPI()


def get_db():
    conn = sqlite3.connect("camera_analytics.db")
    conn.row_factory = sqlite3.Row
    return conn


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
    cur.execute(
        """
        SELECT
            COUNT(*) AS samples,
            COALESCE(SUM(new_unique_persons), 0) AS footfall_sum,
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
    conn.close()

    samples = int(row[0] or 0)
    sum_ms = float(row[7] or 0.0)
    avg_ms = (sum_ms / samples) if samples > 0 else 0.0
    est_fps = (1000.0 / avg_ms) if avg_ms > 0 else 0.0

    return {
        "samples": samples,
        "footfall": int(row[1] or 0),
        "faces": int(row[2] or 0),
        "unique_tracked_persons": int(row[3] or 0),
        "gender": {
            "male": int(row[4] or 0),
            "female": int(row[5] or 0),
            "unknown": int(row[6] or 0),
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
    cur.execute(
        """
        SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, SUM(new_unique_persons) AS cnt
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
        {"t": int(b), "count": int(c or 0)}
        for (b, c) in rows
    ]
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


@app.get("/")
async def index():
    return HTMLResponse(HTML)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    window = "1h"
    try:
        # Initial push
        payload = {
            "type": "snapshot",
            "summary": query_summary(window),
            "footfall": query_footfall_series(window),
            "gender": query_gender_series(window),
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


HTML = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Camera Analytics - FastAPI (Live)</title>
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
      canvas { max-height: 360px; }
    </style>
  </head>
  <body>
    <div class="toolbar">
      <h1>Camera Analytics (Live)</h1>
      <div>
        <label style="margin-right:8px;">Window:</label>
        <select id="window">
          <option value="1h" selected>Last 1h</option>
          <option value="6h">Last 6h</option>
          <option value="24h">Last 24h</option>
          <option value="7d">Last 7d</option>
        </select>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h2>KPIs</h2>
        <div class="kpis">
          <div class="kpi"><div class="num" id="kpi-footfall">0</div><div class="label">Footfall</div></div>
        <!-- <div class="kpi"><div class="num" id="kpi-unique">0</div><div class="label">Unique Tracked</div></div> -->
          <div class="kpi"><div class="num" id="kpi-fps">0</div><div class="label">Estimated FPS</div></div>
        </div>
      </div>
      <div class="card">
        <h2>Gender Distribution</h2>
        <canvas id="genderPie"></canvas>
      </div>
      <div class="card">
        <h2>Footfall Over Time</h2>
        <canvas id="footfallLine"></canvas>
      </div>
    </div>

    <script>
      const wnd = document.getElementById('window');
      let ws;
      let footfallChart, genderPieChart;

      function fmtTs(t){
        const d = new Date(t * 1000);
        return d.toLocaleTimeString();
      }

      function ensureCharts(){
        if (!footfallChart){
          footfallChart = new Chart(document.getElementById('footfallLine'), {
            type: 'line',
            data: { labels: [], datasets: [{ label: 'Footfall', data: [], borderColor: '#4bd1ff', backgroundColor: 'rgba(75,209,255,0.2)', fill: true, tension: 0.3 }]},
            options: { plugins: { legend: { labels: { color: '#e6e6e6' } } }, scales: { x: { ticks: { color: '#9aa4b2' } }, y: { ticks: { color: '#9aa4b2' } } } }
          });
        }
        if (!genderPieChart){
          genderPieChart = new Chart(document.getElementById('genderPie'), {
            type: 'doughnut',
            data: { labels: ['Male', 'Female', 'Unknown'], datasets: [{ data: [0,0,0], backgroundColor: ['#4bd1ff', '#ff6fb1', '#9aa4b2'] }]},
            options: { plugins: { legend: { labels: { color: '#e6e6e6' } } } }
          });
        }
      }

      function updateSummary(sum){
        document.getElementById('kpi-footfall').textContent = sum.footfall;
        document.getElementById('kpi-unique').textContent = sum.unique_tracked_persons;
        document.getElementById('kpi-fps').textContent = (sum.estimated_fps||0).toFixed(1);
        // gender pie
        const g = sum.gender || { male:0, female:0, unknown:0 };
        genderPieChart.data.datasets[0].data = [g.male||0, g.female||0, g.unknown||0];
        genderPieChart.update('none');
      }

      function updateFootfall(series){
        const labels = series.map(p => fmtTs(p.t));
        const counts = series.map(p => p.count);
        footfallChart.data.labels = labels;
        footfallChart.data.datasets[0].data = counts;
        footfallChart.update('none');
      }

      // no gender series chart

      function connect(){
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        ws = new WebSocket(`${proto}://${location.host}/ws`);
        ws.onopen = () => {
          // send current window
          ws.send(JSON.stringify({ type: 'set_window', window: wnd.value }));
        };
        ws.onmessage = (ev) => {
          const msg = JSON.parse(ev.data);
          ensureCharts();
          if (msg.type === 'snapshot' || msg.type === 'update'){
            if (msg.summary) updateSummary(msg.summary);
            if (msg.footfall) updateFootfall(msg.footfall.series || []);
            // no gender series chart
          }
        };
        ws.onclose = () => {
            setTimeout(connect, 2000);
        };
      }

      wnd.addEventListener('change', () => {
        if (ws && ws.readyState === WebSocket.OPEN){
          ws.send(JSON.stringify({ type: 'set_window', window: wnd.value }));
        }
      });

      connect();
    </script>
  </body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("fastapi_dashboard:app", host="localhost", port=5000, reload=False)


