import os
import json
import sqlite3
import qrcode
import socket
import random
import string
from datetime import datetime, timedelta
from fastapi import FastAPI, Path, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import threading
import time

# --- CONFIGURATION ---
# The QR ID acts as a unique identifier for this specific robot/campaign
DEFAULT_QR_ID = "cafe_promo" 

# Cafe promotion QR ID
CAFE_PROMO_QR_ID = "cafe_promo"

# The URL where the user will be redirected AFTER scanning the QR code
TARGET_REDIRECT_URL = "https://www.google.com/search?q=your+advertising+landing+page"

# Cafe promotion redirect URL
CAFE_PROMO_REDIRECT_URL = "https://www.google.com/search?q=cafe+lounge+20+percent+off+coupon"

# SQLite database file
DB_FILE = "qr_counter.db"

# QR code image path
QR_CODE_PATH = "static/qrcode.png"

# Server host and port
SERVER_PORT = int(os.getenv("PORT", "8000"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

# Get the machine's IP address
def get_ip_address():
    try:
        # Get the machine's IP address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Doesn't need to be reachable
        s.connect(('8.8.8.8', 1))
        ip_address = s.getsockname()[0]
        s.close()
        return ip_address
    except Exception:
        # Fallback to localhost if unable to get IP
        return "localhost"

SERVER_HOST = get_ip_address()
print(f"Server will be accessible at: http://{SERVER_HOST}:{SERVER_PORT}")

def get_base_url():
    """Return the base URL for QR scan links.
    Priority: PUBLIC_BASE_URL > RENDER_EXTERNAL_URL > local host:port
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip('/')
    if RENDER_EXTERNAL_URL:
        return RENDER_EXTERNAL_URL.rstrip('/')
    return f"http://{SERVER_HOST}:{SERVER_PORT}"

# Initialize FastAPI
app = FastAPI(
    title="FastAPI QR Scan Tracker",
    description="Backend for tracking QR code scans using SQLite."
)

# Create static directory if it doesn't exist
os.makedirs("static", exist_ok=True)

# Mount static files directory
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize SQLite database
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS qr_scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        qr_id TEXT,
        timestamp TEXT,
        ip_address TEXT
    )
    ''')

    # Add source column if it doesn't exist (tracks 'adrover' vs 'other')
    try:
        cursor.execute("ALTER TABLE qr_scans ADD COLUMN source TEXT")
    except Exception:
        pass
    
    # Create promo codes table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS promo_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        qr_id TEXT,
        created_at TEXT,
        expires_at TEXT,
        used INTEGER DEFAULT 0
    )
    ''')
    
    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_FILE}")
    
# Generate a unique promo code
def generate_promo_code(qr_id):
    # Generate a random 6-character alphanumeric code
    code_chars = string.ascii_uppercase + string.digits
    code = ''.join(random.choice(code_chars) for _ in range(6))
    
    # Set expiration time (15 minutes from now)
    created_at = datetime.now()
    expires_at = created_at + timedelta(minutes=15)
    
    # Format timestamps for SQLite
    created_at_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
    expires_at_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
    
    # Store in database
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO promo_codes (code, qr_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (code, qr_id, created_at_str, expires_at_str)
    )
    conn.commit()
    conn.close()
    
    return {
        "code": code,
        "created_at": created_at,
        "expires_at": expires_at
    }

def get_promo_by_code(qr_id: str, code: str):
    """Fetch promo code record by code and qr_id; returns None if missing."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT code, qr_id, created_at, expires_at, used FROM promo_codes WHERE qr_id = ? AND code = ?",
            (qr_id, code)
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        created_at = datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S")
        expires_at = datetime.strptime(row[3], "%Y-%m-%d %H:%M:%S")
        used = int(row[4] or 0)
        return {"code": row[0], "qr_id": row[1], "created_at": created_at, "expires_at": expires_at, "used": used}
    except Exception:
        return None

# Generate QR code
def generate_qr_code(data, output_path=QR_CODE_PATH):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(output_path)
    print(f"QR code generated and saved to {output_path}")
    return output_path

# Get scan count for a QR ID
def get_scan_count(qr_id, source=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if source:
        cursor.execute("SELECT COUNT(*) FROM qr_scans WHERE qr_id = ? AND source = ?", (qr_id, source))
    else:
        cursor.execute("SELECT COUNT(*) FROM qr_scans WHERE qr_id = ?", (qr_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count

# Get recent scans for a QR ID
def get_recent_scans(qr_id, limit=10):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT timestamp, ip_address, source FROM qr_scans WHERE qr_id = ? ORDER BY id DESC LIMIT ?",
        (qr_id, limit)
    )
    scans = cursor.fetchall()
    conn.close()
    return scans

# Initialize database
init_db()

# --- HTML TEMPLATES (Served by FastAPI) ---
def get_promo_code_html(promo_data, qr_id):
    code = promo_data["code"]
    expires_at = promo_data["expires_at"]
    
    # Calculate remaining time
    now = datetime.now()
    remaining_minutes = int((expires_at - now).total_seconds() / 60)
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Cafe Lounge - 10% OFF Coupon</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 0; background-color: #f5f5f5; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .card {{ background-color: white; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); padding: 30px; text-align: center; }}
            .header {{ color: #8B4513; margin-bottom: 20px; }}
            .promo-code {{ font-size: 36px; font-weight: bold; letter-spacing: 5px; margin: 30px 0; padding: 15px; background-color: #FFF8E1; border: 2px dashed #8B4513; border-radius: 10px; color: #8B4513; }}
            .expiry {{ font-size: 14px; color: #666; margin-top: 20px; }}
            .remaining {{ font-weight: bold; color: #FF5722; }}
            .instructions {{ margin-top: 30px; text-align: left; color: #555; line-height: 1.5; }}
            .footer {{ margin-top: 40px; font-size: 12px; color: #999; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <div class="header">
                    <h1>Cafe Lounge</h1>
                    <h2>10% OFF Your Order</h2>
                </div>
                <p>Show this code to the cashier to redeem your 10% discount:</p>
                <div class="promo-code">{code}</div>
                <div class="expiry">
                    Code expires at: {expires_at.strftime("%I:%M %p")} today<br>
                    <span class="remaining">({remaining_minutes} minutes remaining)</span>
                </div>
                <div class="instructions">
                    <p><strong>How to use:</strong></p>
                    <ol>
                        <li>Show this screen to the cashier when ordering</li>
                        <li>Valid for one-time use only</li>
                        <li>Cannot be combined with other offers</li>
                    </ol>
                </div>
                <div class="footer">
                    Cafe Lounge &copy; 2023 - All rights reserved
                </div>
            </div>
        </div>
    </body>
    </html>
    """

def format_time_str(ts_str: str) -> str:
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%I:%M%p").lower().lstrip('0')
    except Exception:
        return ts_str

def get_dashboard_html(qr_id, total_count, adrover_count, other_count, recent_scans):
    scans_html = ""
    for timestamp, ip, source in recent_scans:
        scans_html += f"<tr><td>{format_time_str(timestamp)}</td><td>{ip}</td><td>{source or 'other'}</td></tr>"
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>QR Code Scan Dashboard</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; }}
            .container {{ max-width: 800px; margin: 0 auto; }}
            .header {{ background-color: #4CAF50; color: white; padding: 20px; text-align: center; }}
            .content {{ padding: 20px; }}
            .card {{ background-color: white; border-radius: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 20px; padding: 20px; }}
            .count {{ font-size: 48px; font-weight: bold; text-align: center; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            tr:nth-child(even) {{ background-color: #f9f9f9; }}
        </style>
        <script>
            async function refreshStats() {{
                try {{
                    const res = await fetch('/api/stats/{qr_id}');
                    const data = await res.json();
                    document.getElementById('scan-count').textContent = data.scan_count_total;
                    document.getElementById('adrover-count').textContent = data.scan_count_adrover;
                    document.getElementById('other-count').textContent = data.scan_count_other;
                    const tbody = document.getElementById('recent-scans');
                    tbody.innerHTML = data.recent_scans.map(s => '\n<tr><td>' + s.timestamp + '</td><td>' + s.ip_address + '</td><td>' + (s.source || 'other') + '</td></tr>').join('');
                }} catch (e) {{ console.error('Failed to refresh stats', e); }}
            }}
            setInterval(refreshStats, 2000);
            window.addEventListener('load', refreshStats);
        </script>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>QR Code Scan Dashboard</h1>
                <p>Campaign ID: {qr_id}</p>
            </div>
            <div class="content">
                <div class="card">
                    <h2>Total Scans</h2>
                    <div id="scan-count" class="count">{total_count}</div>
                </div>
                <div class="card">
                    <h2>Adrover Total Scans</h2>
                    <div id="adrover-count" class="count">{adrover_count}</div>
                </div>
                <div class="card">
                    <h2>Other Scans</h2>
                    <div id="other-count" class="count">{other_count}</div>
                </div>
                <div class="card">
                    <h2>Recent Scans</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Time</th>
                                <th>IP Address</th>
                                <th>Source</th>
                            </tr>
                        </thead>
                        <tbody id="recent-scans">
                            {scans_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

# HTML template for promo code display
def get_promo_code_html(promo_data, qr_id):
    code = promo_data["code"]
    expires_at_fmt = promo_data["expires_at"].strftime("%I:%M%p").lower().lstrip('0')
    
    # Calculate minutes remaining
    now = datetime.now()
    remaining = promo_data["expires_at"] - now
    minutes_remaining = max(0, int(remaining.total_seconds() / 60))
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Cafe Lounge - 10% OFF Coupon</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 0;
                background-color: #f8f9fa;
                color: #333;
                display: flex;
                flex-direction: column;
                min-height: 100vh;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
                flex: 1;
                display: flex;
                flex-direction: column;
                justify-content: center;
            }}
            .header {{
                background-color: #8B4513;
                color: white;
                padding: 20px;
                text-align: center;
                border-radius: 10px 10px 0 0;
            }}
            .content {{
                background-color: white;
                padding: 30px;
                border-radius: 0 0 10px 10px;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                text-align: center;
            }}
            .promo-code {{
                font-size: 36px;
                font-weight: bold;
                letter-spacing: 5px;
                margin: 30px 0;
                padding: 15px;
                background-color: #f8f9fa;
                border: 2px dashed #8B4513;
                border-radius: 10px;
                color: #8B4513;
            }}
            .expiry {{
                color: #dc3545;
                font-weight: bold;
                margin-bottom: 20px;
            }}
            .instructions {{
                margin: 20px 0;
                line-height: 1.6;
            }}
            .footer {{
                margin-top: 30px;
                font-size: 14px;
                color: #6c757d;
            }}
            .logo {{
                font-size: 24px;
                font-weight: bold;
                margin-bottom: 10px;
            }}
            .timer {{
                font-size: 18px;
                margin-top: 10px;
                color: #dc3545;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>10% OFF YOUR ORDER</h1>
                <p>Cafe Lounge Special Offer</p>
            </div>
            <div class="content">
                <div class="logo">CAFE LOUNGE</div>
                <p class="instructions">Show this code to your cashier to receive 10% off your entire order:</p>
                
                <div class="promo-code">{code}</div>
                
                <div class="expiry">Valid until {expires_at_fmt} (15 minutes from scan)</div>
                <div class="timer">({minutes_remaining} minutes remaining)</div>
                
                <p class="instructions">
                    This is a one-time use code.<br>
                    Cannot be combined with other offers.
                </p>
                
                <div class="footer">
                    &copy; 2023 Cafe Lounge. All rights reserved.
                </div>
            </div>
        </div>
    </body>
    </html>
    """

# --- FASTAPI ROUTES ---

@app.get("/", include_in_schema=False)
async def root():
    """Redirects root to the default dashboard ID."""
    return RedirectResponse(url=f"/dashboard/{DEFAULT_QR_ID}")

@app.head("/")
async def root_head():
    """Explicit HEAD handler to satisfy platform probes."""
    return HTMLResponse(content="", status_code=200)

@app.get("/healthz")
async def healthz():
    """Simple health check endpoint used by Render."""
    return {"status": "ok"}

@app.get("/dashboard/{qr_id}", response_class=HTMLResponse)
async def show_dashboard(
    qr_id: str = Path(..., title="The ID of the QR code campaign")
):
    """Serves the HTML dashboard page."""
    # Get scan count and recent scans
    total_count = get_scan_count(qr_id)
    adrover_count = get_scan_count(qr_id, source="adrover")
    other_count = total_count - adrover_count
    recent_scans = get_recent_scans(qr_id)
    
    # Render HTML with data
    html_content = get_dashboard_html(qr_id, total_count, adrover_count, other_count, recent_scans)
    
    return HTMLResponse(content=html_content)

@app.get("/api/stats/{qr_id}")
async def api_stats(qr_id: str = Path(..., title="The ID of the QR code campaign")):
    """Returns JSON stats for live dashboard updates."""
    total_count = get_scan_count(qr_id)
    adrover_count = get_scan_count(qr_id, source="adrover")
    other_count = total_count - adrover_count
    recent_scans = get_recent_scans(qr_id)
    return {
        "scan_count_total": total_count,
        "scan_count_adrover": adrover_count,
        "scan_count_other": other_count,
        "recent_scans": [
            {"timestamp": format_time_str(ts), "ip_address": ip, "source": src}
            for (ts, ip, src) in recent_scans
        ]
    }

@app.get("/qrcode/{qr_id}")
async def get_qr_code(qr_id: str = Path(..., title="The ID of the QR code campaign")):
    """Returns the QR code image."""
    return FileResponse(QR_CODE_PATH)

@app.get("/scan/{qr_id}", response_class=HTMLResponse)
async def scan_qr_code(
    qr_id: str = Path(..., title="The ID of the QR code campaign to increment"),
    request: Request = None
):
    """
    Records a QR code scan in the SQLite database and either displays a promo code or redirects to the target URL.
    """
    # For cafe promotion QR code, implement refresh-safe behavior using cookies
    if qr_id == CAFE_PROMO_QR_ID:
        # Try to reuse an existing promo code from cookie if still valid
        existing_code = None
        try:
            existing_code = request.cookies.get(f"promo_code_{qr_id}")
        except Exception:
            existing_code = None

        if existing_code:
            promo = get_promo_by_code(qr_id, existing_code)
            if promo:
                now_dt = datetime.now()
                if promo.get("expires_at") and promo["expires_at"] > now_dt and int(promo.get("used", 0)) == 0:
                    # Reuse existing code and DO NOT increment scan again
                    html = get_promo_code_html(promo, qr_id)
                    return HTMLResponse(content=html)

        # No valid cookie promo, record the scan ONCE and issue a new code
        try:
            ip_address = request.client.host if request else "unknown"
            source = None
            try:
                source = request.query_params.get("src", None)
            except Exception:
                source = None
            if not source:
                source = "other"

            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                "INSERT INTO qr_scans (qr_id, timestamp, ip_address, source) VALUES (?, ?, ?, ?)",
                (qr_id, timestamp, ip_address, source)
            )
            conn.commit()
            conn.close()
            print(f"Scan recorded for '{qr_id}' from IP {ip_address} (source={source})")
        except Exception as e:
            print(f"Failed to record scan for '{qr_id}'. Error: {e}")

        # Generate and return a new promo code; set cookie to prevent recount on refresh
        try:
            promo_data = generate_promo_code(qr_id)
            print(f"Generated promo code: {promo_data['code']} for {qr_id}")
            html = get_promo_code_html(promo_data, qr_id)
            resp = HTMLResponse(content=html)
            # Cookie lasts until promo expiry (max_age seconds)
            max_age = max(60, int((promo_data["expires_at"] - datetime.now()).total_seconds()))
            resp.set_cookie(
                key=f"promo_code_{qr_id}",
                value=promo_data["code"],
                max_age=max_age,
                httponly=True,
                samesite="Lax"
            )
            return resp
        except Exception as e:
            print(f"Error generating promo code: {e}")
            return HTMLResponse(content=f"<html><body><h1>Error</h1><p>Could not generate promo code: {e}</p></body></html>")
    else:
        # For other QR codes, redirect to the target URL
        return RedirectResponse(url=TARGET_REDIRECT_URL, status_code=302)

# Function to display QR code with OpenCV
def display_qr_with_opencv():
    """Display the QR code in a window with real-time scan count."""
    # Lazy import heavy modules to avoid slowing server startup
    import cv2
    import numpy as np
    # Load the QR code image
    img = cv2.imread(QR_CODE_PATH)
    if img is None:
        print(f"Error: Could not load QR code image from {QR_CODE_PATH}")
        return
    
    # Create window
    cv2.namedWindow("QR Code Scanner", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("QR Code Scanner", 500, 600)
    
    scan_url = f"http://{SERVER_HOST}:{SERVER_PORT}/scan/{DEFAULT_QR_ID}"
    print(f"Displaying QR code window. Scan URL: {scan_url}")
    print("Press 'q' to quit.")
    
    while True:
        # Get current scan count
        count = get_scan_count(DEFAULT_QR_ID)
        
        # Create a copy of the image to draw on
        display_img = img.copy()
        
        # Add a white background area at the bottom for the count
        footer = np.ones((100, display_img.shape[1], 3), dtype=np.uint8) * 255
        display_img = np.vstack((display_img, footer))
        
        # Add text showing the scan count
        cv2.putText(
            display_img, 
            f"Scans: {count}", 
            (20, display_img.shape[0] - 40), 
            cv2.FONT_HERSHEY_SIMPLEX, 
            1.2, 
            (0, 0, 255), 
            2
        )
        
        # Add text with scan URL
        cv2.putText(
            display_img, 
            f"URL: {scan_url}", 
            (20, display_img.shape[0] - 80), 
            cv2.FONT_HERSHEY_SIMPLEX, 
            0.7, 
            (0, 0, 255), 
            1
        )
        
        # Display the image
        cv2.imshow("QR Code Scanner", display_img)
        
        # Exit if 'q' is pressed
        if cv2.waitKey(1000) & 0xFF == ord('q'):
            break
    
    cv2.destroyAllWindows()

# Main function to run the application
def main():
    # Run only the FastAPI server; no OpenCV QR window
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)

if __name__ == "__main__":
    main()
