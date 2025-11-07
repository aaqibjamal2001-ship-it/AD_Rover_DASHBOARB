import asyncio
import struct
import cv2
import json
import time
from typing import Optional, Dict, Any, Tuple, List
import argparse
import os
import glob
import platform


class UnifiedClientDwell:
    def __init__(self, server_ip: str = '127.0.0.1', server_port: int = 12350, camera_source=0, send_ad_id: bool = False, ad_id: Optional[str] = None, show_window: bool = False,
                 ads_enabled: bool = False, ads_dir: str = 'video', ads_fullscreen: bool = False):
        self.server_ip = server_ip
        self.server_port = server_port
        self.camera_source = camera_source
        self.send_ad_id = send_ad_id
        self.ad_id = ad_id

        self.cap = None
        self.connected = False
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connection_retry_delay = 2
        self._last_connect_attempt = 0.0
        self.fps_counter = 0
        self.fps_start_time = time.time()
        self.last_fps_value: Optional[float] = None

        # Client-side presence tracking for dwell display
        # tid -> {"start": float, "last_seen": float}
        self._presence: Dict[int, Dict[str, float]] = {}
        self._presence_ttl_sec: float = 2.0

        # UI window toggle
        self.show_window = show_window
        self._window_name = "Client Camera (Dwell)"

        # Ads playback
        self.ads_enabled = ads_enabled
        self.ads_dir = ads_dir
        self.ads_fullscreen = ads_fullscreen
        self._ads_window_name = "Ads Player"
        self._ad_playlist: List[str] = []
        self._ad_index: int = 0
        self._ad_cap: Optional[cv2.VideoCapture] = None
        self._current_ad_id: Optional[str] = None

        print(f"[CLIENT DWELL] Initialized with server: {server_ip}:{server_port}, camera: {camera_source}, show_window={show_window}, ads_enabled={ads_enabled}, ads_dir={ads_dir}")

    def initialize_camera(self) -> bool:
        print(f"[CLIENT DWELL] Initializing camera with source: {self.camera_source}")
        # Prefer DirectShow on Windows for reliability
        if platform.system() == 'Windows':
            self.cap = cv2.VideoCapture(self.camera_source, cv2.CAP_DSHOW)
        else:
            self.cap = cv2.VideoCapture(self.camera_source)
        if not self.cap.isOpened():
            print(f"[CLIENT DWELL] ERROR: Failed to open camera source: {self.camera_source}. Retrying with default backend...")
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = cv2.VideoCapture(self.camera_source)
            if not self.cap.isOpened():
                print(f"[CLIENT DWELL] ERROR: Camera still failed to open: {self.camera_source}")
                return False
        # Favor FPS: moderate resolution and small buffer
        try:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            print(f"[CLIENT DWELL] Camera configured: 640x480, 30 FPS")
        except Exception as e:
            print(f"[CLIENT DWELL] Warning: Could not set camera properties: {str(e)}")
        # Warmup read
        ret, frame = self.cap.read()
        if ret and frame is not None:
            print(f"[CLIENT DWELL] Camera initialized successfully")
        return bool(ret and frame is not None)

    def set_camera_source(self, new_source) -> bool:
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass
        self.camera_source = new_source
        ok = self.initialize_camera()
        if not ok:
            print(f"Failed to switch to camera {new_source}")
        else:
            print(f"Switched to camera {new_source}")
        return ok

    def _scan_ads(self):
        exts = ("*.mp4", "*.avi", "*.mov", "*.mkv", "*.wmv")
        paths = []
        for ext in exts:
            paths.extend(glob.glob(os.path.join(self.ads_dir, ext)))
        self._ad_playlist = sorted(paths)
        if not self._ad_playlist:
            print(f"[CLIENT DWELL] WARNING: No video files found in '{self.ads_dir}'")
        else:
            print(f"[CLIENT DWELL] Found {len(self._ad_playlist)} ads: {[os.path.basename(p) for p in self._ad_playlist]}")

    def _open_current_ad(self):
        if not self._ad_playlist:
            return False
        if self._ad_cap is not None:
            try:
                self._ad_cap.release()
            except Exception:
                pass
        path = self._ad_playlist[self._ad_index % len(self._ad_playlist)]
        self._ad_cap = cv2.VideoCapture(path)
        if not self._ad_cap.isOpened():
            print(f"[CLIENT DWELL] ERROR: Failed to open ad video: {path}")
            return False
        self._current_ad_id = os.path.basename(path)
        print(f"[CLIENT DWELL] Playing ad: {self._current_ad_id}")
        # Configure window
        try:
            cv2.namedWindow(self._ads_window_name, cv2.WINDOW_NORMAL)
            if self.ads_fullscreen:
                cv2.setWindowProperty(self._ads_window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        except Exception:
            pass
        return True

    def _next_ad(self):
        if not self._ad_playlist:
            return False
        self._ad_index = (self._ad_index + 1) % len(self._ad_playlist)
        return self._open_current_ad()

    def _prev_ad(self):
        if not self._ad_playlist:
            return False
        self._ad_index = (self._ad_index - 1 + len(self._ad_playlist)) % len(self._ad_playlist)
        return self._open_current_ad()

    async def try_connect(self) -> None:
        now = time.time()
        if (now - self._last_connect_attempt) < self.connection_retry_delay:
            return
        self._last_connect_attempt = now
        try:
            print(f"[CLIENT DWELL] Connecting to {self.server_ip}:{self.server_port}")
            reader, writer = await asyncio.open_connection(self.server_ip, self.server_port)
            self.reader, self.writer = reader, writer
            self.connected = True
            print(f"[CLIENT DWELL] Connected")
        except Exception as e:
            self.connected = False
            self.reader, self.writer = None, None
            print(f"[CLIENT DWELL] Connection failed: {str(e)}")

    @staticmethod
    def _encode_request(frame, ad_id: Optional[str]) -> bytes:
        # flags
        flags = 0
        payload = bytearray()
        if ad_id:
            flags |= 0b00000001
        payload.extend(bytes([flags]))

        if ad_id:
            ad_bytes = ad_id.encode('utf-8')
            payload.extend(struct.pack('>I', len(ad_bytes)))
            payload.extend(ad_bytes)

        ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            raise RuntimeError('encode_failed')
        data = buf.tobytes()
        payload.extend(struct.pack('>I', len(data)))
        payload.extend(data)
        return bytes(payload)

    @staticmethod
    async def _read_response(reader: asyncio.StreamReader) -> Dict[str, Any]:
        sz = struct.unpack('>I', await reader.readexactly(4))[0]
        blob = await reader.readexactly(sz)
        return json.loads(blob.decode('utf-8'))

    def _calc_fps(self) -> Optional[float]:
        self.fps_counter += 1
        if self.fps_counter >= 30:
            elapsed = time.time() - self.fps_start_time
            if elapsed > 0:
                fps = self.fps_counter / elapsed
                self.fps_counter = 0
                self.fps_start_time = time.time()
                self.last_fps_value = fps
                return fps
        return self.last_fps_value

    def _update_presence(self, tracked_persons: Any):
        now_ts = time.time()
        cur_ids = []
        for tp in tracked_persons or []:
            tid = tp.get('id')
            if tid is None:
                continue
            cur_ids.append(tid)
            rec = self._presence.get(tid)
            if rec is None:
                self._presence[tid] = {"start": now_ts, "last_seen": now_ts}
            else:
                rec["last_seen"] = now_ts
        # finalize stale
        to_delete = []
        for tid, rec in self._presence.items():
            last = rec.get("last_seen", now_ts)
            if tid not in cur_ids and (now_ts - last) >= self._presence_ttl_sec:
                to_delete.append(tid)
        for tid in to_delete:
            self._presence.pop(tid, None)

    def _draw_overlay(self, frame, analytics: Dict[str, Any]):
        if not analytics:
            return frame
        h, w = frame.shape[:2]
        out = frame.copy()
        overlay = out.copy()
        cv2.rectangle(overlay, (10, 10), (470, 220), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.4, out, 0.6, 0, out)

        font = cv2.FONT_HERSHEY_SIMPLEX
        fs = 0.55
        y = 35
        def put(text):
            nonlocal y
            cv2.putText(out, text, (20, y), font, fs, (0, 255, 0), 1)
            y += 22

        fps_panel = self._calc_fps()
        put(f"Client FPS(avg): {fps_panel:.1f}" if fps_panel is not None else "Client FPS(avg): --")
        if 'server_avg_fps' in analytics:
            put(f"Server FPS(avg): {analytics.get('server_avg_fps', 0):.1f}")
        put(f"Persons: {analytics.get('total_persons', 0)} | Faces: {analytics.get('total_faces', 0)}")
        if 'unique_tracked_persons' in analytics:
            put(f"Unique tracked persons: {analytics['unique_tracked_persons']}")
        if 'current_tracked_persons' in analytics:
            put(f"Current tracked persons: {analytics['current_tracked_persons']}")
        tg = analytics.get('tracked_gender_counts') or {}
        if tg:
            put(f"Tracked genders M:{tg.get('male',0)} F:{tg.get('female',0)} U:{tg.get('unknown',0)}")

        # Draw tracked person boxes with ID and dwell seconds
        now_ts = time.time()
        for tp in analytics.get('tracked_persons', []) or []:
            bx = tp.get('bbox')
            tid = tp.get('id')
            if not bx:
                continue
            x, yb, bw, bh = bx
            x1 = int(x * w)
            y1 = int(yb * h)
            x2 = int((x + bw) * w)
            y2 = int((yb + bh) * h)
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 200, 255), 2)
            dwell_txt = ""
            if tid is not None:
                rec = self._presence.get(tid)
                if rec:
                    dwell = max(0.0, now_ts - rec.get('start', now_ts))
                    dwell_txt = f" (dwell {int(dwell)}s)"
                label = f"ID {tid}{dwell_txt}"
                cv2.putText(out, label, (x1, max(0, y1 - 6)), font, 0.5, (0, 200, 255), 1)
        return out

    async def run(self):
        cam_ok = self.initialize_camera()
        if not cam_ok:
            print('[CLIENT DWELL] Failed to initialize camera; proceeding with ads playback only if enabled')

        # Initialize ads loop if enabled
        if self.ads_enabled:
            self._scan_ads()
            self._open_current_ad()

        # Prepare camera display window if requested
        if self.show_window:
            try:
                cv2.namedWindow(self._window_name, cv2.WINDOW_NORMAL)
            except Exception:
                pass

        # network reader/writer are kept on the instance

        try:
            while True:
                # Attempt non-blocking reconnect periodically
                if not self.connected:
                    await self.try_connect()

                ret, frame = (False, None)
                if self.cap is not None:
                    ret, frame = self.cap.read()
                if not ret:
                    print("[CLIENT DWELL] Warning: Failed to read frame from camera")
                    await asyncio.sleep(0.01)
                    frame = None

                # Optional downscale
                h, w = frame.shape[:2]
                if max(h, w) > 960:
                    scale = 960.0 / max(h, w)
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

                try:
                    if frame is not None and self.connected and self.writer is not None:
                        # Prefer dynamic ad_id from current ad when ads are enabled and no explicit ad_id provided
                        ad_id = None
                        if self.send_ad_id:
                            if self.ad_id is not None:
                                ad_id = self.ad_id
                            elif self.ads_enabled and self._current_ad_id is not None:
                                ad_id = self._current_ad_id
                        self.writer.write(self._encode_request(frame, ad_id))
                        await self.writer.drain()

                    analytics = None
                    if frame is not None and self.connected and self.reader is not None:
                        try:
                            resp = await asyncio.wait_for(self._read_response(self.reader), timeout=5.0)
                            analytics = resp.get('analytics') if resp and resp.get('status') == 'success' else None
                        except Exception:
                            analytics = None
                        if analytics:
                            # Update client-side dwell tracking from tracked persons
                            self._update_presence(analytics.get('tracked_persons'))
                            # Log finalized presence events (from server)
                            for ev in analytics.get('presence_events', []) or []:
                                print(f"[CLIENT DWELL] Presence event: id={ev.get('track_id')} duration={ev.get('duration_sec'):.1f}s")

                    # Show camera window even without analytics
                    if self.show_window and frame is not None:
                        disp = self._draw_overlay(frame, analytics) if analytics else frame
                        cv2.imshow(self._window_name, disp)

                    # Update ads player window
                    if self.ads_enabled and self._ad_cap is not None:
                        try:
                            r2, ad_frame = self._ad_cap.read()
                            if not r2 or ad_frame is None:
                                # Loop to next ad file
                                self._next_ad()
                            else:
                                cv2.imshow(self._ads_window_name, ad_frame)
                        except Exception:
                            pass
                except Exception as e:
                    self.connected = False
                    print(f"[CLIENT DWELL] Connection error: {str(e)}")
                    try:
                        if self.writer:
                            self.writer.close()
                            await self.writer.wait_closed()
                    except Exception:
                        pass
                    self.reader, self.writer = None, None
                    # continue without blocking so windows stay responsive
                    continue

                # Handle keyboard input
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    print("[CLIENT DWELL] User requested exit")
                    break
                elif key == ord('c'):
                    # Toggle camera between 0 and 1 if int
                    if isinstance(self.camera_source, int):
                        alt = 1 if self.camera_source == 0 else 0
                        print(f"[CLIENT DWELL] Switch camera to {alt}")
                        self.set_camera_source(alt)
                elif key == ord('n') and self.ads_enabled:
                    self._next_ad()
                elif key == ord('p') and self.ads_enabled:
                    self._prev_ad()
                elif key == ord('f') and self.ads_enabled:
                    # Toggle fullscreen
                    self.ads_fullscreen = not self.ads_fullscreen
                    try:
                        cv2.setWindowProperty(self._ads_window_name, cv2.WND_PROP_FULLSCREEN,
                                              cv2.WINDOW_FULLSCREEN if self.ads_fullscreen else cv2.WINDOW_NORMAL)
                    except Exception:
                        pass
                await asyncio.sleep(0)
        finally:
            if self.writer:
                try:
                    self.writer.close()
                    await self.writer.wait_closed()
                except Exception:
                    pass
            if self.cap:
                self.cap.release()
            if self._ad_cap:
                try:
                    self._ad_cap.release()
                except Exception:
                    pass
            cv2.destroyAllWindows()


