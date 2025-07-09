from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect, url_for
from flask_cors import CORS
from flask_mail import Mail, Message
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import os
import logging
import re
from datetime import datetime
from time import time
from collections import defaultdict
import json

# ========== APP INITIALIZATION ==========
app = Flask(__name__, static_folder='.', static_url_path='')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')

# ========== DATABASE CONFIGURATION ==========
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///brightwave.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ========== LOGGING CONFIGURATION ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== CORS CONFIGURATION ==========
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
allowed_origins = [origin.strip() for origin in allowed_origins]
CORS(app, origins=allowed_origins, supports_credentials=True)

# ========== EMAIL CONFIGURATION ==========
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS') == 'True'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER')
mail = Mail(app)

# List of email addresses to notify (configurable via environment variable)
NOTIFICATION_EMAILS = os.environ.get('NOTIFICATION_EMAILS', '').split(',')
NOTIFICATION_EMAILS = [email.strip() for email in NOTIFICATION_EMAILS if email.strip()]

# ========== FILE UPLOAD CONFIGURATION ==========
UPLOAD_FOLDER = 'assets/images/properties'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ========== DATABASE MODELS ==========
class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)

class Property(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    property_type = db.Column(db.String(50), nullable=False)  # hostel, land, residential, estate
    location = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Float, nullable=True)
    price_type = db.Column(db.String(20), nullable=True)  # per_semester, per_year, per_sqm, total
    total_rooms = db.Column(db.Integer, nullable=True)
    available_rooms = db.Column(db.Integer, nullable=True)
    size = db.Column(db.String(50), nullable=True)  # For land plots
    amenities = db.Column(db.JSON, nullable=True)
    images = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(20), default='active')  # active, inactive, sold, rented
    construction_status = db.Column(db.String(30), nullable=True)  # planned, ongoing, completed
    completion_date = db.Column(db.Date, nullable=True)
    featured = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PropertyInquiry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('property.id'), nullable=True)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    inquiry_type = db.Column(db.String(30), nullable=False)  # general, property_specific, booking, viewing
    preferred_move_date = db.Column(db.Date, nullable=True)
    budget_range = db.Column(db.String(50), nullable=True)
    message = db.Column(db.Text, nullable=False)
    university = db.Column(db.String(100), nullable=True)
    year_of_study = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), default='new')  # new, contacted, qualified, converted, closed
    priority = db.Column(db.String(10), default='medium')  # low, medium, high
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    property = db.relationship('Property', backref='inquiries')

class ContactMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    subject = db.Column(db.String(200), nullable=True)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='new')  # new, read, responded, closed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ========== UTILITY FUNCTIONS ==========
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def create_admin_user():
    """Create default admin user if none exists"""
    if not Admin.query.first():
        admin = Admin(
            username='admin',
            email='admin@brightwaveenterprises.online',
            password_hash=generate_password_hash('admin123')  # Change this!
        )
        db.session.add(admin)
        db.session.commit()
        logger.info("Default admin user created. Username: admin, Password: admin123")

