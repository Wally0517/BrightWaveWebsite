from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_mail import Mail, Message
import os
import logging
import re
from time import time
from collections import defaultdict

# ========== APP INITIALIZATION ==========
app = Flask(__name__, static_folder='.', static_url_path='')

# ========== LOGGING CONFIGURATION ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== RATE LIMITING (Simple In-Memory) ==========
REQUEST_LIMIT = 10  # Max requests per minute
REQUEST_WINDOW = 60  # Seconds
request_counts = defaultdict(list)

def check_rate_limit(ip):
    now = time()
    request_counts[ip] = [t for t in request_counts[ip] if now - t < REQUEST_WINDOW]
    if len(request_counts[ip]) >= REQUEST_LIMIT:
        return False
    request_counts[ip].append(now)
    return True

# ========== CONFIGURATION VALIDATION ==========
def validate_email(email):
    return bool(re.match(r'^[\w-\.]+@([\w-]+\.)+[\w-]{2,4}$', email))

def validate_origins(origins):
    return all(origin == '*' or re.match(r'^https?://([\w-]+\.)*[\w-]+(:\d+)?$', origin) for origin in origins)

# Validate environment variables
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
allowed_origins = [origin.strip() for origin in allowed_origins if origin.strip()]
if not validate_origins(allowed_origins):
    logger.error("Invalid ALLOWED_ORIGINS format. Must be '*' or valid URLs (e.g., http://example.com).")
    allowed_origins = ['*']

NOTIFICATION_EMAILS = os.environ.get('NOTIFICATION_EMAILS', '').split(',')
NOTIFICATION_EMAILS = [email.strip() for email in NOTIFICATION_EMAILS if email.strip() and validate_email(email.strip())]
if not NOTIFICATION_EMAILS:
    logger.error("No valid notification email addresses provided. Set NOTIFICATION_EMAILS with valid emails (comma-separated).")

# ========== CORS CONFIGURATION ==========
CORS(app, origins=allowed_origins, supports_credentials=True)

# ========== EMAIL CONFIGURATION ==========
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER')
app.config['MAIL_PORT'] = os.environ.get('MAIL_PORT')
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS') == 'True'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER')
mail = Mail(app)

