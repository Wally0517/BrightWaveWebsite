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
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== CORS CONFIGURATION ==========
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
allowed_origins = [origin.strip() for origin in allowed_origins]
CORS(app, origins=allowed_origins, supports_credentials=True)

# ========== EMAIL CONFIGURATION ==========
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT'))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS') == 'True'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER')
mail = Mail(app)

# List of email addresses to notify (configurable via environment variable)
NOTIFICATION_EMAILS = os.environ.get('NOTIFICATION_EMAILS', '').split(',')
NOTIFICATION_EMAILS = [email.strip() for email in NOTIFICATION_EMAILS if email.strip()]

# Validate email configuration
if not all([app.config['MAIL_SERVER'], app.config['MAIL_PORT'], app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD'], app.config['MAIL_DEFAULT_SENDER']]):
    logger.error("Email configuration is incomplete. Please set MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, and MAIL_DEFAULT_SENDER environment variables.")
if not NOTIFICATION_EMAILS:
    logger.error("No notification email addresses provided. Please set NOTIFICATION_EMAILS environment variable (comma-separated list).")

# ========== STATIC PAGE ROUTES ==========
@app.route('/')
def serve_homepage():
    return send_from_directory('.', 'index.html')

@app.route('/about')
def serve_about():
    return send_from_directory('.', 'about.html')

@app.route('/contact')
def serve_contact():
    return send_from_directory('.', 'contact.html')

@app.route('/faq')
def serve_faq():
    return send_from_directory('.', 'faq.html')

@app.route('/hostels')
def serve_hostels():
    return send_from_directory('.', 'hostels.html')

@app.route('/hostels/detail')
def serve_hostel_detail():
    return send_from_directory('.', 'hostel-detail.html')

# ========== STATIC ASSETS (CSS, Images) ==========
@app.route('/assets/<path:filename>')
def serve_static_assets(filename):
    return send_from_directory('assets', filename)

# ========== CONTACT FORM API ==========
@app.route('/api/contact', methods=['POST'])
def handle_contact_form():
    data = request.get_json()
    fullName = data.get('fullName')
    email = data.get('email')
    phone = data.get('phone')
    message = data.get('message')

    # Validate required fields
    if not all([fullName, email, phone, message]):
        logger.warning("Form submission failed: Missing required fields.")
        return jsonify({"success": False, "message": "All fields are required."}), 400

    # Validate NOTIFICATION_EMAILS
    if not NOTIFICATION_EMAILS:
        logger.error("Cannot send email: NOTIFICATION_EMAILS is empty.")
        return jsonify({"success": False, "message": "Server configuration error: No notification emails set."}), 500

    # Prepare email content for notification to team
    email_subject = "New Contact Form Submission - BrightWave Enterprise"
    email_body = f"""
    New Contact Form Submission:
    Name: {fullName}
    Email: {email}
    Phone: {phone}
    Message: {message}
    """

    # Create email message for notification (with customer's email as Reply-To)
    msg = Message(
        subject=email_subject,
        recipients=NOTIFICATION_EMAILS,
        body=email_body,
        reply_to=email  # Customer's email as Reply-To
    )

    try:
        # Send email to the notification list
        mail.send(msg)

        # Send confirmation email to the user
        confirmation_subject = "Thank You for Contacting BrightWave Enterprise"
        confirmation_body = f"""
        Thank you for your message! We have received your inquiry and will get back to you soon.
        If you have any further questions, feel free to reach out to us at {app.config['MAIL_DEFAULT_SENDER']}.
        Regards,
        BrightWave Enterprise Team
        """
        confirmation_msg = Message(
            subject=confirmation_subject,
            recipients=[email],
            body=confirmation_body
        )
        mail.send(confirmation_msg)

        logger.info(f"Email sent to {NOTIFICATION_EMAILS}:\n{email_body}")
        return jsonify({"success": True, "message": "Thank you! Your message has been received."})
    except Exception as e:
        logger.error(f"Error sending email: {str(e)}")
        return jsonify({"success": False, "message": "Failed to send message. Please try again later."}), 500

# ========== LOCAL SERVER RUN ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
