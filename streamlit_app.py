import os
import sqlite3
import qrcode
import io
import csv
import random
import string
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
import streamlit as st
from PIL import Image

# --- CONFIG ---
CAFE_PROMO_QR_ID = "cafe_promo"
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Asia/Karachi")
DB_FILE = os.getenv("DB_FILE", "qr_counter.db")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")

# --- DB UTIL ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS qr_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qr_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            ip_address TEXT,
            source TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qr_id TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

def get_recent_scans(qr_id, limit=20):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT timestamp, ip_address, source FROM qr_scans WHERE qr_id = ? ORDER BY id DESC LIMIT ?",
        (qr_id, limit)
    )
    rows = c.fetchall()
    conn.close()
    return rows

def get_scan_count(qr_id, source=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if source:
        c.execute("SELECT COUNT(*) FROM qr_scans WHERE qr_id = ? AND source = ?", (qr_id, source))
    else:
        c.execute("SELECT COUNT(*) FROM qr_scans WHERE qr_id = ?", (qr_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def record_scan(qr_id, ip_address, source):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO qr_scans (qr_id, timestamp, ip_address, source) VALUES (?, ?, ?, ?)",
        (qr_id, ts, ip_address or "unknown", source)
    )
    conn.commit()
    conn.close()

def format_time_str(ts_str: str) -> str:
    try:
        dt_utc = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if ZoneInfo and DISPLAY_TIMEZONE:
            dt_local = dt_utc.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        else:
            dt_local = dt_utc.astimezone()
        return dt_local.strftime("%I:%M %p").lstrip('0')
    except Exception:
        return ts_str

# --- Promo code ---
def generate_promo_code():
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    now = datetime.now()
    expires_at = now + timedelta(minutes=15)
    return code, now, expires_at

def save_promo_code(qr_id, code, issued_at, expires_at):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO promo_codes (qr_id, code, issued_at, expires_at) VALUES (?, ?, ?, ?)",
        (qr_id, code, issued_at.strftime("%Y-%m-%d %H:%M:%S"), expires_at.strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()

def get_promo_by_code(code):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT qr_id, code, issued_at, expires_at FROM promo_codes WHERE code = ?", (code,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    qr_id, code, issued_at_str, expires_at_str = row
    issued_at = datetime.strptime(issued_at_str, "%Y-%m-%d %H:%M:%S")
    expires_at = datetime.strptime(expires_at_str, "%Y-%m-%d %H:%M:%S")
    return {"qr_id": qr_id, "code": code, "issued_at": issued_at, "expires_at": expires_at}

# --- HTML blocks ---
def promo_html(code: str, expires_at, minutes_remaining: int) -> str:
    return f"""
    <style>
        body {{ font-family: Arial, sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #8B4513; color: white; padding: 20px; text-align: center; border-radius: 10px 10px 0 0; }}
        .content {{ background-color: white; padding: 30px; border-radius: 0 0 10px 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); text-align: center; }}
        .promo-code {{ font-size: 36px; font-weight: bold; letter-spacing: 5px; margin: 30px 0; padding: 15px; background-color: #f8f9fa; border: 2px dashed #8B4513; border-radius: 10px; color: #8B4513; }}
        .instructions {{ margin: 20px 0; line-height: 1.6; }}
        .footer {{ margin-top: 30px; font-size: 14px; color: #6c757d; }}
        .logo {{ font-size: 24px; font-weight: bold; margin-bottom: 10px; }}
        .timer {{ font-size: 18px; margin-top: 10px; color: #dc3545; }}
    </style>
    <div class="container">
        <div class="header">
            <h1>10% OFF YOUR ORDER</h1>
            <p>Cafe Lounge Special Offer</p>
        </div>
        <div class="content">
            <div class="logo">CAFE LOUNGE</div>
            <p class="instructions">Show this code to your cashier to receive 10% off your entire order:</p>
            <div class="promo-code">{code}</div>
            <div class="timer">({minutes_remaining} minutes remaining)</div>
            <p class="instructions">This is a one-time use code.<br>Cannot be combined with other offers.</p>
            <div class="footer">&copy; 2023 Cafe Lounge. All rights reserved.</div>
        </div>
    </div>
    """

# --- Views ---
def view_scan():
    params = st.experimental_get_query_params()
    qr_id = params.get("qr_id", [CAFE_PROMO_QR_ID])[0]
    src = params.get("src", [None])[0]
    code_param = params.get("code", [None])[0]
    cip = params.get("cip", [None])[0]

    if not cip:
        # Fetch client IP via browser JS and set as query param, then rerun
        st.components.v1.html(
            """
            <script>
            (async () => {
              try {
                const r = await fetch('https://api.ipify.org?format=json');
                const j = await r.json();
                const u = new URL(window.location.href);
                u.searchParams.set('cip', j.ip);
                window.location.replace(u.toString());
              } catch(e) { console.log('ipify failed', e); }
            })();
            </script>
            """,
            height=0,
        )
        st.stop()

    if qr_id == CAFE_PROMO_QR_ID:
        promo_data = None
        if code_param:
            promo_data = get_promo_by_code(code_param)
            if promo_data and promo_data["qr_id"] != qr_id:
                promo_data = None
            if promo_data and promo_data["expires_at"] < datetime.now():
                promo_data = None
        if promo_data is None:
            code, issued_at, expires_at = generate_promo_code()
            save_promo_code(qr_id, code, issued_at, expires_at)
            record_scan(qr_id, cip, src)
            st.experimental_set_query_params(view="scan", qr_id=qr_id, src=src or "", code=code, cip=cip)
            promo_data = {"qr_id": qr_id, "code": code, "issued_at": issued_at, "expires_at": expires_at}
        minutes_remaining = max(0, int((promo_data["expires_at"] - datetime.now()).total_seconds() / 60))
        st.markdown(promo_html(promo_data["code"], promo_data["expires_at"], minutes_remaining), unsafe_allow_html=True)
    else:
        # For non-cafe QR IDs, show a continue link
        st.success("Scan recorded. Continue to the promo page:")
        st.markdown("[Continue](https://www.google.com/search?q=your+advertising+landing+page)")

def view_dashboard():
    params = st.experimental_get_query_params()
    qr_id = params.get("qr_id", [CAFE_PROMO_QR_ID])[0]

    st.title("QR Code Scan Dashboard")
    st.caption(f"Campaign ID: {qr_id}")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Scans", get_scan_count(qr_id))
    with col2:
        st.metric("Adrover Scans", get_scan_count(qr_id, source="adrover"))
    with col3:
        st.metric("Other Scans", get_scan_count(qr_id) - get_scan_count(qr_id, source="adrover"))

    rows = get_recent_scans(qr_id, limit=50)
    st.subheader("Recent Scans")
    st.table([
        {"Time": format_time_str(ts), "IP Address": ip, "Source": src or "other"}
        for (ts, ip, src) in rows
    ])

    # Exports
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT timestamp, ip_address, source FROM qr_scans WHERE qr_id = ? ORDER BY id ASC", (qr_id,))
    all_rows = c.fetchall()
    conn.close()

    # CSV download
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp_utc", "local_time_display", "ip_address", "source"]) 
    for ts, ip, src in all_rows:
        writer.writerow([ts, format_time_str(ts), ip, src or "other"]) 
    st.download_button("Download CSV", output.getvalue(), file_name=f"{qr_id}_scans.csv", mime="text/csv")

    # PDF download (optional if reportlab installed)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        buffer = io.BytesIO()
        cpdf = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4
        margin = 40
        y = height - margin
        cpdf.setFont("Helvetica-Bold", 16)
        cpdf.drawString(margin, y, f"Scan Data Report - {qr_id}")
        y -= 24
        cpdf.setFont("Helvetica", 12)
        cpdf.drawString(margin, y, f"Total scans: {len(all_rows)}")
        y -= 18
        cpdf.drawString(margin, y, f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        y -= 24
        cpdf.setFont("Helvetica-Bold", 11)
        cpdf.drawString(margin, y, "UTC Timestamp")
        cpdf.drawString(margin + 160, y, "Local Time")
        cpdf.drawString(margin + 300, y, "IP Address")
        cpdf.drawString(margin + 430, y, "Source")
        y -= 14
        cpdf.setFont("Helvetica", 10)
        for ts, ip, src in all_rows:
            if y < margin + 40:
                cpdf.showPage()
                y = height - margin
                cpdf.setFont("Helvetica-Bold", 11)
                cpdf.drawString(margin, y, "UTC Timestamp")
                cpdf.drawString(margin + 160, y, "Local Time")
                cpdf.drawString(margin + 300, y, "IP Address")
                cpdf.drawString(margin + 430, y, "Source")
                y -= 14
                cpdf.setFont("Helvetica", 10)
            cpdf.drawString(margin, y, ts)
            cpdf.drawString(margin + 160, y, format_time_str(ts))
            cpdf.drawString(margin + 300, y, ip)
            cpdf.drawString(margin + 430, y, (src or "other"))
            y -= 12
        cpdf.save()
        buffer.seek(0)
        st.download_button("Download PDF", buffer.getvalue(), file_name=f"{qr_id}_scans.pdf", mime="application/pdf")
    except Exception:
        st.info("PDF export not available (reportlab not installed).")

def view_qrcode():
    # Build the scan URL that targets this Streamlit app via query params
    base = PUBLIC_BASE_URL or f"http://localhost:{os.getenv('PORT', '8501')}"
    scan_url = f"{base}/?view=scan&qr_id={CAFE_PROMO_QR_ID}&src=adrover"
    st.subheader("Cafe Promo QR (Streamlit)")
    st.write("Scan URL:", scan_url)
    # Generate QR image
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(scan_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    st.image(Image.open(buf), caption="Scan this to open promo in Streamlit")
    st.download_button("Download QR PNG", buf.getvalue(), file_name="cafe_promo_qr_streamlit.png", mime="image/png")

def main():
    st.set_page_config(page_title="QR Scan Tracker", layout="centered")
    init_db()

    params = st.experimental_get_query_params()
    view = params.get("view", ["dashboard"])[0]

    if view == "scan":
        view_scan()
    elif view == "qrcode":
        view_qrcode()
    else:
        view_dashboard()

if __name__ == "__main__":
    main()