def init_sample_data():
    """Initialize with sample property data"""
    if not Property.query.first():
        # Phase 1 Hostel
        phase1 = Property(
            title='BrightWave Phase 1 Hostel',
            description='Modern 10-room self-contained hostel near Kwara State University (KWASU) with private bathrooms, kitchens, 24/7 security, and solar power.',
            property_type='hostel',
            location='Malete, Kwara State, Nigeria',
            price=180000,
            price_type='per_semester',
            total_rooms=10,
            available_rooms=10,
            amenities=['Private Bathroom', 'Private Kitchen', '24/7 Security', 'Solar Power', 'CCTV', 'Water Supply', 'Parking Space'],
            images=['phase1/phase1-main-entrance.jpg', 'phase1/phase1-progress-1.jpg', 'phase1/phase1-progress-2.jpg'],
            construction_status='ongoing',
            completion_date=datetime(2025, 12, 31).date(),
            featured=True
        )
        
        # Phase 2 Hostel
        phase2 = Property(
            title='BrightWave Phase 2',
            description='Expanded hostel facility with 32-35 rooms and enhanced amenities, planned for construction after Phase 1 completion.',
            property_type='hostel',
            location='Malete, Kwara State, Nigeria',
            price=200000,
            price_type='per_semester',
            total_rooms=35,
            available_rooms=35,
            amenities=['Private Bathroom', 'Private Kitchen', '24/7 Security', 'Solar Power', 'CCTV', 'Water Supply', 'Parking Space', 'Common Area', 'Study Rooms'],
            images=['phase2/phase2-placeholder.jpg'],
            construction_status='planned',
            completion_date=datetime(2026, 6, 30).date(),
            featured=False
        )
        
        # BrightWave Estate
        estate = Property(
            title='BrightWave Estate',
            description='6 acres of prime residential land at Obada Ikija, Abeokuta, featuring residential plots, modern homes, and community amenities.',
            property_type='estate',
            location='Obada Ikija, Abeokuta, Ogun State',
            price=2500000,
            price_type='per_sqm',
            size='6 acres',
            amenities=['Gated Community', 'Electricity', 'Water Supply', 'Good Road Network', 'Security', 'Recreational Facilities'],
            images=['brightwave-estate-placeholder.jpg'],
            construction_status='planned',
            completion_date=datetime(2026, 12, 31).date(),
            featured=True
        )
        
        db.session.add_all([phase1, phase2, estate])
        db.session.commit()
        logger.info("Sample property data initialized")

# ========== AUTHENTICATION FUNCTIONS ==========
def login_required(f):
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

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

# ========== STATIC ASSETS ==========
@app.route('/assets/<path:filename>')
def serve_static_assets(filename):
    return send_from_directory('assets', filename)

# ========== PROPERTY API ROUTES ==========
@app.route('/api/properties', methods=['GET'])
def get_properties():
    """Get all properties with filtering options"""
    property_type = request.args.get('type')
    status = request.args.get('status', 'active')
    featured = request.args.get('featured')
    
    query = Property.query.filter_by(status=status)
    
    if property_type:
        query = query.filter_by(property_type=property_type)
    
    if featured:
        query = query.filter_by(featured=True)
    
    properties = query.order_by(Property.created_at.desc()).all()
    
    return jsonify([{
        'id': prop.id,
        'title': prop.title,
        'description': prop.description,
        'property_type': prop.property_type,
        'location': prop.location,
        'price': prop.price,
        'price_type': prop.price_type,
        'total_rooms': prop.total_rooms,
        'available_rooms': prop.available_rooms,
        'size': prop.size,
        'amenities': prop.amenities or [],
        'images': prop.images or [],
        'construction_status': prop.construction_status,
        'completion_date': prop.completion_date.isoformat() if prop.completion_date else None,
        'featured': prop.featured,
        'created_at': prop.created_at.isoformat()
    } for prop in properties])

@app.route('/api/properties/<int:property_id>', methods=['GET'])
def get_property(property_id):
    """Get specific property details"""
    property = Property.query.get_or_404(property_id)
    
    return jsonify({
        'id': property.id,
        'title': property.title,
        'description': property.description,
        'property_type': property.property_type,
        'location': property.location,
        'price': property.price,
        'price_type': property.price_type,
        'total_rooms': property.total_rooms,
        'available_rooms': property.available_rooms,
        'size': property.size,
        'amenities': property.amenities or [],
        'images': property.images or [],
        'construction_status': property.construction_status,
        'completion_date': property.completion_date.isoformat() if property.completion_date else None,
        'featured': property.featured,
        'created_at': property.created_at.isoformat()
    })

