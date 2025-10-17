import os
import qrcode
from PIL import Image, ImageDraw, ImageFont
import socket

# Configuration
PROMO_QR_PATH = "static/cafe_promo_qr.png"
QR_ID = "cafe_promo"
SERVER_PORT = 8000
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
# Default to deployed Render service if env vars are not provided locally
DEFAULT_PUBLIC_BASE_URL = os.getenv("DEFAULT_PUBLIC_BASE_URL", "https://ad-rover-dashboarb.onrender.com")

# Get the machine's IP address
def get_ip_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 1))
        ip_address = s.getsockname()[0]
        s.close()
        return ip_address
    except Exception:
        return "localhost"

SERVER_HOST = get_ip_address()

def create_portrait_promo_qr():
    """Create a portrait-oriented QR code with promotional text"""
    # Create directory if it doesn't exist
    os.makedirs("static", exist_ok=True)
    
    # Generate the QR code URL
    base = (PUBLIC_BASE_URL or RENDER_EXTERNAL_URL or DEFAULT_PUBLIC_BASE_URL)
    # Always prefer a public base URL, otherwise fall back to local host:port
    if base:
        scan_url = f"{base.rstrip('/')}/scan/{QR_ID}"
    else:
        scan_url = f"http://{SERVER_HOST}:{SERVER_PORT}/scan/{QR_ID}"
    
    # Create QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,  # Higher error correction for logo
        box_size=10,
        border=4,
    )
    qr.add_data(scan_url)
    qr.make(fit=True)
    
    # Create QR code image
    qr_img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert to RGB mode for colored background
    qr_img = qr_img.convert("RGB")
    
    # Create a portrait canvas (taller than wide)
    canvas_width = 800
    canvas_height = 1200
    canvas = Image.new('RGB', (canvas_width, canvas_height), color=(255, 255, 255))
    
    # Calculate QR code size and position (centered horizontally, upper portion vertically)
    qr_size = min(canvas_width - 100, 600)  # Leave margins
    qr_img = qr_img.resize((qr_size, qr_size))
    qr_position = ((canvas_width - qr_size) // 2, 150)  # Centered horizontally, upper portion
    
    # Paste QR code onto canvas
    canvas.paste(qr_img, qr_position)
    
    # Add promotional text
    draw = ImageDraw.Draw(canvas)
    
    # Try to use a nice font, fall back to default if not available
    try:
        # Title font (large)
        title_font = ImageFont.truetype("arial.ttf", 72)
    except IOError:
        title_font = ImageFont.load_default()
    
    try:
        # Regular font (medium)
        regular_font = ImageFont.truetype("arial.ttf", 36)
    except IOError:
        regular_font = ImageFont.load_default()
    
    try:
        # Small font
        small_font = ImageFont.truetype("arial.ttf", 24)
    except IOError:
        small_font = ImageFont.load_default()
    
    # Add title
    title_text = "20% OFF"
    title_width = draw.textlength(title_text, font=title_font)
    title_position = ((canvas_width - title_width) // 2, 50)
    draw.text(title_position, title_text, font=title_font, fill=(255, 0, 0))  # Red color
    
    # Add subtitle
    subtitle_text = "YOUR ENTIRE ORDER"
    subtitle_width = draw.textlength(subtitle_text, font=regular_font)
    subtitle_position = ((canvas_width - subtitle_width) // 2, qr_position[1] + qr_size + 50)
    draw.text(subtitle_position, subtitle_text, font=regular_font, fill=(0, 0, 0))
    
    # Add instructions
    instructions_text = "Scan this code & show to cashier"
    instructions_width = draw.textlength(instructions_text, font=regular_font)
    instructions_position = ((canvas_width - instructions_width) // 2, subtitle_position[1] + 60)
    draw.text(instructions_position, instructions_text, font=regular_font, fill=(0, 0, 0))
    
    # Add cafe name
    cafe_text = "CAFE LOUNGE"
    cafe_width = draw.textlength(cafe_text, font=regular_font)
    cafe_position = ((canvas_width - cafe_width) // 2, instructions_position[1] + 100)
    draw.text(cafe_position, cafe_text, font=regular_font, fill=(0, 0, 0))
    
    # Add validity
    validity_text = "Valid until December 31, 2023"
    validity_width = draw.textlength(validity_text, font=small_font)
    validity_position = ((canvas_width - validity_width) // 2, cafe_position[1] + 80)
    draw.text(validity_position, validity_text, font=small_font, fill=(100, 100, 100))
    
    # Save the image
    canvas.save(PROMO_QR_PATH)
    print(f"Promotional QR code created and saved to {PROMO_QR_PATH}")
    print(f"QR code URL: {scan_url}")
    if PUBLIC_BASE_URL or RENDER_EXTERNAL_URL:
        print("Base URL source: environment variable")
    else:
        print("Base URL source: default Render URL (set PUBLIC_BASE_URL to override)")
    
    return PROMO_QR_PATH

if __name__ == "__main__":
    promo_qr_path = create_portrait_promo_qr()
    print(f"Open the image at: {os.path.abspath(promo_qr_path)}")