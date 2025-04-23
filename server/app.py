from flask import Flask, request, jsonify
from flask_cors import CORS
import os

app = Flask(__name__)

# Allow only your frontend domain for security
CORS(app, resources={r"/api/*": {"origins": "https://www.brightwaveenterprises.online"}})

@app.route('/')
def home():
    return jsonify({"message": "BrightWave Backend is Live ✅"})

@app.route('/api/contact', methods=['POST'])
def contact():
    data = request.get_json()

    name = data.get('name')
    email = data.get('email')
    message = data.get('message')

    # Simulate message handling (print to console/log)
    print(f"📬 New Contact Message:\nFrom: {name} <{email}>\nMessage: {message}")

    # TODO: Add email or WhatsApp integration here later

    return jsonify({
        "success": True,
        "message": "Message received! We'll get back to you soon."
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