# ========== ENHANCED CONTACT FORM API ==========
@app.route('/api/contact', methods=['POST'])
def handle_contact_form():
    """Handle general contact form submissions"""
    data = request.get_json()
    
    # Extract form data
    full_name = data.get('fullName', '').strip()
    email = data.get('email', '').strip()
    phone = data.get('phone', '').strip()
    subject = data.get('subject', '').strip()
    message = data.get('message', '').strip()

    # Validate required fields
    if not all([full_name, email, phone, message]):
        return jsonify({"success": False, "message": "All fields are required."}), 400

    # Save to database
    contact_message = ContactMessage(
        full_name=full_name,
        email=email,
        phone=phone,
        subject=subject,
        message=message
    )
    db.session.add(contact_message)
    db.session.commit()

    # Send email notification
    if NOTIFICATION_EMAILS:
        email_subject = f"New Contact Form Submission - {subject or 'General Inquiry'}"
        email_body = f"""
        New Contact Form Submission:
        
        Name: {full_name}
        Email: {email}
        Phone: {phone}
        Subject: {subject}
        Message: {message}
        
        Submitted at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """

        try:
            msg = Message(
                subject=email_subject,
                recipients=NOTIFICATION_EMAILS,
                body=email_body,
                reply_to=email
            )
            mail.send(msg)
            
            # Send confirmation email
            confirmation_msg = Message(
                subject="Thank You for Contacting BrightWave Enterprise",
                recipients=[email],
                body=f"""
                Dear {full_name},
                
                Thank you for your message! We have received your inquiry and will get back to you within 24-48 hours.
                
                If you have any urgent questions, feel free to reach out to us at {app.config['MAIL_DEFAULT_SENDER']}.
                
                Best regards,
                BrightWave Enterprise Team
                """
            )
            mail.send(confirmation_msg)
            
        except Exception as e:
            logger.error(f"Error sending email: {str(e)}")

    return jsonify({"success": True, "message": "Thank you! Your message has been received."})

@app.route('/api/property-inquiry', methods=['POST'])
def handle_property_inquiry():
    """Handle property-specific inquiries"""
    data = request.get_json()
    
    # Extract form data
    property_id = data.get('propertyId')
    full_name = data.get('fullName', '').strip()
    email = data.get('email', '').strip()
    phone = data.get('phone', '').strip()
    inquiry_type = data.get('inquiryType', 'general')
    preferred_move_date = data.get('preferredMoveDate')
    budget_range = data.get('budgetRange', '').strip()
    message = data.get('message', '').strip()
    university = data.get('university', '').strip()
    year_of_study = data.get('yearOfStudy', '').strip()

    # Validate required fields
    if not all([full_name, email, phone, message]):
        return jsonify({"success": False, "message": "All required fields must be filled."}), 400

    # Parse preferred move date
    move_date = None
    if preferred_move_date:
        try:
            move_date = datetime.strptime(preferred_move_date, '%Y-%m-%d').date()
        except ValueError:
            pass

    # Save to database
    inquiry = PropertyInquiry(
        property_id=property_id if property_id else None,
        full_name=full_name,
        email=email,
        phone=phone,
        inquiry_type=inquiry_type,
        preferred_move_date=move_date,
        budget_range=budget_range,
        message=message,
        university=university,
        year_of_study=year_of_study
    )
    db.session.add(inquiry)
    db.session.commit()

    # Get property details for email
    property_info = ""
    if property_id:
        property = Property.query.get(property_id)
        if property:
            property_info = f"Property: {property.title} ({property.location})\n"

    # Send email notification
    if NOTIFICATION_EMAILS:
        email_subject = f"New Property Inquiry - {inquiry_type.title()}"
        email_body = f"""
        New Property Inquiry:
        
        {property_info}Name: {full_name}
        Email: {email}
        Phone: {phone}
        Inquiry Type: {inquiry_type.title()}
        University: {university}
        Year of Study: {year_of_study}
        Budget Range: {budget_range}
        Preferred Move Date: {preferred_move_date or 'Not specified'}
        
        Message: {message}
        
        Submitted at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """

        try:
            msg = Message(
                subject=email_subject,
                recipients=NOTIFICATION_EMAILS,
                body=email_body,
                reply_to=email
            )
            mail.send(msg)
            
            # Send confirmation email
            confirmation_msg = Message(
                subject="Thank You for Your Property Inquiry",
                recipients=[email],
                body=f"""
                Dear {full_name},
                
                Thank you for your interest in our properties! We have received your inquiry and our team will contact you within 24-48 hours.
                
                Your inquiry details:
                - Inquiry Type: {inquiry_type.title()}
                - Preferred Move Date: {preferred_move_date or 'Not specified'}
                - Budget Range: {budget_range}
                
                We look forward to helping you find the perfect accommodation!
                
                Best regards,
                BrightWave Enterprise Team
                """
            )
            mail.send(confirmation_msg)
            
        except Exception as e:
            logger.error(f"Error sending email: {str(e)}")

    return jsonify({"success": True, "message": "Thank you! Your inquiry has been received."})

