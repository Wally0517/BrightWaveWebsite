from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_mail import Mail, Message
import os

# ========== APP INITIALIZATION ==========
app = Flask(__name__, static_folder='.', static_url_path='')

# ========== CORS CONFIGURATION ==========
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
allowed_origins = [origin.strip() for origin in allowed_origins]
CORS(app, origins=allowed_origins, supports_credentials=True)

# ========== EMAIL CONFIGURATION ==========
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'True') == 'True'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'your-email@gmail.com')  # Replace with your email
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'your-app-specific-password')  # Replace with your app-specific password
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'your-email@gmail.com')  # Replace with your email
mail = Mail(app)

# List of email addresses to notify (replace with your actual email addresses)
NOTIFICATION_EMAILS = [
    'email1@example.com',
    'email2@example.com',
    'email3@example.com'
]

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
    fullName = data.get('fullName')  # Updated field name to match form
    email = data.get('email')
    phone = data.get('phone')
    message = data.get('message')

    # Validate required fields
    if not all([fullName, email, phone, message]):
        return jsonify({"success": False, "message": "All fields are required."}), 400

    # Prepare email content
    email_subject = "New Contact Form Submission - BrightWave Enterprises"
    email_body = f"""
    New Contact Form Submission:
    Name: {fullName}
    Email: {email}
    Phone: {phone}
    Message: {message}
    """

    # Create email message
    msg = Message(
        subject=email_subject,
        recipients=NOTIFICATION_EMAILS,
        body=email_body
    )

    try:
        # Send email to the notification list
        mail.send(msg)

        # Send confirmation email to the user
        confirmation_subject = "Thank You for Contacting BrightWave Enterprises"
        confirmation_body = """
        Thank you for your message! We have received your inquiry and will get back to you soon.
        Regards,
        BrightWave Enterprises Team
        """
        confirmation_msg = Message(
            subject=confirmation_subject,
            recipients=[email],
            body=confirmation_body
        )
        mail.send(confirmation_msg)

        print(f"📬 Email sent to {NOTIFICATION_EMAILS}:\n{email_body}")
        return jsonify({"success": True, "message": "Thank you! Your message has been received."})
    except Exception as e:
        print(f"❌ Error sending email: {str(e)}")
        return jsonify({"success": False, "message": "Failed to send message. Please try again later."}), 500

# ========== LOCAL SERVER RUN ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
