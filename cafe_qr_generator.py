import os
import qrcode
from PIL import Image, ImageDraw, ImageFont
import socket

# Configuration
QR_ID = "cafe_promo"
PROMO_QR_PATH_ADROVER = "static/cafe_promo_qr_adrover.png"
PROMO_QR_PATH_PRINTED = "static/cafe_promo_qr_print.png"
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

def create_portrait_promo_qr(scan_url: str, output_path: str, label: str):
    """Create a portrait-oriented QR code with promotional text"""
    # Create directory if it doesn't exist
    os.makedirs("static", exist_ok=True)
    
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
    title_text = "10% OFF"
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
    validity_text = "Offer expires 15 minutes after scan"
    validity_width = draw.textlength(validity_text, font=small_font)
    validity_position = ((canvas_width - validity_width) // 2, cafe_position[1] + 80)
    draw.text(validity_position, validity_text, font=small_font, fill=(100, 100, 100))
    
    # Save the image
    # Add small source label at the bottom
    source_label = f"Source: {label}"
    label_width = draw.textlength(source_label, font=small_font)
    label_position = ((canvas_width - label_width) // 2, validity_position[1] + 60)
    draw.text(label_position, source_label, font=small_font, fill=(120, 120, 120))

    canvas.save(output_path)
    print(f"Promotional QR code created and saved to {output_path}")
    print(f"QR code URL: {scan_url}")
    if PUBLIC_BASE_URL or RENDER_EXTERNAL_URL:
        print("Base URL source: environment variable")
    else:
        print("Base URL source: default Render URL (set PUBLIC_BASE_URL to override)")
    
    return output_path

if __name__ == "__main__":
    base = (PUBLIC_BASE_URL or RENDER_EXTERNAL_URL or DEFAULT_PUBLIC_BASE_URL)
    if base:
        base = base.rstrip('/')
    # Compose URLs with src
    scan_url_adrover = f"{base}/scan/{QR_ID}?src=adrover" if base else f"http://{SERVER_HOST}:{SERVER_PORT}/scan/{QR_ID}?src=adrover"
    scan_url_printed = f"{base}/scan/{QR_ID}?src=printed" if base else f"http://{SERVER_HOST}:{SERVER_PORT}/scan/{QR_ID}?src=printed"

    path_adrover = create_portrait_promo_qr(scan_url_adrover, PROMO_QR_PATH_ADROVER, "Adrover")
    path_printed = create_portrait_promo_qr(scan_url_printed, PROMO_QR_PATH_PRINTED, "Printed")
    print(f"Open Adrover QR at: {os.path.abspath(path_adrover)}")
    print(f"Open Printed QR at: {os.path.abspath(path_printed)}")