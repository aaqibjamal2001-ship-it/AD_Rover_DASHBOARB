# === Environment Configuration ===
import os
import platform
# Only set X11/Qt environment variables on non-Windows systems
if platform.system() != 'Windows':
    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    os.environ.setdefault("QT_X11_NO_MITSHM", "1")

# === Imports ===
import cv2
import time
import numpy as np
import subprocess
import asyncio
import shutil
import struct
import json
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from threading import Thread, Lock
try:
    import requests
except Exception:
    requests = None

# === Flask App Configuration ===
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ADS_FOLDER'] = 'advertisement'
app.config['SECRET_KEY'] = 'jetson-ad-manager-2024'
app.config['DWELL_SERVER_HOST'] = os.environ.get('DWELL_SERVER_HOST', '127.0.0.1')
app.config['DWELL_SERVER_PORT'] = int(os.environ.get('DWELL_SERVER_PORT', '12350'))
app.config['CAMERA_SOURCE'] = int(os.environ.get('CAMERA_SOURCE', '0'))
app.config['DASHBOARD_URL'] = os.environ.get('DASHBOARD_URL', 'http://127.0.0.1:5000')

ALLOWED_EXTENSIONS = {
    'images': {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'},
    'videos': {'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv'}
}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['ADS_FOLDER'], exist_ok=True)

# === Shared State (Current Ad ID) ===
_current_ad_id_lock = Lock()
_current_ad_id = None  # updated by display thread, read by analytics streamer
_latest_cam_frame_lock = Lock()
_latest_cam_frame = None  # updated by analytics streamer for preview
_latest_analytics_lock = Lock()
_latest_analytics = {
    'male': 0,
    'female': 0,
    'unknown': 0,
    'current': 0,
    'unique': 0,
    'avg_ms': 0.0,
    'est_fps': 0.0,
}
_person_seconds_lock = Lock()
_person_seconds_total = 0.0