def parse_camera_arg(cam_arg: str):
    # Allow numeric index or URL/path
    try:
        return int(cam_arg)
    except Exception:
        return cam_arg


async def main():
    parser = argparse.ArgumentParser(description='Unified client with dwell-time overlay')
    parser.add_argument('--server-ip', default='127.0.0.1')
    parser.add_argument('--server-port', type=int, default=12350)
    parser.add_argument('--camera', default='0', help='Camera index (e.g., 0) or URL/path')
    parser.add_argument('--show-window', action='store_true', help='Show overlay window with detections and dwell time')
    parser.add_argument('--send-ad-id', action='store_true', help='Send ad_id with frames')
    parser.add_argument('--ad-id', default=None, help='Ad ID to send when enabled')
    parser.add_argument('--ads', action='store_true', help='Enable ads playback from the video folder')
    parser.add_argument('--ads-dir', default='video', help='Directory containing ad videos')
    parser.add_argument('--ads-fullscreen', action='store_true', help='Play ads window in fullscreen')
    args = parser.parse_args()

    cam_src = parse_camera_arg(args.camera)
    client = UnifiedClientDwell(server_ip=args.server_ip, server_port=args.server_port, camera_source=cam_src,
                                send_ad_id=args.send_ad_id, ad_id=args.ad_id, show_window=args.show_window,
                                ads_enabled=args.ads, ads_dir=args.ads_dir, ads_fullscreen=args.ads_fullscreen)
    await client.run()


if __name__ == '__main__':
    asyncio.run(main())