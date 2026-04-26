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
import secrets
from datetime import datetime, date as date_type
from time import time
from collections import defaultdict
from functools import wraps
import json
import threading
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
runtime_state_lock = threading.Lock()
runtime_state_initialized = False

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
    role = db.Column(db.String(20), default='CEO')  # primary role: CEO, MANAGER, ACCOUNTANT, REALTOR, INVESTOR
    secondary_roles = db.Column(db.JSON, default=list)  # e.g. ["REALTOR"] for a manager who also does sales
    display_name = db.Column(db.String(120), nullable=True)
    has_signed_contract = db.Column(db.Boolean, default=False)
    contract_signed_at = db.Column(db.DateTime, nullable=True)
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

class UserContract(db.Model):
    __tablename__ = 'user_contract'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=False)
    contract_type = db.Column(db.String(20), nullable=False)
    # pending_user_signature, pending_ceo_signature, completed
    status = db.Column(db.String(30), default='pending_user_signature')
    user_signed_at = db.Column(db.DateTime, nullable=True)
    user_signature = db.Column(db.String(200), nullable=True)
    ceo_signed_at = db.Column(db.DateTime, nullable=True)
    ceo_signature = db.Column(db.String(200), nullable=True)
    popup_shown = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('Admin', backref='contracts', foreign_keys=[user_id])

class InvestorProfile(db.Model):
    __tablename__ = 'investor_profile'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('admin.id'), unique=True, nullable=False)
    investment_type = db.Column(db.String(10), nullable=False, default='DEBT')  # DEBT or EQUITY
    investment_amount = db.Column(db.Float, nullable=False)
    investment_date = db.Column(db.Date, nullable=True)
    roi_rate = db.Column(db.Float, default=10.0)         # annual % for DEBT investors
    equity_percentage = db.Column(db.Float, nullable=True)  # % project ownership for EQUITY
    construction_start_date = db.Column(db.Date, nullable=True)
    expected_completion_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    total_distributed = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship('Admin', backref='investor_profile_rel', foreign_keys=[user_id])

class Tenant(db.Model):
    __tablename__ = 'tenant'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    property_name = db.Column(db.String(150), nullable=True)
    unit_number = db.Column(db.String(30), nullable=True)
    lease_start = db.Column(db.Date, nullable=True)
    lease_end = db.Column(db.Date, nullable=True)
    monthly_rent = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default='active')
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class PaymentRecord(db.Model):
    __tablename__ = 'payment_record'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=True)
    tenant_name = db.Column(db.String(120), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    payment_date = db.Column(db.Date, nullable=False, default=date_type.today)
    payment_type = db.Column(db.String(30), default='rent')
    description = db.Column(db.Text, nullable=True)
    recorded_by = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
        password_hash=generate_password_hash(password),
        role='CEO',
        has_signed_contract=True
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


def env_flag(name, default="False"):
    return os.environ.get(name, default).strip().lower() == "true"


def ensure_runtime_state():
    """Initialize DB-backed site state once per process, with a per-request fallback."""
    global runtime_state_initialized

    if runtime_state_initialized:
        return

    with runtime_state_lock:
        if runtime_state_initialized:
            return

        initialize_app_state(
            include_sample_data=env_flag("INIT_SAMPLE_DATA", "True"),
            bootstrap_admin=env_flag("BOOTSTRAP_ADMIN", "False"),
        )
        runtime_state_initialized = True


def get_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


def validate_csrf_token():
    expected = session.get('csrf_token')
    provided = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
    return bool(expected and provided and secrets.compare_digest(expected, provided))

# ========== AUTHENTICATION FUNCTIONS ==========
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('admin_login'))
        ensure_runtime_state()
        return f(*args, **kwargs)
    return decorated_function

def get_current_admin():
    return Admin.query.get(session.get('admin_id'))

def ceo_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return jsonify({"success": False, "message": "Authentication required"}), 401
        admin = get_current_admin()
        if not admin or admin.role != 'CEO':
            return jsonify({"success": False, "message": "CEO access required"}), 403
        return f(*args, **kwargs)
    return decorated_function

CONTRACT_TEXTS = {
    'MANAGER': {
        'title': 'Property & Operations Manager Agreement',
        'body': """PROPERTY & OPERATIONS MANAGER AGREEMENT

This agreement is entered into between BrightWave Habitat Enterprise ("the Company"), represented by its Chief Executive Officer, and the individual granted Manager access to this portal ("the Manager").

1. ROLE AND RESPONSIBILITIES
The Manager is responsible for overseeing property readiness, daily operations, tenant relations, and site standards across all active BrightWave properties. The Manager reports directly to the CEO and is accountable for the operational performance of all assigned properties.

2. SYSTEM ACCESS
The Manager is granted access to property management, inquiry handling, team oversight, and operational data within the BrightWave management portal. Access to full financial records and investor data is restricted to authorised personnel only.

3. CONFIDENTIALITY
All property data, client information, tenant details, team data, and operational information accessed through this portal are strictly confidential. The Manager agrees not to disclose any such information to third parties without written CEO approval. This obligation survives the termination of this agreement.

4. CODE OF CONDUCT
The Manager agrees to maintain the highest professional standards in all interactions with tenants, clients, contractors, and team members. Any conduct that damages the reputation of BrightWave Habitat Enterprise may result in immediate access revocation and legal action.

5. COMMISSION STRUCTURE
The Manager is entitled to a commission of 10% of the gross rent value per unit successfully leased or let on behalf of BrightWave Habitat Enterprise during their term of engagement. Commission is payable upon confirmed tenant occupancy and receipt of the first payment. The CEO reserves the right to adjust commission terms with 14 days written notice.

6. DATA SECURITY
The Manager is solely responsible for keeping their login credentials secure and must not share access with any other person. Any breach of system security must be reported to the CEO immediately.

7. INTELLECTUAL PROPERTY
All work produced, materials created, and processes developed during the term of this agreement remain the intellectual property of BrightWave Habitat Enterprise.

8. AGREEMENT TERM
This agreement is effective from the date both parties sign and remains in force until terminated by either party with 14 days written notice, or immediately by the Company in the event of serious misconduct or breach of any term of this agreement.

By signing below, the Manager confirms they have read, understood, and fully agree to all terms outlined in this agreement. This constitutes a binding agreement between both parties once countersigned by the CEO of BrightWave Habitat Enterprise."""
    },
    'ACCOUNTANT': {
        'title': 'Financial Controller Agreement',
        'body': """FINANCIAL CONTROLLER AGREEMENT

This agreement is entered into between BrightWave Habitat Enterprise ("the Company"), represented by its Chief Executive Officer, and the individual granted Accountant access to this portal ("the Accountant").

1. ROLE AND RESPONSIBILITIES
The Accountant is responsible for tracking all financial transactions, managing rent collection records, overseeing investor distributions, preparing financial reports, and maintaining accurate financial data within the BrightWave management system.

2. SYSTEM ACCESS
The Accountant is granted access to financial dashboards, investor distribution records, payment tracking, and all financial data within the portal. Access to operational management functions beyond financial oversight is restricted.

3. STRICT FINANCIAL CONFIDENTIALITY
The Accountant acknowledges that all financial information, investor data, payment records, profit figures, and any financial details accessed through this system are strictly confidential. Disclosure of any financial information to unauthorised parties constitutes a serious breach of this agreement and may result in legal proceedings.

4. ACCURACY AND INTEGRITY
The Accountant agrees to maintain the highest standard of accuracy in all financial records. Any discrepancies discovered must be reported to the CEO immediately. Deliberate falsification of financial records will result in immediate termination and potential criminal proceedings.

5. INVESTOR DATA PROTECTION
Investor names, investment amounts, and personal financial details are subject to the highest level of protection. The Accountant must not discuss, share, or reference investor information outside of official company communications.

6. COMPLIANCE
The Accountant agrees to operate in accordance with applicable Nigerian financial regulations and accounting standards throughout the term of this agreement.

7. DATA SECURITY
The Accountant is responsible for keeping login credentials secure at all times and must report any suspected unauthorised access immediately.

8. AGREEMENT TERM
This agreement is effective from the date both parties sign and remains in force until terminated by either party with 14 days written notice, or immediately by the Company in the event of serious misconduct, financial fraud, or breach of confidentiality.

By signing below, the Accountant confirms they have read, understood, and fully agree to all terms outlined in this agreement. This constitutes a binding agreement between both parties once countersigned by the CEO of BrightWave Habitat Enterprise."""
    },
    'REALTOR': {
        'title': 'Real Estate Agent Agreement',
        'body': """REAL ESTATE AGENT AGREEMENT

This agreement is entered into between BrightWave Habitat Enterprise ("the Company"), represented by its Chief Executive Officer, and the individual granted Realtor access to this portal ("the Realtor").

1. ROLE AND RESPONSIBILITIES
The Realtor is responsible for managing client inquiries, conducting property showings, handling lead pipelines, and facilitating the sales or letting process for all BrightWave properties. The Realtor operates as an authorised representative of BrightWave Habitat Enterprise.

2. SYSTEM ACCESS
The Realtor is granted access to property listings, client inquiries, and lead management features within the portal. Access to financial records, investor data, and administrative functions is restricted.

3. CLIENT REPRESENTATION
The Realtor agrees to represent BrightWave Habitat Enterprise and its clients with professionalism and integrity at all times. All client interactions must be conducted in accordance with the company's standards and values.

4. EXCLUSIVITY FOR LISTED PROPERTIES
For properties actively listed under BrightWave Habitat Enterprise, the Realtor agrees to represent only BrightWave's interests and must not simultaneously represent competing parties on the same transaction without prior written CEO approval.

5. COMMISSION STRUCTURE
The Realtor is entitled to the following commission rates on transactions successfully completed on behalf of BrightWave Habitat Enterprise:
   — 10% of the gross rent value per residential or hostel unit successfully leased or let.
   — 10% of the agreed sale price per land plot sold.
   — 10% of the gross contract value per service apartment successfully arranged or let.
Commission is earned upon completion of the transaction, confirmed in writing by the CEO, and is subject to the company's standard payment schedule. The CEO reserves the right to adjust commission terms with 14 days written notice.

6. CONFIDENTIALITY
All client details, property pricing information, negotiation discussions, and internal company information are strictly confidential. The Realtor agrees not to disclose such information to competitors or unauthorised parties.

7. CODE OF CONDUCT
The Realtor agrees to maintain honest, transparent, and professional conduct at all times. Misrepresentation of any property or company information to clients is strictly prohibited and will result in immediate termination.

8. DATA SECURITY
The Realtor is responsible for keeping their login credentials secure and must report any suspected breach immediately.

9. AGREEMENT TERM
This agreement is effective from the date both parties sign and remains in force until terminated by either party with 7 days written notice, or immediately by the Company in the event of serious misconduct or breach of any term herein.

By signing below, the Realtor confirms they have read, understood, and fully agree to all terms outlined in this agreement. This constitutes a binding agreement between both parties once countersigned by the CEO of BrightWave Habitat Enterprise."""
    },
    'INVESTOR': {
        'title': 'Investment Agreement — BrightWave Habitat Enterprise',
        'body': """INVESTMENT AGREEMENT

This agreement is entered into between BrightWave Habitat Enterprise ("the Company"), represented by its Chief Executive Officer, and the investor granted access to this portal ("the Investor").

IMPORTANT NOTICE — PLEASE READ CAREFULLY

---

1. COMPANY OVERVIEW
BrightWave Habitat Enterprise is a Nigerian real estate development company focused on student accommodation, residential housing, and estate development. The Company is currently in its early growth phase, with Phase 1 (BrightWave Hostel, Malete, Kwara State) as the first completed project.

2. PRE-REVENUE PHASE DISCLOSURE
The Investor acknowledges and accepts that the current investment coincides with an active construction and development phase. No distributions or returns will be made during the construction period, which is estimated at 12 to 18 months from the investment date. Returns commence upon project completion and first revenue generation. The exact timeline may vary due to construction, regulatory, or market factors beyond the Company's control.

3. INVESTMENT TERMS
The specific investment amount, type (Debt or Equity), return rate, and distribution schedule applicable to this Investor are as specified in the Investor's profile within this portal and as separately confirmed in writing by the CEO. These terms are personalised and confidential.

   DEBT INVESTMENT TERMS:
   — The Investor lends capital to the Company at the agreed annual interest rate.
   — Interest distributions are paid annually, commencing after project completion.
   — Principal is returned at the end of the agreed investment term.
   — The annual return rate recommended by the Company for founding investors is 10% per annum.

   EQUITY INVESTMENT TERMS:
   — The Investor acquires an ownership stake in a specific BrightWave development project (not the entire company).
   — Distributions are made from project revenues on an annual basis, proportional to the equity stake.
   — The equity stake may appreciate or depreciate based on project performance.
   — There is no guaranteed fixed return for equity investors.

4. RISK DISCLOSURE
The Investor acknowledges that real estate investment carries inherent risks, including but not limited to: construction delays, cost overruns, changes in market conditions, regulatory changes, and force majeure events. The Company will communicate all material developments in a timely manner, but cannot guarantee specific outcomes.

5. USE OF FUNDS
All investment funds will be used exclusively for property development, construction costs, professional services, regulatory compliance, and operational setup directly related to BrightWave projects. A detailed fund utilisation breakdown is available upon request from the CEO.

6. TRANSPARENCY AND REPORTING
The Company commits to providing the Investor with regular updates through this portal, including construction progress milestones, financial performance reports, and distribution schedules. The Investor's dashboard will reflect current project status at all times.

7. CONFIDENTIALITY
The Investor agrees to keep the terms of this agreement, their investment amount, and all non-public company information strictly confidential. Disclosure of such information to competitors or the public without written CEO approval is a breach of this agreement.

8. PORTAL ACCESS
Access to the Investor Portal is granted solely to the named Investor for the purpose of monitoring their investment. Access credentials must not be shared. The Company reserves the right to revoke portal access at any time.

9. GOVERNING LAW
This agreement is governed by the laws of the Federal Republic of Nigeria. Any dispute arising from this agreement shall be resolved through good-faith negotiation, and if unresolved, through the applicable Nigerian legal framework.

10. BINDING AGREEMENT
This agreement becomes binding upon the digital signatures of both the Investor and the CEO of BrightWave Habitat Enterprise. Both parties will retain a signed copy accessible through the portal.

By signing below, the Investor confirms they have read, understood, accepted all risk disclosures, and fully agree to all terms outlined in this agreement."""
    }
}

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