# === DB (Ad Plays) ===
def get_db():
    conn = sqlite3.connect("camera_analytics.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_ad_plays_table():
    conn = get_db()
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
    conn.close()

def record_ad_play_start(ad_id: str) -> int:
    ts = time.time()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ad_plays (ad_id, start_ts) VALUES (?, ?)",
        (ad_id, float(ts)),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id

def record_ad_play_end(row_id: int):
    ts = time.time()
    conn = get_db()
    cur = conn.cursor()
    # Compute duration based on start_ts
    cur.execute("SELECT start_ts FROM ad_plays WHERE id = ?", (row_id,))
    r = cur.fetchone()
    start_ts = float(r[0]) if r else ts
    dur = max(0.0, float(ts) - start_ts)
    cur.execute(
        "UPDATE ad_plays SET end_ts = ?, duration_sec = ? WHERE id = ?",
        (float(ts), float(dur), int(row_id)),
    )
    conn.commit()
    conn.close()

# === Dashboard Notify (ad list changes) ===
def notify_dashboard_ad_change():
    url = (app.config.get('DASHBOARD_URL') or 'http://127.0.0.1:5000').rstrip('/') + '/api/notify_ad_change'
    if not requests:
        return
    try:
        requests.post(url, timeout=1.5)
    except Exception:
        pass

# === Helper Functions ===
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in (ALLOWED_EXTENSIONS['images'] | ALLOWED_EXTENSIONS['videos'])

def get_file_type(filename):
    ext = filename.rsplit('.', 1)[1].lower()
    return 'image' if ext in ALLOWED_EXTENSIONS['images'] else 'video' if ext in ALLOWED_EXTENSIONS['videos'] else 'unknown'

def get_file_info(filepath):
    try:
        stat = os.stat(filepath)
        return {
            'size': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            'size_mb': round(stat.st_size / (1024 * 1024), 2)
        }
    except:
        return {'size': 0, 'modified': 'Unknown', 'size_mb': 0}

def resize_to_fullscreen(image, screen_width, screen_height):
    h, w = image.shape[:2]
    scale = min(screen_width / w, screen_height / h)
    resized = cv2.resize(image, (int(w * scale), int(h * scale)))
    bg = np.zeros((screen_height, screen_width, 3), dtype=np.uint8)
    x, y = (screen_width - resized.shape[1]) // 2, (screen_height - resized.shape[0]) // 2
    bg[y:y+resized.shape[0], x:x+resized.shape[1]] = resized
    return bg

def wait_for_display(max_attempts=15):
    print("⏳ Checking for display availability...")

    # Windows: skip Xorg checks; OpenCV manages its own windowing
    if platform.system() == 'Windows':
        print("ℹ️ Windows detected; skipping Xorg/xdpyinfo checks.")
        return True

    # Check for Xorg process first (Linux/Jetson)
    for i in range(10):  # Try every 1s for 10s
        if subprocess.run(['pgrep', '-x', 'Xorg'], capture_output=True).returncode == 0:
            print("✅ Xorg is running")
            break
        print(f"Waiting for Xorg... ({i+1}/10)")
        time.sleep(1)
    else:
        print("❌ Xorg not found. Skipping display wait.")
        return False

    # Now check xdpyinfo (i.e. X11 access is ready)
    for attempt in range(max_attempts):
        for xauth in [
            '/home/jetson/.Xauthority',
            f'/tmp/.X0-{os.getuid()}',
            '/var/run/lightdm/jetson/xauthority'
        ]:
            if os.path.exists(xauth):
                os.environ['XAUTHORITY'] = xauth
                result = subprocess.run(['xdpyinfo'], env=os.environ.copy(), capture_output=True)
                if result.returncode == 0:
                    print(f"✅ Display ready after {attempt+1} attempts")
                    return True
        print(f"🔁 Waiting for X display... ({attempt+1}/{max_attempts})")
        time.sleep(1)

    print("⚠️ Timeout: Display not ready")
    return False

def initialize_fullscreen_window(name, width, height):
    for attempt in range(5):
        try:
            print(f"Initializing fullscreen window (attempt {attempt+1})")
            if attempt > 0:
                try: cv2.destroyWindow(name)
                except: pass
                time.sleep(0.5)

            cv2.namedWindow(name, cv2.WINDOW_NORMAL | cv2.WINDOW_GUI_EXPANDED)
            time.sleep(1)
            test_img = np.zeros((100, 100, 3), dtype=np.uint8)
            cv2.imshow(name, test_img)
            cv2.waitKey(1)

            cv2.setWindowProperty(name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
            cv2.moveWindow(name, 0, 0)
            time.sleep(0.5)

            fullscreen_test = np.zeros((height, width, 3), dtype=np.uint8)
            cv2.imshow(name, fullscreen_test)
            cv2.waitKey(100)
            return True
        except Exception as e:
            print(f"Attempt {attempt+1} failed: {e}")
            time.sleep(2)
    return False

def get_screen_size():
    # Determine screen size, especially for Windows
    try:
        if platform.system() == 'Windows':
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
            w = ctypes.windll.user32.GetSystemMetrics(0)
            h = ctypes.windll.user32.GetSystemMetrics(1)
            if w > 0 and h > 0:
                return w, h
    except Exception as e:
        print(f"Screen size detection failed: {e}")
    # Default portrait fallback (Jetson kiosk)
    return 1080, 1920

# === Camera Analytics Streamer ===
class AnalyticsStreamer:
    def __init__(self, server_host: str, server_port: int, camera_source=0):
        self.server_host = server_host
        self.server_port = server_port
        self.camera_source = camera_source
        self.cap = None
        self.reader = None
        self.writer = None
        self.connected = False
        self._stop = False
        self._last_connect_attempt = 0.0
        self._retry_delay = 2.0
        self._last_ts = time.time()

    def _open_camera(self):
        # Prefer DirectShow on Windows for reliability
        try:
            self.cap = cv2.VideoCapture(self.camera_source, cv2.CAP_DSHOW)
            ok = self.cap is not None and self.cap.isOpened()
            if not ok:
                self.cap = cv2.VideoCapture(self.camera_source)
        except Exception:
            self.cap = cv2.VideoCapture(self.camera_source)

    async def _try_connect(self):
        now = time.time()
        if self.connected or (now - self._last_connect_attempt) < self._retry_delay:
            return
        self._last_connect_attempt = now
        try:
            self.reader, self.writer = await asyncio.open_connection(self.server_host, self.server_port)
            self.connected = True
            print(f"[AD_MANAGER] Connected to analytics server {self.server_host}:{self.server_port}")
        except Exception as e:
            self.connected = False
            self.reader = None
            self.writer = None
            print(f"[AD_MANAGER] Server connect failed: {e}")

    def _encode(self, frame: np.ndarray, ad_id: str) -> bytes:
        ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            return b""
        data = buf.tobytes()
        ad_bytes = ad_id.encode() if ad_id else b""
        # Unified protocol: [1B flags] (bit0=ad_id present) [ad_len][ad_bytes]? [frame_len][jpg]
        flags = 0b00000001 if ad_bytes else 0
        parts = [struct.pack("B", flags)]
        if flags & 0b00000001:
            parts.append(struct.pack(">I", len(ad_bytes)))
            parts.append(ad_bytes)
        parts.append(struct.pack(">I", len(data)))
        parts.append(data)
        return b"".join(parts)

    async def run(self):
        self._open_camera()
        if not (self.cap and self.cap.isOpened()):
            print("[AD_MANAGER] Camera failed to open; analytics stream disabled.")
            return
        print("[AD_MANAGER] Camera opened for analytics streaming")

        while not self._stop:
            await self._try_connect()
            ret, frame = self.cap.read()
            if not ret or frame is None:
                await asyncio.sleep(0.01)
                continue

            # Update latest camera preview frame
            try:
                with _latest_cam_frame_lock:
                    global _latest_cam_frame
                    _latest_cam_frame = frame.copy()
            except Exception:
                pass

            # Read current ad id safely
            with _current_ad_id_lock:
                ad_id = _current_ad_id or ""

            if self.connected and self.writer is not None:
                try:
                    payload = self._encode(frame, ad_id)
                    if payload:
                        self.writer.write(payload)
                        await self.writer.drain()
                        # Read analytics response
                        try:
                            # Response format: [4B len][json]
                            hdr = await asyncio.wait_for(self.reader.readexactly(4), timeout=0.2)
                            msg_len = struct.unpack('>I', hdr)[0]
                            data = await asyncio.wait_for(self.reader.readexactly(msg_len), timeout=0.5)
                            resp = json.loads(data.decode('utf-8'))
                            analytics = resp.get('analytics') or {}
                            # Update overlay metrics
                            tg = analytics.get('tracked_gender_counts') or {}
                            cur = int(analytics.get('current_tracked_persons') or 0)
                            uniq = int(analytics.get('unique_tracked_persons') or 0)
                            avg_ms = float(analytics.get('processing_time_ms') or 0.0)
                            est_fps = float(analytics.get('server_avg_fps') or 0.0)
                            with _latest_analytics_lock:
                                _latest_analytics.update({
                                    'male': int(tg.get('male') or 0),
                                    'female': int(tg.get('female') or 0),
                                    'unknown': int(tg.get('unknown') or 0),
                                    'current': cur,
                                    'unique': uniq,
                                    'avg_ms': avg_ms,
                                    'est_fps': est_fps,
                                })
                            # Integrate person-seconds (current persons * dt)
                            now = time.time()
                            dt = max(0.0, now - self._last_ts)
                            self._last_ts = now
                            try:
                                with _person_seconds_lock:
                                    global _person_seconds_total
                                    _person_seconds_total += (cur * dt)
                            except Exception:
                                pass
                        except Exception:
                            # Ignore read timeouts or parse errors
                            pass
                except Exception as e:
                    print(f"[AD_MANAGER] Stream error: {e}")
                    try:
                        if self.writer:
                            self.writer.close()
                            await self.writer.wait_closed()
                    except Exception:
                        pass
                    self.connected = False
                    self.reader = None
                    self.writer = None
            else:
                await asyncio.sleep(0.05)

        try:
            if self.cap:
                self.cap.release()
        except Exception:
            pass

def start_analytics_streamer():
    streamer = AnalyticsStreamer(
        app.config['DWELL_SERVER_HOST'],
        app.config['DWELL_SERVER_PORT'],
        app.config['CAMERA_SOURCE'],
    )
    asyncio.run(streamer.run())

# === Flask Routes ===
@app.route('/')
def index():
    # Serve the ad manager UI template
    return render_template('ad_manager.html')

@app.route('/api/ads', methods=['GET'])
def get_ads():
    try:
        ads = []
        for f in os.listdir(app.config['ADS_FOLDER']):
            if allowed_file(f):
                path = os.path.join(app.config['ADS_FOLDER'], f)
                ads.append({
                    'filename': f,
                    'type': get_file_type(f),
                    'size': get_file_info(path)['size'],
                    'size_mb': get_file_info(path)['size_mb'],
                    'modified': get_file_info(path)['modified'],
                    'url': f'/api/ads/file/{f}'
                })
        ads.sort(key=lambda x: x['modified'], reverse=True)
        return jsonify({'success': True, 'ads': ads, 'total': len(ads)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/ads/file/<filename>')
def get_ad_file(filename):
    try:
        return send_from_directory(app.config['ADS_FOLDER'], filename)
    except FileNotFoundError:
        return jsonify({'error': 'File not found'}), 404

@app.route('/api/upload', methods=['POST'])
def upload_ad():
    try:
        file = request.files.get('file')
        if not file or file.filename == '' or not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Invalid file'}), 400
        fname = secure_filename(file.filename)
        fpath = os.path.join(app.config['ADS_FOLDER'], fname)
        if os.path.exists(fpath):
            return jsonify({'success': False, 'error': f'File "{fname}" already exists'}), 409
        file.save(fpath)
        # Notify dashboard to refresh immediately
        notify_dashboard_ad_change()
        return jsonify({'success': True, 'message': f'File "{fname}" uploaded', 'file': get_file_info(fpath)})
    except RequestEntityTooLarge:
        return jsonify({'success': False, 'error': 'File too large'}), 413
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/ads/<filename>', methods=['DELETE'])
def delete_ad(filename):
    try:
        path = os.path.join(app.config['ADS_FOLDER'], secure_filename(filename))
        if not os.path.exists(path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        os.remove(path)
        # Notify dashboard to refresh immediately
        notify_dashboard_ad_change()
        return jsonify({'success': True, 'message': f'File "{filename}" deleted'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# === Fullscreen Display Thread ===
def play_ads_fullscreen():
    wait_for_display()
    time.sleep(1)
    width, height = get_screen_size()
    window_name = "AdPlayer"
    preview_window = "CameraPreview"

    if not initialize_fullscreen_window(window_name, width, height):
        print("Fullscreen initialization failed")
        return
    try:
        cv2.namedWindow(preview_window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(preview_window, 640, 360)
    except Exception:
        pass

    idle_img = cv2.imread('adrover.jpg')
    print("Ad display loop started")

    while True:
        try:
            ads = sorted([f for f in os.listdir(app.config['ADS_FOLDER']) if allowed_file(f)])
            if not ads:
                if idle_img is not None:
                    cv2.imshow(window_name, resize_to_fullscreen(idle_img, width, height))
                    if cv2.waitKey(5000) & 0xFF == ord('q'): break
                continue

            for f in ads:
                path = os.path.join(app.config['ADS_FOLDER'], f)
                typ = get_file_type(f)
                print(f"Playing: {f} ({typ})")

                # Update current ad id and record play start
                with _current_ad_id_lock:
                    global _current_ad_id
                    _current_ad_id = f
                play_row_id = record_ad_play_start(f)

                if typ == 'image':
                    img = cv2.imread(path)
                    if img is not None:
                        disp_img = resize_to_fullscreen(img, width, height)
                        # Show camera preview if available
                        try:
                            with _latest_cam_frame_lock:
                                prev = _latest_cam_frame.copy() if _latest_cam_frame is not None else None
                        except Exception:
                            prev = None
                        if prev is not None:
                            try:
                                # Resize preview and show in separate window
                                ph, pw = 360, 640
                                pv = cv2.resize(prev, (pw, ph))
                                # Overlay analytics text
                                with _latest_analytics_lock:
                                    an = dict(_latest_analytics)
                                with _person_seconds_lock:
                                    psec = _person_seconds_total
                                overlay = pv.copy()
                                text_lines = [
                                    f"Current: {an.get('current',0)}  Unique: {an.get('unique',0)}",
                                    f"Male: {an.get('male',0)}  Female: {an.get('female',0)}  Unknown: {an.get('unknown',0)}",
                                    f"Person-seconds: {int(psec)}  Avg ms: {an.get('avg_ms',0):.1f}  FPS: {an.get('est_fps',0):.1f}",
                                ]
                                y = 20
                                for t in text_lines:
                                    cv2.putText(overlay, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2, cv2.LINE_AA)
                                    y += 24
                                pv = overlay
                                cv2.imshow(preview_window, pv)
                            except Exception:
                                pass
                        cv2.imshow(window_name, disp_img)
                        # Show image for ~15s
                        end_quit = cv2.waitKey(15000) & 0xFF == ord('q')
                        record_ad_play_end(play_row_id)
                        if end_quit:
                            return
                    else:
                        print(f"Could not load image: {f}")
                        record_ad_play_end(play_row_id)
                elif typ == 'video':
                    # Prefer GStreamer on Jetson; fallback to OpenCV on Windows or when gst-launch is unavailable
                    use_gst = (platform.system() != 'Windows') and (shutil.which('gst-launch-1.0') is not None)
                    if use_gst:
                        try:
                            # Show idle image between transitions to avoid previous frame flash
                            if idle_img is not None:
                                cv2.imshow(window_name, resize_to_fullscreen(idle_img, width, height))
                                cv2.waitKey(1)

                            subprocess.run([
                                "gst-launch-1.0", "filesrc", f"location={path}", "!", "qtdemux", "name=demux",
                                "demux.video_0", "!", "queue", "!", "h264parse", "!", "nvv4l2decoder",
                                "!", "nvvidconv", "flip-method=3", "!", "nvoverlaysink", "sync=false"
                            ], check=True)
                        except subprocess.CalledProcessError as e:
                            print("GStreamer error:", e)
                        finally:
                            record_ad_play_end(play_row_id)
                    else:
                        # OpenCV playback fallback
                        cap = cv2.VideoCapture(path)
                        if not cap.isOpened():
                            print(f"Could not open video: {f}")
                            record_ad_play_end(play_row_id)
                        else:
                            start_time = time.time()
                            while True:
                                ret, frame = cap.read()
                                if not ret:
                                    break
                                disp = resize_to_fullscreen(frame, width, height)
                                # Update camera preview if available
                                try:
                                    with _latest_cam_frame_lock:
                                        prev = _latest_cam_frame.copy() if _latest_cam_frame is not None else None
                                except Exception:
                                    prev = None
                                if prev is not None:
                                    try:
                                        ph, pw = 360, 640
                                        pv = cv2.resize(prev, (pw, ph))
                                        with _latest_analytics_lock:
                                            an = dict(_latest_analytics)
                                        with _person_seconds_lock:
                                            psec = _person_seconds_total
                                        overlay = pv.copy()
                                        text_lines = [
                                            f"Current: {an.get('current',0)}  Unique: {an.get('unique',0)}",
                                            f"Male: {an.get('male',0)}  Female: {an.get('female',0)}  Unknown: {an.get('unknown',0)}",
                                            f"Person-seconds: {int(psec)}  Avg ms: {an.get('avg_ms',0):.1f}  FPS: {an.get('est_fps',0):.1f}",
                                        ]
                                        y = 20
                                        for t in text_lines:
                                            cv2.putText(overlay, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2, cv2.LINE_AA)
                                            y += 24
                                        pv = overlay
                                        cv2.imshow(preview_window, pv)
                                    except Exception:
                                        pass
                                cv2.imshow(window_name, disp)
                                # pace based on FPS if available, else ~30fps
                                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                                if fps <= 0:
                                    fps = 30.0
                                if cv2.waitKey(int(1000 / fps)) & 0xFF == ord('q'):
                                    break
                            cap.release()
                            record_ad_play_end(play_row_id)
                # Clear current ad id when finished
                with _current_ad_id_lock:
                    _current_ad_id = None
        except Exception as e:
            print("Display loop error:", e)
            try:
                cv2.destroyAllWindows()
                time.sleep(2)
                initialize_fullscreen_window(window_name, width, height)
            except: pass

# === Startup ===
init_ad_plays_table()
Thread(target=start_analytics_streamer, daemon=True).start()

def start_flask_server():
    print("Starting Flask server...")
    app.run(host='0.0.0.0', port=5002, debug=True, use_reloader=False)

if platform.system() == 'Windows':
    # On Windows, OpenCV HighGUI must run in the main thread to avoid freezes
    Thread(target=start_flask_server, daemon=True).start()
    play_ads_fullscreen()
else:
    Thread(target=play_ads_fullscreen, daemon=True).start()
    if __name__ == '__main__':
        start_flask_server()
