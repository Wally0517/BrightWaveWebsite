from flask import Flask, request, jsonify
from flask_cors import CORS
import os

app = Flask(__name__)

# Read allowed origins from environment variable
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
allowed_origins = [origin.strip() for origin in allowed_origins]

# Apply CORS only to allowed origins
CORS(app, origins=allowed_origins, supports_credentials=True)

@app.route('/')
def home():
    return jsonify({"message": "BrightWave Backend is Live ✅"})

@app.route('/api/contact', methods=['POST'])
def contact():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    message = data.get('message')

    print(f"Message from {name} ({email}): {message}")
    return jsonify({"success": True, "message": "Message received!"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