@app.before_request
def enforce_admin_csrf():
    if not request.path.startswith('/admin/'):
        return None

    if request.method not in {'POST', 'PUT', 'DELETE'}:
        return None

    if request.path == '/admin/login' or 'admin_id' not in session:
        return None

    if not validate_csrf_token():
        return jsonify({"success": False, "message": "Invalid or missing CSRF token"}), 403

    return None


@app.after_request
def apply_security_headers(response):
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault('X-XSS-Protection', '1; mode=block')

    if request.path.startswith('/admin'):
        response.headers.setdefault('Cache-Control', 'no-store')

    return response

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

@app.route('/management/')
@app.route('/management')
def management_redirect():
    return redirect(url_for('admin_login'), code=301)

@app.route('/manifest.json')
def pwa_manifest():
    manifest = {
        "name": "BrightWave Habitat Enterprise",
        "short_name": "BrightWave",
        "description": "BrightWave Habitat Enterprise Management Portal",
        "start_url": "/admin/dashboard",
        "scope": "/admin/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#111827",
        "theme_color": "#475569",
        "icons": [
            {"src": "/assets/images/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/assets/images/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ]
    }
    from flask import Response
    return Response(json.dumps(manifest), mimetype='application/json')

@app.route('/sw.js')
def service_worker():
    sw_code = """
const CACHE_NAME = 'brightwave-portal-v3';
const STATIC_ASSETS = ['/admin/login'];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

// Network-first for static assets only; never cache admin pages or API calls
self.addEventListener('fetch', event => {
    if (event.request.method !== 'GET') return;
    const url = event.request.url;
    if (url.includes('/admin/')) return; // never cache admin pages or API
    if (url.includes('/sw.js')) return;

    event.respondWith(
        fetch(event.request)
            .then(response => {
                if (response.ok && (url.includes('/assets/') || url.includes('/manifest.json'))) {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
                }
                return response;
            })
            .catch(() => caches.match(event.request))
    );
});
"""
    from flask import Response
    resp = Response(sw_code, mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp

@app.route('/api/site-content', methods=['GET'])
def get_public_site_content():
    try:
        ensure_runtime_state()
        return jsonify(get_site_content())
    except Exception as e:
        logger.error(f"Error fetching site content: {str(e)}")
        return jsonify(DEFAULT_SITE_CONTENT)

@app.route('/api/team-members', methods=['GET'])
def get_public_team_members():
    try:
        ensure_runtime_state()
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
        ensure_runtime_state()
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
        ensure_runtime_state()
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
        ensure_runtime_state()
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

        # Send notification emails in background so SMTP timeout never kills the worker
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
            confirmation_body = f"""
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
            def _send_contact_emails(subject, body, reply, conf_body, user_email, user_name):
                try:
                    with app.app_context():
                        mail.send(Message(subject=subject, recipients=NOTIFICATION_EMAILS, body=body, reply_to=reply))
                        mail.send(Message(subject="Thank You for Contacting BrightWave Habitat Enterprise", recipients=[user_email], body=conf_body))
                except Exception as e:
                    logger.error(f"Contact email send failed: {str(e)}")
            threading.Thread(
                target=_send_contact_emails,
                args=(email_subject, email_body, email, confirmation_body, email, full_name),
                daemon=True
            ).start()

        return jsonify({"success": True, "message": "Thank you! Your message has been received."})
    except Exception as e:
        logger.error(f"Error handling contact form: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/api/property-inquiry', methods=['POST'])
@limiter.limit("3 per minute")
def handle_property_inquiry():
    """Handle property-specific inquiries"""
    try:
        ensure_runtime_state()
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
            def _send_inquiry_emails(subject, body, reply, user_email, user_name):
                try:
                    with app.app_context():
                        mail.send(Message(subject=subject, recipients=NOTIFICATION_EMAILS, body=body, reply_to=reply))
                        mail.send(Message(
                            subject="Thank You for Your Property Inquiry",
                            recipients=[user_email],
                            body=f"Dear {user_name},\n\nThank you for your interest in our properties! We have received your inquiry and our team will contact you within 24-48 hours.\n\nBest regards,\nBrightWave Habitat Enterprise Team"
                        ))
                except Exception as e:
                    logger.error(f"Inquiry email send failed: {str(e)}")
            threading.Thread(
                target=_send_inquiry_emails,
                args=(email_subject, email_body, email, email, full_name),
                daemon=True
            ).start()

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
        from sqlalchemy import func as sqlfunc
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

        # Tenant stats
        active_tenants = Tenant.query.filter_by(status='active').count()
        total_tenants = Tenant.query.count()

        # Revenue stats
        now = datetime.utcnow()
        month_start = date_type(now.year, now.month, 1)
        monthly_revenue = db.session.query(sqlfunc.sum(PaymentRecord.amount)).filter(
            PaymentRecord.payment_date >= month_start
        ).scalar() or 0
        total_revenue = db.session.query(sqlfunc.sum(PaymentRecord.amount)).scalar() or 0

        # Recent data
        recent_inquiries = PropertyInquiry.query.order_by(PropertyInquiry.created_at.desc()).limit(5).all()
        recent_messages = ContactMessage.query.order_by(ContactMessage.created_at.desc()).limit(5).all()
        recent_payments = PaymentRecord.query.order_by(PaymentRecord.created_at.desc()).limit(5).all()
        recent_tenants = Tenant.query.order_by(Tenant.created_at.desc()).limit(5).all()

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
            'active_tenants': active_tenants,
            'total_tenants': total_tenants,
            'monthly_revenue': monthly_revenue,
            'total_revenue': total_revenue,
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
                } for msg in recent_messages],
                'payments': [{
                    'id': p.id,
                    'tenant_name': p.tenant_name or 'Unknown',
                    'amount': p.amount,
                    'payment_type': p.payment_type,
                    'payment_date': p.payment_date.strftime('%Y-%m-%d') if p.payment_date else ''
                } for p in recent_payments],
                'tenants': [{
                    'id': t.id,
                    'name': t.name,
                    'property_name': t.property_name or '',
                    'status': t.status,
                    'created_at': t.created_at.strftime('%Y-%m-%d')
                } for t in recent_tenants]
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
            ensure_runtime_state()
            data = request.get_json()
            username = data.get('username')
            password = data.get('password')
            
            if not username or not password:
                return jsonify({"success": False, "message": "Username and password required"}), 400
            
            admin = Admin.query.filter_by(username=username, is_active=True).first()
            
            if admin and check_password_hash(admin.password_hash, password):
                session['admin_id'] = admin.id
                session['admin_role'] = admin.role
                session['csrf_token'] = secrets.token_urlsafe(32)
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
    session.pop('csrf_token', None)
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
    """Render role-aware admin dashboard"""
    admin = get_current_admin()
    if not admin or not admin.is_active:
        session.clear()
        return redirect(url_for('admin_login'))

    user_name = admin.display_name or admin.username

    no_cache_headers = {
        'Cache-Control': 'no-store, no-cache, must-revalidate',
        'Pragma': 'no-cache',
    }

    if admin.role == 'CEO':
        pending_sigs_count = UserContract.query.filter_by(status='pending_ceo_signature').count()
        from flask import make_response
        resp = make_response(render_template_string(
            ENHANCED_ADMIN_DASHBOARD_TEMPLATE,
            csrf_token=get_csrf_token(),
            user_role='CEO',
            user_name=user_name,
            pending_sigs_count=pending_sigs_count,
        ))
        for k, v in no_cache_headers.items():
            resp.headers[k] = v
        return resp

    # Non-CEO: handle contract signing flow
    contract = UserContract.query.filter_by(user_id=admin.id).order_by(UserContract.created_at.desc()).first()
    if not contract:
        contract = UserContract(user_id=admin.id, contract_type=admin.role, status='pending_user_signature')
        db.session.add(contract)
        db.session.commit()

    needs_contract_signing = (contract.status == 'pending_user_signature')
    awaiting_ceo_signature = (contract.status == 'pending_ceo_signature')
    show_agreement_popup = False
    if contract.status == 'completed' and not contract.popup_shown:
        show_agreement_popup = True
        contract.popup_shown = True
        db.session.commit()

    investor_profile = None
    if admin.role == 'INVESTOR':
        investor_profile = InvestorProfile.query.filter_by(user_id=admin.id).first()

    all_roles = [admin.role] + (admin.secondary_roles or [])

    from flask import make_response
    resp = make_response(render_template_string(
        ROLE_DASHBOARD_TEMPLATE,
        csrf_token=get_csrf_token(),
        user_role=admin.role,
        all_roles=all_roles,
        all_roles_json=json.dumps(all_roles),
        user_name=user_name,
        needs_contract_signing=needs_contract_signing,
        awaiting_ceo_signature=awaiting_ceo_signature,
        show_agreement_popup=show_agreement_popup,
        contract_id=contract.id,
        contract_title=CONTRACT_TEXTS.get(admin.role, {}).get('title', 'Agreement'),
        contract_body=CONTRACT_TEXTS.get(admin.role, {}).get('body', ''),
        investor_profile=investor_profile,
    ))
    for k, v in no_cache_headers.items():
        resp.headers[k] = v
    return resp

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

# ========== CONTRACT API ==========
@app.route('/admin/api/my-contract/sign', methods=['POST'])
@login_required
def sign_my_contract():
    try:
        admin = get_current_admin()
        data = request.get_json()
        signature = (data.get('signature') or '').strip()
        if not signature or len(signature) < 2:
            return jsonify({"success": False, "message": "Signature is required"}), 400

        contract = UserContract.query.filter_by(user_id=admin.id).order_by(UserContract.created_at.desc()).first()
        if not contract or contract.status != 'pending_user_signature':
            return jsonify({"success": False, "message": "No contract pending your signature"}), 400

        contract.user_signature = signature
        contract.user_signed_at = datetime.utcnow()
        contract.status = 'pending_ceo_signature'
        db.session.commit()
        return jsonify({"success": True, "message": "Contract signed. Awaiting CEO co-signature."})
    except Exception as e:
        logger.error(f"Error signing contract: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/pending-contracts', methods=['GET'])
@login_required
@ceo_required
def get_pending_contracts():
    try:
        contracts = UserContract.query.filter_by(status='pending_ceo_signature').order_by(UserContract.created_at.asc()).all()
        result = []
        for c in contracts:
            user = Admin.query.get(c.user_id)
            result.append({
                'id': c.id,
                'user_id': c.user_id,
                'user_name': user.display_name or user.username if user else 'Unknown',
                'user_email': user.email if user else '',
                'role': c.contract_type,
                'user_signature': c.user_signature,
                'user_signed_at': c.user_signed_at.strftime('%Y-%m-%d %H:%M') if c.user_signed_at else '',
                'created_at': c.created_at.strftime('%Y-%m-%d'),
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error fetching pending contracts: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/contracts/<int:contract_id>/ceo-sign', methods=['POST'])
@login_required
@ceo_required
def ceo_sign_contract(contract_id):
    try:
        ceo = get_current_admin()
        data = request.get_json()
        signature = (data.get('signature') or '').strip()
        if not signature or len(signature) < 2:
            return jsonify({"success": False, "message": "CEO signature is required"}), 400

        contract = UserContract.query.get_or_404(contract_id)
        if contract.status != 'pending_ceo_signature':
            return jsonify({"success": False, "message": "Contract is not awaiting CEO signature"}), 400

        contract.ceo_signature = signature
        contract.ceo_signed_at = datetime.utcnow()
        contract.status = 'completed'
        contract.popup_shown = False  # triggers popup on user's next login

        user = Admin.query.get(contract.user_id)
        if user:
            user.has_signed_contract = True
            user.contract_signed_at = datetime.utcnow()

        db.session.commit()
        return jsonify({"success": True, "message": "Agreement completed. Both parties have signed."})
    except Exception as e:
        logger.error(f"Error CEO signing contract {contract_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== TEAM ACCOUNTS API ==========
@app.route('/admin/api/accounts', methods=['GET', 'POST'])
@login_required
@ceo_required
def admin_accounts():
    try:
        if request.method == 'GET':
            accounts = Admin.query.order_by(Admin.created_at.desc()).all()
            result = []
            for a in accounts:
                contract = UserContract.query.filter_by(user_id=a.id).order_by(UserContract.created_at.desc()).first()
                result.append({
                    'id': a.id,
                    'username': a.username,
                    'email': a.email,
                    'role': a.role,
                    'secondary_roles': a.secondary_roles or [],
                    'display_name': a.display_name or '',
                    'is_active': a.is_active,
                    'has_signed_contract': a.has_signed_contract,
                    'contract_status': contract.status if contract else 'no_contract',
                    'created_at': a.created_at.strftime('%Y-%m-%d'),
                })
            return jsonify(result)

        data = request.get_json()
        required = ['username', 'email', 'password', 'role']
        if not all(data.get(f) for f in required):
            return jsonify({"success": False, "message": "Username, email, password, and role are required"}), 400

        valid_roles = ['CEO', 'MANAGER', 'ACCOUNTANT', 'REALTOR', 'INVESTOR']
        if data['role'] not in valid_roles:
            return jsonify({"success": False, "message": f"Role must be one of: {', '.join(valid_roles)}"}), 400

        if len(data['password']) < 8:
            return jsonify({"success": False, "message": "Password must be at least 8 characters"}), 400

        raw_secondary = data.get('secondary_roles') or []
        secondary = [r for r in raw_secondary if r in valid_roles and r != data['role'] and r != 'CEO']

        new_admin = Admin(
            username=data['username'].strip(),
            email=data['email'].strip().lower(),
            password_hash=generate_password_hash(data['password']),
            role=data['role'],
            secondary_roles=secondary,
            display_name=(data.get('display_name') or '').strip() or None,
            is_active=True,
            has_signed_contract=False,
        )
        db.session.add(new_admin)
        db.session.commit()
        if new_admin.role != 'CEO':
            contract = UserContract(user_id=new_admin.id, contract_type=new_admin.role, status='pending_user_signature')
            db.session.add(contract)
            db.session.commit()
        return jsonify({"success": True, "message": f"{data['role']} account created successfully", "id": new_admin.id})
    except IntegrityError:
        db.session.rollback()
        return jsonify({"success": False, "message": "Username or email already exists"}), 409
    except Exception as e:
        logger.error(f"Error managing accounts: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/accounts/<int:account_id>', methods=['PUT', 'DELETE'])
@login_required
@ceo_required
def admin_account_detail(account_id):
    try:
        account = Admin.query.get_or_404(account_id)
        ceo = get_current_admin()

        if request.method == 'PUT':
            data = request.get_json()
            if 'display_name' in data:
                account.display_name = (data['display_name'] or '').strip() or None
            if 'is_active' in data:
                if account.id == ceo.id and not data['is_active']:
                    return jsonify({"success": False, "message": "Cannot deactivate your own account"}), 400
                account.is_active = bool(data['is_active'])
            if 'role' in data and account.id != ceo.id:
                valid_roles = ['CEO', 'MANAGER', 'ACCOUNTANT', 'REALTOR', 'INVESTOR']
                if data['role'] in valid_roles:
                    account.role = data['role']
            if 'secondary_roles' in data and account.id != ceo.id:
                valid_roles = ['CEO', 'MANAGER', 'ACCOUNTANT', 'REALTOR', 'INVESTOR']
                primary = data.get('role', account.role)
                account.secondary_roles = [r for r in (data['secondary_roles'] or []) if r in valid_roles and r != primary and r != 'CEO']
            if data.get('new_password') and len(data['new_password']) >= 8:
                account.password_hash = generate_password_hash(data['new_password'])
            db.session.commit()
            return jsonify({"success": True, "message": "Account updated"})

        if account.id == ceo.id:
            return jsonify({"success": False, "message": "Cannot delete your own account"}), 400
        account.is_active = False
        db.session.commit()
        return jsonify({"success": True, "message": "Account deactivated"})
    except Exception as e:
        logger.error(f"Error on account {account_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== TENANT API ==========
@app.route('/admin/api/tenants', methods=['GET', 'POST'])
@login_required
def admin_tenants():
    try:
        if request.method == 'GET':
            status_filter = request.args.get('status')
            q = Tenant.query
            if status_filter:
                q = q.filter_by(status=status_filter)
            tenants = q.order_by(Tenant.created_at.desc()).all()
            return jsonify([{
                'id': t.id,
                'name': t.name,
                'email': t.email or '',
                'phone': t.phone or '',
                'property_name': t.property_name or '',
                'unit_number': t.unit_number or '',
                'lease_start': t.lease_start.isoformat() if t.lease_start else '',
                'lease_end': t.lease_end.isoformat() if t.lease_end else '',
                'monthly_rent': t.monthly_rent or 0,
                'status': t.status,
                'notes': t.notes or '',
                'created_at': t.created_at.strftime('%Y-%m-%d'),
            } for t in tenants])

        data = request.get_json() or {}
        if not data.get('name'):
            return jsonify({"success": False, "message": "Tenant name is required"}), 400
        tenant = Tenant(
            name=data['name'].strip(),
            email=(data.get('email') or '').strip() or None,
            phone=(data.get('phone') or '').strip() or None,
            property_name=(data.get('property_name') or '').strip() or None,
            unit_number=(data.get('unit_number') or '').strip() or None,
            lease_start=date_type.fromisoformat(data['lease_start']) if data.get('lease_start') else None,
            lease_end=date_type.fromisoformat(data['lease_end']) if data.get('lease_end') else None,
            monthly_rent=float(data.get('monthly_rent') or 0),
            status=data.get('status', 'active'),
            notes=(data.get('notes') or '').strip() or None,
        )
        db.session.add(tenant)
        db.session.commit()
        return jsonify({"success": True, "message": "Tenant added", "id": tenant.id})
    except Exception as e:
        logger.error(f"Error managing tenants: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/tenants/<int:tenant_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_tenant_detail(tenant_id):
    try:
        tenant = Tenant.query.get_or_404(tenant_id)
        if request.method == 'DELETE':
            tenant.status = 'vacated'
            db.session.commit()
            return jsonify({"success": True, "message": "Tenant marked as vacated"})
        data = request.get_json() or {}
        for field in ['name', 'email', 'phone', 'property_name', 'unit_number', 'status', 'notes']:
            if field in data:
                setattr(tenant, field, (data[field] or '').strip() or None if field != 'status' else data[field])
        if data.get('monthly_rent') is not None:
            tenant.monthly_rent = float(data['monthly_rent'])
        if data.get('lease_start'):
            tenant.lease_start = date_type.fromisoformat(data['lease_start'])
        if data.get('lease_end'):
            tenant.lease_end = date_type.fromisoformat(data['lease_end'])
        db.session.commit()
        return jsonify({"success": True, "message": "Tenant updated"})
    except Exception as e:
        logger.error(f"Error on tenant {tenant_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== PAYMENT RECORD API ==========
@app.route('/admin/api/payments', methods=['GET', 'POST'])
@login_required
def admin_payments():
    try:
        if request.method == 'GET':
            payments = PaymentRecord.query.order_by(PaymentRecord.created_at.desc()).limit(50).all()
            return jsonify([{
                'id': p.id,
                'tenant_id': p.tenant_id,
                'tenant_name': p.tenant_name or '',
                'amount': p.amount,
                'payment_date': p.payment_date.isoformat() if p.payment_date else '',
                'payment_type': p.payment_type,
                'description': p.description or '',
                'recorded_by': p.recorded_by or '',
                'created_at': p.created_at.strftime('%Y-%m-%d %H:%M'),
            } for p in payments])

        data = request.get_json() or {}
        if not data.get('amount'):
            return jsonify({"success": False, "message": "Amount is required"}), 400
        tenant_name = data.get('tenant_name', '').strip()
        tenant_id = data.get('tenant_id') or None
        if tenant_id:
            t = Tenant.query.get(int(tenant_id))
            if t:
                tenant_name = t.name
        admin = get_current_admin()
        payment = PaymentRecord(
            tenant_id=int(tenant_id) if tenant_id else None,
            tenant_name=tenant_name or None,
            amount=float(data['amount']),
            payment_date=date_type.fromisoformat(data['payment_date']) if data.get('payment_date') else date_type.today(),
            payment_type=data.get('payment_type', 'rent'),
            description=(data.get('description') or '').strip() or None,
            recorded_by=admin.display_name or admin.username if admin else None,
        )
        db.session.add(payment)
        db.session.commit()
        return jsonify({"success": True, "message": "Payment recorded", "id": payment.id})
    except Exception as e:
        logger.error(f"Error managing payments: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== INVESTOR PROFILE API ==========
@app.route('/admin/api/investors', methods=['GET', 'POST'])
@login_required
@ceo_required
def admin_investors():
    try:
        if request.method == 'GET':
            profiles = InvestorProfile.query.order_by(InvestorProfile.created_at.desc()).all()
            result = []
            for p in profiles:
                user = Admin.query.get(p.user_id)
                result.append({
                    'id': p.id,
                    'user_id': p.user_id,
                    'investor_name': user.display_name or user.username if user else 'Unknown',
                    'investor_email': user.email if user else '',
                    'investment_type': p.investment_type,
                    'investment_amount': p.investment_amount,
                    'investment_date': p.investment_date.isoformat() if p.investment_date else None,
                    'roi_rate': p.roi_rate,
                    'equity_percentage': p.equity_percentage,
                    'construction_start_date': p.construction_start_date.isoformat() if p.construction_start_date else None,
                    'expected_completion_date': p.expected_completion_date.isoformat() if p.expected_completion_date else None,
                    'total_distributed': p.total_distributed,
                    'notes': p.notes or '',
                    'created_at': p.created_at.strftime('%Y-%m-%d'),
                })
            return jsonify(result)

        data = request.get_json()
        if not data.get('user_id') or not data.get('investment_amount') or not data.get('investment_type'):
            return jsonify({"success": False, "message": "user_id, investment_amount, and investment_type are required"}), 400

        existing = InvestorProfile.query.filter_by(user_id=data['user_id']).first()
        if existing:
            return jsonify({"success": False, "message": "Investor profile already exists for this user"}), 409

        def parse_date(d):
            return datetime.strptime(d, '%Y-%m-%d').date() if d else None

        profile = InvestorProfile(
            user_id=int(data['user_id']),
            investment_type=data['investment_type'],
            investment_amount=float(data['investment_amount']),
            investment_date=parse_date(data.get('investment_date')),
            roi_rate=float(data.get('roi_rate') or 10.0),
            equity_percentage=float(data['equity_percentage']) if data.get('equity_percentage') else None,
            construction_start_date=parse_date(data.get('construction_start_date')),
            expected_completion_date=parse_date(data.get('expected_completion_date')),
            notes=(data.get('notes') or '').strip() or None,
        )
        db.session.add(profile)
        db.session.commit()
        return jsonify({"success": True, "message": "Investor profile created", "id": profile.id})
    except Exception as e:
        logger.error(f"Error managing investors: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/investors/<int:profile_id>', methods=['PUT'])
@login_required
@ceo_required
def admin_investor_detail(profile_id):
    try:
        profile = InvestorProfile.query.get_or_404(profile_id)
        data = request.get_json()
        def parse_date(d):
            return datetime.strptime(d, '%Y-%m-%d').date() if d else None
        if 'investment_amount' in data:
            profile.investment_amount = float(data['investment_amount'])
        if 'investment_type' in data:
            profile.investment_type = data['investment_type']
        if 'roi_rate' in data:
            profile.roi_rate = float(data['roi_rate'])
        if 'equity_percentage' in data:
            profile.equity_percentage = float(data['equity_percentage']) if data['equity_percentage'] else None
        if 'investment_date' in data:
            profile.investment_date = parse_date(data['investment_date'])
        if 'construction_start_date' in data:
            profile.construction_start_date = parse_date(data['construction_start_date'])
        if 'expected_completion_date' in data:
            profile.expected_completion_date = parse_date(data['expected_completion_date'])
        if 'total_distributed' in data:
            profile.total_distributed = float(data['total_distributed'])
        if 'notes' in data:
            profile.notes = (data['notes'] or '').strip() or None
        profile.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"success": True, "message": "Investor profile updated"})
    except Exception as e:
        logger.error(f"Error updating investor profile {profile_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/my-investment', methods=['GET'])
@login_required
def my_investment():
    try:
        admin = get_current_admin()
        if admin.role != 'INVESTOR':
            return jsonify({"success": False, "message": "Not an investor account"}), 403
        profile = InvestorProfile.query.filter_by(user_id=admin.id).first()
        if not profile:
            return jsonify(None)
        return jsonify({
            'investment_type': profile.investment_type,
            'investment_amount': profile.investment_amount,
            'investment_date': profile.investment_date.isoformat() if profile.investment_date else None,
            'roi_rate': profile.roi_rate,
            'equity_percentage': profile.equity_percentage,
            'construction_start_date': profile.construction_start_date.isoformat() if profile.construction_start_date else None,
            'expected_completion_date': profile.expected_completion_date.isoformat() if profile.expected_completion_date else None,
            'total_distributed': profile.total_distributed,
            'notes': profile.notes or '',
        })
    except Exception as e:
        logger.error(f"Error fetching investment: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== ADMIN TEMPLATES ==========
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BrightWave Habitat Enterprise</title>
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#475569">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="BrightWave">
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
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js?v=3').catch(() => {});
        }

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
    <title>CEO Dashboard - BrightWave Habitat Enterprise</title>
    <meta name="csrf-token" content="{{ csrf_token }}">
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#475569">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="BrightWave CEO">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
    <style>
        .ceo-nav-btn { color: #94a3b8; }
        .ceo-nav-btn:hover { background: rgba(71,85,105,0.5); color: #e2e8f0; }
        .ceo-nav-btn.active { background: #475569; color: #ffffff; font-weight: 600; }
        .scrollbar-none::-webkit-scrollbar { display: none; }
        .scrollbar-none { -ms-overflow-style: none; scrollbar-width: none; }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <script>
        const PENDING_SIGS_COUNT = {{ pending_sigs_count }};
        const USER_NAME = '{{ user_name }}';
    </script>

    <!-- HEADER -->
    <header class="bg-slate-900 border-b border-slate-700/60 shadow-2xl sticky top-0 z-40">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex items-center justify-between h-16">
                <div class="flex items-center gap-3">
                    <div class="relative flex-shrink-0">
                        <img src="/assets/images/brightwave-logo.png" alt="BrightWave" class="h-10 w-10 rounded-full ring-2 ring-slate-400/40 shadow-lg object-cover">
                        <div class="absolute -inset-1 bg-gradient-to-r from-blue-400 to-blue-600 rounded-full blur opacity-20 pointer-events-none"></div>
                    </div>
                    <div>
                        <span class="text-lg font-bold text-white leading-none block">BrightWave</span>
                        <p class="text-xs text-slate-400 font-medium">CEO Portal &middot; {{ user_name }}</p>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    {% if pending_sigs_count > 0 %}
                    <button onclick="showSection('signaturesSection')" class="relative bg-red-600 hover:bg-red-700 text-white text-xs sm:text-sm font-medium py-1.5 px-2 sm:px-3 rounded-lg transition-colors">
                        <i class="fas fa-pen-nib mr-1"></i><span class="hidden sm:inline">Signatures</span>
                        <span class="absolute -top-1.5 -right-1.5 bg-yellow-400 text-gray-900 text-xs font-bold rounded-full w-5 h-5 flex items-center justify-center">{{ pending_sigs_count }}</span>
                    </button>
                    {% endif %}
                    <button id="changePasswordBtn" title="Change Password" class="text-slate-400 hover:text-white p-2 rounded-lg hover:bg-slate-700/60 transition-colors">
                        <i class="fas fa-key text-sm"></i>
                    </button>
                    <a href="/admin/logout" class="bg-slate-700/70 hover:bg-slate-600 border border-slate-600/40 text-slate-300 hover:text-white text-sm px-3 py-1.5 rounded-lg transition-colors">
                        <i class="fas fa-sign-out-alt mr-1"></i>Logout
                    </a>
                </div>
            </div>
        </div>
    </header>

    <!-- NAV TABS -->
    <nav class="bg-gray-800/90 backdrop-blur border-b border-gray-700/50 sticky top-16 z-30">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <!-- Mobile: hamburger bar -->
            <div class="flex items-center gap-3 md:hidden h-12">
                <button id="hamburgerBtn" class="text-slate-400 hover:text-white p-2 rounded-lg hover:bg-slate-700/50 transition-colors flex-shrink-0" aria-label="Menu">
                    <i class="fas fa-bars text-base" id="hamburgerIcon"></i>
                </button>
                <span id="mobileNavLabel" class="text-sm font-medium text-white flex items-center gap-1.5 truncate">
                    <i class="fas fa-chart-line text-slate-400"></i> Overview
                </span>
            </div>
            <!-- Desktop: full tab row -->
            <div id="navItemsContainer" class="hidden md:flex gap-1 overflow-x-auto scrollbar-none py-2">
                <button onclick="showSection('overviewSection')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-chart-line mr-1.5"></i>Overview
                </button>
                <button onclick="showSection('tenantsSection')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-home mr-1.5"></i>Tenants
                </button>
                <button onclick="showSection('paymentsSection')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-money-bill-wave mr-1.5"></i>Payments
                </button>
                <button onclick="showSection('signaturesSection')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-signature mr-1.5"></i>Signatures{% if pending_sigs_count > 0 %}<span class="ml-1.5 bg-red-500 text-white text-xs px-1.5 py-0.5 rounded-full">{{ pending_sigs_count }}</span>{% endif %}
                </button>
                <button onclick="showSection('accountsSection')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-users mr-1.5"></i>Accounts
                </button>
                <button onclick="showSection('investorsSection')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-chart-pie mr-1.5"></i>Investors
                </button>
                <button onclick="showSection('propertiesSection')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-building mr-1.5"></i>Properties
                </button>
                <button onclick="showSection('contentSection')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-globe mr-1.5"></i>Website
                </button>
                <button onclick="showSection('teamSection')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-id-card mr-1.5"></i>Our Team
                </button>
                <button onclick="showSection('inquiriesSection2')" class="ceo-nav-btn whitespace-nowrap text-sm px-3 sm:px-4 py-2 rounded-lg transition-all">
                    <i class="fas fa-envelope mr-1.5"></i>Inquiries
                </button>
            </div>
            <!-- Mobile: dropdown menu (hidden by default) -->
            <div id="mobileNavMenu" class="md:hidden hidden pb-2 space-y-0.5">
                <button onclick="showSection('overviewSection')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-chart-line w-4 text-center"></i>Overview
                </button>
                <button onclick="showSection('tenantsSection')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-home w-4 text-center"></i>Tenants
                </button>
                <button onclick="showSection('paymentsSection')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-money-bill-wave w-4 text-center"></i>Payments
                </button>
                <button onclick="showSection('signaturesSection')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-signature w-4 text-center"></i>Signatures{% if pending_sigs_count > 0 %}<span class="ml-auto bg-red-500 text-white text-xs px-1.5 py-0.5 rounded-full">{{ pending_sigs_count }}</span>{% endif %}
                </button>
                <button onclick="showSection('accountsSection')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-users w-4 text-center"></i>Accounts
                </button>
                <button onclick="showSection('investorsSection')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-chart-pie w-4 text-center"></i>Investors
                </button>
                <button onclick="showSection('propertiesSection')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-building w-4 text-center"></i>Properties
                </button>
                <button onclick="showSection('contentSection')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-globe w-4 text-center"></i>Website
                </button>
                <button onclick="showSection('teamSection')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-id-card w-4 text-center"></i>Our Team
                </button>
                <button onclick="showSection('inquiriesSection2')" class="ceo-nav-btn mobile-nav-item w-full text-left text-sm px-4 py-2.5 rounded-lg transition-all flex items-center gap-2">
                    <i class="fas fa-envelope w-4 text-center"></i>Inquiries
                </button>
            </div>
        </div>
    </nav>

    <main class="max-w-7xl mx-auto py-6 px-4 sm:px-6 lg:px-8">
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


        <!-- PENDING SIGNATURES SECTION -->
        <section id="signaturesSection" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Pending Signatures</h2>
            <div id="signaturesContent" class="space-y-4">
                <!-- Populated by JS -->
            </div>
        </section>

        <!-- TEAM ACCOUNTS SECTION -->
        <section id="accountsSection" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Team Accounts</h2>

            <!-- Edit Account Panel (hidden until Edit clicked) -->
            <div id="editAccountPanel" class="hidden bg-gray-700 border border-slate-500/50 p-4 rounded-xl mb-4">
                <h4 class="font-semibold mb-3 text-slate-200 flex items-center gap-2"><i class="fas fa-user-edit text-blue-400"></i> Editing: <span id="editAccName" class="text-white"></span></h4>
                <input type="hidden" id="editAccId">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Primary Role</label>
                        <select id="editAccRole" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-sm">
                            <option value="MANAGER">Manager</option>
                            <option value="ACCOUNTANT">Accountant</option>
                            <option value="REALTOR">Realtor</option>
                            <option value="INVESTOR">Investor</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Additional Roles</label>
                        <div class="flex gap-4 flex-wrap pt-1">
                            <label class="flex items-center gap-1.5 text-sm cursor-pointer text-gray-300"><input type="checkbox" class="edit-sec-role accent-blue-500" value="MANAGER"> Manager</label>
                            <label class="flex items-center gap-1.5 text-sm cursor-pointer text-gray-300"><input type="checkbox" class="edit-sec-role accent-green-500" value="ACCOUNTANT"> Accountant</label>
                            <label class="flex items-center gap-1.5 text-sm cursor-pointer text-gray-300"><input type="checkbox" class="edit-sec-role accent-amber-500" value="REALTOR"> Realtor</label>
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="saveAccountEdit()" class="bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium py-1.5 px-4 rounded-lg transition-colors">Save Changes</button>
                    <button onclick="closeAccountEdit()" class="bg-gray-600 hover:bg-gray-500 text-white text-sm font-medium py-1.5 px-4 rounded-lg transition-colors">Cancel</button>
                    <span id="editAccMessage" class="text-sm ml-1"></span>
                </div>
            </div>

            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="font-semibold mb-3 text-slate-300">Create New Account</h3>
                <form id="createAccountForm" class="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Display Name</label>
                        <input type="text" id="accDisplayName" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Username *</label>
                        <input type="text" id="accUsername" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Email *</label>
                        <input type="email" id="accEmail" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Password *</label>
                        <input type="password" id="accPassword" required minlength="8" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Role *</label>
                        <select id="accRole" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="">Select Role</option>
                            <option value="MANAGER">Manager</option>
                            <option value="ACCOUNTANT">Accountant</option>
                            <option value="REALTOR">Realtor</option>
                            <option value="INVESTOR">Investor</option>
                        </select>
                    </div>
                    <div class="md:col-span-3">
                        <label class="block text-xs font-medium mb-1 text-gray-400">Additional Roles (optional — e.g. Manager who also does sales)</label>
                        <div class="flex gap-4 flex-wrap">
                            <label class="flex items-center gap-1.5 text-sm text-gray-300 cursor-pointer"><input type="checkbox" name="secondary_roles" value="MANAGER" class="accent-blue-500"> Manager</label>
                            <label class="flex items-center gap-1.5 text-sm text-gray-300 cursor-pointer"><input type="checkbox" name="secondary_roles" value="ACCOUNTANT" class="accent-green-500"> Accountant</label>
                            <label class="flex items-center gap-1.5 text-sm text-gray-300 cursor-pointer"><input type="checkbox" name="secondary_roles" value="REALTOR" class="accent-amber-500"> Realtor</label>
                        </div>
                        <p class="text-xs text-gray-600 mt-1">Cannot add CEO or same as primary role. Investor cannot have secondary roles.</p>
                    </div>
                    <div class="flex items-end">
                        <button type="submit" class="bg-slate-600 hover:bg-slate-700 text-white text-sm font-medium py-2 px-4 rounded-lg w-full">Create Account</button>
                    </div>
                </form>
                <p id="accountMessage" class="text-sm mt-2 hidden"></p>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg">
                <h3 class="font-semibold mb-3 text-slate-300">All Accounts</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm">
                        <thead>
                            <tr class="border-b border-gray-600">
                                <th class="py-2 text-left">Name</th>
                                <th class="py-2 text-left">Username</th>
                                <th class="py-2 text-left">Email</th>
                                <th class="py-2 text-left">Primary Role</th>
                                <th class="py-2 text-left">Also</th>
                                <th class="py-2 text-left">Contract</th>
                                <th class="py-2 text-left">Status</th>
                                <th class="py-2 text-left">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="accountsTable"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- INVESTORS SECTION -->
        <section id="investorsSection" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Investor Profiles</h2>
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="font-semibold mb-3 text-slate-300">Create Investor Profile</h3>
                <p class="text-xs text-gray-400 mb-3">First create an INVESTOR account, then link their investment details here.</p>
                <form id="createInvestorForm" class="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Investor Account *</label>
                        <select id="invUserId" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="">Select Investor</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Investment Type *</label>
                        <select id="invType" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="DEBT">Debt (Fixed Return + Principal Back)</option>
                            <option value="EQUITY">Equity (Project Ownership Stake)</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Investment Amount (₦) *</label>
                        <input type="number" id="invAmount" required step="1000" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Investment Date</label>
                        <input type="date" id="invDate" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">ROI Rate % (Annual, Debt)</label>
                        <input type="number" id="invRoi" value="10" step="0.5" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Equity % (Equity type only)</label>
                        <input type="number" id="invEquity" step="0.1" placeholder="e.g. 10" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Construction Start Date</label>
                        <input type="date" id="invConstructionStart" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Expected Completion</label>
                        <input type="date" id="invCompletion" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Notes</label>
                        <input type="text" id="invNotes" placeholder="Optional notes" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div class="md:col-span-3">
                        <button type="submit" class="bg-emerald-700 hover:bg-emerald-800 text-white text-sm font-medium py-2 px-6 rounded-lg">Create Investor Profile</button>
                        <span id="investorMessage" class="ml-3 text-sm"></span>
                    </div>
                </form>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg">
                <h3 class="font-semibold mb-3 text-slate-300">All Investors</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm">
                        <thead>
                            <tr class="border-b border-gray-600">
                                <th class="py-2 text-left">Investor</th>
                                <th class="py-2 text-left">Type</th>
                                <th class="py-2 text-left">Amount</th>
                                <th class="py-2 text-left">ROI/Equity</th>
                                <th class="py-2 text-left">Distributed</th>
                                <th class="py-2 text-left">Completion</th>
                                <th class="py-2 text-left">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="investorsTable"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- TENANTS SECTION -->
        <section id="tenantsSection" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Tenants</h2>
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="font-semibold mb-3 text-slate-300">Add Tenant</h3>
                <form id="addTenantForm" class="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Full Name *</label><input type="text" id="tnName" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Email</label><input type="email" id="tnEmail" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Phone</label><input type="text" id="tnPhone" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Property</label><input type="text" id="tnProperty" placeholder="e.g. BrightWave Hostel Phase 1" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit / Room No.</label><input type="text" id="tnUnit" placeholder="e.g. Room 12" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Monthly Rent (₦)</label><input type="number" id="tnRent" step="1000" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Lease Start</label><input type="date" id="tnLeaseStart" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Lease End</label><input type="date" id="tnLeaseEnd" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Status</label>
                        <select id="tnStatus" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="active">Active</option><option value="vacated">Vacated</option>
                        </select>
                    </div>
                    <div class="md:col-span-3"><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label><textarea id="tnNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea></div>
                    <div class="flex items-end gap-3">
                        <button type="submit" class="bg-teal-700 hover:bg-teal-800 text-white text-sm font-medium py-2 px-4 rounded-lg">Add Tenant</button>
                        <span id="tenantMsg" class="text-sm"></span>
                    </div>
                </form>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg">
                <div class="flex items-center gap-3 mb-3">
                    <h3 class="font-semibold text-slate-300 flex-1">All Tenants</h3>
                    <select id="tnFilterStatus" onchange="loadTenants(this.value)" class="bg-gray-700 border border-gray-600 text-sm rounded-lg px-3 py-1.5 text-white">
                        <option value="">All</option><option value="active">Active</option><option value="vacated">Vacated</option>
                    </select>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm">
                        <thead><tr class="border-b border-gray-600">
                            <th class="py-2 text-left text-gray-400">Name</th>
                            <th class="py-2 text-left text-gray-400">Contact</th>
                            <th class="py-2 text-left text-gray-400">Property / Unit</th>
                            <th class="py-2 text-left text-gray-400">Rent</th>
                            <th class="py-2 text-left text-gray-400">Lease</th>
                            <th class="py-2 text-left text-gray-400">Status</th>
                            <th class="py-2 text-left text-gray-400">Actions</th>
                        </tr></thead>
                        <tbody id="tenantsTable"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- PAYMENTS SECTION -->
        <section id="paymentsSection" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Payments</h2>
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="font-semibold mb-3 text-slate-300">Record Payment</h3>
                <form id="addPaymentForm" class="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Tenant</label>
                        <select id="pmtTenantId" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="">-- Select or type below --</option>
                        </select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Tenant Name (if not listed)</label><input type="text" id="pmtTenantName" placeholder="Free-text name" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Amount (₦) *</label><input type="number" id="pmtAmount" required step="100" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Payment Date *</label><input type="date" id="pmtDate" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Payment Type</label>
                        <select id="pmtType" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="rent">Rent</option><option value="deposit">Deposit</option><option value="fee">Fee</option><option value="other">Other</option>
                        </select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Description</label><input type="text" id="pmtDesc" placeholder="Optional notes" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div class="flex items-end gap-3">
                        <button type="submit" class="bg-emerald-700 hover:bg-emerald-800 text-white text-sm font-medium py-2 px-4 rounded-lg">Record Payment</button>
                        <span id="paymentMsg" class="text-sm"></span>
                    </div>
                </form>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg">
                <h3 class="font-semibold mb-3 text-slate-300">Recent Payments (last 50)</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm">
                        <thead><tr class="border-b border-gray-600">
                            <th class="py-2 text-left text-gray-400">Tenant</th>
                            <th class="py-2 text-left text-gray-400">Amount</th>
                            <th class="py-2 text-left text-gray-400">Date</th>
                            <th class="py-2 text-left text-gray-400">Type</th>
                            <th class="py-2 text-left text-gray-400">Description</th>
                            <th class="py-2 text-left text-gray-400">Recorded By</th>
                        </tr></thead>
                        <tbody id="paymentsTable"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <!-- Enhanced Statistics -->
        <section id="overviewSection" class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Dashboard Overview</h2>
            <div id="stats" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
                <!-- Stats will be populated by JavaScript -->
            </div>
            <div id="businessStats" class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <!-- Business metrics cards -->
            </div>
            <div id="recentActivity" class="bg-gray-800 p-4 rounded-lg">
                <!-- Recent activity will be populated -->
            </div>
        </section>

        <!-- Add Property Form -->
        <section id="propertiesSection" class="mb-8 hidden">
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

        <section id="contentSection" class="mb-8 hidden">
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

        <section id="teamSection" class="mb-8 hidden">
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

        <!-- Properties Table (part of propertiesSection) -->
        <section class="mb-8 hidden" id="propertiesTableSection">
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
        <section id="inquiriesSection2" class="hidden">
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
        const adminCsrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

        // Enhanced dashboard functionality
        async function fetchData(url, options = {}) {
            const headers = new Headers(options.headers || {});
            if (adminCsrfToken && ['POST', 'PUT', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
                headers.set('X-CSRF-Token', adminCsrfToken);
            }
            const response = await fetch(url, {
                credentials: 'include',
                headers,
                ...options
            });
            if (!response.ok) throw new Error('Network response was not ok');
            return response.json();
        }

        function fmtNGN(v) { return '\u20a6' + Number(v || 0).toLocaleString('en-NG'); }

        async function loadStats() {
            try {
                const stats = await fetchData('/admin/api/stats');

                // Business metrics row
                document.getElementById('businessStats').innerHTML = `
                    <div class="bg-teal-800/60 border border-teal-700/40 p-5 rounded-xl flex items-start gap-4 shadow">
                        <div class="w-10 h-10 bg-teal-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-users text-teal-300"></i>
                        </div>
                        <div>
                            <p class="text-xs text-teal-400 font-medium uppercase tracking-wide">Active Tenants</p>
                            <p class="text-3xl font-bold text-white mt-0.5">${stats.active_tenants}</p>
                            <p class="text-xs text-teal-300 mt-1">${stats.total_tenants} total recorded</p>
                        </div>
                    </div>
                    <div class="bg-emerald-800/60 border border-emerald-700/40 p-5 rounded-xl flex items-start gap-4 shadow">
                        <div class="w-10 h-10 bg-emerald-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-money-bill-wave text-emerald-300"></i>
                        </div>
                        <div>
                            <p class="text-xs text-emerald-400 font-medium uppercase tracking-wide">This Month Revenue</p>
                            <p class="text-2xl font-bold text-white mt-0.5">${fmtNGN(stats.monthly_revenue)}</p>
                            <p class="text-xs text-emerald-300 mt-1">All time: ${fmtNGN(stats.total_revenue)}</p>
                        </div>
                    </div>
                    <div class="bg-slate-700/80 border border-slate-600/40 p-5 rounded-xl flex items-start gap-4 shadow">
                        <div class="w-10 h-10 bg-slate-600 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-building text-slate-300"></i>
                        </div>
                        <div>
                            <p class="text-xs text-slate-400 font-medium uppercase tracking-wide">Properties</p>
                            <p class="text-3xl font-bold text-white mt-0.5">${stats.total_properties}</p>
                            <p class="text-xs text-slate-400 mt-1">${stats.active_properties} active &bull; H:${stats.property_breakdown.hostels} L:${stats.property_breakdown.land_plots} R:${stats.property_breakdown.residential}</p>
                        </div>
                    </div>
                `;

                // Website metrics row
                document.getElementById('stats').innerHTML = `
                    <div class="bg-green-800/60 border border-green-700/40 p-5 rounded-xl flex items-start gap-4 shadow">
                        <div class="w-10 h-10 bg-green-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-search text-green-300"></i>
                        </div>
                        <div>
                            <p class="text-xs text-green-400 font-medium uppercase tracking-wide">Inquiries</p>
                            <p class="text-3xl font-bold text-white mt-0.5">${stats.total_inquiries}</p>
                            <p class="text-xs text-green-300 mt-1">${stats.new_inquiries} new</p>
                        </div>
                    </div>
                    <div class="bg-blue-800/60 border border-blue-700/40 p-5 rounded-xl flex items-start gap-4 shadow">
                        <div class="w-10 h-10 bg-blue-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-envelope text-blue-300"></i>
                        </div>
                        <div>
                            <p class="text-xs text-blue-400 font-medium uppercase tracking-wide">Messages</p>
                            <p class="text-3xl font-bold text-white mt-0.5">${stats.contact_messages}</p>
                            <p class="text-xs text-blue-300 mt-1">${stats.new_messages} new</p>
                        </div>
                    </div>
                    <div class="bg-purple-800/60 border border-purple-700/40 p-5 rounded-xl flex items-start gap-4 shadow">
                        <div class="w-10 h-10 bg-purple-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-users text-purple-300"></i>
                        </div>
                        <div>
                            <p class="text-xs text-purple-400 font-medium uppercase tracking-wide">Active Team</p>
                            <p class="text-3xl font-bold text-white mt-0.5">${stats.active_team_members}</p>
                            <p class="text-xs text-purple-300 mt-1">On About page</p>
                        </div>
                    </div>
                `;

                // Recent activity
                const noRows = '<tr><td colspan="99" class="text-gray-500 text-sm py-3 text-center italic">None yet</td></tr>';
                document.getElementById('recentActivity').innerHTML = `
                    <h3 class="text-base font-semibold text-slate-300 mb-4 flex items-center gap-2"><i class="fas fa-clock text-slate-400"></i> Recent Activity</h3>
                    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                        <div>
                            <h4 class="text-xs font-semibold text-teal-400 uppercase tracking-wide mb-3 flex items-center gap-1.5"><i class="fas fa-money-bill-wave"></i> Recent Payments</h4>
                            <table class="w-full text-sm">
                                <thead><tr class="border-b border-gray-700"><th class="py-1 text-left text-gray-500 font-medium">Tenant</th><th class="py-1 text-left text-gray-500 font-medium">Amount</th><th class="py-1 text-left text-gray-500 font-medium">Date</th></tr></thead>
                                <tbody>${stats.recent_activity.payments.length ? stats.recent_activity.payments.map(p => `
                                    <tr class="border-b border-gray-700/50">
                                        <td class="py-2 text-white">${p.tenant_name}</td>
                                        <td class="py-2 text-emerald-400 font-medium">${fmtNGN(p.amount)}</td>
                                        <td class="py-2 text-gray-400 text-xs">${p.payment_date}</td>
                                    </tr>`).join('') : noRows}</tbody>
                            </table>
                        </div>
                        <div>
                            <h4 class="text-xs font-semibold text-blue-400 uppercase tracking-wide mb-3 flex items-center gap-1.5"><i class="fas fa-users"></i> Recent Tenants</h4>
                            <table class="w-full text-sm">
                                <thead><tr class="border-b border-gray-700"><th class="py-1 text-left text-gray-500 font-medium">Name</th><th class="py-1 text-left text-gray-500 font-medium">Property</th><th class="py-1 text-left text-gray-500 font-medium">Status</th></tr></thead>
                                <tbody>${stats.recent_activity.tenants.length ? stats.recent_activity.tenants.map(t => `
                                    <tr class="border-b border-gray-700/50">
                                        <td class="py-2 text-white">${t.name}</td>
                                        <td class="py-2 text-gray-400 text-xs">${t.property_name || '—'}</td>
                                        <td class="py-2"><span class="text-xs px-2 py-0.5 rounded-full ${t.status === 'active' ? 'bg-teal-800 text-teal-300' : 'bg-gray-700 text-gray-400'}">${t.status}</span></td>
                                    </tr>`).join('') : noRows}</tbody>
                            </table>
                        </div>
                    </div>
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mt-6">
                        <div>
                            <h4 class="text-xs font-semibold text-green-400 uppercase tracking-wide mb-3 flex items-center gap-1.5"><i class="fas fa-search"></i> Latest Inquiries</h4>
                            ${stats.recent_activity.inquiries.length ? stats.recent_activity.inquiries.map(inq => `
                                <div class="text-sm mb-2 p-3 bg-gray-700/60 border border-gray-600/40 rounded-lg">
                                    <span class="font-semibold text-white">${inq.name}</span>
                                    <span class="ml-2 text-xs bg-green-800/60 text-green-300 px-2 py-0.5 rounded-full">${inq.inquiry_type}</span>
                                    <br><span class="text-gray-500 text-xs mt-1 block">${inq.created_at}</span>
                                </div>`).join('') : '<p class="text-gray-500 text-sm italic">None yet</p>'}
                        </div>
                        <div>
                            <h4 class="text-xs font-semibold text-blue-400 uppercase tracking-wide mb-3 flex items-center gap-1.5"><i class="fas fa-envelope"></i> Latest Messages</h4>
                            ${stats.recent_activity.messages.length ? stats.recent_activity.messages.map(msg => `
                                <div class="text-sm mb-2 p-3 bg-gray-700/60 border border-gray-600/40 rounded-lg">
                                    <span class="font-semibold text-white">${msg.name}</span>
                                    <span class="ml-2 text-xs bg-blue-800/60 text-blue-300 px-2 py-0.5 rounded-full">${msg.form_origin}</span>
                                    <br><span class="text-gray-500 text-xs mt-1 block">${msg.created_at}</span>
                                </div>`).join('') : '<p class="text-gray-500 text-sm italic">None yet</p>'}
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
                headers: {
                    'X-CSRF-Token': adminCsrfToken
                },
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

        // ===== HAMBURGER MENU =====
        document.getElementById('hamburgerBtn')?.addEventListener('click', () => {
            const menu = document.getElementById('mobileNavMenu');
            const icon = document.getElementById('hamburgerIcon');
            const isOpen = !menu.classList.contains('hidden');
            menu.classList.toggle('hidden', isOpen);
            icon.className = isOpen ? 'fas fa-bars text-base' : 'fas fa-times text-base';
        });

        // ===== CEO SECTION NAVIGATION =====
        function showSection(sectionId) {
            const sections = ['overviewSection','tenantsSection','paymentsSection','signaturesSection','accountsSection','investorsSection','propertiesSection','contentSection','teamSection','inquiriesSection2','propertiesTableSection'];
            sections.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.classList.add('hidden');
            });
            const target = document.getElementById(sectionId);
            if (target) target.classList.remove('hidden');
            document.querySelectorAll('.ceo-nav-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll(`.ceo-nav-btn[onclick="showSection('${sectionId}')"]`).forEach(b => b.classList.add('active'));
            const mobileLabelBtn = document.querySelector(`.mobile-nav-item[onclick="showSection('${sectionId}')"]`);
            const mobileLabel = document.getElementById('mobileNavLabel');
            if (mobileLabelBtn && mobileLabel) mobileLabel.innerHTML = mobileLabelBtn.innerHTML.replace('w-full text-left', '').trim().split('</i>')[0] + '</i>' + mobileLabelBtn.textContent.trim();
            const mobileMenu = document.getElementById('mobileNavMenu');
            if (mobileMenu) mobileMenu.classList.add('hidden');
            const hamburgerIcon = document.getElementById('hamburgerIcon');
            if (hamburgerIcon) hamburgerIcon.className = 'fas fa-bars text-base';
            if (sectionId === 'signaturesSection') loadPendingContracts();
            if (sectionId === 'accountsSection') { loadAccounts(); loadInvestorAccountOptions(); }
            if (sectionId === 'investorsSection') { loadInvestors(); loadInvestorAccountOptions(); }
            if (sectionId === 'tenantsSection') loadTenants();
            if (sectionId === 'paymentsSection') { loadPayments(); loadTenantOptions(); }
            if (sectionId === 'propertiesSection') {
                const tableSection = document.getElementById('propertiesTableSection');
                if (tableSection) tableSection.classList.remove('hidden');
            }
        }

        // ===== PENDING SIGNATURES =====
        async function loadPendingContracts() {
            try {
                const contracts = await fetchData('/admin/api/pending-contracts');
                const el = document.getElementById('signaturesContent');
                if (!contracts.length) {
                    el.innerHTML = '<div class="bg-gray-800 p-6 rounded-lg text-gray-400 text-center">No pending signatures.</div>';
                    return;
                }
                el.innerHTML = contracts.map(c => `
                    <div class="bg-gray-800 p-5 rounded-lg border border-yellow-600">
                        <div class="flex justify-between items-start mb-3">
                            <div>
                                <p class="font-semibold text-lg">${c.user_name}</p>
                                <p class="text-sm text-gray-400">${c.user_email} &mdash; <span class="text-yellow-400 font-medium">${c.role}</span></p>
                                <p class="text-xs text-gray-500 mt-1">User signed: ${c.user_signed_at}</p>
                            </div>
                            <span class="bg-yellow-600 text-white text-xs px-2 py-1 rounded">Pending CEO Signature</span>
                        </div>
                        <div class="bg-gray-700 p-3 rounded mb-3">
                            <p class="text-sm text-gray-300">User signature: <strong class="text-white">"${c.user_signature}"</strong></p>
                        </div>
                        <div class="flex items-center gap-3">
                            <input type="text" id="ceoSig_${c.id}" placeholder="Type your full name to sign" class="flex-1 px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <button onclick="ceoSignContract(${c.id})" class="bg-emerald-700 hover:bg-emerald-800 text-white text-sm font-medium py-2 px-5 rounded-lg">Sign & Approve</button>
                        </div>
                        <p id="ceoSigMsg_${c.id}" class="text-sm mt-2 hidden"></p>
                    </div>
                `).join('');
            } catch (e) {
                document.getElementById('signaturesContent').innerHTML = '<p class="text-red-400">Error loading pending contracts.</p>';
            }
        }

        async function ceoSignContract(contractId) {
            const sig = document.getElementById('ceoSig_' + contractId).value.trim();
            const msgEl = document.getElementById('ceoSigMsg_' + contractId);
            if (!sig) { msgEl.textContent = 'Please type your full name to sign'; msgEl.className = 'text-sm mt-2 text-red-400'; msgEl.classList.remove('hidden'); return; }
            try {
                const res = await fetchData('/admin/api/contracts/' + contractId + '/ceo-sign', {
                    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({signature: sig})
                });
                msgEl.textContent = res.message;
                msgEl.className = 'text-sm mt-2 text-green-400';
                msgEl.classList.remove('hidden');
                setTimeout(() => loadPendingContracts(), 1500);
            } catch (e) {
                msgEl.textContent = 'Error signing contract';
                msgEl.className = 'text-sm mt-2 text-red-400';
                msgEl.classList.remove('hidden');
            }
        }

        // ===== TEAM ACCOUNTS =====
        async function loadAccounts() {
            try {
                const accounts = await fetchData('/admin/api/accounts');
                const roleColors = {CEO:'bg-purple-700',MANAGER:'bg-blue-700',ACCOUNTANT:'bg-green-700',REALTOR:'bg-amber-700',INVESTOR:'bg-emerald-700'};
                const statusColors = {completed:'text-green-400',pending_ceo_signature:'text-yellow-400',pending_user_signature:'text-orange-400',no_contract:'text-gray-500'};
                const statusLabels = {completed:'Signed',pending_ceo_signature:'Awaiting CEO',pending_user_signature:'Awaiting User',no_contract:'No Contract'};
                document.getElementById('accountsTable').innerHTML = accounts.map(a => {
                    const secondary = (a.secondary_roles || []).map(r => `<span class="text-xs px-1.5 py-0.5 rounded text-white ${roleColors[r] || 'bg-gray-600'} mr-1">${r}</span>`).join('');
                    return `
                    <tr class="border-b border-gray-700 hover:bg-gray-750">
                        <td class="py-2 pr-3">${a.display_name || '-'}</td>
                        <td class="py-2 pr-3 text-gray-300">${a.username}</td>
                        <td class="py-2 pr-3 text-gray-400 text-xs">${a.email}</td>
                        <td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded-full text-white ${roleColors[a.role] || 'bg-gray-600'}">${a.role}</span></td>
                        <td class="py-2 pr-3">${secondary || '<span class="text-xs text-gray-600">—</span>'}</td>
                        <td class="py-2 pr-3 text-xs ${statusColors[a.contract_status] || 'text-gray-500'}">${statusLabels[a.contract_status] || a.contract_status}</td>
                        <td class="py-2 pr-3"><span class="text-xs ${a.is_active ? 'text-green-400' : 'text-red-400'}">${a.is_active ? 'Active' : 'Inactive'}</span></td>
                        <td class="py-2 flex gap-2 flex-wrap">
                            <button onclick="editAccount(${a.id}, ${JSON.stringify(a.display_name || a.username)}, '${a.role}', ${JSON.stringify(a.secondary_roles || [])})" class="text-xs text-blue-400 hover:text-blue-300">Edit</button>
                            <button onclick="toggleAccount(${a.id}, ${!a.is_active})" class="text-xs ${a.is_active ? 'text-red-400 hover:text-red-300' : 'text-green-400 hover:text-green-300'}">${a.is_active ? 'Deactivate' : 'Activate'}</button>
                        </td>
                    </tr>`;
                }).join('');
            } catch (e) {
                document.getElementById('accountsTable').innerHTML = '<tr><td colspan="7" class="text-red-400 py-2">Error loading accounts</td></tr>';
            }
        }

        async function loadInvestorAccountOptions() {
            try {
                const accounts = await fetchData('/admin/api/accounts');
                const investors = accounts.filter(a => a.role === 'INVESTOR' && a.is_active);
                const sel = document.getElementById('invUserId');
                if (!sel) return;
                sel.innerHTML = '<option value="">Select Investor</option>' + investors.map(a => `<option value="${a.id}">${a.display_name || a.username} (${a.email})</option>`).join('');
            } catch (e) {}
        }

        async function toggleAccount(accountId, activate) {
            try {
                await fetchData('/admin/api/accounts/' + accountId, {
                    method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({is_active: activate})
                });
                loadAccounts();
            } catch (e) { alert('Error updating account'); }
        }

        function editAccount(id, name, currentRole, currentSecondary) {
            document.getElementById('editAccId').value = id;
            document.getElementById('editAccName').textContent = name;
            document.getElementById('editAccRole').value = currentRole;
            document.querySelectorAll('.edit-sec-role').forEach(cb => {
                cb.checked = currentSecondary.includes(cb.value);
            });
            const panel = document.getElementById('editAccountPanel');
            panel.classList.remove('hidden');
            panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            document.getElementById('editAccMessage').textContent = '';
        }

        function closeAccountEdit() {
            document.getElementById('editAccountPanel').classList.add('hidden');
        }

        async function saveAccountEdit() {
            const id = document.getElementById('editAccId').value;
            const role = document.getElementById('editAccRole').value;
            const secondaryRoles = Array.from(document.querySelectorAll('.edit-sec-role:checked')).map(cb => cb.value);
            const msgEl = document.getElementById('editAccMessage');
            try {
                const res = await fetchData('/admin/api/accounts/' + id, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ role, secondary_roles: secondaryRoles })
                });
                msgEl.textContent = '✓ ' + (res.message || 'Saved');
                msgEl.className = 'text-sm ml-1 text-green-400';
                setTimeout(() => { closeAccountEdit(); loadAccounts(); }, 1200);
            } catch (e) {
                msgEl.textContent = e.message || 'Error saving';
                msgEl.className = 'text-sm ml-1 text-red-400';
            }
        }

        document.getElementById('createAccountForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('accountMessage');
            try {
                const secondaryChecks = document.querySelectorAll('input[name="secondary_roles"]:checked');
                const secondaryRoles = Array.from(secondaryChecks).map(cb => cb.value);
                const res = await fetchData('/admin/api/accounts', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        display_name: document.getElementById('accDisplayName').value,
                        username: document.getElementById('accUsername').value,
                        email: document.getElementById('accEmail').value,
                        password: document.getElementById('accPassword').value,
                        role: document.getElementById('accRole').value,
                        secondary_roles: secondaryRoles,
                    })
                });
                msgEl.textContent = res.message;
                msgEl.className = 'text-sm mt-2 text-green-400';
                msgEl.classList.remove('hidden');
                document.getElementById('createAccountForm').reset();
                loadAccounts();
            } catch (e) {
                msgEl.textContent = e.message || 'Error creating account';
                msgEl.className = 'text-sm mt-2 text-red-400';
                msgEl.classList.remove('hidden');
            }
        });

        // ===== INVESTORS =====
        function formatNGN(amount) {
            return '₦' + Number(amount).toLocaleString('en-NG');
        }

        async function loadInvestors() {
            try {
                const investors = await fetchData('/admin/api/investors');
                document.getElementById('investorsTable').innerHTML = investors.map(p => {
                    const annualReturn = p.investment_type === 'DEBT' ? (p.investment_amount * p.roi_rate / 100) : null;
                    const display = p.investment_type === 'DEBT'
                        ? `${p.roi_rate}% p.a = ${formatNGN(annualReturn)}/yr`
                        : `${p.equity_percentage || '?'}% equity`;
                    return `
                        <tr class="border-b border-gray-700 hover:bg-gray-750">
                            <td class="py-2 pr-3">
                                <p class="font-medium">${p.investor_name}</p>
                                <p class="text-xs text-gray-400">${p.investor_email}</p>
                            </td>
                            <td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded-full text-white ${p.investment_type === 'DEBT' ? 'bg-blue-700' : 'bg-emerald-700'}">${p.investment_type}</span></td>
                            <td class="py-2 pr-3 font-medium">${formatNGN(p.investment_amount)}</td>
                            <td class="py-2 pr-3 text-sm text-gray-300">${display}</td>
                            <td class="py-2 pr-3 text-emerald-400">${formatNGN(p.total_distributed)}</td>
                            <td class="py-2 pr-3 text-xs text-gray-400">${p.expected_completion_date || 'TBD'}</td>
                            <td class="py-2">
                                <button onclick="editInvestor(${p.id}, ${p.total_distributed})" class="text-xs text-blue-400 hover:text-blue-300">Update Dist.</button>
                            </td>
                        </tr>
                    `;
                }).join('') || '<tr><td colspan="7" class="text-gray-400 py-4 text-center">No investor profiles yet</td></tr>';
            } catch (e) {
                document.getElementById('investorsTable').innerHTML = '<tr><td colspan="7" class="text-red-400 py-2">Error loading investors</td></tr>';
            }
        }

        async function editInvestor(profileId, currentDist) {
            const newDist = prompt('Enter total amount distributed so far (₦):', currentDist);
            if (newDist === null) return;
            try {
                await fetchData('/admin/api/investors/' + profileId, {
                    method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({total_distributed: parseFloat(newDist)})
                });
                loadInvestors();
            } catch (e) { alert('Error updating distribution'); }
        }

        document.getElementById('createInvestorForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('investorMessage');
            try {
                const res = await fetchData('/admin/api/investors', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        user_id: document.getElementById('invUserId').value,
                        investment_type: document.getElementById('invType').value,
                        investment_amount: document.getElementById('invAmount').value,
                        investment_date: document.getElementById('invDate').value,
                        roi_rate: document.getElementById('invRoi').value,
                        equity_percentage: document.getElementById('invEquity').value || null,
                        construction_start_date: document.getElementById('invConstructionStart').value,
                        expected_completion_date: document.getElementById('invCompletion').value,
                        notes: document.getElementById('invNotes').value,
                    })
                });
                msgEl.textContent = res.message;
                msgEl.className = 'text-sm text-green-400';
                document.getElementById('createInvestorForm').reset();
                loadInvestors();
            } catch (e) {
                msgEl.textContent = e.message || 'Error creating investor profile';
                msgEl.className = 'text-sm text-red-400';
            }
        });

        // ===== TENANTS =====
        async function loadTenants(statusFilter) {
            try {
                const url = statusFilter ? '/admin/api/tenants?status=' + statusFilter : '/admin/api/tenants';
                const tenants = await fetchData(url);
                const statusColors = {active:'bg-teal-800 text-teal-300', vacated:'bg-gray-700 text-gray-400'};
                document.getElementById('tenantsTable').innerHTML = tenants.length ? tenants.map(t => `
                    <tr class="border-b border-gray-700 hover:bg-gray-750">
                        <td class="py-2 pr-3 font-medium text-white">${t.name}</td>
                        <td class="py-2 pr-3 text-xs text-gray-400">${t.email || ''}${t.phone ? '<br>'+t.phone : ''}</td>
                        <td class="py-2 pr-3 text-xs text-gray-300">${t.property_name || '—'}${t.unit_number ? ' / '+t.unit_number : ''}</td>
                        <td class="py-2 pr-3 text-emerald-400 font-medium text-sm">${t.monthly_rent ? fmtNGN(t.monthly_rent) : '—'}</td>
                        <td class="py-2 pr-3 text-xs text-gray-400">${t.lease_start || ''}${t.lease_end ? ' → '+t.lease_end : ''}</td>
                        <td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded-full ${statusColors[t.status] || 'bg-gray-700 text-gray-400'}">${t.status}</span></td>
                        <td class="py-2">
                            <button onclick="vacateTenant(${t.id})" class="text-xs text-red-400 hover:text-red-300">${t.status === 'active' ? 'Mark Vacated' : 'Vacated'}</button>
                        </td>
                    </tr>`).join('') : '<tr><td colspan="7" class="text-gray-400 py-4 text-center">No tenants found</td></tr>';
            } catch (e) {
                document.getElementById('tenantsTable').innerHTML = '<tr><td colspan="7" class="text-red-400 py-2">Error loading tenants</td></tr>';
            }
        }

        async function vacateTenant(id) {
            if (!confirm('Mark this tenant as vacated?')) return;
            try {
                await fetchData('/admin/api/tenants/' + id, { method: 'DELETE' });
                loadTenants(document.getElementById('tnFilterStatus').value);
                loadStats();
            } catch (e) { alert('Error updating tenant'); }
        }

        document.getElementById('addTenantForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('tenantMsg');
            try {
                const res = await fetchData('/admin/api/tenants', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        name: document.getElementById('tnName').value,
                        email: document.getElementById('tnEmail').value,
                        phone: document.getElementById('tnPhone').value,
                        property_name: document.getElementById('tnProperty').value,
                        unit_number: document.getElementById('tnUnit').value,
                        monthly_rent: document.getElementById('tnRent').value,
                        lease_start: document.getElementById('tnLeaseStart').value,
                        lease_end: document.getElementById('tnLeaseEnd').value,
                        status: document.getElementById('tnStatus').value,
                        notes: document.getElementById('tnNotes').value,
                    })
                });
                msgEl.textContent = res.message;
                msgEl.className = 'text-sm text-green-400';
                document.getElementById('addTenantForm').reset();
                loadTenants();
                loadStats();
            } catch (err) {
                msgEl.textContent = err.message || 'Error adding tenant';
                msgEl.className = 'text-sm text-red-400';
            }
        });

        // ===== PAYMENTS =====
        async function loadTenantOptions() {
            try {
                const tenants = await fetchData('/admin/api/tenants?status=active');
                const sel = document.getElementById('pmtTenantId');
                if (!sel) return;
                sel.innerHTML = '<option value="">-- Select tenant --</option>' + tenants.map(t => `<option value="${t.id}">${t.name}${t.property_name ? ' ('+t.property_name+')' : ''}</option>`).join('');
            } catch (e) {}
        }

        async function loadPayments() {
            try {
                const payments = await fetchData('/admin/api/payments');
                document.getElementById('paymentsTable').innerHTML = payments.length ? payments.map(p => `
                    <tr class="border-b border-gray-700">
                        <td class="py-2 pr-3 text-white">${p.tenant_name || '—'}</td>
                        <td class="py-2 pr-3 text-emerald-400 font-medium">${fmtNGN(p.amount)}</td>
                        <td class="py-2 pr-3 text-xs text-gray-400">${p.payment_date}</td>
                        <td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded bg-gray-700 text-gray-300">${p.payment_type}</span></td>
                        <td class="py-2 pr-3 text-xs text-gray-400">${p.description || '—'}</td>
                        <td class="py-2 text-xs text-gray-500">${p.recorded_by || '—'}</td>
                    </tr>`).join('') : '<tr><td colspan="6" class="text-gray-400 py-4 text-center">No payments recorded</td></tr>';
            } catch (e) {
                document.getElementById('paymentsTable').innerHTML = '<tr><td colspan="6" class="text-red-400 py-2">Error loading payments</td></tr>';
            }
        }

        document.getElementById('addPaymentForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('paymentMsg');
            try {
                const tenantSelect = document.getElementById('pmtTenantId');
                const res = await fetchData('/admin/api/payments', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        tenant_id: tenantSelect.value || null,
                        tenant_name: document.getElementById('pmtTenantName').value,
                        amount: document.getElementById('pmtAmount').value,
                        payment_date: document.getElementById('pmtDate').value,
                        payment_type: document.getElementById('pmtType').value,
                        description: document.getElementById('pmtDesc').value,
                    })
                });
                msgEl.textContent = res.message;
                msgEl.className = 'text-sm text-green-400';
                document.getElementById('addPaymentForm').reset();
                loadPayments();
                loadStats();
            } catch (err) {
                msgEl.textContent = err.message || 'Error recording payment';
                msgEl.className = 'text-sm text-red-400';
            }
        });

        // Initialize dashboard - show overview by default
        document.addEventListener('DOMContentLoaded', () => {
            showSection('overviewSection');
            loadStats();
            loadProperties();
            loadInquiries();
            loadMessages();
            loadSiteContent();
            loadTeamMembers();
        });

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js?v=3').catch(() => {});
        }
    </script>
</body>
</html>
"""

ROLE_DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ user_role }} Portal - BrightWave Habitat Enterprise</title>
    <meta name="csrf-token" content="{{ csrf_token }}">
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#475569">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="BrightWave">
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .contract-scroll::-webkit-scrollbar { width: 6px; }
        .contract-scroll::-webkit-scrollbar-track { background: #374151; }
        .contract-scroll::-webkit-scrollbar-thumb { background: #6B7280; border-radius: 3px; }
        .timeline-bar { transition: width 0.8s ease; }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <script>
        const USER_ROLE = '{{ user_role }}';
        const ALL_ROLES = {{ all_roles_json }};
        const USER_NAME = '{{ user_name }}';
        const NEEDS_CONTRACT = {{ 'true' if needs_contract_signing else 'false' }};
        const AWAITING_CEO = {{ 'true' if awaiting_ceo_signature else 'false' }};
        const SHOW_AGREEMENT_POPUP = {{ 'true' if show_agreement_popup else 'false' }};
        const CONTRACT_ID = {{ contract_id or 'null' }};
        const adminCsrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
        let activeRole = USER_ROLE;
    </script>

    <!-- CONTRACT SIGNING OVERLAY -->
    {% if needs_contract_signing %}
    <div id="contractOverlay" class="fixed inset-0 bg-black bg-opacity-90 z-50 flex items-center justify-center p-4">
        <div class="bg-gray-800 rounded-2xl shadow-2xl max-w-2xl w-full max-h-screen flex flex-col">
            <div class="p-6 border-b border-gray-700 flex-shrink-0">
                <div class="flex items-center gap-3 mb-1">
                    <div class="w-10 h-10 bg-slate-600 rounded-full flex items-center justify-center text-lg font-bold">BW</div>
                    <div>
                        <p class="text-xs text-gray-400 uppercase tracking-wide">BrightWave Habitat Enterprise</p>
                        <h2 class="text-xl font-bold text-white">{{ contract_title }}</h2>
                    </div>
                </div>
                <p class="text-sm text-yellow-400 mt-2">Please read this agreement carefully before proceeding to your dashboard.</p>
            </div>
            <div id="contractText" class="contract-scroll p-6 overflow-y-auto flex-1 text-sm text-gray-300 leading-relaxed whitespace-pre-line" style="max-height: calc(60vh - 120px); min-height: 180px;">{{ contract_body }}</div>
            <div id="scrollPrompt" class="text-center text-xs text-gray-500 py-2 flex-shrink-0">Scroll to the bottom to continue</div>
            <div id="signatureSection" class="p-6 border-t border-gray-700 flex-shrink-0 hidden">
                <div class="flex items-start gap-2 mb-4">
                    <input type="checkbox" id="agreeCheck" class="mt-1 w-4 h-4 accent-emerald-500">
                    <label for="agreeCheck" class="text-sm text-gray-300 cursor-pointer">I have read and fully understood this agreement. I agree to all terms and conditions.</label>
                </div>
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2 text-gray-300">Your Full Name (Digital Signature)</label>
                    <input type="text" id="userSignature" placeholder="Type your full legal name" class="w-full px-4 py-3 bg-gray-700 border border-gray-600 rounded-lg text-white placeholder-gray-500 font-medium">
                </div>
                <button id="signBtn" onclick="submitSignature()" class="w-full bg-emerald-700 hover:bg-emerald-600 text-white font-semibold py-3 px-6 rounded-lg transition-colors disabled:opacity-50">
                    Sign Agreement &amp; Continue to Dashboard
                </button>
                <p id="signError" class="text-red-400 text-sm mt-2 hidden"></p>
            </div>
        </div>
    </div>
    {% endif %}

    <!-- AGREEMENT COMPLETE POPUP -->
    {% if show_agreement_popup %}
    <div id="agreementPopup" class="fixed inset-0 bg-black bg-opacity-80 z-50 flex items-center justify-center p-4">
        <div class="bg-gray-800 rounded-2xl shadow-2xl max-w-md w-full p-8 text-center border border-emerald-600">
            <div class="w-20 h-20 bg-emerald-700 rounded-full flex items-center justify-center mx-auto mb-4">
                <svg class="w-10 h-10 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
            </div>
            <h2 class="text-2xl font-bold text-white mb-2">Agreement Complete</h2>
            <p class="text-gray-300 mb-1">Two-way agreement signed</p>
            <div class="bg-gray-700 rounded-lg p-4 my-4 text-left text-sm">
                <p class="text-gray-400 mb-1">Between:</p>
                <p class="text-white font-medium">BrightWave Habitat Enterprise (CEO)</p>
                <p class="text-gray-400 text-xs my-1">and</p>
                <p class="text-white font-medium">{{ user_name }} ({{ user_role }})</p>
            </div>
            <p class="text-gray-400 text-xs mb-6">Your signed agreement is now on record. Welcome to BrightWave Habitat Enterprise.</p>
            <button onclick="document.getElementById('agreementPopup').remove()" class="bg-emerald-700 hover:bg-emerald-600 text-white font-semibold py-2 px-8 rounded-lg">Continue to Dashboard</button>
        </div>
    </div>
    {% endif %}

    <header class="bg-gray-800 shadow border-b border-gray-700">
        <div class="max-w-7xl mx-auto py-4 px-4 sm:px-6 lg:px-8 flex justify-between items-center flex-wrap gap-3">
            <div>
                <p class="text-xs text-gray-500 uppercase tracking-widest">BrightWave Habitat Enterprise</p>
                <h1 id="portalTitle" class="text-xl font-bold text-slate-300">
                    {% if user_role == 'MANAGER' %}Property Manager Portal
                    {% elif user_role == 'ACCOUNTANT' %}Finance Portal
                    {% elif user_role == 'REALTOR' %}Realtor Portal
                    {% elif user_role == 'INVESTOR' %}Investor Portal
                    {% else %}{{ user_role }} Portal{% endif %}
                </h1>
            </div>
            <div class="flex items-center gap-3 flex-wrap">
                {% if all_roles | length > 1 %}
                <div id="roleSwitcher" class="flex gap-1 bg-gray-700 p-1 rounded-lg">
                    {% for r in all_roles %}
                    <button onclick="switchRole('{{ r }}')" id="roleBtn_{{ r }}"
                        class="role-switch-btn text-xs font-medium px-3 py-1.5 rounded-md transition-colors {% if r == user_role %}bg-slate-600 text-white{% else %}text-gray-400 hover:text-white{% endif %}">
                        {{ r }}
                    </button>
                    {% endfor %}
                </div>
                {% endif %}
                <div class="text-right hidden sm:block">
                    <p class="text-sm font-medium text-white">{{ user_name }}</p>
                    <p id="activeRoleLabel" class="text-xs text-gray-400">{{ user_role }}</p>
                </div>
                {% if awaiting_ceo_signature %}
                <span class="bg-yellow-600 text-white text-xs px-3 py-1 rounded-full">Awaiting CEO Signature</span>
                {% endif %}
                <a href="/admin/logout" class="text-gray-400 hover:text-white text-sm border border-gray-600 rounded-lg px-3 py-1.5">Logout</a>
            </div>
        </div>
    </header>

    {% if awaiting_ceo_signature %}
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 mt-4">
        <div class="bg-yellow-900 border border-yellow-600 rounded-lg p-4 flex items-center gap-3">
            <svg class="w-5 h-5 text-yellow-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
            <div>
                <p class="text-yellow-300 font-medium text-sm">Your agreement has been submitted and is pending CEO co-signature.</p>
                <p class="text-yellow-400 text-xs mt-0.5">You will be notified once the CEO signs. Limited dashboard access is available in the meantime.</p>
            </div>
        </div>
    </div>
    {% endif %}

    <main class="max-w-7xl mx-auto py-6 px-4 sm:px-6 lg:px-8">

        {% for r in all_roles %}
        <div id="roleSection_{{ r }}" class="{% if r != user_role %}hidden{% endif %}">

            {% if r == 'INVESTOR' %}
            <!-- INVESTOR DASHBOARD -->
            <div id="investorLoading" class="text-center py-12 text-gray-400">Loading your investment data...</div>
            <div id="investorDashboard" class="hidden space-y-6">
                <div id="investmentSummaryCard" class="bg-gradient-to-br from-slate-800 to-gray-900 border border-slate-600 rounded-2xl p-6"></div>
                <div class="bg-gray-800 rounded-xl p-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Project Timeline</h3>
                    <div id="projectTimeline"></div>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <div class="bg-gray-800 rounded-xl p-5"><p class="text-xs text-gray-400 uppercase tracking-wide mb-1">Amount Invested</p><p id="invAmountDisplay" class="text-2xl font-bold text-white">-</p></div>
                    <div class="bg-gray-800 rounded-xl p-5"><p class="text-xs text-gray-400 uppercase tracking-wide mb-1">Expected Total Return</p><p id="invExpectedReturn" class="text-2xl font-bold text-emerald-400">-</p><p id="invReturnNote" class="text-xs text-gray-500 mt-1"></p></div>
                    <div class="bg-gray-800 rounded-xl p-5"><p class="text-xs text-gray-400 uppercase tracking-wide mb-1">Distributed So Far</p><p id="invDistributed" class="text-2xl font-bold text-blue-400">-</p></div>
                </div>
                <div class="bg-gray-800 rounded-xl p-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Distribution Schedule</h3>
                    <div id="distributionSchedule"></div>
                </div>
                <div class="bg-gray-800 rounded-xl p-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Your Documents</h3>
                    <div class="flex items-center gap-3 p-4 bg-gray-700 rounded-lg">
                        <svg class="w-8 h-8 text-emerald-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                        <div><p class="font-medium text-white">Investment Agreement</p><p id="docStatus" class="text-xs text-gray-400 mt-0.5">Loading...</p></div>
                    </div>
                </div>
            </div>
            <div id="investorNoProfile" class="hidden bg-gray-800 rounded-xl p-8 text-center">
                <p class="text-gray-400 text-lg font-medium">Investment profile not set up yet</p>
                <p class="text-gray-500 text-sm mt-2">Your CEO will configure your investment details. Please check back shortly.</p>
            </div>

            {% elif r == 'MANAGER' %}
            <!-- MANAGER DASHBOARD -->
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <div class="bg-gray-800 rounded-xl p-5"><p class="text-xs text-gray-400 uppercase tracking-wide mb-1">Properties</p><p id="mgr_properties" class="text-3xl font-bold">-</p></div>
                <div class="bg-blue-900 rounded-xl p-5"><p class="text-xs text-blue-300 uppercase tracking-wide mb-1">Open Inquiries</p><p id="mgr_inquiries" class="text-3xl font-bold">-</p></div>
                <div class="bg-purple-900 rounded-xl p-5"><p class="text-xs text-purple-300 uppercase tracking-wide mb-1">Team Members</p><p id="mgr_team" class="text-3xl font-bold">-</p></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-6 mb-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Recent Inquiries</h3>
                <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Name</th><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Status</th><th class="py-2 text-left text-gray-400">Date</th></tr></thead><tbody id="mgr_inquiriesTable"></tbody></table></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Properties Overview</h3>
                <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Location</th><th class="py-2 text-left text-gray-400">Status</th></tr></thead><tbody id="mgr_propertiesTable"></tbody></table></div>
            </div>

            {% elif r == 'ACCOUNTANT' %}
            <!-- ACCOUNTANT DASHBOARD -->
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <div class="bg-gray-800 rounded-xl p-5"><p class="text-xs text-gray-400 uppercase tracking-wide mb-1">Total Inquiries</p><p id="acc_inquiries" class="text-3xl font-bold">-</p></div>
                <div class="bg-emerald-900 rounded-xl p-5"><p class="text-xs text-emerald-300 uppercase tracking-wide mb-1">New Inquiries</p><p id="acc_new_inquiries" class="text-3xl font-bold">-</p></div>
                <div class="bg-blue-900 rounded-xl p-5"><p class="text-xs text-blue-300 uppercase tracking-wide mb-1">Contact Messages</p><p id="acc_messages" class="text-3xl font-bold">-</p></div>
            </div>
            <div class="bg-yellow-900 border border-yellow-700 rounded-xl p-5 mb-6">
                <p class="text-yellow-300 font-medium">Financial modules are being set up</p>
                <p class="text-yellow-400 text-sm mt-1">Full payment tracking, rent collection, and investor distribution modules are coming soon.</p>
            </div>
            <div class="bg-gray-800 rounded-xl p-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Inquiry Overview</h3>
                <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Name</th><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Status</th></tr></thead><tbody id="acc_inquiriesTable"></tbody></table></div>
            </div>

            {% elif r == 'REALTOR' %}
            <!-- REALTOR DASHBOARD -->
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <div class="bg-gray-800 rounded-xl p-5"><p class="text-xs text-gray-400 uppercase tracking-wide mb-1">Active Properties</p><p id="rel_properties" class="text-3xl font-bold">-</p></div>
                <div class="bg-amber-900 rounded-xl p-5"><p class="text-xs text-amber-300 uppercase tracking-wide mb-1">Open Leads</p><p id="rel_inquiries" class="text-3xl font-bold">-</p></div>
                <div class="bg-green-900 rounded-xl p-5"><p class="text-xs text-green-300 uppercase tracking-wide mb-1">New Today</p><p id="rel_new" class="text-3xl font-bold">-</p></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-6 mb-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Available Properties</h3>
                <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Location</th><th class="py-2 text-left text-gray-400">Price</th><th class="py-2 text-left text-gray-400">Status</th></tr></thead><tbody id="rel_propertiesTable"></tbody></table></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Leads / Inquiries</h3>
                <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Name</th><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Status</th><th class="py-2 text-left text-gray-400">Date</th></tr></thead><tbody id="rel_inquiriesTable"></tbody></table></div>
            </div>
            {% endif %}

        </div>
        {% endfor %}

    </main>

    <script>
        async function fetchData(url, options = {}) {
            const headers = new Headers(options.headers || {});
            if (adminCsrfToken && ['POST','PUT','DELETE'].includes((options.method || 'GET').toUpperCase())) {
                headers.set('X-CSRF-Token', adminCsrfToken);
            }
            const response = await fetch(url, { credentials: 'include', headers, ...options });
            if (!response.ok) { const e = await response.json().catch(() => ({})); throw new Error(e.message || 'Request failed'); }
            return response.json();
        }

        function formatNGN(v) { return '₦' + Number(v).toLocaleString('en-NG'); }

        const ROLE_TITLES = {INVESTOR:'Investor Portal',MANAGER:'Property Manager Portal',ACCOUNTANT:'Finance Portal',REALTOR:'Realtor Portal'};

        function switchRole(role) {
            activeRole = role;
            ALL_ROLES.forEach(r => {
                const sec = document.getElementById('roleSection_' + r);
                if (sec) sec.classList.toggle('hidden', r !== role);
                const btn = document.getElementById('roleBtn_' + r);
                if (btn) {
                    btn.classList.toggle('bg-slate-600', r === role);
                    btn.classList.toggle('text-white', r === role);
                    btn.classList.toggle('text-gray-400', r !== role);
                }
            });
            const labelEl = document.getElementById('activeRoleLabel');
            if (labelEl) labelEl.textContent = role;
            const titleEl = document.getElementById('portalTitle');
            if (titleEl) titleEl.textContent = ROLE_TITLES[role] || role + ' Portal';
            loadRoleDashboard(role);
        }

        function loadRoleDashboard(role) {
            if (role === 'INVESTOR') loadInvestorDashboard();
            else if (role === 'MANAGER') loadManagerDashboard();
            else if (role === 'ACCOUNTANT') loadAccountantDashboard();
            else if (role === 'REALTOR') loadRealtorDashboard();
        }

        // ===== CONTRACT SIGNING =====
        if (NEEDS_CONTRACT) {
            const contractText = document.getElementById('contractText');
            const scrollPrompt = document.getElementById('scrollPrompt');
            const signatureSection = document.getElementById('signatureSection');
            function checkContractScrollEnd() {
                if (!contractText) return;
                if (contractText.scrollTop + contractText.clientHeight >= contractText.scrollHeight - 40) {
                    scrollPrompt.classList.add('hidden');
                    signatureSection.classList.remove('hidden');
                }
            }
            if (contractText) {
                contractText.addEventListener('scroll', checkContractScrollEnd);
                // Check immediately on load — short contracts or large screens may not need scrolling
                setTimeout(checkContractScrollEnd, 200);
            }
        }

        async function submitSignature() {
            const sig = document.getElementById('userSignature').value.trim();
            const agreed = document.getElementById('agreeCheck').checked;
            const errEl = document.getElementById('signError');
            if (!agreed) { errEl.textContent = 'Please check the agreement box to confirm.'; errEl.classList.remove('hidden'); return; }
            if (!sig || sig.length < 2) { errEl.textContent = 'Please type your full name as your signature.'; errEl.classList.remove('hidden'); return; }
            try {
                document.getElementById('signBtn').disabled = true;
                document.getElementById('signBtn').textContent = 'Submitting...';
                await fetchData('/admin/api/my-contract/sign', {
                    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({signature: sig})
                });
                document.getElementById('contractOverlay').innerHTML = `
                    <div class="bg-gray-800 rounded-2xl shadow-2xl max-w-md w-full p-8 text-center">
                        <div class="w-16 h-16 bg-yellow-600 rounded-full flex items-center justify-center mx-auto mb-4">
                            <svg class="w-8 h-8 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                        </div>
                        <h2 class="text-xl font-bold text-white mb-2">Agreement Submitted</h2>
                        <p class="text-gray-300 mb-4">Your agreement has been signed and submitted. It is now awaiting co-signature from the CEO of BrightWave Habitat Enterprise.</p>
                        <p class="text-gray-400 text-sm">You will be notified once the CEO signs. Your dashboard will be fully available upon completion.</p>
                        <button onclick="document.getElementById('contractOverlay').remove()" class="mt-6 bg-slate-600 hover:bg-slate-700 text-white font-medium py-2 px-8 rounded-lg">Continue to Dashboard</button>
                    </div>`;
            } catch (e) {
                document.getElementById('signBtn').disabled = false;
                document.getElementById('signBtn').textContent = 'Sign Agreement & Continue to Dashboard';
                errEl.textContent = e.message || 'Error submitting signature. Please try again.';
                errEl.classList.remove('hidden');
            }
        }

        // ===== INVESTOR DASHBOARD =====
        async function loadInvestorDashboard() {
            try {
                const profile = await fetchData('/admin/api/my-investment');
                document.getElementById('investorLoading').classList.add('hidden');
                if (!profile) {
                    document.getElementById('investorNoProfile').classList.remove('hidden');
                    return;
                }
                document.getElementById('investorDashboard').classList.remove('hidden');

                const amount = profile.investment_amount;
                const type = profile.investment_type;
                const roi = profile.roi_rate;
                const equity = profile.equity_percentage;
                const distributed = profile.total_distributed || 0;

                document.getElementById('invAmountDisplay').textContent = formatNGN(amount);
                document.getElementById('invDistributed').textContent = formatNGN(distributed);

                // Summary card
                const badgeColor = type === 'DEBT' ? 'bg-blue-700' : 'bg-emerald-700';
                const summaryLabel = type === 'DEBT' ? `${roi}% Annual Return` : `${equity}% Project Equity`;
                document.getElementById('investmentSummaryCard').innerHTML = `
                    <div class="flex justify-between items-start mb-4">
                        <div>
                            <p class="text-xs text-gray-400 uppercase tracking-widest mb-1">Your Investment</p>
                            <p class="text-4xl font-bold text-white">${formatNGN(amount)}</p>
                        </div>
                        <span class="text-sm font-semibold px-3 py-1 rounded-full text-white ${badgeColor}">${type}</span>
                    </div>
                    <div class="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
                        <div><p class="text-gray-400 text-xs">Return Structure</p><p class="text-white font-medium mt-0.5">${summaryLabel}</p></div>
                        <div><p class="text-gray-400 text-xs">Investment Date</p><p class="text-white font-medium mt-0.5">${profile.investment_date || 'Pending'}</p></div>
                        <div><p class="text-gray-400 text-xs">Expected Completion</p><p class="text-white font-medium mt-0.5">${profile.expected_completion_date || 'TBD'}</p></div>
                    </div>`;

                // Expected return
                if (type === 'DEBT') {
                    const annualReturn = amount * roi / 100;
                    const totalInterest = annualReturn * 4;
                    const grandTotal = amount + totalInterest;
                    document.getElementById('invExpectedReturn').textContent = formatNGN(grandTotal);
                    document.getElementById('invReturnNote').textContent = `Principal ${formatNGN(amount)} + ${formatNGN(totalInterest)} interest over 4 years`;
                } else {
                    document.getElementById('invExpectedReturn').textContent = `${equity}% ownership`;
                    document.getElementById('invReturnNote').textContent = 'Returns proportional to project revenue';
                }

                // Timeline
                const now = new Date();
                const completion = profile.expected_completion_date ? new Date(profile.expected_completion_date) : null;
                const start = profile.construction_start_date ? new Date(profile.construction_start_date) : null;
                let progress = 0;
                if (start && completion) {
                    const total = completion - start;
                    const elapsed = Math.min(now - start, total);
                    progress = Math.max(0, Math.min(100, Math.round((elapsed / total) * 100)));
                }
                const isComplete = completion && now >= completion;
                document.getElementById('projectTimeline').innerHTML = `
                    <div class="mb-3">
                        <div class="flex justify-between text-xs text-gray-400 mb-1">
                            <span>${isComplete ? 'Construction Complete' : 'Construction in Progress'}</span>
                            <span>${progress}%</span>
                        </div>
                        <div class="w-full bg-gray-700 rounded-full h-3">
                            <div class="timeline-bar ${isComplete ? 'bg-emerald-500' : 'bg-blue-500'} h-3 rounded-full" style="width: ${progress}%"></div>
                        </div>
                    </div>
                    <div class="grid grid-cols-4 gap-2 text-xs text-center mt-3">
                        <div class="flex flex-col items-center gap-1"><div class="w-6 h-6 rounded-full bg-emerald-600 flex items-center justify-center text-white text-xs">1</div><span class="text-gray-400">Land</span><span class="text-emerald-400">Done</span></div>
                        <div class="flex flex-col items-center gap-1"><div class="w-6 h-6 rounded-full ${start ? 'bg-emerald-600' : 'bg-gray-600'} flex items-center justify-center text-white text-xs">2</div><span class="text-gray-400">Start</span><span class="${start ? 'text-emerald-400' : 'text-gray-500'}">${start ? 'Done' : 'Pending'}</span></div>
                        <div class="flex flex-col items-center gap-1"><div class="w-6 h-6 rounded-full ${isComplete ? 'bg-emerald-600' : 'bg-blue-600'} flex items-center justify-center text-white text-xs">3</div><span class="text-gray-400">Build</span><span class="${isComplete ? 'text-emerald-400' : 'text-blue-400'}">${isComplete ? 'Done' : 'Active'}</span></div>
                        <div class="flex flex-col items-center gap-1"><div class="w-6 h-6 rounded-full ${isComplete ? 'bg-emerald-600' : 'bg-gray-600'} flex items-center justify-center text-white text-xs">4</div><span class="text-gray-400">Returns</span><span class="${isComplete ? 'text-emerald-400' : 'text-gray-500'}">${isComplete ? 'Active' : 'Pending'}</span></div>
                    </div>
                    <div class="mt-3 text-xs text-gray-500 text-center">
                        ${!isComplete ? 'No distributions during construction phase. Returns begin upon project completion.' : 'Project complete. Annual distributions are active.'}
                    </div>`;

                // Distribution schedule (for DEBT: show 4 annual payments)
                if (type === 'DEBT' && completion) {
                    const annualReturn = amount * roi / 100;
                    const scheduleRows = [1, 2, 3, 4].map(yr => {
                        const paymentDate = new Date(completion);
                        paymentDate.setFullYear(paymentDate.getFullYear() + yr);
                        const isPast = now > paymentDate;
                        const statusClass = isPast ? 'text-emerald-400' : 'text-yellow-400';
                        const status = isPast ? 'Due' : 'Scheduled';
                        const finalPayment = yr === 4 ? ` + ${formatNGN(amount)} principal` : '';
                        return `<div class="flex justify-between items-center py-3 border-b border-gray-700 last:border-0 text-sm">
                            <div><span class="text-white">Year ${yr} Distribution</span>${finalPayment ? `<span class="text-xs text-emerald-400 ml-2">${finalPayment}</span>` : ''}</div>
                            <div class="text-right"><p class="font-medium text-white">${formatNGN(yr === 4 ? annualReturn + amount : annualReturn)}</p><p class="text-xs text-gray-400">${paymentDate.toLocaleDateString('en-GB', {month:'short',year:'numeric'})}</p></div>
                            <span class="text-xs ${statusClass} font-medium">${status}</span>
                        </div>`;
                    });
                    document.getElementById('distributionSchedule').innerHTML = scheduleRows.join('');
                } else if (type === 'EQUITY') {
                    document.getElementById('distributionSchedule').innerHTML = `<p class="text-gray-400 text-sm">Equity distributions are paid annually from project revenues, proportional to your ${equity}% ownership stake. Exact amounts depend on project performance. Distributions begin after project completion.</p>`;
                } else {
                    document.getElementById('distributionSchedule').innerHTML = `<p class="text-gray-400 text-sm">Distribution schedule will be available once the expected completion date is set by the CEO.</p>`;
                }

                // Contract status
                document.getElementById('docStatus').textContent = CONTRACT_ID ? 'Both parties signed \u2014 Agreement on file' : 'Pending signatures';

            } catch (e) {
                document.getElementById('investorLoading').textContent = 'Error loading investment data. Please refresh.';
            }
        }

        // ===== MANAGER DASHBOARD =====
        async function loadManagerDashboard() {
            try {
                const stats = await fetchData('/admin/api/stats');
                document.getElementById('mgr_properties').textContent = stats.active_properties;
                document.getElementById('mgr_inquiries').textContent = stats.total_inquiries;
                document.getElementById('mgr_team').textContent = stats.active_team_members;
                const inquiries = await fetchData('/admin/api/inquiries');
                document.getElementById('mgr_inquiriesTable').innerHTML = inquiries.slice(0, 10).map(i => `
                    <tr class="border-b border-gray-700"><td class="py-2 pr-3">${i.full_name}</td><td class="py-2 pr-3 text-gray-400 text-xs">${i.property_title}</td><td class="py-2 pr-3 text-xs">${i.inquiry_type}</td><td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${i.status}</span></td><td class="py-2 text-xs text-gray-500">${new Date(i.created_at).toLocaleDateString()}</td></tr>
                `).join('') || '<tr><td colspan="5" class="text-gray-400 py-3 text-center">No inquiries</td></tr>';
                const props = await fetchData('/admin/api/properties');
                document.getElementById('mgr_propertiesTable').innerHTML = props.map(p => `
                    <tr class="border-b border-gray-700"><td class="py-2 pr-3 font-medium">${p.title}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.property_type}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.location}</td><td class="py-2"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${p.construction_status || p.status}</span></td></tr>
                `).join('');
            } catch (e) {}
        }

        // ===== ACCOUNTANT DASHBOARD =====
        async function loadAccountantDashboard() {
            try {
                const stats = await fetchData('/admin/api/stats');
                document.getElementById('acc_inquiries').textContent = stats.total_inquiries;
                document.getElementById('acc_new_inquiries').textContent = stats.new_inquiries;
                document.getElementById('acc_messages').textContent = stats.contact_messages;
                const inquiries = await fetchData('/admin/api/inquiries');
                document.getElementById('acc_inquiriesTable').innerHTML = inquiries.slice(0, 10).map(i => `
                    <tr class="border-b border-gray-700"><td class="py-2 pr-3">${i.full_name}</td><td class="py-2 pr-3 text-gray-400 text-xs">${i.property_title}</td><td class="py-2 pr-3 text-xs">${i.inquiry_type}</td><td class="py-2"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${i.status}</span></td></tr>
                `).join('') || '<tr><td colspan="4" class="text-gray-400 py-3 text-center">No data</td></tr>';
            } catch (e) {}
        }

        // ===== REALTOR DASHBOARD =====
        async function loadRealtorDashboard() {
            try {
                const stats = await fetchData('/admin/api/stats');
                document.getElementById('rel_properties').textContent = stats.active_properties;
                document.getElementById('rel_inquiries').textContent = stats.total_inquiries;
                document.getElementById('rel_new').textContent = stats.new_inquiries;
                const props = await fetchData('/admin/api/properties');
                document.getElementById('rel_propertiesTable').innerHTML = props.map(p => `
                    <tr class="border-b border-gray-700"><td class="py-2 pr-3 font-medium">${p.title}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.property_type}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.location}</td><td class="py-2 pr-3 text-xs">${p.price ? '₦'+Number(p.price).toLocaleString() : p.price_type}</td><td class="py-2"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${p.construction_status || p.status}</span></td></tr>
                `).join('');
                const inquiries = await fetchData('/admin/api/inquiries');
                document.getElementById('rel_inquiriesTable').innerHTML = inquiries.slice(0, 10).map(i => `
                    <tr class="border-b border-gray-700"><td class="py-2 pr-3">${i.full_name}</td><td class="py-2 pr-3 text-gray-400 text-xs">${i.property_title}</td><td class="py-2 pr-3 text-xs">${i.inquiry_type}</td><td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${i.status}</span></td><td class="py-2 text-xs text-gray-500">${new Date(i.created_at).toLocaleDateString()}</td></tr>
                `).join('') || '<tr><td colspan="5" class="text-gray-400 py-3 text-center">No leads yet</td></tr>';
            } catch (e) {}
        }

        document.addEventListener('DOMContentLoaded', () => {
            loadRoleDashboard(USER_ROLE);
        });

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js?v=3').catch(() => {});
        }
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    with app.app_context():
        ensure_runtime_state()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