# Validate email configuration
if not all([app.config['MAIL_SERVER'], app.config['MAIL_PORT'], app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD'], app.config['MAIL_DEFAULT_SENDER']]):
    logger.error("Email configuration incomplete. Set MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, and MAIL_DEFAULT_SENDER.")
if not app.config['MAIL_PORT'].isdigit() or not (1 <= int(app.config['MAIL_PORT']) <= 65535):
    logger.error("Invalid MAIL_PORT. Must be a number between 1 and 65535.")
    app.config['MAIL_PORT'] = 587  # Default to common SMTP port

# ========== STATIC PAGE ROUTES ==========
@app.route('/')
def serve_homepage():
    logger.info(f"Serving index.html to {request.remote_addr}")
    return send_from_directory('.', 'index.html')

@app.route('/about')
def serve_about():
    logger.info(f"Serving about.html to {request.remote_addr}")
    return send_from_directory('.', 'about.html')

@app.route('/contact')
def serve_contact():
    logger.info(f"Serving contact.html to {request.remote_addr}")
    return send_from_directory('.', 'contact.html')

@app.route('/faq')
def serve_faq():
    logger.info(f"Serving faq.html to {request.remote_addr}")
    return send_from_directory('.', 'faq.html')

@app.route('/hostels')
def serve_hostels():
    logger.info(f"Serving hostels.html to {request.remote_addr}")
    return send_from_directory('.', 'hostels.html')

@app.route('/hostels/detail')
def serve_hostel_detail():
    logger.info(f"Serving hostel-detail.html to {request.remote_addr}")
    return send_from_directory('.', 'hostel-detail.html')

@app.route('/style.css')
def serve_styles():
    logger.info(f"Serving style.css to {request.remote_addr}")
    return send_from_directory('.', 'style.css')

# ========== STATIC ASSETS (CSS, Images) ==========
@app.route('/assets/<path:filename>')
def serve_static_assets(filename):
    logger.info(f"Serving asset {filename} to {request.remote_addr}")
    return send_from_directory('assets', filename)

# ========== CONTACT FORM API ==========
@app.route('/api/contact', methods=['POST'])
def handle_contact_form():
    client_ip = request.remote_addr
    if not check_rate_limit(client_ip):
        logger.warning(f"Rate limit exceeded for {client_ip}")
        return jsonify({"success": False, "message": "Too many requests. Please try again later."}), 429

    # Validate request size
    if request.content_length and request.content_length > 1024:  # Max 1KB
        logger.warning(f"Request from {client_ip} too large: {request.content_length} bytes")
        return jsonify({"success": False, "message": "Request too large."}), 413

    data = request.get_json()
    fullName = data.get('fullName', '').strip()
    email = data.get('email', '').strip()
    phone = data.get('phone', '').strip()
    message = data.get('message', '').strip()
    formOrigin = data.get('formOrigin', 'Unknown').strip()

    # Validate required fields
    if not all([fullName, email, phone, message]):
        logger.warning(f"Form submission failed from {client_ip}: Missing fields (fullName: {bool(fullName)}, email: {bool(email)}, phone: {bool(phone)}, message: {bool(message)})")
        return jsonify({"success": False, "message": "All fields are required."}), 400

    # Validate email and phone formats
    if not validate_email(email):
        logger.warning(f"Form submission failed from {client_ip}: Invalid email format: {email}")
        return jsonify({"success": False, "message": "Invalid email address."}), 400
    if not re.match(r'^\+?\d{10,15}$', phone):
        logger.warning(f"Form submission failed from {client_ip}: Invalid phone format: {phone}")
        return jsonify({"success": False, "message": "Invalid phone number."}), 400

    # Validate NOTIFICATION_EMAILS
    if not NOTIFICATION_EMAILS:
        logger.error(f"Cannot send email: NOTIFICATION_EMAILS is empty for request from {client_ip}")
        return jsonify({"success": False, "message": "Server configuration error: No notification emails set."}), 500

    # Prepare email content for notification to team
    email_subject = "New Contact Form Submission - BrightWave Enterprises"
    email_body = f"""
    New Contact Form Submission:
    Form Origin: {formOrigin}
    Name: {fullName}
    Email: {email}
    Phone: {phone}
    Message: {message}
    Submitted from IP: {client_ip}
    """

    # Create email message for notification (with customer's email as Reply-To)
    msg = Message(
        subject=email_subject,
        recipients=NOTIFICATION_EMAILS,
        body=email_body,
        reply_to=email
    )

    try:
        # Send email to the notification list
        mail.send(msg)

        # Send confirmation email to the user
        confirmation_subject = "Thank You for Contacting BrightWave Enterprises"
        confirmation_body = f"""
        Dear {fullName},

        Thank you for your message! We have received your inquiry from the {formOrigin} and will get back to you soon.
        If you have any further questions, feel free to reach out to us at {app.config['MAIL_DEFAULT_SENDER']}.

        Regards,
        BrightWave Enterprises Team
        """
        confirmation_msg = Message(
            subject=confirmation_subject,
            recipients=[email],
            body=confirmation_body
        )
        mail.send(confirmation_msg)

        logger.info(f"Email sent to {NOTIFICATION_EMAILS} from {client_ip}:\n{email_body}")
        return jsonify({"success": True, "message": "Thank you! Your message has been received."})
    except Exception as e:
        logger.error(f"Error sending email for {client_ip}: {str(e)}")
        return jsonify({"success": False, "message": "Failed to send message. Please try again later."}), 500

# ========== RESPONSE HEADERS ==========
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

# ========== LOCAL SERVER RUN ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
