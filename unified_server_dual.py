#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified analytics server  (MiVOLO  |  light DeepFace)
– TCP 12350  (default)
– optional --show   : pop-up server-side preview with bbox + ID + gender + AGE + dwell
– age is estimated ONCE per track-ID and stored in DB when the person leaves
"""

import argparse
import asyncio
import cv2
import json
import numpy as np
import sqlite3
import struct
import time
import torch
from collections import deque
from typing import Dict, Optional, Tuple, List, Any

# ----------  speed tweaks  ----------
torch.set_grad_enabled(False)
if torch.cuda.is_available():
    torch.backends.cudnn.benchmark = True
# ------------------------------------


# ============================================================================
#  Base
# ============================================================================
class BaseEngine:
    def process(self, frame_bgr: np.ndarray) -> Dict[str, Any]:
        raise NotImplementedError


# ============================================================================
#  MiVOLO engine  (full)
# ============================================================================
class MivoloEngine(BaseEngine):
    def __init__(self):
        from main import Predictor  # local project file
        from ultralytics import YOLO

        device = "cuda" if torch.cuda.is_available() else "cpu"

        class Config:
            def __init__(self, **entries):
                self.__dict__.update(entries)

        args = {
            "detector_weights": "models/yolov8x_person_face.pt",
            "checkpoint": "models/model_imdb_cross_person_4.22_99.46.pth.tar",
            "device": device,
            "with_persons": True,
            "draw": False,
            "disable_faces": False,
            "conf_thresh": 0.35,
            "iou_thresh": 0.55,
            "max_det": 50,
            "use_grayscale": False,
            "resize_input": False,
            "input_size": 640,
        }
        self._config = Config(**args)
        self._predictor = Predictor(self._config, verbose=False)

        # lightweight tracker
        self._tracker = YOLO("yolo11n.pt")

        # -----------  preview  -------------
        self._show_preview = False
        self._window_name = "Server preview – ID / gender / age / dwell"
        self._window_created = False
        # ------------------------------------

        self._seen_track_id_to_gender: Dict[int, str] = {}
        self._seen_track_id_to_age: Dict[int, Optional[float]] = {}   # age cache
        self._recent_face_events: List[Tuple] = []
        self._face_event_ttl_sec = 10.0
        self._proc_ms_window: deque = deque(maxlen=60)

        # presence
        self._presence: Dict[int, Dict[str, float]] = {}
        self._presence_ttl_sec = 2.0

    # ------------------------------------------------------------------
    def _draw_preview(self, frame_bgr: np.ndarray, analytics: dict) -> np.ndarray:
        if not self._show_preview:
            return frame_bgr

        out = frame_bgr.copy()
        h, w = out.shape[:2]
        now = time.time()
        font = cv2.FONT_HERSHEY_SIMPLEX

        for tp in analytics.get("tracked_persons", []):
            tid = tp.get("id")
            if tid is None:
                continue
            x, y, bw, bh = tp["bbox"]  # norm
            x1 = int(x * w)
            y1 = int(y * h)
            x2 = int((x + bw) * w)
            y2 = int((y + bh) * h)

            rec   = self._presence.get(tid)
            dwell = int(now - rec["start"]) if rec else 0
            gender = self._seen_track_id_to_gender.get(tid, "unknown")
            age    = self._seen_track_id_to_age.get(tid)              # age
            age_str = f"{age:.0f}y" if age is not None else "--y"

            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 200, 255), 2)
            label = f"ID:{tid}  {gender}  {age_str}  {dwell}s"
            cv2.putText(out, label, (x1, max(y1 - 6, 15)), font, 0.55, (0, 200, 255), 2)
        return out

    # ------------------------------------------------------------------
    def process(self, frame_bgr: np.ndarray) -> Dict[str, Any]:
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        start = time.time()
        detected_objects, _ = self._predictor.recognize(frame_rgb)
        processing_ms = (time.time() - start) * 1000.0
        self._proc_ms_window.append(processing_ms)

        analytics: Dict[str, Any] = {
            "timestamp": time.time(),
            "processing_time_ms": processing_ms,
            "total_persons": int(getattr(detected_objects, "n_persons", 0)),
            "total_faces": int(getattr(detected_objects, "n_faces", 0)),
            "person_detections": [],
            "face_detections": [],
        }

        # ----------  parse MiVOLO outputs  ----------
        try:
            boxes = detected_objects.yolo_results.boxes
            classes = boxes.cls.cpu().numpy().astype(int) if hasattr(boxes, "cls") else []
            orig_h, orig_w = detected_objects.yolo_results.orig_shape

            mivolo_person_boxes_xyxy: List[List[float]] = []
            mivolo_person_genders: List[str] = []
            mivolo_person_ages: List[Optional[float]] = []

            for idx in range(len(boxes)):
                cls_id = int(classes[idx]) if idx < len(classes) else 0
                xyxy = boxes.xyxy[idx].cpu().numpy() if hasattr(boxes, "xyxy") else None
                conf = float(boxes.conf[idx].cpu().numpy()) if hasattr(boxes, "conf") else 0.0
                if xyxy is None:
                    continue
                bbox_norm = [
                    float(xyxy[0] / orig_w),
                    float(xyxy[1] / orig_h),
                    float((xyxy[2] - xyxy[0]) / orig_w),
                    float((xyxy[3] - xyxy[1]) / orig_h),
                ]

                age = None
                gender = "unknown"
                if hasattr(detected_objects, "ages") and idx < len(detected_objects.ages):
                    age = detected_objects.ages[idx]
                if hasattr(detected_objects, "genders") and idx < len(detected_objects.genders):
                    gender = detected_objects.genders[idx] or "unknown"

                data = {
                    "bbox": bbox_norm,
                    "age": float(age) if isinstance(age, (int, float)) else age,
                    "gender": str(gender).lower() if gender else "unknown",
                    "confidence": conf,
                    "class_id": cls_id,
                }

                if cls_id == 0:  # person
                    analytics["person_detections"].append(data)
                    mivolo_person_boxes_xyxy.append(xyxy)
                    mivolo_person_genders.append(str(gender).lower() if gender else "unknown")
                    mivolo_person_ages.append(float(age) if isinstance(age, (int, float)) else None)
                else:  # face
                    analytics["face_detections"].append(data)
        except Exception:
            pass

        # ----------  tracking + ID + gender + AGE + presence  ----------
        try:
            pre_seen_ids = set(self._seen_track_id_to_gender.keys())
            tr_results = self._tracker.track(frame_bgr, persist=True, conf=0.6, classes=[0], verbose=False)
            tr_boxes = tr_results[0].boxes if tr_results and len(tr_results) > 0 else None
            cur_ids: List[int] = []
            frame_track_gender: Dict[int, str] = {}
            frame_track_age: Dict[int, Optional[float]] = {}
            h_img, w_img = frame_bgr.shape[:2]
            tracked_persons: List[Dict] = []

            if tr_boxes is not None:
                # IoU helper
                def iou(a, b):
                    ax1, ay1, ax2, ay2 = a
                    bx1, by1, bx2, by2 = b
                    inter_x1 = max(ax1, bx1)
                    inter_y1 = max(ay1, by1)
                    inter_x2 = min(ax2, bx2)
                    inter_y2 = min(ay2, by2)
                    inter_w = max(0.0, inter_x2 - inter_x1)
                    inter_h = max(0.0, inter_y2 - inter_y1)
                    inter = inter_w * inter_h
                    a_area = max(0.0, (ax2 - ax1)) * max(0.0, (ay2 - ay1))
                    b_area = max(0.0, (bx2 - bx1)) * max(0.0, (by2 - by1))
                    union = a_area + b_area - inter + 1e-6
                    return inter / union

                for b in tr_boxes:
                    if b.id is None:
                        continue
                    tid = int(b.id)
                    cur_ids.append(tid)
                    t_xyxy = b.xyxy.squeeze().cpu().numpy().tolist()
                    tbx = [
                        float(t_xyxy[0] / w_img),
                        float(t_xyxy[1] / h_img),
                        float((t_xyxy[2] - t_xyxy[0]) / w_img),
                        float((t_xyxy[3] - t_xyxy[1]) / h_img),
                    ]

                    # best gender + age via IoU
                    best_iou, best_gender, best_age = 0.0, None, None
                    for p_xyxy, p_gender, p_age in zip(
                            mivolo_person_boxes_xyxy,
                            mivolo_person_genders,
                            mivolo_person_ages):
                        v = iou(t_xyxy, p_xyxy)
                        if v > best_iou:
                            best_iou, best_gender, best_age = v, p_gender, p_age

                    if best_iou >= 0.3 and best_gender:
                        self._seen_track_id_to_gender[tid] = best_gender
                        self._seen_track_id_to_age[tid] = best_age
                        frame_track_gender[tid] = best_gender
                        frame_track_age[tid] = best_age
                        # first-seen log
                        if tid not in pre_seen_ids:
                            print(f"[FIRST] ID:{tid:>3}  gender:{best_gender:<7}  "
                                  f"age:{best_age if best_age is not None else '--':<4}  "
                                  f"first_seen_ts:{time.time():.3f}")
                    else:
                        self._seen_track_id_to_gender.setdefault(tid, "unknown")
                        self._seen_track_id_to_age.setdefault(tid, None)
                        if tid not in frame_track_gender:
                            frame_track_gender[tid] = "unknown"
                            frame_track_age[tid] = None

                    tracked_persons.append({
                        "id": tid,
                        "bbox": tbx,
                        "age": self._seen_track_id_to_age.get(tid)   # age shipped to client
                    })

            # gender counts
            g_m = sum(1 for g in self._seen_track_id_to_gender.values() if g == "male")
            g_f = sum(1 for g in self._seen_track_id_to_gender.values() if g == "female")
            g_u = sum(1 for g in self._seen_track_id_to_gender.values() if g not in ("male", "female"))

            analytics["current_tracked_persons"] = len(cur_ids)
            analytics["unique_tracked_persons"] = len(self._seen_track_id_to_gender)
            analytics["tracked_gender_counts"] = {"male": g_m, "female": g_f, "unknown": g_u}
            new_ids = [tid for tid in cur_ids if tid not in pre_seen_ids]
            analytics["new_unique_persons"] = len(new_ids)
            nm = sum(1 for tid in new_ids if frame_track_gender.get(tid) == "male")
            nf = sum(1 for tid in new_ids if frame_track_gender.get(tid) == "female")
            nu = len(new_ids) - nm - nf
            analytics["new_gender_counts"] = {"male": nm, "female": nf, "unknown": nu}
            analytics["tracked_persons"] = tracked_persons

            # ----------  presence events  ----------
            now_ts = time.time()
            presence_events: List[Dict] = []
            for tid in cur_ids:
                rec = self._presence.get(tid)
                if rec is None:
                    self._presence[tid] = {"start": now_ts, "last_seen": now_ts}
                else:
                    rec["last_seen"] = now_ts

            to_delete = []
            for tid, rec in self._presence.items():
                last = rec.get("last_seen", now_ts)
                if tid not in cur_ids and (now_ts - last) >= self._presence_ttl_sec:
                    start_ts = rec.get("start", last)
                    duration = max(0.0, last - start_ts)
                    presence_events.append({
                        "track_id": tid,
                        "start_ts": float(start_ts),
                        "end_ts": float(last),
                        "duration_sec": float(duration),
                    })
                    to_delete.append(tid)
            for tid in to_delete:
                self._presence.pop(tid, None)
            analytics["presence_events"] = presence_events

            # ----------  face de-duplication  ----------
            self._recent_face_events = [
                e for e in self._recent_face_events
                if (now_ts - e[1]) <= self._face_event_ttl_sec
            ]

            def iou_face(a, b):
                ax1, ay1, ax2, ay2 = a
                bx1, by1, bx2, by2 = b
                inter_x1 = max(ax1, bx1)
                inter_y1 = max(ay1, by1)
                inter_x2 = min(ax2, bx2)
                inter_y2 = min(ay2, by2)
                inter_w = max(0.0, inter_x2 - inter_x1)
                inter_h = max(0.0, inter_y2 - inter_y1)
                inter = inter_w * inter_h
                a_area = max(0.0, (ax2 - ax1)) * max(0.0, (ay2 - ay1))
                b_area = max(0.0, (bx2 - bx1)) * max(0.0, (by2 - by1))
                union = a_area + b_area - inter + 1e-6
                return inter / union

            new_unique_faces = 0
            for f_xyxy in locals().get("mivolo_face_boxes_xyxy", []) or []:
                matched = False
                for prev_xyxy, _ in self._recent_face_events:
                    if iou_face(f_xyxy, prev_xyxy) >= 0.5:
                        matched = True
                        break
                if not matched:
                    new_unique_faces += 1
                    self._recent_face_events.append((f_xyxy, now_ts))
            analytics["new_unique_faces"] = new_unique_faces
        except Exception:
            pass

        # ----------  FPS  ----------
        if len(self._proc_ms_window):
            analytics["server_avg_fps"] = 1000.0 / (sum(self._proc_ms_window) / len(self._proc_ms_window))
        else:
            analytics["server_avg_fps"] = 0.0

        # ----------  preview window  ----------
        if self._show_preview:
            vis = self._draw_preview(frame_bgr, analytics)
            if not self._window_created:
                cv2.namedWindow(self._window_name, cv2.WINDOW_NORMAL)
                self._window_created = True
            # cv2.imshow(self._window_name, vis)
            # cv2.waitKey(1)  # non-blocking

        return analytics


# ============================================================================
#  Light engine  (person counter only)
# ============================================================================
class DeepfaceLiteEngine(BaseEngine):
    def __init__(self):
        from ultralytics import YOLO
        self._yolo = YOLO("yolo11n.pt")
        self._tracked_ids = set()

    def process(self, frame_bgr: np.ndarray) -> Dict[str, Any]:
        start = time.time()
        results = self._yolo.track(frame_bgr, persist=True, conf=0.6, classes=[0], verbose=False)
        boxes = results[0].boxes if results and len(results) > 0 else []
        total_persons = 0
        pre_count = len(self._tracked_ids)
        if boxes:
            for box in boxes:
                if int(box.cls) == 0:
                    total_persons += 1
                if box.id is not None:
                    self._tracked_ids.add(int(box.id))
        processing_ms = (time.time() - start) * 1000.0
        return {
            "timestamp": time.time(),
            "processing_time_ms": processing_ms,
            "total_persons": int(total_persons),
            "total_faces": 0,
            "current_tracked_persons": int(total_persons),
            "unique_tracked_persons": len(self._tracked_ids),
            "new_unique_persons": max(0, len(self._tracked_ids) - pre_count),
            "tracked_gender_counts": {"male": 0, "female": 0, "unknown": len(self._tracked_ids)},
            "person_detections": [],
            "face_detections": [],
            "presence_events": [],
        }


# ============================================================================
#  TCP helpers
# ============================================================================
async def parse_request(reader: asyncio.StreamReader) -> Tuple[Optional[str], np.ndarray]:
    async def _readexactly(n: int) -> bytes:
        return await reader.readexactly(n)

    flags = (await _readexactly(1))[0]
    ad_id = None
    if flags & 0b00000001:
        ad_len = struct.unpack(">I", await _readexactly(4))[0]
        ad_id = (await _readexactly(ad_len)).decode("utf-8")
    frame_len = struct.unpack(">I", await _readexactly(4))[0]
    frame_bytes = await _readexactly(frame_len)
    frame = cv2.imdecode(np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR)
    return ad_id, frame


async def write_response(writer: asyncio.StreamWriter, payload: Dict[str, Any]):
    data = json.dumps(payload).encode("utf-8")
    writer.write(struct.pack(">I", len(data)) + data)
    await writer.drain()


# ============================================================================
#  Server
# ============================================================================
class UnifiedServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 12350,
        engine: str = "mivolo",
        show_preview: bool = False,
    ):
        self._host = host
        self._port = port
        engine = (engine or "mivolo").lower()
        if engine == "mivolo":
            self._engine = MivoloEngine()
            self._engine._show_preview = show_preview
        elif engine == "deepface":
            self._engine = DeepfaceLiteEngine()
        else:
            raise ValueError("engine must be one of: mivolo, deepface")

        # SQLite
        self._db = sqlite3.connect("camera_analytics.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db_lock = asyncio.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    def _init_db(self):
        cur = self._db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ad_id TEXT,
                processing_time_ms REAL,
                total_persons INTEGER,
                total_faces INTEGER,
                current_tracked_persons INTEGER,
                unique_tracked_persons INTEGER,
                gender_male INTEGER,
                gender_female INTEGER,
                gender_unknown INTEGER,
                new_unique_persons INTEGER,
                new_unique_faces INTEGER,
                new_gender_male INTEGER,
                new_gender_female INTEGER,
                new_gender_unknown INTEGER
            )
            """
        )
        for col in [
            "new_unique_persons",
            "new_unique_faces",
            "new_gender_male",
            "new_gender_female",
            "new_gender_unknown",
        ]:
            try:
                cur.execute(f"ALTER TABLE analytics ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass

        # ----------  NEW: add age column to presence_log  ----------
        try:
            cur.execute("ALTER TABLE presence_log ADD COLUMN age REAL")
        except sqlite3.OperationalError:
            pass  # column already exists
        # -----------------------------------------------------------

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS presence_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER,
                ad_id TEXT,
                start_ts REAL NOT NULL,
                end_ts REAL NOT NULL,
                duration_sec REAL NOT NULL
                ,age REAL                                      -- NEW
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recent_tracks (
                track_id INTEGER NOT NULL,
                ad_id TEXT,
                last_ts REAL NOT NULL,
                PRIMARY KEY(track_id, ad_id)
            )
            """
        )
        self._db.commit()

    # ------------------------------------------------------------------
    async def _save_analytics(self, ad_id: Optional[str], analytics: Dict[str, Any]):
        tg = analytics.get("tracked_gender_counts") or {}
        ng = analytics.get("new_gender_counts") or {}
        row = (
            float(analytics.get("timestamp") or time.time()),
            ad_id,
            float(analytics.get("processing_time_ms") or 0.0),
            int(analytics.get("total_persons") or 0),
            int(analytics.get("total_faces") or 0),
            int(analytics.get("current_tracked_persons") or 0),
            int(analytics.get("unique_tracked_persons") or 0),
            int(tg.get("male") or 0),
            int(tg.get("female") or 0),
            int(tg.get("unknown") or 0),
            int(analytics.get("new_unique_persons") or 0),
            int(analytics.get("new_unique_faces") or 0),
            int(ng.get("male") or 0),
            int(ng.get("female") or 0),
            int(ng.get("unknown") or 0),
        )
        async with self._db_lock:
            cur = self._db.cursor()
            cur.execute(
                """
                INSERT INTO analytics (
                    ts, ad_id, processing_time_ms, total_persons, total_faces,
                    current_tracked_persons, unique_tracked_persons,
                    gender_male, gender_female, gender_unknown, new_unique_persons, new_unique_faces,
                    new_gender_male, new_gender_female, new_gender_unknown
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            # ----------  NEW: save age inside presence_log  ----------
            for ev in analytics.get("presence_events", []) or []:
                tid = int(ev.get("track_id") or 0)
                age = self._engine._seen_track_id_to_age.get(tid)   # MiVOLO only
                cur.execute(
                    """
                    INSERT INTO presence_log (track_id, ad_id, start_ts, end_ts, duration_sec, age)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (tid, ad_id,
                     float(ev.get("start_ts") or 0.0),
                     float(ev.get("end_ts") or 0.0),
                     float(ev.get("duration_sec") or 0.0),
                     float(age) if age is not None else None)
                )
            # ---------------------------------------------------------

            # upsert recent tracks
            try:
                now_ts = time.time()
                for tp in analytics.get("tracked_persons") or []:
                    tid = tp.get("id")
                    if tid is None:
                        continue
                    cur.execute(
                        """
                        INSERT INTO recent_tracks (track_id, ad_id, last_ts)
                        VALUES (?, ?, ?)
                        ON CONFLICT(track_id, ad_id) DO UPDATE SET last_ts=excluded.last_ts
                        """,
                        (int(tid), ad_id, float(now_ts)),
                    )
                cur.execute("DELETE FROM recent_tracks WHERE last_ts < ?", (now_ts - 120.0,))
            except Exception:
                pass
            self._db.commit()

    # ------------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        try:
            while True:
                try:
                    ad_id, frame = await parse_request(reader)
                except asyncio.IncompleteReadError:
                    break
                except Exception:
                    break

                if frame is None:
                    await write_response(writer, {"status": "error", "message": "invalid_frame"})
                    continue

                # downscale huge frames
                h, w = frame.shape[:2]
                if max(h, w) > 1280:
                    scale = 1280.0 / max(h, w)
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

                analytics = self._engine.process(frame)
                payload = {"status": "success", "analytics": analytics}
                if ad_id is not None:
                    payload["ad_id"] = ad_id
                await write_response(writer, payload)

                # persist (best-effort)
                try:
                    await self._save_analytics(ad_id, analytics)
                except Exception:
                    pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    async def start(self):
        server = await asyncio.start_server(self._handle, self._host, self._port)
        print(f"[Server] Listening on {self._host}:{self._port}")
        async with server:
            await server.serve_forever()


# ============================================================================
#  entry
# ============================================================================
async def main():
    parser = argparse.ArgumentParser(description="Unified analytics server")
    parser.add_argument("--show", action="store_true", help="open server-side preview window")
    args = parser.parse_args()

    server = UnifiedServer(host="0.0.0.0", port=12350, engine="mivolo", show_preview=args.show)
    await server.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Server] Shut-down requested")