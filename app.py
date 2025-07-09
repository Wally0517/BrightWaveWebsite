from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect, url_for
from flask_cors import CORS
from flask_mail import Mail, Message
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import os
import logging
import re
from datetime import datetime
from time import time
from collections import defaultdict
import json

# ========== LOAD ENVIRONMENT VARIABLES ==========
load_dotenv()

# Validate required environment variables
required_envs = ['SECRET_KEY', 'DATABASE_URL', 'MAIL_USERNAME', 'MAIL_PASSWORD', 'MAIL_DEFAULT_SENDER']
missing_envs = [env for env in required_envs if not os.environ.get(env)]
if missing_envs:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_envs)}")

# ========== APP INITIALIZATION ==========
app = Flask(__name__, static_folder='.', static_url_path='')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
app.config['SESSION_COOKIE_SECURE'] = True  # Secure cookies for HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ========== DATABASE CONFIGURATION ==========
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'connect_args': {'sslmode': 'require'} if 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI'] else {}
}
db = SQLAlchemy(app)

# ========== LOGGING CONFIGURATION ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== CORS CONFIGURATION ==========
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "https://brightwaveproperties.online").split(",")
allowed_origins = [origin.strip() for origin in allowed_origins if origin.strip()]
if not allowed_origins:
    logger.warning("No allowed origins configured for CORS")
CORS(app, origins=allowed_origins, supports_credentials=True)

# ========== EMAIL CONFIGURATION ==========
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'True') == 'True'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER')
mail = Mail(app)

# List of email addresses to notify
NOTIFICATION_EMAILS = os.environ.get('NOTIFICATION_EMAILS', '').split(',')
NOTIFICATION_EMAILS = [email.strip() for email in NOTIFICATION_EMAILS if email.strip()]
if not NOTIFICATION_EMAILS:
    logger.warning("No notification emails configured")

# ========== RATE LIMITING ==========
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

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
    property_type = db.Column(db.String(50), nullable=False)
    location = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Float, nullable=True)
    price_type = db.Column(db.String(20), nullable=True)
    total_rooms = db.Column(db.Integer, nullable=True)
    available_rooms = db.Column(db.Integer, nullable=True)
    size = db.Column(db.String(50), nullable=True)
    amenities = db.Column(db.JSON, nullable=True)
    images = db.Column(db.JSON, nullable=True)
    status = db.Column(db.String(20), default='active')
    construction_status = db.Column(db.String(30), nullable=True)
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
    inquiry_type = db.Column(db.String(30), nullable=False)
    preferred_move_date = db.Column(db.Date, nullable=True)
    budget_range = db.Column(db.String(50), nullable=True)
    message = db.Column(db.Text, nullable=False)
    university = db.Column(db.String(100), nullable=True)
    year_of_study = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), default='new')
    priority = db.Column(db.String(10), default='medium')
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
    status = db.Column(db.String(20), default='new')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ========== UTILITY FUNCTIONS ==========
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def create_admin_user():
    """Create default admin user if none exists"""
    if not Admin.query.first():
        admin = Admin(
            username='admin',
            email='admin@brightwaveproperties.online',  # Updated email
            password_hash=generate_password_hash('admin123')
        )
        db.session.add(admin)
        db.session.commit()
        logger.info("Default admin user created. Username: admin, Password: admin123")

def init_sample_data():
    """Initialize with sample property data"""
    if not Property.query.first():
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

@app.route('/assets/<path:filename>')
def serve_static_assets(filename):
    return send_from_directory('assets', filename)

