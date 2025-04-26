from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os

app = Flask(__name__, static_folder='.', static_url_path='')

# Handle allowed CORS origins
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
allowed_origins = [origin.strip() for origin in allowed_origins]
CORS(app, origins=allowed_origins, supports_credentials=True)

# ========== ROOT ENDPOINT ==========
@app.route('/')
def index():
    return send_from_directory('brightwaveEnterprises-homepage', 'index.html')

# ========== PAGE ROUTES ==========
@app.route('/AboutUs')
def about_page():
    return send_from_directory('AboutUs', 'index.html')

@app.route('/ContactUs')
def contact_page():
    return send_from_directory('ContactUs', 'index.html')

@app.route('/hostels')
def hostels_page():
    return send_from_directory('hostels', 'index.html')

@app.route('/hostels/detail')
def hostel_detail_page():
    return send_from_directory('hostels/detail', 'index.html')

@app.route('/faq')
def faq_page():
    return send_from_directory('faq', 'index.html')

@app.route('/brightwaveEnterprises-homepage')
def homepage():
    return send_from_directory('brightwaveEnterprises-homepage', 'index.html')

# ========== STATIC FILES ==========
@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory('assets', filename)

# ========== FORM API ==========
@app.route('/api/contact', methods=['POST'])
def contact():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    message = data.get('message')

    print(f"Message from {name} ({email}): {message}")
    return jsonify({"success": True, "message": "Message received!"})

# ========== RUN LOCALLY ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