# ========== FILE UPLOAD API ==========
@app.route('/admin/api/upload', methods=['POST'])
@login_required
def upload_image():
    """Handle property image uploads"""
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "No file provided"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "message": "No file selected"}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        return jsonify({"success": True, "filename": f"images/properties/{filename}"})
    return jsonify({"success": False, "message": "Invalid file type"}), 400

# ========== ADMIN AUTHENTICATION ==========
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        
        admin = Admin.query.filter_by(username=username, is_active=True).first()
        
        if admin and check_password_hash(admin.password_hash, password):
            session['admin_id'] = admin.id
            return jsonify({"success": True, "message": "Login successful", "redirect": "/admin/dashboard"})
        else:
            return jsonify({"success": False, "message": "Invalid credentials"}), 401
    
    # Return login form HTML
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    return redirect(url_for('admin_login'))

# ========== ADMIN DASHBOARD ==========
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    return render_template_string(ADMIN_DASHBOARD_TEMPLATE)

# ========== ADMIN API ROUTES ==========
@app.route('/admin/api/stats')
@login_required
def admin_stats():
    """Get dashboard statistics"""
    total_properties = Property.query.count()
    active_properties = Property.query.filter_by(status='active').count()
    total_inquiries = PropertyInquiry.query.count()
    new_inquiries = PropertyInquiry.query.filter_by(status='new').count()
    contact_messages = ContactMessage.query.count()
    new_messages = ContactMessage.query.filter_by(status='new').count()
    
    return jsonify({
        'total_properties': total_properties,
        'active_properties': active_properties,
        'total_inquiries': total_inquiries,
        'new_inquiries': new_inquiries,
        'contact_messages': contact_messages,
        'new_messages': new_messages
    })

@app.route('/admin/api/properties', methods=['GET'])
@login_required
def admin_get_properties():
    """Get all properties for admin"""
    properties = Property.query.order_by(Property.created_at.desc()).all()
    
    return jsonify([{
        'id': prop.id,
        'title': prop.title,
        'property_type': prop.property_type,
        'location': prop.location,
        'price': prop.price,
        'price_type': prop.price_type,
        'total_rooms': prop.total_rooms,
        'available_rooms': prop.available_rooms,
        'status': prop.status,
        'construction_status': prop.construction_status,
        'featured': prop.featured,
        'created_at': prop.created_at.isoformat()
    } for prop in properties])

