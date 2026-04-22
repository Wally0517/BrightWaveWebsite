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
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== LOAD ENVIRONMENT VARIABLES ==========
load_dotenv()

# Validate required environment variables
required_envs = ['SECRET_KEY']
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
database_url = os.environ.get('DATABASE_URL')
if not database_url:
    database_url = 'sqlite:///brightwave_local.db'
    logger.warning("DATABASE_URL not set; falling back to local SQLite storage")

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'connect_args': {'sslmode': 'require'} if 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI'] else {}
}
db = SQLAlchemy(app)

# ========== CORS CONFIGURATION ==========
SITE_URL = os.environ.get("SITE_URL", "https://www.brightwavehabitat.com").rstrip("/")
default_allowed_origins = f"{SITE_URL},https://brightwavehabitat.com"
allowed_origins = os.environ.get("ALLOWED_ORIGINS", default_allowed_origins).split(",")
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
    phone = db.Column(db.String(20), nullable=True)  # Made optional for about page
    subject = db.Column(db.String(200), nullable=True)
    message = db.Column(db.Text, nullable=False)
    form_origin = db.Column(db.String(50), default='Unknown')  # Track form source
    status = db.Column(db.String(20), default='new')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class SiteContent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(120), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TeamMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(120), nullable=False)
    bio = db.Column(db.Text, nullable=False)
    image_path = db.Column(db.String(255), nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

DEFAULT_SITE_CONTENT = {
    'home.hero_badge': 'Trusted property for students, families, and investors',
    'home.hero_title': 'Property opportunities in Nigeria, presented with real proof and clear process.',
    'home.hero_subtitle': 'BrightWave Habitat Enterprise serves clients looking for live student accommodation, credible land opportunities, family homes, and long-term estate projects, starting with an active flagship already operating in Malete, Kwara State.',
    'home.about_intro': 'BrightWave Habitat Enterprise is a Nigerian property business focused on student accommodation, land opportunities, residential homes, and estate growth, with BrightWave Hostel Phase 1 in Malete as the first live proof of delivery.',
    'about.hero_subtitle': 'A focused Nigerian property company building credibility through real delivery, clear communication, and a premium standard of presentation.',
    'about.intro_body': 'BrightWave Habitat Enterprise is a Nigerian real estate business founded to compete at the top end of trust and presentation. We serve students, families, and investors through student hostels, land opportunities, residential homes, and estate development, with Phase 1 in Malete as the first live proof of our standard.',
    'about.team_heading': 'Meet the Active Team',
    'about.team_subheading': 'The people currently responsible for delivery, operations, and client support at BrightWave.'
}

LEGACY_SITE_CONTENT = {
    'home.hero_badge': 'Trusted property for students, families, and investors',
    'home.hero_title': 'Building trusted property opportunities across Nigeria.',
    'home.hero_subtitle': 'BrightWave Habitat Enterprise serves students, families, and investors with live student accommodation in Malete and broader property opportunities presented with clearer process and stronger trust.',
    'home.about_intro': 'BrightWave Habitat Enterprise is a Nigerian property business focused on student accommodation, land opportunities, residential homes, and estate growth, with BrightWave Hostel Phase 1 in Malete as the first live proof of delivery.',
    'about.hero_subtitle': 'A growing Nigerian property company building credibility through delivered projects, clear communication, and steady expansion.',
    'about.intro_body': 'BrightWave Habitat Enterprise is a Nigerian real estate business serving students, families, and investors through student hostels, land opportunities, residential homes, and estate development. Phase 1 in Malete is the current flagship delivery, while the broader company pipeline continues to grow around real execution.',
}

OLDER_LEGACY_SITE_CONTENT = {
    'home.hero_badge': 'Now Open: BrightWave Hostel Phase 1',
    'home.hero_title': 'Affordable student rooms that are ready in Malete.',
    'home.hero_subtitle': 'The first 10 self-contained rooms are now available with solar backup, water, security, and easy access to campus life in Kwara State.',
    'home.about_intro': 'BrightWave Habitat Enterprise is focused on real, present inventory first. Phase 1 is open now, while future hostel, land, and residential projects are shown clearly as upcoming.',
    'about.hero_subtitle': 'A focused Nigerian property company building credibility through real delivery, starting with BrightWave Hostel Phase 1 in Malete.',
    'about.intro_body': 'BrightWave Habitat Enterprise is a Nigerian real estate business founded to build housing people can actually trust. We are currently leading with BrightWave Hostel Phase 1 in Malete, while future hostels, land opportunities, and residential projects remain in the pipeline until they are ready to be presented properly.',
}

DEFAULT_TEAM_MEMBERS = [
    {
        'name': 'Wally H.',
        'role': 'Founder & CEO',
        'bio': 'Wally leads BrightWave Habitat Enterprise with a practical focus on student housing delivery, transparent communication, and steady long-term growth.',
        'image_path': 'images/ceo-wally.jpg',
        'sort_order': 1,
        'is_active': True,
    },
    {
        'name': 'Al-Ameen A.',
        'role': 'Project & Property Manager',
        'bio': 'Al-Ameen oversees site standards, property readiness, and day-to-day operational quality across BrightWave projects.',
        'image_path': 'images/property-manager-alameen.jpg',
        'sort_order': 2,
        'is_active': True,
    },
    {
        'name': 'Kamal B.',
        'role': 'Realtor',
        'bio': 'Kamal manages client conversations and helps prospects move from inquiry to a clear property decision.',
        'image_path': 'images/realtor-kamal.jpg',
        'sort_order': 3,
        'is_active': True,
    }
]

# ========== UTILITY FUNCTIONS ==========
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def ensure_cms_baseline():
    """Create CMS tables and seed defaults lazily for existing deployments."""
    db.create_all()
    changes_made = False

    try:
        for slug, value in DEFAULT_SITE_CONTENT.items():
            existing_item = SiteContent.query.filter_by(slug=slug).first()
            if not existing_item:
                db.session.add(SiteContent(slug=slug, value=value))
                changes_made = True
            elif existing_item.value in {LEGACY_SITE_CONTENT.get(slug), OLDER_LEGACY_SITE_CONTENT.get(slug)}:
                existing_item.value = value
                changes_made = True

        if not TeamMember.query.first():
            for member in DEFAULT_TEAM_MEMBERS:
                db.session.add(TeamMember(**member))
                changes_made = True

        if changes_made:
            db.session.commit()
    except Exception:
        db.session.rollback()
        raise

def get_site_content():
    ensure_cms_baseline()
    return {
        item.slug: item.value
        for item in SiteContent.query.order_by(SiteContent.slug.asc()).all()
    }

def serialize_team_member(member):
    return {
        'id': member.id,
        'name': member.name,
        'role': member.role,
        'bio': member.bio,
        'image_path': member.image_path,
        'sort_order': member.sort_order,
        'is_active': member.is_active,
        'created_at': member.created_at.isoformat(),
        'updated_at': member.updated_at.isoformat(),
    }

def create_admin_user():
    """Create an admin user only when bootstrap credentials are supplied."""
    username = os.environ.get('ADMIN_BOOTSTRAP_USERNAME')
    email = os.environ.get('ADMIN_BOOTSTRAP_EMAIL')
    password = os.environ.get('ADMIN_BOOTSTRAP_PASSWORD')

    if not all([username, email, password]):
        logger.info("Admin bootstrap skipped: bootstrap credentials were not provided")
        return False

    existing_admin = Admin.query.filter(
        (Admin.username == username) | (Admin.email == email)
    ).first()
    if existing_admin:
        logger.info("Admin bootstrap skipped: requested admin already exists")
        return False

    admin = Admin(
        username=username,
        email=email,
        password_hash=generate_password_hash(password)
    )
    db.session.add(admin)
    try:
        db.session.commit()
        logger.info("Bootstrap admin user created for %s", email)
        return True
    except IntegrityError:
        db.session.rollback()
        logger.warning("Admin bootstrap skipped after integrity conflict")
        return False

def init_sample_data():
    if Property.query.first():
        return

    # --- Hostels (keep images) ---
    phase1 = Property(
        title='BrightWave Phase 1 Hostel',
        description='Modern 10-room self-contained hostel near KWASU with private bathrooms, kitchens, 24/7 security, and solar power.',
        property_type='hostel',
        location='Malete, Kwara State, Nigeria',
        price=None, price_type='Now Open - Contact for Rates',
        total_rooms=10, available_rooms=10,
        amenities=['Private Bathroom','Private Kitchen','24/7 Security','Solar Power','CCTV','Water Supply','Parking Space'],
        images=['images/phase1/phase1-main-entrance.jpg', 'images/phase1/phase1-aerial-topview.jpg'],
        construction_status='completed', completion_date=datetime(2026, 3, 25).date(),
        featured=True, status='active'
    )

    phase2 = Property(
        title='BrightWave Hostel Phase 2',
        description='30-room modern hostel with enhanced amenities.',
        property_type='hostel', location='Malete, Kwara State',
        price=480000, price_type='per session',
        total_rooms=20, available_rooms=20,
        amenities=['Self-contained rooms','24/7 Security & CCTV','Solar power backup','Recreation facilities','Study Areas','Common Spaces'],
        images=['images/hostels/brightwave-phase2-render.jpg'],
        construction_status='planning', completion_date=datetime(2027, 6, 30).date(),
        featured=False, status='active'
    )

    phase3 = Property(
        title='BrightWave Hostel Phase 3',
        description='40-room premium hostel complex with gym, library, and recreational facilities.',
        property_type='hostel', location='GreenCity, Malete, Kwara State',
        price=520000, price_type='per session',
        total_rooms=40, available_rooms=40,
        amenities=['Self-contained rooms','24/7 Security & CCTV','Solar power backup','Gym','Library','Recreation facilities'],
        images=['images/hostels/brightwave-phase3-concept.jpg'],
        construction_status='pending', completion_date=datetime(2028, 12, 31).date(),
        featured=False, status='active'
    )

    # --- Lands ---
    # Visible + active (the only one):
    land_obada_ikija = Property(
        title='BrightWave Estate - Obada Ikija',
        description='6 acres of prime residential land at Obada Ikija, Abeokuta.',
        property_type='land', location='Obada Ikija, Abeokuta, Ogun State',
        price=2500000, price_type='per_sqm', size='6 acres',
        amenities=['Gated Community','Electricity','Water Supply','Good Road Network','Security','Recreational Facilities'],
        images=['images/lands/brightwave-obada_ikija.jpg'],
        construction_status='completed', featured=True, status='active'
    )

    # Keep these but show “Coming soon” (no images) — OR set status='inactive' to hide entirely.
    land_fate = Property(
        title='Investment Land - Fate Road',
        description='800sqm investment plot in Kwara State. Ideal for development or long-term investment.',
        property_type='land', location='Kwara State',
        price=1000000, price_type='per plot (800sqm)', size='800sqm',
        amenities=['Clear documentation','Strategic location','Flexible payment plans','Investment guidance'],
        images=[], construction_status='planning', featured=False, status='active'
    )

    # --- Homes (future) ---
    # Removed home_gra (4-Bedroom Duplex - GRA)

    home_adewole = Property(
        title='3-Bedroom Bungalow - Adewole',
        description='Contemporary 3-bedroom bungalow in planned estate.',
        property_type='residential', location='Adewole Estate, Ilorin, Kwara State',
        price=None, price_type='Coming 2026', total_rooms=3,
        amenities=['Modern designs','Quality construction','Estate development','Contemporary style'],
        images=[], construction_status='planning', completion_date=datetime(2026, 8, 31).date(),
        featured=False, status='active'
    )

    db.session.add_all([
        phase1, phase2, phase3,
        land_obada_ikija, land_fate,
        home_adewole
    ])
    db.session.commit()


def reconcile_property_catalog():
    """Keep live catalog visibility aligned with the current market-facing site."""
    hidden_titles = {
        'Investment Land - Fate Road',
        '3-Bedroom Bungalow - Adewole',
    }
    coming_soon_titles = {
        'BrightWave Hostel Phase 2',
        'BrightWave Hostel Phase 3',
        'BrightWave Estate - Obada Ikija',
    }

    properties = Property.query.all()
    changed = False

    for prop in properties:
        if prop.title in hidden_titles:
            if prop.status != 'inactive':
                prop.status = 'inactive'
                changed = True
            if prop.featured:
                prop.featured = False
                changed = True

        if prop.title in coming_soon_titles:
            if prop.construction_status != 'coming-soon':
                prop.construction_status = 'coming-soon'
                changed = True
            if prop.title == 'BrightWave Estate - Obada Ikija' and prop.featured:
                prop.featured = False
                changed = True

    if changed:
        db.session.commit()


def initialize_app_state(include_sample_data=False, bootstrap_admin=False):
    """Run one-time database initialization outside the web worker startup path."""
    db.create_all()
    ensure_cms_baseline()
    if include_sample_data:
        init_sample_data()
    reconcile_property_catalog()
    if bootstrap_admin:
        create_admin_user()

# ========== AUTHENTICATION FUNCTIONS ==========
def login_required(f):
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

CANONICAL_HOST = SITE_URL.replace("https://", "").replace("http://", "")
REDIRECT_HOSTS = {
    'brightwaveenterprises.online',
    'www.brightwaveenterprises.online',
    'brightwavehabitat.com',
}
CANONICAL_ROUTE_MAP = {
    '/index.html': '/',
    '/about.html': '/about',
    '/faq.html': '/faq',
    '/hostel-detail.html': '/hostels/detail',
}


@app.before_request
def enforce_canonical_urls():
    host = request.host.split(':', 1)[0].lower()
    if request.path == '/health' or host in {'localhost', '127.0.0.1'}:
        return None

    if host in REDIRECT_HOSTS:
        query = f"?{request.query_string.decode()}" if request.query_string else ""
        return redirect(f"{SITE_URL}{request.path}{query}", code=301)

    if request.path in CANONICAL_ROUTE_MAP:
        query = f"?{request.query_string.decode()}" if request.query_string else ""
        return redirect(f"{SITE_URL}{CANONICAL_ROUTE_MAP[request.path]}{query}", code=301)

    if host != CANONICAL_HOST and host:
        query = f"?{request.query_string.decode()}" if request.query_string else ""
        return redirect(f"{SITE_URL}{request.path}{query}", code=301)

    return None

# ========== STATIC PAGE ROUTES ==========
@app.route('/')
def serve_homepage():
    return send_from_directory('.', 'index.html')

@app.route('/about')
def serve_about():
    return send_from_directory('.', 'about.html') if os.path.exists('about.html') \
            else send_from_directory('.', 'index.html')
    

@app.route('/contact')
def serve_contact():
    return send_from_directory('.', 'contact.html') if os.path.exists('contact.html') \
            else send_from_directory('.', 'index.html')
    

@app.route('/faq')
def serve_faq():
    return send_from_directory('.', 'faq.html')

@app.route('/hostels')
def serve_hostels():
    return send_from_directory('.', 'hostels.html') if os.path.exists('hostels.html') \
            else send_from_directory('.', 'index.html')

@app.route('/hostels/detail')
def serve_hostel_detail():
    return send_from_directory('.', 'hostel-detail.html')

@app.route('/assets/<path:filename>')
def serve_static_assets(filename):
    return send_from_directory('assets', filename)

@app.route('/health')
def health():
    return 'ok', 200

@app.route('/api/site-content', methods=['GET'])
def get_public_site_content():
    try:
        return jsonify(get_site_content())
    except Exception as e:
        logger.error(f"Error fetching site content: {str(e)}")
        return jsonify(DEFAULT_SITE_CONTENT)

@app.route('/api/team-members', methods=['GET'])
def get_public_team_members():
    try:
        ensure_cms_baseline()
        members = TeamMember.query.filter_by(is_active=True).order_by(TeamMember.sort_order.asc(), TeamMember.created_at.asc()).all()
        return jsonify([serialize_team_member(member) for member in members])
    except Exception as e:
        logger.error(f"Error fetching team members: {str(e)}")
        return jsonify(DEFAULT_TEAM_MEMBERS)

# ========== PROPERTY API ROUTES ==========
@app.route('/api/properties', methods=['GET'])
def get_properties():
    """Get all properties with filtering options - matches frontend expectations"""
    try:
        property_type = request.args.get('type')
        status = request.args.get('status', 'active')
        featured = request.args.get('featured')
        
        query = Property.query.filter_by(status=status)
        
        if property_type:
            query = query.filter_by(property_type=property_type)
        
        if featured:
            query = query.filter_by(featured=True)
        
        properties = query.order_by(
            case((Property.featured.is_(True), 0), else_=1),
            case(
                (Property.construction_status == 'completed', 0),
                (Property.construction_status == 'ongoing-final', 1),
                (Property.construction_status == 'coming-soon', 2),
                (Property.construction_status == 'pending', 3),
                (Property.construction_status == 'planning', 4),
                else_=4
            ),
            case(
                (Property.property_type == 'hostel', 0),
                (Property.property_type == 'residential', 1),
                (Property.property_type == 'land', 2),
                else_=3
            ),
            Property.created_at.desc()
        ).all()
        
        # Format response to match frontend expectations
        formatted_properties = []
        for prop in properties:
            formatted_prop = {
                'id': prop.id,
                'title': prop.title,
                'description': prop.description,
                'type': prop.property_type,  # Frontend expects 'type' not 'property_type'
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
            }
            formatted_properties.append(formatted_prop)
        
        return jsonify(formatted_properties)
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
            'type': property.property_type,
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
@limiter.limit("3 per minute")  # Rate limit contact submissions
def handle_contact_form():
    """Handle contact form submissions from both homepage and about page"""
    try:
        data = request.get_json()
        full_name = data.get('fullName', '').strip()
        email = data.get('email', '').strip()
        phone = data.get('phone', '').strip()
        subject = data.get('subject', '').strip()
        message = data.get('message', '').strip()
        form_origin = data.get('formOrigin', 'Unknown')

        # Validate required fields
        if not all([full_name, email, message]):
            return jsonify({"success": False, "message": "Name, email, and message are required."}), 400

        # Email validation
        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_regex, email):
            return jsonify({"success": False, "message": "Please enter a valid email address."}), 400

        # Save to database
        contact_message = ContactMessage(
            full_name=full_name,
            email=email,
            phone=phone,
            subject=subject,
            message=message,
            form_origin=form_origin
        )
        db.session.add(contact_message)
        db.session.commit()

        # Send notification emails
        if NOTIFICATION_EMAILS:
            email_subject = f"New {form_origin} - {subject or 'General Inquiry'}"
            email_body = f"""
            New Contact Form Submission:

            Source: {form_origin}
            Name: {full_name}
            Email: {email}
            Phone: {phone or 'Not provided'}
            Subject: {subject or 'No subject'}
            
            Message:
            {message}

            Submitted at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            """
            try:
                # Send notification to admin
                msg = Message(
                    subject=email_subject,
                    recipients=NOTIFICATION_EMAILS,
                    body=email_body,
                    reply_to=email
                )
                mail.send(msg)
                
                # Send confirmation to user
                confirmation_msg = Message(
                    subject="Thank You for Contacting BrightWave Habitat Enterprise",
                    recipients=[email],
                    body=f"""
                    Dear {full_name},

                    Thank you for your message! We have received your inquiry and will get back to you within 24-48 hours.

                    Your message:
                    {message[:200]}{'...' if len(message) > 200 else ''}

                    Best regards,
                    BrightWave Habitat Enterprise Team
                    
                    Email: brightwavehabitat@gmail.com
                    WhatsApp: +234 803 766 9462, +234 903 840 2914
                    Location: Malete, Kwara State, Nigeria
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
@limiter.limit("3 per minute")
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

        # Email validation
        email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_regex, email):
            return jsonify({"success": False, "message": "Please enter a valid email address."}), 400

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
                    subject="Thank You for Your Property Inquiry",
                    recipients=[email],
                    body=f"""
                    Dear {full_name},

                    Thank you for your interest in our properties! We have received your inquiry and our team will contact you within 24-48 hours.

                    Best regards,
                    BrightWave Habitat Enterprise Team
                    """
                )
                mail.send(confirmation_msg)
            except Exception as e:
                logger.error(f"Error sending email: {str(e)}")

        return jsonify({"success": True, "message": "Thank you! Your inquiry has been received."})
    except Exception as e:
        logger.error(f"Error handling property inquiry: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== ADMIN DASHBOARD ENHANCEMENTS ==========
@app.route('/admin/api/stats')
@login_required
def admin_stats():
    """Get enhanced dashboard statistics"""
    try:
        ensure_cms_baseline()
        total_properties = Property.query.count()
        active_properties = Property.query.filter_by(status='active').count()
        hostels = Property.query.filter_by(property_type='hostel').count()
        land_plots = Property.query.filter_by(property_type='land').count()
        residential = Property.query.filter_by(property_type='residential').count()
        active_team_members = TeamMember.query.filter_by(is_active=True).count()
        
        total_inquiries = PropertyInquiry.query.count()
        new_inquiries = PropertyInquiry.query.filter_by(status='new').count()
        contact_messages = ContactMessage.query.count()
        new_messages = ContactMessage.query.filter_by(status='new').count()
        
        # Recent activity
        recent_inquiries = PropertyInquiry.query.order_by(PropertyInquiry.created_at.desc()).limit(5).all()
        recent_messages = ContactMessage.query.order_by(ContactMessage.created_at.desc()).limit(5).all()
        
        return jsonify({
            'total_properties': total_properties,
            'active_properties': active_properties,
            'property_breakdown': {
                'hostels': hostels,
                'land_plots': land_plots,
                'residential': residential
            },
            'total_inquiries': total_inquiries,
            'new_inquiries': new_inquiries,
            'contact_messages': contact_messages,
            'new_messages': new_messages,
            'active_team_members': active_team_members,
            'recent_activity': {
                'inquiries': [{
                    'id': inq.id,
                    'name': inq.full_name,
                    'inquiry_type': inq.inquiry_type,
                    'created_at': inq.created_at.strftime('%Y-%m-%d %H:%M')
                } for inq in recent_inquiries],
                'messages': [{
                    'id': msg.id,
                    'name': msg.full_name,
                    'form_origin': msg.form_origin,
                    'created_at': msg.created_at.strftime('%Y-%m-%d %H:%M')
                } for msg in recent_messages]
            }
        })
    except Exception as e:
        logger.error(f"Error fetching stats: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/site-content', methods=['GET', 'PUT'])
@login_required
def admin_site_content():
    try:
        ensure_cms_baseline()
        if request.method == 'GET':
            return jsonify(get_site_content())

        data = request.get_json() or {}
        for slug in DEFAULT_SITE_CONTENT.keys():
            if slug in data:
                existing = SiteContent.query.filter_by(slug=slug).first()
                if existing:
                    existing.value = str(data.get(slug, '')).strip()
                else:
                    db.session.add(SiteContent(slug=slug, value=str(data.get(slug, '')).strip()))
        db.session.commit()
        return jsonify({"success": True, "message": "Website content updated successfully"})
    except Exception as e:
        logger.error(f"Error updating site content: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/team-members', methods=['GET', 'POST'])
@login_required
def admin_team_members():
    try:
        ensure_cms_baseline()
        if request.method == 'GET':
            members = TeamMember.query.order_by(TeamMember.sort_order.asc(), TeamMember.created_at.asc()).all()
            return jsonify([serialize_team_member(member) for member in members])

        data = request.get_json() or {}
        if not all([data.get('name'), data.get('role'), data.get('bio')]):
            return jsonify({"success": False, "message": "Name, role, and bio are required"}), 400

        member = TeamMember(
            name=data['name'].strip(),
            role=data['role'].strip(),
            bio=data['bio'].strip(),
            image_path=(data.get('image_path') or '').strip() or None,
            sort_order=int(data.get('sort_order') or 0),
            is_active=bool(data.get('is_active', True))
        )
        db.session.add(member)
        db.session.commit()
        return jsonify({"success": True, "message": "Team member added successfully", "member": serialize_team_member(member)})
    except Exception as e:
        logger.error(f"Error creating team member: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/team-members/<int:member_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_team_member_detail(member_id):
    try:
        ensure_cms_baseline()
        member = TeamMember.query.get_or_404(member_id)

        if request.method == 'DELETE':
            db.session.delete(member)
            db.session.commit()
            return jsonify({"success": True, "message": "Team member removed successfully"})

        data = request.get_json() or {}
        if not all([data.get('name'), data.get('role'), data.get('bio')]):
            return jsonify({"success": False, "message": "Name, role, and bio are required"}), 400

        member.name = data['name'].strip()
        member.role = data['role'].strip()
        member.bio = data['bio'].strip()
        member.image_path = (data.get('image_path') or '').strip() or None
        member.sort_order = int(data.get('sort_order') or 0)
        member.is_active = bool(data.get('is_active', True))
        member.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"success": True, "message": "Team member updated successfully", "member": serialize_team_member(member)})
    except Exception as e:
        logger.error(f"Error updating team member {member_id}: {str(e)}")
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
    """Render enhanced admin dashboard"""
    return render_template_string(ENHANCED_ADMIN_DASHBOARD_TEMPLATE)

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
    """Get all contact messages with form origin tracking"""
    try:
        messages = ContactMessage.query.order_by(ContactMessage.created_at.desc()).all()
        return jsonify([{
            'id': msg.id,
            'full_name': msg.full_name,
            'email': msg.email,
            'phone': msg.phone,
            'subject': msg.subject,
            'message': msg.message,
            'form_origin': msg.form_origin,
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
    <title>Admin Login - BrightWave Habitat Enterprise</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white min-h-screen flex items-center justify-center">
    <div class="max-w-md w-full bg-gray-800 p-8 rounded-lg shadow-lg">
        <div class="text-center mb-8">
            <h1 class="text-3xl font-bold text-slate-400">BrightWave Admin</h1>
            <p class="text-gray-300 mt-2">Habitat Enterprise Management</p>
        </div>
        <form id="loginForm" class="space-y-6">
            <div>
                <label class="block text-sm font-medium mb-2">Username</label>
                <input type="text" id="username" required 
                       class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg focus:outline-none focus:border-slate-500">
            </div>
            <div>
                <label class="block text-sm font-medium mb-2">Password</label>
                <input type="password" id="password" required 
                       class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg focus:outline-none focus:border-slate-500">
            </div>
            <div>
                <button type="submit" 
                        class="w-full bg-slate-600 hover:bg-slate-700 text-white font-medium py-2 px-4 rounded-lg focus:outline-none">
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

ENHANCED_ADMIN_DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Dashboard - BrightWave Habitat Enterprise</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <header class="bg-gray-800 shadow">
        <div class="max-w-7xl mx-auto py-4 px-4 sm:px-6 lg:px-8 flex justify-between items-center">
            <h1 class="text-2xl font-bold text-slate-400">BrightWave Habitat Enterprise Dashboard</h1>
            <div>
                <button id="changePasswordBtn" class="text-slate-400 hover:text-slate-300 mr-4">Change Password</button>
                <a href="/admin/logout" class="text-slate-400 hover:text-slate-300">Logout</a>
            </div>
        </div>
    </header>
    <main class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <!-- Change Password Form -->
        <section id="passwordForm" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Change Password</h2>
            <form id="updatePasswordForm" class="bg-gray-800 p-4 rounded-lg space-y-4 max-w-md">
                <div>
                    <label class="block text-sm font-medium mb-2">New Password</label>
                    <input type="password" id="newPassword" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
                <div>
                    <button type="submit" class="bg-slate-600 hover:bg-slate-700 text-white font-medium py-2 px-4 rounded-lg">Update Password</button>
                    <button type="button" id="cancelPassword" class="bg-gray-600 hover:bg-gray-700 text-white font-medium py-2 px-4 rounded-lg ml-2">Cancel</button>
                </div>
                <p id="passwordMessage" class="text-red-500 text-sm hidden"></p>
            </form>
        </section>

        <!-- Enhanced Statistics -->
        <section class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Dashboard Overview</h2>
            <div id="stats" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                <!-- Stats will be populated by JavaScript -->
            </div>
            <div id="propertyBreakdown" class="bg-gray-800 p-4 rounded-lg mb-4">
                <!-- Property breakdown will be populated -->
            </div>
            <div id="recentActivity" class="bg-gray-800 p-4 rounded-lg">
                <!-- Recent activity will be populated -->
            </div>
        </section>

        <!-- Add Property Form -->
        <section class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Add New Property</h2>
            <div class="bg-gray-800 p-4 rounded-lg">
                <form id="addPropertyForm" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium mb-2">Title</label>
                        <input type="text" id="title" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Property Type</label>
                        <select id="property_type" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                            <option value="hostel">Hostel</option>
                            <option value="land">Land</option>
                            <option value="residential">Residential</option>
                        </select>
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-2">Description</label>
                        <textarea id="description" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></textarea>
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
                            <option value="per session">Per Session</option>
                            <option value="per year">Per Year</option>
                            <option value="per plot">Per Plot</option>
                            <option value="total">Total</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Construction Status</label>
                        <select id="construction_status" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                            <option value="">Select</option>
                            <option value="completed">Completed</option>
                            <option value="ongoing">Ongoing</option>
                            <option value="planning">Planning</option>
                        </select>
                    </div>
                    <div class="md:col-span-2 flex items-center space-x-4">
                        <label class="flex items-center">
                            <input type="checkbox" id="featured" class="mr-2">
                            <span class="text-sm font-medium">Featured Property</span>
                        </label>
                        <button type="submit" class="bg-slate-600 hover:bg-slate-700 text-white font-medium py-2 px-4 rounded-lg">Add Property</button>
                    </div>
                </form>
            </div>
        </section>

        <section class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Website Content</h2>
            <div class="bg-gray-800 p-4 rounded-lg">
                <form id="siteContentForm" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium mb-2">Home Hero Badge</label>
                        <input type="text" id="home_hero_badge" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Home Hero Title</label>
                        <input type="text" id="home_hero_title" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-2">Home Hero Subtitle</label>
                        <textarea id="home_hero_subtitle" rows="3" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></textarea>
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-2">Home About Intro</label>
                        <textarea id="home_about_intro" rows="3" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></textarea>
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-2">About Hero Subtitle</label>
                        <textarea id="about_hero_subtitle" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></textarea>
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-2">About Intro Body</label>
                        <textarea id="about_intro_body" rows="4" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">About Team Heading</label>
                        <input type="text" id="about_team_heading" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">About Team Subheading</label>
                        <input type="text" id="about_team_subheading" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div class="md:col-span-2">
                        <button type="submit" class="bg-slate-600 hover:bg-slate-700 text-white font-medium py-2 px-4 rounded-lg">Save Website Content</button>
                        <span id="siteContentMessage" class="ml-3 text-sm"></span>
                    </div>
                </form>
            </div>
        </section>

        <section class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Team Members</h2>
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <form id="teamMemberForm" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <input type="hidden" id="teamMemberId">
                    <div>
                        <label class="block text-sm font-medium mb-2">Name</label>
                        <input type="text" id="teamName" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Role</label>
                        <input type="text" id="teamRole" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-2">Bio</label>
                        <textarea id="teamBio" rows="3" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Upload Image</label>
                        <input type="file" id="teamImageUpload" accept="image/*" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Image Path</label>
                        <input type="text" id="teamImagePath" placeholder="images/ceo-wally.jpg" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Sort Order</label>
                        <input type="number" id="teamSortOrder" value="0" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div class="flex items-center pt-8">
                        <label class="flex items-center">
                            <input type="checkbox" id="teamIsActive" checked class="mr-2">
                            <span class="text-sm font-medium">Active on site</span>
                        </label>
                    </div>
                    <div class="md:col-span-2">
                        <button type="submit" class="bg-slate-600 hover:bg-slate-700 text-white font-medium py-2 px-4 rounded-lg">Save Team Member</button>
                        <button type="button" id="resetTeamForm" class="bg-gray-600 hover:bg-gray-700 text-white font-medium py-2 px-4 rounded-lg ml-2">Reset</button>
                        <span id="teamMessage" class="ml-3 text-sm"></span>
                    </div>
                </form>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg overflow-x-auto">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="border-b border-gray-600">
                            <th class="py-2 text-left">Name</th>
                            <th class="py-2 text-left">Role</th>
                            <th class="py-2 text-left">Order</th>
                            <th class="py-2 text-left">Active</th>
                            <th class="py-2 text-left">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="teamMembersTable"></tbody>
                </table>
            </div>
        </section>

        <!-- Properties Table -->
        <section class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Properties</h2>
            <div class="bg-gray-800 p-4 rounded-lg overflow-x-auto">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="border-b border-gray-600">
                            <th class="py-2 text-left">Title</th>
                            <th class="py-2 text-left">Type</th>
                            <th class="py-2 text-left">Location</th>
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

        <!-- Inquiries and Messages Tabs -->
        <section>
            <div class="flex space-x-4 mb-4">
                <button id="inquiriesTab" class="bg-slate-600 text-white px-4 py-2 rounded-lg">Property Inquiries</button>
                <button id="messagesTab" class="bg-gray-600 text-white px-4 py-2 rounded-lg">Contact Messages</button>
            </div>
            
            <div id="inquiriesSection" class="bg-gray-800 p-4 rounded-lg">
                <h3 class="text-lg font-semibold mb-4">Property Inquiries</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm">
                        <thead>
                            <tr class="border-b border-gray-600">
                                <th class="py-2 text-left">Name</th>
                                <th class="py-2 text-left">Property</th>
                                <th class="py-2 text-left">Type</th>
                                <th class="py-2 text-left">Status</th>
                                <th class="py-2 text-left">Date</th>
                                <th class="py-2 text-left">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="inquiriesTable">
                            <!-- Inquiries will be populated by JavaScript -->
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div id="messagesSection" class="bg-gray-800 p-4 rounded-lg hidden">
                <h3 class="text-lg font-semibold mb-4">Contact Messages</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm">
                        <thead>
                            <tr class="border-b border-gray-600">
                                <th class="py-2 text-left">Name</th>
                                <th class="py-2 text-left">Source</th>
                                <th class="py-2 text-left">Subject</th>
                                <th class="py-2 text-left">Status</th>
                                <th class="py-2 text-left">Date</th>
                                <th class="py-2 text-left">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="messagesTable">
                            <!-- Messages will be populated by JavaScript -->
                        </tbody>
                    </table>
                </div>
            </div>
        </section>
    </main>

    <script>
        // Enhanced dashboard functionality
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
                
                // Main stats cards
                document.getElementById('stats').innerHTML = `
                    <div class="bg-slate-700 p-4 rounded-lg">
                        <h3 class="text-lg font-medium text-slate-300">Total Properties</h3>
                        <p class="text-2xl font-bold">${stats.total_properties}</p>
                        <p class="text-sm text-gray-400">Active: ${stats.active_properties}</p>
                    </div>
                    <div class="bg-green-700 p-4 rounded-lg">
                        <h3 class="text-lg font-medium text-green-300">Inquiries</h3>
                        <p class="text-2xl font-bold">${stats.total_inquiries}</p>
                        <p class="text-sm text-green-200">New: ${stats.new_inquiries}</p>
                    </div>
                    <div class="bg-blue-700 p-4 rounded-lg">
                        <h3 class="text-lg font-medium text-blue-300">Messages</h3>
                        <p class="text-2xl font-bold">${stats.contact_messages}</p>
                        <p class="text-sm text-blue-200">New: ${stats.new_messages}</p>
                    </div>
                    <div class="bg-amber-700 p-4 rounded-lg">
                        <h3 class="text-lg font-medium text-amber-300">Properties by Type</h3>
                        <p class="text-sm">Hostels: ${stats.property_breakdown.hostels}</p>
                        <p class="text-sm">Land: ${stats.property_breakdown.land_plots}</p>
                        <p class="text-sm">Residential: ${stats.property_breakdown.residential}</p>
                    </div>
                    <div class="bg-purple-700 p-4 rounded-lg">
                        <h3 class="text-lg font-medium text-purple-200">Active Team</h3>
                        <p class="text-2xl font-bold">${stats.active_team_members}</p>
                        <p class="text-sm text-purple-100">Shown on About page</p>
                    </div>
                `;

                // Recent activity
                document.getElementById('recentActivity').innerHTML = `
                    <h3 class="text-lg font-semibold mb-4">Recent Activity</h3>
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <h4 class="font-medium mb-2">Latest Inquiries</h4>
                            ${stats.recent_activity.inquiries.map(inq => `
                                <div class="text-sm mb-2 p-2 bg-gray-700 rounded">
                                    <strong>${inq.name}</strong> - ${inq.inquiry_type}
                                    <br><span class="text-gray-400">${inq.created_at}</span>
                                </div>
                            `).join('')}
                        </div>
                        <div>
                            <h4 class="font-medium mb-2">Latest Messages</h4>
                            ${stats.recent_activity.messages.map(msg => `
                                <div class="text-sm mb-2 p-2 bg-gray-700 rounded">
                                    <strong>${msg.name}</strong> - ${msg.form_origin}
                                    <br><span class="text-gray-400">${msg.created_at}</span>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                `;
            } catch (error) {
                console.error('Error loading stats:', error);
            }
        }

        async function loadProperties() {
            try {
                const properties = await fetchData('/admin/api/properties');
                document.getElementById('propertiesTable').innerHTML = properties.map(prop => `
                    <tr class="border-b border-gray-600">
                        <td class="py-2">${prop.title}</td>
                        <td class="py-2"><span class="px-2 py-1 text-xs rounded ${
                            prop.property_type === 'hostel' ? 'bg-slate-600' : 
                            prop.property_type === 'land' ? 'bg-green-600' : 'bg-amber-600'
                        }">${prop.property_type}</span></td>
                        <td class="py-2">${prop.location}</td>
                        <td class="py-2">${prop.construction_status || 'N/A'}</td>
                        <td class="py-2">
                            <button onclick="deleteProperty(${prop.id})" class="text-red-400 hover:underline">Delete</button>
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
                document.getElementById('inquiriesTable').innerHTML = inquiries.map(inq => `
                    <tr class="border-b border-gray-600">
                        <td class="py-2">${inq.full_name}</td>
                        <td class="py-2">${inq.property_title}</td>
                        <td class="py-2">${inq.inquiry_type}</td>
                        <td class="py-2">
                            <select onchange="updateInquiry(${inq.id}, this.value)" class="bg-gray-700 text-white px-2 py-1 rounded">
                                <option value="new" ${inq.status === 'new' ? 'selected' : ''}>New</option>
                                <option value="contacted" ${inq.status === 'contacted' ? 'selected' : ''}>Contacted</option>
                                <option value="qualified" ${inq.status === 'qualified' ? 'selected' : ''}>Qualified</option>
                                <option value="converted" ${inq.status === 'converted' ? 'selected' : ''}>Converted</option>
                                <option value="closed" ${inq.status === 'closed' ? 'selected' : ''}>Closed</option>
                            </select>
                        </td>
                        <td class="py-2">${new Date(inq.created_at).toLocaleDateString()}</td>
                        <td class="py-2">
                            <button onclick="viewInquiry(${inq.id})" class="text-blue-400 hover:underline">View</button>
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
                document.getElementById('messagesTable').innerHTML = messages.map(msg => `
                    <tr class="border-b border-gray-600">
                        <td class="py-2">${msg.full_name}</td>
                        <td class="py-2"><span class="px-2 py-1 text-xs rounded bg-blue-600">${msg.form_origin}</span></td>
                        <td class="py-2">${msg.subject || 'No Subject'}</td>
                        <td class="py-2">
                            <select onchange="updateMessage(${msg.id}, this.value)" class="bg-gray-700 text-white px-2 py-1 rounded">
                                <option value="new" ${msg.status === 'new' ? 'selected' : ''}>New</option>
                                <option value="read" ${msg.status === 'read' ? 'selected' : ''}>Read</option>
                                <option value="responded" ${msg.status === 'responded' ? 'selected' : ''}>Responded</option>
                                <option value="closed" ${msg.status === 'closed' ? 'selected' : ''}>Closed</option>
                            </select>
                        </td>
                        <td class="py-2">${new Date(msg.created_at).toLocaleDateString()}</td>
                        <td class="py-2">
                            <button onclick="viewMessage(${msg.id})" class="text-blue-400 hover:underline">View</button>
                        </td>
                    </tr>
                `).join('');
            } catch (error) {
                console.error('Error loading messages:', error);
            }
        }

        async function loadSiteContent() {
            try {
                const content = await fetchData('/admin/api/site-content');
                document.getElementById('home_hero_badge').value = content['home.hero_badge'] || '';
                document.getElementById('home_hero_title').value = content['home.hero_title'] || '';
                document.getElementById('home_hero_subtitle').value = content['home.hero_subtitle'] || '';
                document.getElementById('home_about_intro').value = content['home.about_intro'] || '';
                document.getElementById('about_hero_subtitle').value = content['about.hero_subtitle'] || '';
                document.getElementById('about_intro_body').value = content['about.intro_body'] || '';
                document.getElementById('about_team_heading').value = content['about.team_heading'] || '';
                document.getElementById('about_team_subheading').value = content['about.team_subheading'] || '';
            } catch (error) {
                console.error('Error loading site content:', error);
            }
        }

        async function loadTeamMembers() {
            try {
                const members = await fetchData('/admin/api/team-members');
                document.getElementById('teamMembersTable').innerHTML = members.map(member => `
                    <tr class="border-b border-gray-600">
                        <td class="py-2">${member.name}</td>
                        <td class="py-2">${member.role}</td>
                        <td class="py-2">${member.sort_order}</td>
                        <td class="py-2">${member.is_active ? 'Yes' : 'No'}</td>
                        <td class="py-2">
                            <button onclick="editTeamMember(${member.id})" class="text-blue-400 hover:underline mr-3">Edit</button>
                            <button onclick="deleteTeamMember(${member.id})" class="text-red-400 hover:underline">Delete</button>
                        </td>
                    </tr>
                `).join('');
                window.teamMembersCache = members;
            } catch (error) {
                console.error('Error loading team members:', error);
            }
        }

        function resetTeamMemberForm() {
            document.getElementById('teamMemberId').value = '';
            document.getElementById('teamName').value = '';
            document.getElementById('teamRole').value = '';
            document.getElementById('teamBio').value = '';
            document.getElementById('teamImagePath').value = '';
            document.getElementById('teamImageUpload').value = '';
            document.getElementById('teamSortOrder').value = '0';
            document.getElementById('teamIsActive').checked = true;
            document.getElementById('teamMessage').textContent = '';
        }

        async function uploadSelectedTeamImage() {
            const fileInput = document.getElementById('teamImageUpload');
            if (!fileInput.files.length) return document.getElementById('teamImagePath').value.trim();

            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            const response = await fetch('/admin/api/upload', {
                method: 'POST',
                credentials: 'include',
                body: formData
            });
            const result = await response.json();
            if (!response.ok || !result.success) {
                throw new Error(result.message || 'Image upload failed');
            }
            return result.filename;
        }

        function editTeamMember(id) {
            const member = (window.teamMembersCache || []).find(item => item.id === id);
            if (!member) return;
            document.getElementById('teamMemberId').value = member.id;
            document.getElementById('teamName').value = member.name;
            document.getElementById('teamRole').value = member.role;
            document.getElementById('teamBio').value = member.bio;
            document.getElementById('teamImagePath').value = member.image_path || '';
            document.getElementById('teamSortOrder').value = member.sort_order || 0;
            document.getElementById('teamIsActive').checked = member.is_active;
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        async function deleteTeamMember(id) {
            if (!confirm('Remove this team member from the site?')) return;
            try {
                await fetchData(`/admin/api/team-members/${id}`, { method: 'DELETE' });
                loadTeamMembers();
                loadStats();
                resetTeamMemberForm();
            } catch (error) {
                alert('Error deleting team member');
            }
        }

        // Action functions
        async function updateInquiry(id, status) {
            try {
                const response = await fetchData(`/admin/api/inquiries/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status })
                });
                loadStats(); // Refresh stats
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
                loadStats(); // Refresh stats
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
                    loadProperties();
                    loadStats();
                } catch (error) {
                    alert('Error deleting property');
                }
            }
        }

        function viewInquiry(id) {
            // Simple modal implementation - you can enhance this
            alert('Inquiry details would be shown in a modal. ID: ' + id);
        }

        function viewMessage(id) {
            // Simple modal implementation - you can enhance this
            alert('Message details would be shown in a modal. ID: ' + id);
        }

        // Tab switching
        document.getElementById('inquiriesTab').addEventListener('click', () => {
            document.getElementById('inquiriesSection').classList.remove('hidden');
            document.getElementById('messagesSection').classList.add('hidden');
            document.getElementById('inquiriesTab').classList.add('bg-slate-600');
            document.getElementById('inquiriesTab').classList.remove('bg-gray-600');
            document.getElementById('messagesTab').classList.add('bg-gray-600');
            document.getElementById('messagesTab').classList.remove('bg-slate-600');
        });

        document.getElementById('messagesTab').addEventListener('click', () => {
            document.getElementById('messagesSection').classList.remove('hidden');
            document.getElementById('inquiriesSection').classList.add('hidden');
            document.getElementById('messagesTab').classList.add('bg-slate-600');
            document.getElementById('messagesTab').classList.remove('bg-gray-600');
            document.getElementById('inquiriesTab').classList.add('bg-gray-600');
            document.getElementById('inquiriesTab').classList.remove('bg-slate-600');
        });

        // Password change functionality
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

        // Add property form
        document.getElementById('addPropertyForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const propertyData = {
                title: document.getElementById('title').value,
                description: document.getElementById('description').value,
                property_type: document.getElementById('property_type').value,
                location: document.getElementById('location').value,
                price: parseFloat(document.getElementById('price').value) || null,
                price_type: document.getElementById('price_type').value || null,
                construction_status: document.getElementById('construction_status').value || null,
                featured: document.getElementById('featured').checked
            };

            try {
                const response = await fetchData('/admin/api/properties', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(propertyData)
                });
                alert('Property added successfully');
                document.getElementById('addPropertyForm').reset();
                loadProperties();
                loadStats();
            } catch (error) {
                alert('Error adding property');
            }
        });

        document.getElementById('siteContentForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const message = document.getElementById('siteContentMessage');
            try {
                const payload = {
                    'home.hero_badge': document.getElementById('home_hero_badge').value,
                    'home.hero_title': document.getElementById('home_hero_title').value,
                    'home.hero_subtitle': document.getElementById('home_hero_subtitle').value,
                    'home.about_intro': document.getElementById('home_about_intro').value,
                    'about.hero_subtitle': document.getElementById('about_hero_subtitle').value,
                    'about.intro_body': document.getElementById('about_intro_body').value,
                    'about.team_heading': document.getElementById('about_team_heading').value,
                    'about.team_subheading': document.getElementById('about_team_subheading').value
                };
                const response = await fetchData('/admin/api/site-content', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                message.textContent = response.message;
                message.className = 'ml-3 text-sm text-green-400';
            } catch (error) {
                message.textContent = 'Error saving website content';
                message.className = 'ml-3 text-sm text-red-400';
            }
        });

        document.getElementById('teamMemberForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const message = document.getElementById('teamMessage');
            try {
                const imagePath = await uploadSelectedTeamImage();
                const payload = {
                    name: document.getElementById('teamName').value,
                    role: document.getElementById('teamRole').value,
                    bio: document.getElementById('teamBio').value,
                    image_path: imagePath,
                    sort_order: parseInt(document.getElementById('teamSortOrder').value || '0', 10),
                    is_active: document.getElementById('teamIsActive').checked
                };
                const memberId = document.getElementById('teamMemberId').value;
                const url = memberId ? `/admin/api/team-members/${memberId}` : '/admin/api/team-members';
                const method = memberId ? 'PUT' : 'POST';
                const response = await fetchData(url, {
                    method,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                message.textContent = response.message;
                message.className = 'ml-3 text-sm text-green-400';
                resetTeamMemberForm();
                loadTeamMembers();
                loadStats();
            } catch (error) {
                message.textContent = error.message || 'Error saving team member';
                message.className = 'ml-3 text-sm text-red-400';
            }
        });

        document.getElementById('resetTeamForm').addEventListener('click', resetTeamMemberForm);

        // Initialize dashboard
        document.addEventListener('DOMContentLoaded', () => {
            loadStats();
            loadProperties();
            loadInquiries();
            loadMessages();
            loadSiteContent();
            loadTeamMembers();
        });
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