# ========== PROPERTY API ROUTES ==========
@app.route('/api/properties', methods=['GET'])
def get_properties():
    """Get all properties with filtering options"""
    try:
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
    except Exception as e:
        logger.error(f"Error fetching properties: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/api/properties/<int:property_id>', methods=['GET'])
def get_property(property_id):
    """Get specific property details"""
    try:
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
    except Exception as e:
        logger.error(f"Error fetching property {property_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== CONTACT FORM API ==========
@app.route('/api/contact', methods=['POST'])
def handle_contact_form():
    """Handle general contact form submissions"""
    try:
        data = request.get_json()
        full_name = data.get('fullName', '').strip()
        email = data.get('email', '').strip()
        phone = data.get('phone', '').strip()
        subject = data.get('subject', '').strip()
        message = data.get('message', '').strip()

        if not all([full_name, email, phone, message]):
            return jsonify({"success": False, "message": "All fields are required."}), 400

        contact_message = ContactMessage(
            full_name=full_name,
            email=email,
            phone=phone,
            subject=subject,
            message=message
        )
        db.session.add(contact_message)
        db.session.commit()

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
                confirmation_msg = Message(
                    subject="Thank You for Contacting BrightWave Properties",  # Updated
                    recipients=[email],
                    body=f"""
                    Dear {full_name},

                    Thank you for your message! We have received your inquiry and will get back to you within 24-48 hours.

                    Best regards,
                    BrightWave Properties Team  # Updated
                    """
                )
                mail.send(confirmation_msg)
            except Exception as e:
                logger.error(f"Error sending email: {str(e)}")

        return jsonify({"success": True, "message": "Thank you! Your message has been received."})
    except Exception as e:
        logger.error(f"Error handling contact form: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/api/property-inquiry', methods=['POST'])
def handle_property_inquiry():
    """Handle property-specific inquiries"""
    try:
        data = request.get_json()
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

        if not all([full_name, email, phone, message]):
            return jsonify({"success": False, "message": "All required fields must be filled."}), 400

        move_date = None
        if preferred_move_date:
            try:
                move_date = datetime.strptime(preferred_move_date, '%Y-%m-%d').date()
            except ValueError:
                pass

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

        property_info = ""
        if property_id:
            property = Property.query.get(property_id)
            if property:
                property_info = f"Property: {property.title} ({property.location})\n"

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
                confirmation_msg = Message(
                    subject="Thank You for Your Property Inquiry",  # Updated
                    recipients=[email],
                    body=f"""
                    Dear {full_name},

                    Thank you for your interest in our properties! We have received your inquiry and our team will contact you within 24-48 hours.

                    Best regards,
                    BrightWave Properties Team  # Updated
                    """
                )
                mail.send(confirmation_msg)
            except Exception as e:
                logger.error(f"Error sending email: {str(e)}")

        return jsonify({"success": True, "message": "Thank you! Your inquiry has been received."})
    except Exception as e:
        logger.error(f"Error handling property inquiry: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== FILE UPLOAD API ==========
@app.route('/admin/api/upload', methods=['POST'])
@login_required
def upload_image():
    """Handle property image uploads"""
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "message": "No file provided"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "message": "No file selected"}), 400
        if file and allowed_file(file.filename):
            filename = secure_filename(f"{int(time())}_{file.filename}")
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            return jsonify({"success": True, "filename": f"images/properties/{filename}"})
        return jsonify({"success": False, "message": "Invalid file type"}), 400
    except Exception as e:
        logger.error(f"Error uploading image: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== ADMIN AUTHENTICATION ==========
@app.route('/admin/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def admin_login():
    """Handle admin login"""
    if request.method == 'POST':
        try:
            data = request.get_json()
            username = data.get('username')
            password = data.get('password')
            
            if not username or not password:
                return jsonify({"success": False, "message": "Username and password required"}), 400
            
            admin = Admin.query.filter_by(username=username, is_active=True).first()
            
            if admin and check_password_hash(admin.password_hash, password):
                session['admin_id'] = admin.id
                return jsonify({"success": True, "message": "Login successful", "redirect": "/admin/dashboard"})
            else:
                return jsonify({"success": False, "message": "Invalid credentials"}), 401
        except Exception as e:
            logger.error(f"Error during login: {str(e)}")
            return jsonify({"success": False, "message": "Internal server error"}), 500
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/admin/logout')
@login_required
def admin_logout():
    """Handle admin logout"""
    session.pop('admin_id', None)
    return redirect(url_for('admin_login'))

@app.route('/admin/api/update-password', methods=['POST'])
@login_required
def update_admin_password():
    """Update admin password"""
    try:
        data = request.get_json()
        new_password = data.get('newPassword')
        if not new_password or len(new_password) < 8:
            return jsonify({"success": False, "message": "Password must be at least 8 characters"}), 400
        
        admin = Admin.query.get(session['admin_id'])
        if admin:
            admin.password_hash = generate_password_hash(new_password)
            db.session.commit()
            return jsonify({"success": True, "message": "Password updated successfully"})
        return jsonify({"success": False, "message": "Admin not found"}), 404
    except Exception as e:
        logger.error(f"Error updating password: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== ADMIN DASHBOARD ==========
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    """Render admin dashboard"""
    return render_template_string(ADMIN_DASHBOARD_TEMPLATE)

@app.route('/admin/api/stats')
@login_required
def admin_stats():
    """Get dashboard statistics"""
    try:
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
    except Exception as e:
        logger.error(f"Error fetching stats: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/properties', methods=['GET', 'POST'])
@login_required
def admin_properties():
    """Handle property CRUD operations"""
    if request.method == 'GET':
        try:
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
                'size': prop.size,
                'amenities': prop.amenities or [],
                'images': prop.images or [],
                'status': prop.status,
                'construction_status': prop.construction_status,
                'completion_date': prop.completion_date.isoformat() if prop.completion_date else None,
                'featured': prop.featured,
                'created_at': prop.created_at.isoformat()
            } for prop in properties])
        except Exception as e:
            logger.error(f"Error fetching properties: {str(e)}")
            return jsonify({"success": False, "message": "Internal server error"}), 500

    elif request.method == 'POST':
        try:
            data = request.get_json()
            if not all([data.get('title'), data.get('description'), data.get('property_type'), data.get('location')]):
                return jsonify({"success": False, "message": "Required fields missing"}), 400
            
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
            return jsonify({"success": True, "message": "Property created successfully", "id": property.id})
        except Exception as e:
            logger.error(f"Error creating property: {str(e)}")
            return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/properties/<int:property_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_property_detail(property_id):
    """Update or delete property"""
    try:
        property = Property.query.get_or_404(property_id)
        
        if request.method == 'PUT':
            data = request.get_json()
            if not all([data.get('title'), data.get('description'), data.get('property_type'), data.get('location')]):
                return jsonify({"success": False, "message": "Required fields missing"}), 400
            
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
        
        elif request.method == 'DELETE':
            db.session.delete(property)
            db.session.commit()
            return jsonify({"success": True, "message": "Property deleted successfully"})
    except Exception as e:
        logger.error(f"Error handling property {property_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/inquiries', methods=['GET'])
@login_required
def admin_get_inquiries():
    """Get all property inquiries"""
    try:
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
    except Exception as e:
        logger.error(f"Error fetching inquiries: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/inquiries/<int:inquiry_id>', methods=['PUT'])
@login_required
def admin_update_inquiry(inquiry_id):
    """Update inquiry status and priority"""
    try:
        inquiry = PropertyInquiry.query.get_or_404(inquiry_id)
        data = request.get_json()
        
        inquiry.status = data.get('status', inquiry.status)
        inquiry.priority = data.get('priority', inquiry.priority)
        inquiry.updated_at = datetime.utcnow()
        
        db.session.commit()
        return jsonify({"success": True, "message": "Inquiry updated successfully"})
    except Exception as e:
        logger.error(f"Error updating inquiry {inquiry_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/contact-messages', methods=['GET'])
@login_required
def admin_get_contact_messages():
    """Get all contact messages"""
    try:
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
    except Exception as e:
        logger.error(f"Error fetching contact messages: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/contact-messages/<int:message_id>', methods=['PUT'])
@login_required
def admin_update_contact_message(message_id):
    """Update contact message status"""
    try:
        message = ContactMessage.query.get_or_404(message_id)
        data = request.get_json()
        
        message.status = data.get('status', message.status)
        db.session.commit()
        return jsonify({"success": True, "message": "Contact message updated successfully"})
    except Exception as e:
        logger.error(f"Error updating contact message {message_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== ADMIN TEMPLATES ==========
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login - BrightWave Properties</title>  <!-- Updated -->
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
    <title>Admin Dashboard - BrightWave Properties</title>  <!-- Updated -->
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <header class="bg-gray-800 shadow">
        <div class="max-w-7xl mx-auto py-4 px-4 sm:px-6 lg:px-8 flex justify-between items-center">
            <h1 class="text-2xl font-bold text-blue-400">BrightWave Admin Dashboard</h1>
            <div>
                <button id="changePasswordBtn" class="text-blue-400 hover:text-blue-300 mr-4">Change Password</button>
                <a href="/admin/logout" class="text-blue-400 hover:text-blue-300">Logout</a>
            </div>
        </div>
    </header>
    <main class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <!-- Change Password Form -->
        <section id="passwordForm" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Change Password</h2>
            <form id="updatePasswordForm" class="bg-gray-800 p-4 rounded-lg space-y-4">
                <div>
                    <label class="block text-sm font-medium mb-2">New Password</label>
                    <input type="password" id="newPassword" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <button type="submit" class="bg-blue-500 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg">Update Password</button>
                    <button type="button" id="cancelPassword" class="bg-gray-600 hover:bg-gray-700 text-white font-medium py-2 px-4 rounded-lg ml-2">Cancel</button>
                </div>
                <p id="passwordMessage" class="text-red-500 text-sm hidden"></p>
            </form>
        </section>
        <!-- Add Property Form -->
        <section class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Add New Property</h2>
            <form id="addPropertyForm" class="bg-gray-800 p-4 rounded-lg space-y-4">
                <div>
                    <label class="block text-sm font-medium mb-2">Title</label>
                    <input type="text" id="title" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Description</label>
                    <textarea id="description" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></textarea>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Property Type</label>
                    <select id="property_type" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                        <option value="hostel">Hostel</option>
                        <option value="land">Land</option>
                        <option value="residential">Residential</option>
                        <option value="estate">Estate</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Location</label>
                    <input type="text" id="location" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Price</label>
                    <input type="number" id="price" step="0.01" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Price Type</label>
                    <select id="price_type" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                        <option value="">Select</option>
                        <option value="per_semester">Per Semester</option>
                        <option value="per_year">Per Year</option>
                        <option value="per_sqm">Per Sqm</option>
                        <option value="total">Total</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Total Rooms</label>
                    <input type="number" id="total_rooms" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Available Rooms</label>
                    <input type="number" id="available_rooms" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Size</label>
                    <input type="text" id="size" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Amenities (comma-separated)</label>
                    <input type="text" id="amenities" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Construction Status</label>
                    <select id="construction_status" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                        <option value="">Select</option>
                        <option value="planned">Planned</option>
                        <option value="ongoing">Ongoing</option>
                        <option value="completed">Completed</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Completion Date</label>
                    <input type="date" id="completion_date" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Featured</label>
                    <input type="checkbox" id="featured" class="h-4 w-4 text-blue-500">
                </div>
                <div>
                    <button type="submit" class="bg-blue-500 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg">Add Property</button>
                </div>
            </form>
        </section>
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
        async function fetchData(url, options = {}) {
            const response = await fetch(url, {
                credentials: 'include',
                ...options
            });
            if (!response.ok) throw new Error('Network response was not ok');
            return response.json();
        }

        async function loadStats() {
            try {
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
            } catch (error) {
                console.error('Error loading stats:', error);
            }
        }

        async function loadProperties() {
            try {
                const properties = await fetchData('/admin/api/properties');
                const tableBody = document.getElementById('propertiesTable');
                tableBody.innerHTML = properties.map(prop => `
                    <tr class="border-b border-gray-600">
                        <td class="py-2">${prop.title}</td>
                        <td class="py-2">${prop.property_type}</td>
                        <td class="py-2">${prop.status}</td>
                        <td class="py-2">
                            <button onclick="editProperty(${prop.id}, '${prop.title}', '${prop.description}', '${prop.property_type}', '${prop.location}', '${prop.price || ''}', '${prop.price_type || ''}', '${prop.total_rooms || ''}', '${prop.available_rooms || ''}', '${prop.size || ''}', '${prop.amenities ? prop.amenities.join(',') : ''}', '${prop.construction_status || ''}', '${prop.completion_date || ''}', ${prop.featured})" class="text-blue-400 hover:underline">Edit</button>
                            <button onclick="deleteProperty(${prop.id})" class="text-red-400 hover:underline ml-2">Delete</button>
                        </td>
                    </tr>
                `).join('');
            } catch (error) {
                console.error('Error loading properties:', error);
            }
        }

        async function loadInquiries() {
            try {
                const inquiries = await fetchData('/admin/api/inquiries');
                const tableBody = document.getElementById('inquiriesTable');
                tableBody.innerHTML = inquiries.map(inq => `
                    <tr class="border-b border-gray-600">
                        <td class="py-2">${inq.full_name}</td>
                        <td class="py-2">${inq.property_title}</td>
                        <td class="py-2">${inq.status}</td>
                        <td class="py-2">
                            <select onchange="updateInquiry(${inq.id}, this.value)">
                                <option value="new" ${inq.status === 'new' ? 'selected' : ''}>New</option>
                                <option value="contacted" ${inq.status === 'contacted' ? 'selected' : ''}>Contacted</option>
                                <option value="qualified" ${inq.status === 'qualified' ? 'selected' : ''}>Qualified</option>
                                <option value="converted" ${inq.status === 'converted' ? 'selected' : ''}>Converted</option>
                                <option value="closed" ${inq.status === 'closed' ? 'selected' : ''}>Closed</option>
                            </select>
                        </td>
                    </tr>
                `).join('');
            } catch (error) {
                console.error('Error loading inquiries:', error);
            }
        }

        async function loadMessages() {
            try {
                const messages = await fetchData('/admin/api/contact-messages');
                const tableBody = document.getElementById('messagesTable');
                tableBody.innerHTML = messages.map(msg => `
                    <tr class="border-b border-gray-600">
                        <td class="py-2">${msg.full_name}</td>
                        <td class="py-2">${msg.subject || 'No Subject'}</td>
                        <td class="py-2">${msg.status}</td>
                        <td class="py-2">
                            <select onchange="updateMessage(${msg.id}, this.value)">
                                <option value="new" ${msg.status === 'new' ? 'selected' : ''}>New</option>
                                <option value="read" ${msg.status === 'read' ? 'selected' : ''}>Read</option>
                                <option value="responded" ${msg.status === 'responded' ? 'selected' : ''}>Responded</option>
                                <option value="closed" ${msg.status === 'closed' ? 'selected' : ''}>Closed</option>
                            </select>
                        </td>
                    </tr>
                `).join('');
            } catch (error) {
                console.error('Error loading messages:', error);
            }
        }

        async function updateInquiry(id, status) {
            try {
                const response = await fetchData(`/admin/api/inquiries/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status })
                });
                alert(response.message);
                loadInquiries();
            } catch (error) {
                alert('Error updating inquiry');
            }
        }

        async function updateMessage(id, status) {
            try {
                const response = await fetchData(`/admin/api/contact-messages/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status })
                });
                alert(response.message);
                loadMessages();
            } catch (error) {
                alert('Error updating message');
            }
        }

        async function deleteProperty(id) {
            if (confirm('Are you sure you want to delete this property?')) {
                try {
                    const response = await fetchData(`/admin/api/properties/${id}`, {
                        method: 'DELETE'
                    });
                    alert(response.message);
                    loadProperties();
                } catch (error) {
                    alert('Error deleting property');
                }
            }
        }

        function editProperty(id, title, description, property_type, location, price, price_type, total_rooms, available_rooms, size, amenities, construction_status, completion_date, featured) {
            document.getElementById('addPropertyForm').innerHTML = `
                <h2 class="text-xl font-semibold mb-4">Edit Property</h2>
                <input type="hidden" id="property_id" value="${id}">
                <div>
                    <label class="block text-sm font-medium mb-2">Title</label>
                    <input type="text" id="title" value="${title}" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Description</label>
                    <textarea id="description" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">${description}</textarea>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Property Type</label>
                    <select id="property_type" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                        <option value="hostel" ${property_type === 'hostel' ? 'selected' : ''}>Hostel</option>
                        <option value="land" ${property_type === 'land' ? 'selected' : ''}>Land</option>
                        <option value="residential" ${property_type === 'residential' ? 'selected' : ''}>Residential</option>
                        <option value="estate" ${property_type === 'estate' ? 'selected' : ''}>Estate</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Location</label>
                    <input type="text" id="location" value="${location}" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Price</label>
                    <input type="number" id="price" step="0.01" value="${price}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Price Type</label>
                    <select id="price_type" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                        <option value="">Select</option>
                        <option value="per_semester" ${price_type === 'per_semester' ? 'selected' : ''}>Per Semester</option>
                        <option value="per_year" ${price_type === 'per_year' ? 'selected' : ''}>Per Year</option>
                        <option value="per_sqm" ${price_type === 'per_sqm' ? 'selected' : ''}>Per Sqm</option>
                        <option value="total" ${price_type === 'total' ? 'selected' : ''}>Total</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Total Rooms</label>
                    <input type="number" id="total_rooms" value="${total_rooms}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Available Rooms</label>
                    <input type="number" id="available_rooms" value="${available_rooms}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Size</label>
                    <input type="text" id="size" value="${size}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Amenities (comma-separated)</label>
                    <input type="text" id="amenities" value="${amenities}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Construction Status</label>
                    <select id="construction_status" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                        <option value="">Select</option>
                        <option value="planned" ${construction_status === 'planned' ? 'selected' : ''}>Planned</option>
                        <option value="ongoing" ${construction_status === 'ongoing' ? 'selected' : ''}>Ongoing</option>
                        <option value="completed" ${construction_status === 'completed' ? 'selected' : ''}>Completed</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Completion Date</label>
                    <input type="date" id="completion_date" value="${completion_date}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Featured</label>
                    <input type="checkbox" id="featured" ${featured ? 'checked' : ''} class="h-4 w-4 text-blue-500">
                </div>
                <div>
                    <button type="submit" class="bg-blue-500 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg">Update Property</button>
                    <button type="button" onclick="resetPropertyForm()" class="bg-gray-600 hover:bg-gray-700 text-white font-medium py-2 px-4 rounded-lg ml-2">Cancel</button>
                </div>
            `;
            document.getElementById('addPropertyForm').onsubmit = async (e) => {
                e.preventDefault();
                const id = document.getElementById('property_id').value;
                const propertyData = {
                    title: document.getElementById('title').value,
                    description: document.getElementById('description').value,
                    property_type: document.getElementById('property_type').value,
                    location: document.getElementById('location').value,
                    price: parseFloat(document.getElementById('price').value) || null,
                    price_type: document.getElementById('price_type').value || null,
                    total_rooms: parseInt(document.getElementById('total_rooms').value) || null,
                    available_rooms: parseInt(document.getElementById('available_rooms').value) || null,
                    size: document.getElementById('size').value || null,
                    amenities: document.getElementById('amenities').value.split(',').map(a => a.trim()).filter(a => a),
                    construction_status: document.getElementById('construction_status').value || null,
                    completion_date: document.getElementById('completion_date').value || null,
                    featured: document.getElementById('featured').checked
                };
                try {
                    const response = await fetchData(`/admin/api/properties/${id}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(propertyData)
                    });
                    alert(response.message);
                    resetPropertyForm();
                    loadProperties();
                } catch (error) {
                    alert('Error updating property');
                }
            };
        }

        function resetPropertyForm() {
            document.getElementById('addPropertyForm').innerHTML = `
                <h2 class="text-xl font-semibold mb-4">Add New Property</h2>
                <div>
                    <label class="block text-sm font-medium mb-2">Title</label>
                    <input type="text" id="title" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Description</label>
                    <textarea id="description" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></textarea>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Property Type</label>
                    <select id="property_type" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                        <option value="hostel">Hostel</option>
                        <option value="land">Land</option>
                        <option value="residential">Residential</option>
                        <option value="estate">Estate</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Location</label>
                    <input type="text" id="location" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Price</label>
                    <input type="number" id="price" step="0.01" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Price Type</label>
                    <select id="price_type" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                        <option value="">Select</option>
                        <option value="per_semester">Per Semester</option>
                        <option value="per_year">Per Year</option>
                        <option value="per_sqm">Per Sqm</option>
                        <option value="total">Total</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Total Rooms</label>
                    <input type="number" id="total_rooms" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Available Rooms</label>
                    <input type="number" id="available_rooms" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Size</label>
                    <input type="text" id="size" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Amenities (comma-separated)</label>
                    <input type="text" id="amenities" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Construction Status</label>
                    <select id="construction_status" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                        <option value="">Select</option>
                        <option value="planned">Planned</option>
                        <option value="ongoing">Ongoing</option>
                        <option value="completed">Completed</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Completion Date</label>
                    <input type="date" id="completion_date" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">Featured</label>
                    <input type="checkbox" id="featured" class="h-4 w-4 text-blue-500">
                </div>
                <div>
                    <button type="submit" class="bg-blue-500 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg">Add Property</button>
                </div>
            `;
            document.getElementById('addPropertyForm').onsubmit = async (e) => {
                e.preventDefault();
                const propertyData = {
                    title: document.getElementById('title').value,
                    description: document.getElementById('description').value,
                    property_type: document.getElementById('property_type').value,
                    location: document.getElementById('location').value,
                    price: parseFloat(document.getElementById('price').value) || null,
                    price_type: document.getElementById('price_type').value || null,
                    total_rooms: parseInt(document.getElementById('total_rooms').value) || null,
                    available_rooms: parseInt(document.getElementById('available_rooms').value) || null,
                    size: document.getElementById('size').value || null,
                    amenities: document.getElementById('amenities').value.split(',').map(a => a.trim()).filter(a => a),
                    construction_status: document.getElementById('construction_status').value || null,
                    completion_date: document.getElementById('completion_date').value || null,
                    featured: document.getElementById('featured').checked
                };
                try {
                    const response = await fetchData('/admin/api/properties', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(propertyData)
                    });
                    alert(response.message);
                    loadProperties();
                } catch (error) {
                    alert('Error adding property');
                }
            };
        }

        document.getElementById('changePasswordBtn').addEventListener('click', () => {
            document.getElementById('passwordForm').classList.toggle('hidden');
        });

        document.getElementById('cancelPassword').addEventListener('click', () => {
            document.getElementById('passwordForm').classList.add('hidden');
            document.getElementById('newPassword').value = '';
            document.getElementById('passwordMessage').classList.add('hidden');
        });

        document.getElementById('updatePasswordForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const newPassword = document.getElementById('newPassword').value;
            const passwordMessage = document.getElementById('passwordMessage');
            try {
                const response = await fetchData('/admin/api/update-password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ newPassword })
                });
                passwordMessage.textContent = response.message;
                passwordMessage.classList.remove('hidden', 'text-red-500');
                passwordMessage.classList.add('text-green-500');
                setTimeout(() => {
                    document.getElementById('passwordForm').classList.add('hidden');
                    document.getElementById('newPassword').value = '';
                    passwordMessage.classList.add('hidden');
                }, 2000);
            } catch (error) {
                passwordMessage.textContent = 'Error updating password';
                passwordMessage.classList.remove('hidden');
                passwordMessage.classList.add('text-red-500');
            }
        });

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
    try:
        db.create_all()
        create_admin_user()
        init_sample_data()
    except Exception as e:
        logger.error(f"Database initialization error: {str(e)}")
        raise

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