@app.route('/admin/api/properties', methods=['POST'])
@login_required
def admin_create_property():
    """Create new property"""
    data = request.get_json()
    
    # Parse completion date
    completion_date = None
    if data.get('completion_date'):
        try:
            completion_date = datetime.strptime(data['completion_date'], '%Y-%m-%d').date()
        except ValueError:
            pass
    
    property = Property(
        title=data['title'],
        description=data['description'],
        property_type=data['property_type'],
        location=data['location'],
        price=data.get('price'),
        price_type=data.get('price_type'),
        total_rooms=data.get('total_rooms'),
        available_rooms=data.get('available_rooms'),
        size=data.get('size'),
        amenities=data.get('amenities', []),
        images=data.get('images', []),
        status=data.get('status', 'active'),
        construction_status=data.get('construction_status'),
        completion_date=completion_date,
        featured=data.get('featured', False)
    )
    
    db.session.add(property)
    db.session.commit()
    
    return jsonify({"success": True, "message": "Property created successfully"})

@app.route('/admin/api/properties/<int:property_id>', methods=['PUT'])
@login_required
def admin_update_property(property_id):
    """Update existing property"""
    property = Property.query.get_or_404(property_id)
    data = request.get_json()
    
    # Parse completion date
    completion_date = None
    if data.get('completion_date'):
        try:
            completion_date = datetime.strptime(data['completion_date'], '%Y-%m-%d').date()
        except ValueError:
            pass
    
    property.title = data['title']
    property.description = data['description']
    property.property_type = data['property_type']
    property.location = data['location']
    property.price = data.get('price')
    property.price_type = data.get('price_type')
    property.total_rooms = data.get('total_rooms')
    property.available_rooms = data.get('available_rooms')
    property.size = data.get('size')
    property.amenities = data.get('amenities', [])
    property.images = data.get('images', [])
    property.status = data.get('status', 'active')
    property.construction_status = data.get('construction_status')
    property.completion_date = completion_date
    property.featured = data.get('featured', False)
    property.updated_at = datetime.utcnow()
    
    db.session.commit()
    
    return jsonify({"success": True, "message": "Property updated successfully"})

@app.route('/admin/api/proproperties/<int:property_id>', methods=['DELETE'])
@login_required
def admin_delete_property(property_id):
    """Delete property"""
    property = Property.query.get_or_404(property_id)
    db.session.delete(property)
    db.session.commit()
    
    return jsonify({"success": True, "message": "Property deleted successfully"})

@app.route('/admin/api/inquiries', methods=['GET'])
@login_required
def admin_get_inquiries():
    """Get all property inquiries"""
    inquiries = PropertyInquiry.query.order_by(PropertyInquiry.created_at.desc()).all()
    
    return jsonify([{
        'id': inquiry.id,
        'property_title': inquiry.property.title if inquiry.property else 'General Inquiry',
        'full_name': inquiry.full_name,
        'email': inquiry.email,
        'phone': inquiry.phone,
        'inquiry_type': inquiry.inquiry_type,
        'university': inquiry.university,
        'year_of_study': inquiry.year_of_study,
        'budget_range': inquiry.budget_range,
        'preferred_move_date': inquiry.preferred_move_date.isoformat() if inquiry.preferred_move_date else None,
        'message': inquiry.message,
        'status': inquiry.status,
        'priority': inquiry.priority,
        'created_at': inquiry.created_at.isoformat()
    } for inquiry in inquiries])

@app.route('/admin/api/inquiries/<int:inquiry_id>', methods=['PUT'])
@login_required
def admin_update_inquiry(inquiry_id):
    """Update inquiry status"""
    inquiry = PropertyInquiry.query.get_or_404(inquiry_id)
    data = request.get_json()
    
    inquiry.status = data.get('status', inquiry.status)
    inquiry.priority = data.get('priority', inquiry.priority)
    inquiry.updated_at = datetime.utcnow()
    
    db.session.commit()
    
    return jsonify({"success": True, "message": "Inquiry updated successfully"})

