from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os

# ========== APP INITIALIZATION ==========
app = Flask(__name__, static_folder='.', static_url_path='')

# ========== CORS CONFIGURATION ==========
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
allowed_origins = [origin.strip() for origin in allowed_origins]
CORS(app, origins=allowed_origins, supports_credentials=True)

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
    name = data.get('name')
    email = data.get('email')
    message = data.get('message')

    print(f"📬 New Message Received:\nName: {name}\nEmail: {email}\nMessage: {message}")

    return jsonify({"success": True, "message": "Thank you! Your message has been received."})

# ========== LOCAL SERVER RUN ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
