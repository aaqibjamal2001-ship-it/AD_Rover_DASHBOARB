import asyncio
import struct
import json
import time
from typing import Dict, Optional, Tuple, List

import cv2
import numpy as np
import torch
import sqlite3
from collections import deque

# Performance-friendly defaults for RTX 4050
torch.set_grad_enabled(False)
if torch.cuda.is_available():
    torch.backends.cudnn.benchmark = True


class BaseEngine:
    def process(self, frame_bgr: np.ndarray) -> Dict:
        raise NotImplementedError


class MivoloEngine(BaseEngine):
    def __init__(self):
        from main import Predictor  # Uses optimized Predictor in project
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
            # Tuned for speed/accuracy balance
            "conf_thresh": 0.35,
            "iou_thresh": 0.55,
            "max_det": 50,
            # Extra speed options
            "use_grayscale": False,
            "resize_input": False,
            "input_size": 640,
        }

        self._config = Config(**args)
        self._predictor = Predictor(self._config, verbose=False)
        # Lightweight YOLO for tracking IDs (persons only)
        self._tracker = YOLO("yolo11n.pt")
        self._seen_track_id_to_gender: Dict[int, str] = {}
        # Temporal cache for unique faces to avoid counting same face each frame
        self._recent_face_events: list = []  # list of tuples: (xyxy, timestamp)
        self._face_event_ttl_sec: float = 10.0
        # Rolling window for server processing FPS
        self._proc_ms_window: deque = deque(maxlen=60)

        # Presence tracking per tracked person ID
        # tid -> {"start": float, "last_seen": float}
        self._presence: Dict[int, Dict[str, float]] = {}
        # TTL to consider a person has left if not seen for this many seconds
        self._presence_ttl_sec: float = 2.0

    def process(self, frame_bgr: np.ndarray) -> Dict:
        # MiVOLO expects RGB
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        start = time.time()
        detected_objects, _ = self._predictor.recognize(frame_rgb)
        processing_ms = (time.time() - start) * 1000.0
        self._proc_ms_window.append(processing_ms)

        # Extract simple analytics
        analytics: Dict[str, any] = {
            "timestamp": time.time(),
            "processing_time_ms": processing_ms,
            "total_persons": int(getattr(detected_objects, "n_persons", 0)),
            "total_faces": int(getattr(detected_objects, "n_faces", 0)),
            "person_detections": [],
            "face_detections": [],
        }

        # Collect MiVOLO detections with genders
        try:
            boxes = detected_objects.yolo_results.boxes
            classes = boxes.cls.cpu().numpy().astype(int) if hasattr(boxes, "cls") else []
            orig_h, orig_w = detected_objects.yolo_results.orig_shape

            mivolo_person_boxes_xyxy: List[List[float]] = []
            mivolo_person_genders: List[str] = []
            mivolo_face_boxes_xyxy: List[List[float]] = []

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

                if cls_id == 0:
                    analytics["person_detections"].append(data)
                    mivolo_person_boxes_xyxy.append(xyxy)
                    mivolo_person_genders.append(str(gender).lower() if gender else "unknown")
                else:
                    analytics["face_detections"].append(data)
                    mivolo_face_boxes_xyxy.append(xyxy)
        except Exception:
            pass

        # Run tracking to get stable IDs and aggregate unique-person gender
        try:
            pre_seen_ids = set(self._seen_track_id_to_gender.keys())
            tr_results = self._tracker.track(frame_bgr, persist=True, conf=0.6, classes=[0], verbose=False)
            tr_boxes = tr_results[0].boxes if tr_results and len(tr_results) > 0 else None
            cur_ids: List[int] = []
            # Track a temporary mapping for this frame to know the best gender per tracked ID
            frame_track_gender: Dict[int, str] = {}
            h_img, w_img = frame_bgr.shape[:2]
            tracked_persons: List[Dict] = []
            if tr_boxes is not None:
                # Associate tracked boxes with MiVOLO person detections using IoU
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
                    # Normalize bbox for client drawing
                    tbx = [
                        float(t_xyxy[0] / w_img),
                        float(t_xyxy[1] / h_img),
                        float((t_xyxy[2] - t_xyxy[0]) / w_img),
                        float((t_xyxy[3] - t_xyxy[1]) / h_img),
                    ]
                    tracked_persons.append({"id": tid, "bbox": tbx})
                    # Find best IoU with MiVOLO person detections
                    best_iou, best_gender = 0.0, None
                    for p_xyxy, p_gender in zip(mivolo_person_boxes_xyxy, mivolo_person_genders):
                        v = iou(t_xyxy, p_xyxy)
                        if v > best_iou:
                            best_iou = v
                            best_gender = p_gender
                    if best_iou >= 0.3 and best_gender:
                        self._seen_track_id_to_gender[tid] = best_gender
                        frame_track_gender[tid] = best_gender
                    else:
                        self._seen_track_id_to_gender.setdefault(tid, "unknown")
                        if tid not in frame_track_gender:
                            frame_track_gender[tid] = "unknown"

            # Aggregate gender counts for unique tracked persons seen so far
            g_m = sum(1 for g in self._seen_track_id_to_gender.values() if g == "male")
            g_f = sum(1 for g in self._seen_track_id_to_gender.values() if g == "female")
            g_u = sum(1 for g in self._seen_track_id_to_gender.values() if g not in ("male", "female"))

            analytics["current_tracked_persons"] = len(cur_ids)
            analytics["unique_tracked_persons"] = len(self._seen_track_id_to_gender)
            analytics["tracked_gender_counts"] = {"male": g_m, "female": g_f, "unknown": g_u}
            # Count new unique persons first seen in this frame
            new_ids = [tid for tid in cur_ids if tid not in pre_seen_ids]
            analytics["new_unique_persons"] = len(new_ids)
            # Count genders only for new unique persons (once)
            nm = sum(1 for tid in new_ids if frame_track_gender.get(tid) == "male")
            nf = sum(1 for tid in new_ids if frame_track_gender.get(tid) == "female")
            nu = len(new_ids) - nm - nf
            analytics["new_gender_counts"] = {"male": nm, "female": nf, "unknown": nu}
            # Include tracked persons with IDs for overlay drawing
            analytics["tracked_persons"] = tracked_persons

            # Presence tracking: create events when IDs disappear beyond TTL
            now_ts = time.time()
            presence_events: List[Dict] = []
            # Update seen IDs
            for tid in cur_ids:
                rec = self._presence.get(tid)
                if rec is None:
                    self._presence[tid] = {"start": now_ts, "last_seen": now_ts}
                else:
                    rec["last_seen"] = now_ts
            # Finalize events for IDs not seen recently
            to_delete: List[int] = []
            for tid, rec in self._presence.items():
                last = rec.get("last_seen", now_ts)
                if tid not in cur_ids and (now_ts - last) >= self._presence_ttl_sec:
                    start_ts = rec.get("start", last)
                    end_ts = last
                    duration = max(0.0, end_ts - start_ts)
                    presence_events.append({
                        "track_id": tid,
                        "start_ts": float(start_ts),
                        "end_ts": float(end_ts),
                        "duration_sec": float(duration),
                    })
                    to_delete.append(tid)
            for tid in to_delete:
                self._presence.pop(tid, None)

            analytics["presence_events"] = presence_events

            # Unique faces: use temporal IoU de-duplication with TTL
            # Purge expired
            self._recent_face_events = [e for e in self._recent_face_events if (now_ts - e[1]) <= self._face_event_ttl_sec]

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
                for prev_xyxy, ts_seen in self._recent_face_events:
                    if iou_face(f_xyxy, prev_xyxy) >= 0.5:
                        matched = True
                        break
                if not matched:
                    new_unique_faces += 1
                    self._recent_face_events.append((f_xyxy, now_ts))
            analytics["new_unique_faces"] = int(new_unique_faces)
        except Exception:
            pass

        # Rolling average processing FPS from server
        if len(self._proc_ms_window) > 0:
            avg_ms = sum(self._proc_ms_window) / len(self._proc_ms_window)
            analytics["server_avg_fps"] = (1000.0 / avg_ms) if avg_ms > 0 else 0.0
        else:
            analytics["server_avg_fps"] = 0.0

        # Optional debug prints
        try:
            if analytics.get("new_unique_persons"):
                print(f"[Server] New persons: {analytics['new_unique_persons']}")
            if analytics.get("new_unique_faces"):
                print(f"[Server] New faces: {analytics['new_unique_faces']}")
            ng = analytics.get("new_gender_counts") or {}
            if any(ng.get(k, 0) for k in ("male", "female", "unknown")):
                print(f"[Server] New genders: {ng}")
            for ev in analytics.get("presence_events", []) or []:
                print(f"[Presence] ID {ev['track_id']} duration {ev['duration_sec']:.2f}s")
        except Exception:
            pass

        return analytics