@app.route('/admin/api/contact-messages', methods=['GET'])
@login_required
def admin_get_contact_messages():
    """Get all contact messages"""
    messages = ContactMessage.query.order_by(ContactMessage.created_at.desc()).all()
    
    return jsonify([{
        'id': msg.id,
        'full_name': msg.full_name,
        'email': msg.email,
        'phone': msg.phone,
        'subject': msg.subject,
        'message': msg.message,
        'status': msg.status,
        'created_at': msg.created_at.isoformat()
    } for msg in messages])

@app.route('/admin/api/contact-messages/<int:message_id>', methods=['PUT'])
@login_required
def admin_update_contact_message(message_id):
    """Update contact message status"""
    message = ContactMessage.query.get_or_404(message_id)
    data = request.get_json()
    
    message.status = data.get('status', message.status)
    db.session.commit()
    
    return jsonify({"success": True, "message": "Contact message updated successfully"})

# ========== ADMIN TEMPLATES ==========
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login - BrightWave Enterprise</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white min-h-screen flex items-center justify-center">
    <div class="max-w-md w-full bg-gray-800 p-8 rounded-lg shadow-lg">
        <div class="text-center mb-8">
            <h1 class="text-3xl font-bold text-blue-400">BrightWave Admin</h1>
            <p class="text-gray-300 mt-2">Property Management System</p>
        </div>
        
        <form id="loginForm" class="space-y-6">
            <div>
                <label class="block text-sm font-medium mb-2">Username</label>
                <input type="text" id="username" required 
                       class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg focus:outline-none focus:border-blue-500">
            </div>
            
            <div>
                <label class="block text-sm font-medium mb-2">Password</label>
                <input type="password" id="password" required 
                       class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg focus:outline-none focus:border-blue-500">
            </div>
            
            <div>
                <button type="submit" 
                        class="w-full bg-blue-500 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg focus:outline-none">
                    Login
                </button>
            </div>
            
            <p id="errorMessage" class="text-red-500 text-sm text-center hidden"></p>
        </form>
    </div>

    <script>
        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const errorMessage = document.getElementById('errorMessage');

            try {
                const response = await fetch('/admin/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                const data = await response.json();

                if (data.success) {
                    window.location.href = data.redirect || '/admin/dashboard';
                } else {
                    errorMessage.textContent = data.message || 'Login failed';
                    errorMessage.classList.remove('hidden');
                }
            } catch (error) {
                errorMessage.textContent = 'An error occurred. Please try again.';
                errorMessage.classList.remove('hidden');
            }
        });
    </script>
