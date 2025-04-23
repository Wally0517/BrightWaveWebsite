from flask import Flask, request, jsonify
from flask_cors import CORS
import os

app = Flask(__name__)

# Enable CORS for all domains (you can specify origins later if needed)
CORS(app, resources={r"/api/*": {"origins": "*"}})

@app.route('/')
def home():
    return jsonify({"message": "BrightWave Backend is Live ✅"})

# Contact endpoint that handles form submissions
@app.route('/api/contact', methods=['POST', 'OPTIONS'])
def contact():
    # Handle preflight CORS request
    if request.method == 'OPTIONS':
        response = app.make_default_options_response()
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add("Access-Control-Allow-Headers", "Content-Type")
        response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
        return response

    # Process form submission
    try:
        data = request.get_json()
        name = data.get('name')
        email = data.get('email')
        message = data.get('message')

        if not name or not email or not message:
            return jsonify({"success": False, "message": "All fields are required."}), 400

        # Log the message (You can replace this with email/DB logic)
        print(f"📥 New Contact Message\nFrom: {name}\nEmail: {email}\nMessage: {message}")

        return jsonify({"success": True, "message": "Message received successfully!"}), 200

    except Exception as e:
        print(f"❌ Error handling contact form: {e}")
        return jsonify({"success": False, "message": "Internal server error."}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