class DeepfaceLiteEngine(BaseEngine):
    """Fast path: YOLOv8 person tracking only, no heavy DeepFace calls (FPS oriented)."""
    def __init__(self):
        from ultralytics import YOLO
        self._yolo = YOLO("yolo11n.pt")
        self._tracked_ids = set()

    def process(self, frame_bgr: np.ndarray) -> Dict:
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


def parse_request(reader: asyncio.StreamReader) -> Tuple[Optional[str], np.ndarray]:
    """Unified protocol:
    [1B flags]
      bit0 = 1 => ad_id present
    if ad_id present: [4B ad_len][ad_id bytes]
    [4B frame_len][jpg bytes]
    """
    async def _readexactly(n: int) -> bytes:
        return await reader.readexactly(n)

    async def _read() -> Tuple[Optional[str], np.ndarray]:
        flags = (await _readexactly(1))[0]
        ad_id = None
        if flags & 0b00000001:
            ad_len = struct.unpack(">I", await _readexactly(4))[0]
            ad_id = (await _readexactly(ad_len)).decode()
        frame_len = struct.unpack(">I", await _readexactly(4))[0]
        frame_bytes = await _readexactly(frame_len)
        frame = cv2.imdecode(np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR)
        return ad_id, frame

    return _read()


async def write_response(writer: asyncio.StreamWriter, payload: Dict):
    data = json.dumps(payload).encode("utf-8")
    writer.write(struct.pack(">I", len(data)) + data)
    await writer.drain()


class UnifiedServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 12350, engine: str = "mivolo"):
        self._host = host
        self._port = port
        engine = (engine or "mivolo").lower()
        if engine == "mivolo":
            self._engine = MivoloEngine()
        elif engine == "deepface":
            self._engine = DeepfaceLiteEngine()
        else:
            raise ValueError("engine must be one of: mivolo, deepface")

        # SQLite persistence (reuses project DB if present)
        self._db = sqlite3.connect("camera_analytics.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db_lock = asyncio.Lock()
        self._init_db()

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
                new_unique_persons INTEGER
            )
            """
        )
        # In case the table already existed without the new columns
        for col in [
            "new_unique_persons",
            "new_unique_faces",
            "new_gender_male",
            "new_gender_female",
            "new_gender_unknown",
        ]:
            try:
                cur.execute(f"ALTER TABLE analytics ADD COLUMN {col} INTEGER")
            except Exception:
                pass

        # Presence events table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS presence_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER,
                ad_id TEXT,
                start_ts REAL NOT NULL,
                end_ts REAL NOT NULL,
                duration_sec REAL NOT NULL
            )
            """
        )

        # Recent tracks table for sliding-window viewers
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

    async def _save_analytics(self, ad_id: Optional[str], analytics: Dict):
        # Extract stable fields; ignore per-detection arrays for storage brevity
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

            # Persist presence events if any
            for ev in analytics.get("presence_events", []) or []:
                cur.execute(
                    """
                    INSERT INTO presence_log (track_id, ad_id, start_ts, end_ts, duration_sec)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(ev.get("track_id") or 0),
                        ad_id,
                        float(ev.get("start_ts") or 0.0),
                        float(ev.get("end_ts") or 0.0),
                        float(ev.get("duration_sec") or 0.0),
                    ),
                )

            # Upsert recent tracks for sliding window viewers
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
                # Prune stale recent tracks to keep table small (older than 120s)
                cur.execute("DELETE FROM recent_tracks WHERE last_ts < ?", (now_ts - 120.0,))
            except Exception:
                pass

            self._db.commit()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        try:
            while True:
                try:
                    ad_id, frame = await parse_request(reader)
                except asyncio.IncompleteReadError:
                    break
                except Exception:
                    # Malformed request
                    break

                if frame is None:
                    await write_response(writer, {"status": "error", "message": "invalid_frame"})
                    continue

                # Optional: downscale on server if huge to sustain FPS
                h, w = frame.shape[:2]
                if max(h, w) > 1280:
                    scale = 1280.0 / max(h, w)
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

                analytics = self._engine.process(frame)
                payload = {"status": "success", "analytics": analytics}
                if ad_id is not None:
                    payload["ad_id"] = ad_id
                await write_response(writer, payload)
                # Persist asynchronously (best-effort)
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

    async def start(self):
        server = await asyncio.start_server(self._handle, self._host, self._port)
        async with server:
            await server.serve_forever()


async def main():
    # Default to MiVOLO engine for best speed/accuracy on RTX 4050
    server = UnifiedServer(host="0.0.0.0", port=12350, engine="mivolo")
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())