</body>
</html>
"""

ADMIN_DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Dashboard - BrightWave Enterprise</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <header class="bg-gray-800 shadow">
        <div class="max-w-7xl mx-auto py-4 px-4 sm:px-6 lg:px-8 flex justify-between items-center">
            <h1 class="text-2xl font-bold text-blue-400">BrightWave Admin Dashboard</h1>
            <a href="/admin/logout" class="text-blue-400 hover:text-blue-300">Logout</a>
        </div>
    </header>

    <main class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <!-- Statistics -->
        <section class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Overview</h2>
            <div id="stats" class="grid grid-cols-1 md:grid-cols-3 gap-4">
                <!-- Stats will be populated by JavaScript -->
            </div>
        </section>

        <!-- Properties -->
        <section class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Properties</h2>
            <div class="bg-gray-800 p-4 rounded-lg">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="border-b border-gray-600">
                            <th class="py-2 text-left">Title</th>
                            <th class="py-2 text-left">Type</th>
                            <th class="py-2 text-left">Status</th>
                            <th class="py-2 text-left">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="propertiesTable">
                        <!-- Properties will be populated by JavaScript -->
                    </tbody>
                </table>
            </div>
        </section>

        <!-- Inquiries -->
        <section class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Property Inquiries</h2>
            <div class="bg-gray-800 p-4 rounded-lg">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="border-b border-gray-600">
                            <th class="py-2 text-left">Name</th>
                            <th class="py-2 text-left">Property</th>
                            <th class="py-2 text-left">Status</th>
                            <th class="py-2 text-left">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="inquiriesTable">
                        <!-- Inquiries will be populated by JavaScript -->
                    </tbody>
                </table>
            </div>
        </section>

        <!-- Contact Messages -->
        <section>
            <h2 class="text-xl font-semibold mb-4">Contact Messages</h2>
            <div class="bg-gray-800 p-4 rounded-lg">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="border-b border-gray-600">
                            <th class="py-2 text-left">Name</th>
                            <th class="py-2 text-left">Subject</th>
                            <th class="py-2 text-left">Status</th>
                            <th class="py-2 text-left">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="messagesTable">
                        <!-- Messages will be populated by JavaScript -->
                    </tbody>
                </table>
            </div>
        </section>
    </main>

    <script>
        async function fetchData(url) {
            const response = await fetch(url);
            return response.json();
        }

        async function loadStats() {
            const stats = await fetchData('/admin/api/stats');
            const statsContainer = document.getElementById('stats');
            statsContainer.innerHTML = `
                <div class="bg-gray-700 p-4 rounded-lg">
                    <h3 class="text-lg font-medium">Properties</h3>
                    <p>Total: ${stats.total_properties}</p>
                    <p>Active: ${stats.active_properties}</p>
                </div>
                <div class="bg-gray-700 p-4 rounded-lg">
                    <h3 class="text-lg font-medium">Inquiries</h3>
                    <p>Total: ${stats.total_inquiries}</p>
                    <p>New: ${stats.new_inquiries}</p>
                </div>
                <div class="bg-gray-700 p-4 rounded-lg">
                    <h3 class="text-lg font-medium">Messages</h3>
                    <p>Total: ${stats.contact_messages}</p>
                    <p>New: ${stats.new_messages}</p>
                </div>
            `;
        }

        async function loadProperties() {
            const properties = await fetchData('/admin/api/properties');
            const tableBody = document.getElementById('propertiesTable');
            tableBody.innerHTML = properties.map(prop => `
                <tr class="border-b border-gray-600">
                    <td class="py-2">${prop.title}</td>
                    <td class="py-2">${prop.property_type}</td>
                    <td class="py-2">${prop.status}</td>
                    <td class="py-2">
                        <button class="text-blue-400 hover:underline">Edit</button>
                        <button class="text-red-400 hover:underline ml-2">Delete</button>
                    </td>
                </tr>
            `).join('');
        }

        async function loadInquiries() {
            const inquiries = await fetchData('/admin/api/inquiries');
            const tableBody = document.getElementById('inquiriesTable');
            tableBody.innerHTML = inquiries.map(inq => `
                <tr class="border-b border-gray-600">
                    <td class="py-2">${inq.full_name}</td>
                    <td class="py-2">${inq.property_title}</td>
                    <td class="py-2">${inq.status}</td>
                    <td class="py-2">
                        <button class="text-blue-400 hover:underline">Update</button>
                    </td>
                </tr>
            `).join('');
        }

        async function loadMessages() {
            const messages = await fetchData('/admin/api/contact-messages');
            const tableBody = document.getElementById('messagesTable');
            tableBody.innerHTML = messages.map(msg => `
                <tr class="border-b border-gray-600">
                    <td class="py-2">${msg.full_name}</td>
                    <td class="py-2">${msg.subject || 'No Subject'}</td>
                    <td class="py-2">${msg.status}</td>
                    <td class="py-2">
                        <button class="text-blue-400 hover:underline">Update</button>
                    </td>
                </tr>
            `).join('');
        }

        // Load data on page load
        document.addEventListener('DOMContentLoaded', () => {
            loadStats();
            loadProperties();
            loadInquiries();
            loadMessages();
        });
    </script>
</body>
</html>
"""

# ========== DATABASE INITIALIZATION ==========
with app.app_context():
    db.create_all()
    create_admin_user()
    init_sample_data()

if __name__ == '__main__':
    app.run(debug=True)
