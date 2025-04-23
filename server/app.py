from flask import Flask, request, jsonify
from flask_cors import CORS
import os

app = Flask(__name__)

# Configure CORS to explicitly allow your frontend's origin
CORS(app, resources={r"/api/*": {"origins": [
    "https://www.brightwaveenterprises.online",
    "https://brightwaveenterprises.online",
    "http://www.brightwaveenterprises.online",
    "http://brightwaveenterprises.online"
]}})

@app.route('/')
def home():
    return jsonify({"message": "BrightWave Backend is Live ✅"})

@app.route('/api/contact', methods=['POST'])
def contact():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    message = data.get('message')

    if not name or not email or not message:
        return jsonify({"success": False, "message": "All fields are required."}), 400

    # Log to console (can replace with email, DB, or webhook later)
    print(f"📬 New Contact Form Message\nFrom: {name} <{email}>\nMessage: {message}\n")

    return jsonify({"success": True, "message": "Message received successfully!"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
