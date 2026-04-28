from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect, url_for, make_response
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
from datetime import datetime, date as date_type, timedelta
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
    investment_term_years = db.Column(db.Integer, nullable=True)  # e.g. 3, 5, 10
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship('Admin', backref='investor_profile_rel', foreign_keys=[user_id])

class PropertyUnit(db.Model):
    __tablename__ = 'property_unit'
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('property.id'), nullable=False)
    unit_code = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), default='available')  # available, reserved, occupied, maintenance
    monthly_rent = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    property = db.relationship('Property', backref='units')

    __table_args__ = (
        db.UniqueConstraint('property_id', 'unit_code', name='uq_property_unit_code'),
    )

class ConstructionUpdate(db.Model):
    __tablename__ = 'construction_update'
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('property.id'), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    milestone_key = db.Column(db.String(50), nullable=True)
    progress_percentage = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text, nullable=True)
    happened_on = db.Column(db.Date, nullable=True)
    is_public = db.Column(db.Boolean, default=True)
    updated_by = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    property = db.relationship('Property', backref='construction_updates')

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


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_token'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    user = db.relationship('Admin', backref='reset_tokens')

class ContractTemplate(db.Model):
    __tablename__ = 'contract_template'
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(30), unique=True, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.String(80), nullable=True)

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


def serialize_property_unit(unit):
    return {
        'id': unit.id,
        'property_id': unit.property_id,
        'property_title': unit.property.title if unit.property else '',
        'unit_code': unit.unit_code,
        'status': unit.status,
        'monthly_rent': unit.monthly_rent,
        'notes': unit.notes or '',
        'sort_order': unit.sort_order,
        'created_at': unit.created_at.isoformat() if unit.created_at else None,
        'updated_at': unit.updated_at.isoformat() if unit.updated_at else None,
    }


def serialize_construction_update(update):
    return {
        'id': update.id,
        'property_id': update.property_id,
        'property_title': update.property.title if update.property else '',
        'title': update.title,
        'milestone_key': update.milestone_key,
        'progress_percentage': update.progress_percentage,
        'notes': update.notes or '',
        'happened_on': update.happened_on.isoformat() if update.happened_on else None,
        'is_public': update.is_public,
        'updated_by': update.updated_by,
        'created_at': update.created_at.isoformat() if update.created_at else None,
        'updated_at': update.updated_at.isoformat() if update.updated_at else None,
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


def seed_default_units():
    phase1 = Property.query.filter_by(title='BrightWave Phase 1 Hostel').first()
    if not phase1:
        return

    if PropertyUnit.query.filter_by(property_id=phase1.id).first():
        return

    default_codes = ['1A', '2A', '3A', '4A', '5A', '1B', '2B', '3B', '4B', '5B']
    for idx, code in enumerate(default_codes):
        db.session.add(PropertyUnit(
            property_id=phase1.id,
            unit_code=code,
            status='available',
            sort_order=idx,
        ))
    db.session.commit()


def sync_property_units_from_tenants():
    units = PropertyUnit.query.order_by(PropertyUnit.property_id.asc(), PropertyUnit.sort_order.asc()).all()
    if not units:
        return

    active_statuses = {'active', 'reserved'}
    active_tenants = Tenant.query.filter(Tenant.status.in_(active_statuses)).all()
    occupancy_map = {}
    for tenant in active_tenants:
        key = (
            (tenant.property_name or '').strip().lower(),
            (tenant.unit_number or '').strip().upper(),
        )
        if key[0] and key[1]:
            occupancy_map[key] = tenant

    changed = False
    for unit in units:
        prop_name = (unit.property.title or '').strip().lower() if unit.property else ''
        unit_key = (prop_name, (unit.unit_code or '').strip().upper())
        occupied = unit_key in occupancy_map
        desired_status = 'occupied' if occupied else ('available' if unit.status != 'maintenance' else 'maintenance')
        if unit.status != desired_status:
            unit.status = desired_status
            changed = True

    properties = Property.query.all()
    for prop in properties:
        prop_units = [u for u in units if u.property_id == prop.id]
        if prop_units:
            available_count = sum(1 for u in prop_units if u.status == 'available')
            if prop.total_rooms != len(prop_units):
                prop.total_rooms = len(prop_units)
                changed = True
            if prop.available_rooms != available_count:
                prop.available_rooms = available_count
                changed = True

    if changed:
        db.session.commit()


def seed_default_construction_updates():
    existing = ConstructionUpdate.query.first()
    if existing:
        return

    candidate = Property.query.filter(
        Property.property_type == 'hostel',
        Property.construction_status != 'completed'
    ).order_by(Property.created_at.asc()).first()
    if not candidate:
        return

    defaults = [
        ('Land secured', 'land-secured', 0, 'Project land has been secured and documented.'),
        ('Construction begins', 'construction-begins', 15, 'Mobilisation and site setup are underway.'),
        ('Foundation complete', 'foundation-complete', 35, 'Foundation works are complete.'),
        ('Structure complete', 'structure-complete', 60, 'Core structural works are complete.'),
        ('Finishing stage', 'finishing-stage', 85, 'Internal finishing and utilities are being completed.'),
        ('Project complete', 'project-complete', 100, 'Construction is complete and ready for operations.'),
    ]
    for idx, (title, key, pct, notes) in enumerate(defaults):
        db.session.add(ConstructionUpdate(
            property_id=candidate.id,
            title=title,
            milestone_key=key,
            progress_percentage=pct,
            notes=notes,
            happened_on=None,
            is_public=True,
            updated_by='system',
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
    db.session.commit()


def get_investor_project_property():
    latest_public = ConstructionUpdate.query.filter_by(is_public=True).order_by(
        ConstructionUpdate.progress_percentage.desc(),
        ConstructionUpdate.updated_at.desc()
    ).first()
    if latest_public and latest_public.property:
        return latest_public.property

    return Property.query.filter(
        Property.property_type == 'hostel',
        Property.construction_status != 'completed'
    ).order_by(Property.created_at.asc()).first()


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


def seed_contract_templates():
    for role, data in CONTRACT_TEXTS.items():
        if not ContractTemplate.query.filter_by(role=role).first():
            ct = ContractTemplate(role=role, title=data['title'], body=data['body'])
            db.session.add(ct)
    db.session.commit()

def initialize_app_state(include_sample_data=False, bootstrap_admin=False):
    """Run one-time database initialization outside the web worker startup path."""
    db.create_all()
    ensure_cms_baseline()
    seed_contract_templates()
    if include_sample_data:
        init_sample_data()
    seed_default_units()
    seed_default_construction_updates()
    reconcile_property_catalog()
    sync_property_units_from_tenants()
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

def get_admin_roles(admin):
    if not admin:
        return []
    roles = [admin.role] + (admin.secondary_roles or [])
    deduped = []
    for role in roles:
        if role and role not in deduped:
            deduped.append(role)
    return deduped

def admin_has_any_role(admin, *allowed_roles):
    return bool(set(get_admin_roles(admin)).intersection(set(allowed_roles)))

def get_or_create_contract_for_role(admin, role):
    contract = UserContract.query.filter_by(
        user_id=admin.id,
        contract_type=role,
    ).order_by(UserContract.created_at.desc()).first()
    if contract:
        return contract
    contract = UserContract(
        user_id=admin.id,
        contract_type=role,
        status='pending_user_signature',
    )
    db.session.add(contract)
    db.session.commit()
    return contract

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

This agreement is entered into between BrightWave Habitat Enterprise, registered in Nigeria under the Corporate Affairs Commission (CAC) as BrightWave Habitat Enterprise Ltd ("the Company"), represented by its Chief Executive Officer (Walihlah Hamza), and the individual granted Manager access to this portal ("the Manager").

1. ROLE AND RESPONSIBILITIES
The Manager is responsible for overseeing property readiness, daily operations, tenant relations, and site standards across all active BrightWave properties. The Manager reports directly to the CEO and is accountable for the operational performance of all assigned properties. This engagement is on a contractor basis and does not constitute an employment relationship unless separately confirmed in writing.

2. SYSTEM ACCESS
The Manager is granted access to property management, inquiry handling, team oversight, and operational data within the BrightWave management portal. Access to full financial records and investor data is restricted to authorised personnel only. Access may be revoked at any time at the discretion of the CEO.

3. CONFIDENTIALITY
All property data, client information, tenant details, team data, and operational information accessed through this portal are strictly confidential. The Manager agrees not to disclose, share, or make available any such information to third parties without prior written CEO approval. This obligation is perpetual and survives the termination or expiry of this agreement.

4. CODE OF CONDUCT
The Manager agrees to maintain the highest professional standards in all interactions with tenants, clients, contractors, and team members. Any conduct that damages the reputation of BrightWave Habitat Enterprise, involves misuse of Company resources, or constitutes a conflict of interest, may result in immediate access revocation and legal action under applicable Nigerian law.

5. COMMISSION STRUCTURE
The Manager is entitled to a commission of 10% of the gross rent value per unit successfully leased or let on behalf of BrightWave Habitat Enterprise during their term of engagement. Commission is payable upon confirmed tenant occupancy and verified receipt of the tenant's first full payment. The CEO reserves the right to adjust commission terms with 14 days written notice. No commission is payable on transactions not confirmed in writing by the CEO.

6. DATA SECURITY
The Manager is solely responsible for keeping their login credentials secure and must not share access with any other person under any circumstances. Any suspected breach of system security must be reported to the CEO immediately. Unauthorised sharing of access credentials constitutes a material breach of this agreement.

7. INTELLECTUAL PROPERTY
All work produced, materials created, processes developed, client lists compiled, and operational knowledge gained during the term of this engagement remain the sole intellectual property of BrightWave Habitat Enterprise Ltd. The Manager shall not reproduce, use, or distribute any such materials outside this engagement without prior written CEO approval.

8. AGREEMENT TERM AND TERMINATION
This agreement is effective from the date both parties digitally sign and remains in force until terminated by either party with 14 days written notice, or immediately by the Company in the event of serious misconduct, breach of confidentiality, fraud, or any material breach of this agreement. Upon termination, the Manager must immediately cease use of all Company systems and must not retain, copy, or share any Company data.

9. BREACH AND REMEDIES
In the event of a material breach by the Manager — including but not limited to unauthorised disclosure of confidential information, misuse of Company systems, acceptance of undisclosed payments, or misconduct — the Company reserves the right to:
   (a) Immediately revoke all portal and system access without prior notice;
   (b) Withhold any unpaid commission pending the outcome of investigation;
   (c) Pursue civil remedies including injunctive relief, recovery of financial losses, and damages;
   (d) Report the breach to relevant law enforcement agencies or professional bodies where warranted.

In the event of a material breach by the Company — including unjustified withholding of earned commission — the Manager may serve written notice of the breach and, if unresolved within 14 days, pursue civil recovery through the appropriate Nigerian court.

10. NON-SOLICITATION
During the term of this agreement and for a period of 12 months following termination, the Manager agrees not to directly solicit, approach, or transact with any tenant, client, or prospect introduced by or through BrightWave Habitat Enterprise for personal commercial benefit or on behalf of a competing property business.

11. FORCE MAJEURE
Neither party shall be in breach of this agreement for any failure or delay caused by circumstances beyond their reasonable control, including natural disasters, government actions, civil unrest, power failure, or disruptions to essential infrastructure. The affected party must give prompt written notice and take all reasonable steps to minimise the impact.

12. AMENDMENTS
No variation, addition, or amendment to this agreement shall be valid or binding unless made in writing and confirmed by both parties through the Company's authorised digital portal or a separately executed written instrument.

13. DISPUTE RESOLUTION
Any dispute arising from or in connection with this agreement shall first be addressed through good-faith negotiation within 21 days of written notice of the dispute. If unresolved, the matter shall be referred to mediation. Failing mediation, disputes shall be submitted to and resolved by the Kwara State High Court, Ilorin Division, to whose exclusive jurisdiction both parties irrevocably submit.

14. GOVERNING LAW
This agreement is governed by and shall be construed in accordance with the laws of the Federal Republic of Nigeria, including the Companies and Allied Matters Act (CAMA) 2020 and the Labour Act Cap L1 LFN 2004 (where applicable by context).

15. SEVERABILITY
If any provision of this agreement is found by a court of competent jurisdiction to be invalid, illegal, or unenforceable, that provision shall be modified to the minimum extent necessary to make it enforceable, and the remaining provisions shall continue in full force and effect.

16. ENTIRE AGREEMENT
This agreement, together with any written amendments duly executed by both parties, constitutes the entire agreement between the parties with respect to its subject matter and supersedes all prior discussions, representations, understandings, and agreements, whether oral or written.

By signing below, the Manager confirms they have read, understood, and fully agree to all terms outlined in this agreement. This constitutes a legally binding agreement between both parties once countersigned by the Chief Executive Officer (Walihlah Hamza) of BrightWave Habitat Enterprise Ltd. Digital signatures are valid and binding under the Evidence Act 2011 of Nigeria."""
    },
    'ACCOUNTANT': {
        'title': 'Financial Controller Agreement',
        'body': """FINANCIAL CONTROLLER AGREEMENT

This agreement is entered into between BrightWave Habitat Enterprise, registered in Nigeria under the Corporate Affairs Commission (CAC) as BrightWave Habitat Enterprise Ltd ("the Company"), represented by its Chief Executive Officer (Walihlah Hamza), and the individual granted Accountant access to this portal ("the Accountant").

1. ROLE AND RESPONSIBILITIES
The Accountant is responsible for tracking all financial transactions, managing rent collection records, overseeing investor distributions, preparing financial reports, and maintaining accurate and complete financial data within the BrightWave management system. The Accountant reports directly to the CEO and is accountable for the integrity of all financial records. This engagement is on a contractor basis and does not constitute an employment relationship unless separately confirmed in writing.

2. SYSTEM ACCESS
The Accountant is granted access to financial dashboards, investor distribution records, payment tracking, and all financial data within the portal. Access to operational management functions, administrative settings, and property management features beyond financial oversight is restricted to authorised personnel only. Access may be revoked at any time at the discretion of the CEO.

3. STRICT FINANCIAL CONFIDENTIALITY
The Accountant acknowledges that all financial information, investor data, payment records, profit figures, company accounts, and any financial details accessed through this system are strictly confidential. Disclosure of any such information to unauthorised parties — whether individuals, organisations, or media — constitutes a serious breach of this agreement, may attract civil liability, and may result in criminal proceedings under applicable Nigerian law. This obligation is perpetual and survives the termination of this agreement.

4. ACCURACY AND INTEGRITY
The Accountant agrees to maintain the highest standard of accuracy, transparency, and completeness in all financial records. Any discrepancies, errors, or irregularities discovered must be reported to the CEO immediately and in writing. Deliberate falsification, manipulation, concealment, or misrepresentation of financial records constitutes gross misconduct and will result in immediate termination, civil liability for any resulting financial loss, and referral to the Economic and Financial Crimes Commission (EFCC) and/or law enforcement authorities.

5. INVESTOR DATA PROTECTION
Investor identities, investment amounts, return structures, and all associated personal and financial details are subject to the highest level of confidentiality. The Accountant must not discuss, share, reference, or disclose investor information outside of official Company communications under any circumstances. Any breach of investor data protection may attract personal civil liability under applicable Nigerian data protection principles.

6. COMPLIANCE
The Accountant agrees to operate in strict accordance with applicable Nigerian financial regulations, ICAN accounting standards, the Financial Reporting Council of Nigeria (FRCN) framework, and any internal financial controls and policies established by the CEO. The Accountant must disclose any known conflict of interest to the CEO immediately.

7. DATA SECURITY
The Accountant is solely responsible for keeping login credentials secure at all times and must not share access with any other person. Any suspected unauthorised access to Company systems must be reported to the CEO immediately. Sharing credentials constitutes a material breach of this agreement.

8. AGREEMENT TERM AND TERMINATION
This agreement is effective from the date both parties digitally sign and remains in force until terminated by either party with 14 days written notice, or immediately by the Company in the event of serious misconduct, financial fraud, breach of confidentiality, or any material breach. Upon termination, the Accountant must immediately cease all system access and must not retain, copy, or disclose any Company financial data.

9. BREACH AND REMEDIES
In the event of a material breach by the Accountant — including but not limited to falsification of records, unauthorised disclosure of investor data, fraud, or misappropriation — the Company reserves the right to:
   (a) Immediately revoke all system access without prior notice;
   (b) Withhold any outstanding payments pending investigation;
   (c) Pursue civil and criminal remedies under Nigerian law, including referral to the EFCC and relevant law enforcement authorities;
   (d) Report the breach to ICAN or equivalent professional body where applicable.

In the event of a material breach by the Company — including unjustified withholding of agreed compensation — the Accountant may serve written notice and, if unresolved within 14 days, pursue civil recovery through the appropriate Nigerian court.

10. FORCE MAJEURE
Neither party shall be in breach for failure or delay caused by circumstances beyond their reasonable control. The affected party must give prompt written notice and take all reasonable steps to minimise the impact.

11. AMENDMENTS
No variation or amendment to this agreement shall be valid unless made in writing and confirmed by both parties through the Company's authorised digital portal or a separately executed written instrument.

12. DISPUTE RESOLUTION
Disputes shall first be addressed through good-faith negotiation within 21 days of written notice. If unresolved, the matter proceeds to mediation. Failing mediation, disputes shall be submitted to the Kwara State High Court, Ilorin Division, to whose exclusive jurisdiction both parties irrevocably submit.

13. GOVERNING LAW
This agreement is governed by the laws of the Federal Republic of Nigeria, including the Companies and Allied Matters Act (CAMA) 2020, the Financial Reporting Council Act 2011, and the Labour Act Cap L1 LFN 2004 where applicable.

14. SEVERABILITY
If any provision is found invalid or unenforceable, it shall be modified to the minimum extent necessary and the remaining provisions continue in full force.

15. ENTIRE AGREEMENT
This agreement constitutes the entire understanding between the parties regarding its subject matter and supersedes all prior discussions, representations, and agreements, whether oral or written.

By signing below, the Accountant confirms they have read, understood, and fully agree to all terms outlined in this agreement. This constitutes a legally binding agreement between both parties once countersigned by the Chief Executive Officer (Walihlah Hamza) of BrightWave Habitat Enterprise Ltd. Digital signatures are valid and binding under the Evidence Act 2011 of Nigeria."""
    },
    'REALTOR': {
        'title': 'Real Estate Agent Agreement',
        'body': """REAL ESTATE AGENT AGREEMENT

This agreement is entered into between BrightWave Habitat Enterprise, registered in Nigeria under the Corporate Affairs Commission (CAC) as BrightWave Habitat Enterprise Ltd ("the Company"), represented by its Chief Executive Officer (Walihlah Hamza), and the individual granted Realtor access to this portal ("the Realtor").

1. ROLE AND RESPONSIBILITIES
The Realtor is responsible for managing client inquiries, conducting property showings, handling lead pipelines, and facilitating the letting, sale, and service apartment arrangement process for all BrightWave properties. The Realtor operates as an authorised representative of BrightWave Habitat Enterprise and must represent the Company's interests with professionalism and honesty at all times. This engagement is on a contractor basis and does not constitute an employment relationship unless separately confirmed in writing.

2. SYSTEM ACCESS
The Realtor is granted access to property listings, client inquiries, and lead management features within the portal. Access to financial records, investor data, team management, and administrative functions is restricted to authorised personnel only.

3. CLIENT REPRESENTATION
The Realtor agrees to represent BrightWave Habitat Enterprise and its clients with integrity, honesty, and professionalism. All client interactions must comply with the Company's standards and applicable Nigerian real estate practice guidelines.

4. EXCLUSIVITY FOR LISTED PROPERTIES
For properties actively listed under BrightWave Habitat Enterprise, the Realtor agrees to represent only the Company's interests. The Realtor must not simultaneously represent competing parties on the same transaction, or accept undisclosed payments or commissions from any third party in relation to a BrightWave transaction, without prior written CEO approval. Such conduct constitutes a material breach of this agreement.

5. COMMISSION STRUCTURE
The Realtor is entitled to the following commission rates on transactions successfully completed and confirmed in writing by the CEO on behalf of BrightWave Habitat Enterprise:
   — 10% of the gross rent value per residential or hostel unit successfully leased or let.
   — 10% of the agreed sale price per land plot sold.
   — 10% of the gross contract value per service apartment successfully arranged or let.
Commission is earned upon full completion of the transaction confirmed in writing by the CEO, and is payable in accordance with the Company's standard payment schedule. No commission is payable on incomplete, uncertified, or disputed transactions. The CEO reserves the right to adjust commission terms for future engagements with 14 days written notice.

6. CONFIDENTIALITY
All client details, property pricing information, negotiation discussions, internal company strategy, and all other non-public Company information are strictly confidential. The Realtor agrees not to disclose such information to competitors, third parties, or the public without prior written CEO approval. This obligation survives the termination of this agreement.

7. CODE OF CONDUCT
The Realtor agrees to maintain honest, transparent, and fully professional conduct at all times. Misrepresentation of any property specification, pricing, or Company information to clients is strictly prohibited. Violations may result in immediate termination and personal civil liability for any resulting losses to the Company or clients.

8. DATA SECURITY
The Realtor is solely responsible for keeping login credentials secure and must not share access with any other person. Any suspected security breach must be reported to the CEO immediately. Sharing credentials constitutes a material breach.

9. AGREEMENT TERM AND TERMINATION
This agreement is effective from the date both parties digitally sign and remains in force until terminated by either party with 7 days written notice, or immediately by the Company in the event of serious misconduct, misrepresentation, breach of confidentiality, or any material breach. Upon termination, all access is revoked immediately.

10. BREACH AND REMEDIES
In the event of a material breach by the Realtor — including but not limited to misrepresentation to clients, acceptance of undisclosed commissions, breach of exclusivity, or disclosure of confidential information — the Company reserves the right to:
   (a) Immediately revoke all portal and system access without prior notice;
   (b) Withhold any outstanding commission pending investigation and potential offset against Company losses;
   (c) Pursue civil remedies including recovery of losses, damages, and injunctive relief under Nigerian law;
   (d) Report the breach to relevant authorities or professional bodies.

In the event of a material breach by the Company — including unjustified withholding of earned commission — the Realtor may serve written notice and, if unresolved within 14 days, pursue civil recovery through the appropriate Nigerian court.

11. NON-SOLICITATION
During the term of this agreement and for 12 months following termination, the Realtor must not directly solicit, approach, or transact with any BrightWave client, lead, or prospect introduced through this engagement for personal commercial benefit or on behalf of a competing property business.

12. FORCE MAJEURE
Neither party shall be in breach for delays or failures caused by circumstances beyond their reasonable control. The affected party must give prompt written notice and take all reasonable steps to minimise the impact.

13. AMENDMENTS
Any variation to this agreement must be made in writing and confirmed by both parties through the Company's authorised digital portal or a separately executed instrument.

14. DISPUTE RESOLUTION
Disputes shall first be addressed through good-faith negotiation within 21 days of written notice. If unresolved, the matter proceeds to mediation. Failing mediation, disputes shall be submitted to the Kwara State High Court, Ilorin Division, to whose exclusive jurisdiction both parties irrevocably submit.

15. GOVERNING LAW
This agreement is governed by the laws of the Federal Republic of Nigeria, including the Companies and Allied Matters Act (CAMA) 2020 and applicable Nigerian real estate and commercial practice frameworks.

16. SEVERABILITY
If any provision is found invalid or unenforceable, it shall be modified to the minimum extent necessary and the remaining provisions continue in full force.

17. ENTIRE AGREEMENT
This agreement constitutes the entire understanding between the parties and supersedes all prior discussions, representations, and agreements, whether oral or written.

By signing below, the Realtor confirms they have read, understood, and fully agree to all terms outlined in this agreement. This constitutes a legally binding agreement between both parties once countersigned by the Chief Executive Officer (Walihlah Hamza) of BrightWave Habitat Enterprise Ltd. Digital signatures are valid and binding under the Evidence Act 2011 of Nigeria."""
    },
    'INVESTOR': {
        'title': 'Investment Agreement — BrightWave Habitat Enterprise',
        'body': """INVESTMENT AGREEMENT

This agreement is entered into between BrightWave Habitat Enterprise, registered in Nigeria under the Corporate Affairs Commission (CAC) as BrightWave Habitat Enterprise Ltd ("the Company"), represented by its Chief Executive Officer (Walihlah Hamza), and the investor granted access to this portal ("the Investor").

IMPORTANT NOTICE — PLEASE READ CAREFULLY

---

1. COMPANY OVERVIEW
BrightWave Habitat Enterprise is a Nigerian real estate development company, incorporated and registered under the Corporate Affairs Commission (CAC) of Nigeria as BrightWave Habitat Enterprise Ltd, focused on student accommodation, residential housing, and estate development. The Company is currently in its early growth phase, with Phase 1 (BrightWave Hostel, Malete, Kwara State) as the first completed project.

2. PRE-REVENUE PHASE DISCLOSURE
The Investor acknowledges and accepts that the current investment coincides with an active construction and development phase. No distributions or returns will be made during the construction period, which is estimated at 12 to 18 months from the investment date. Returns commence upon project completion and first revenue generation. The exact timeline may vary due to construction, regulatory, or market factors beyond the Company's control.

3. INVESTMENT TERMS
The specific investment amount, type (Debt or Equity), return rate, investment term, and distribution schedule applicable to this Investor are as specified in the Investor's profile within this portal and as separately confirmed in writing by the CEO. These terms are personalised and confidential.

DEBT INVESTMENT TERMS:
— The Investor lends capital to the Company at the agreed annual interest rate for the agreed investment term (typically 3, 5, or 10 years as confirmed in the Investor's profile).
— Interest distributions are paid annually, commencing after project completion and first revenue generation.
— Principal is returned in full at the end of the agreed investment term.
— The annual return rate recommended by the Company for founding investors is 10% per annum.
— The Investor may not demand early repayment of principal except by separate written agreement with the CEO.

EQUITY INVESTMENT TERMS:
— The Investor acquires an ownership stake in a specific BrightWave development project (not the entire company or its other projects).
— Distributions are made from project revenues on an annual basis, proportional to the Investor's confirmed equity stake.
— The equity stake may appreciate or depreciate based on project performance. There is no guaranteed fixed return.
— The Investor's equity interest is non-transferable without prior written CEO approval.

4. REPRESENTATIONS AND WARRANTIES
The Investor confirms that:
   (a) They are of legal age and are legally capable of entering into this agreement under the laws of Nigeria;
   (b) The funds invested originate from legitimate sources and comply with applicable Nigerian anti-money laundering regulations;
   (c) They have conducted their own independent due diligence and are investing on the basis of their own assessment of the risks involved.

5. RISK DISCLOSURE
The Investor acknowledges that real estate investment carries inherent risks, including but not limited to: construction delays, cost overruns, changes in market conditions, regulatory changes, adverse economic conditions, and force majeure events. The Company will communicate all material developments in a timely manner but cannot guarantee specific financial outcomes. Past performance does not guarantee future results. Investment in pre-completion or early-stage projects carries heightened risk, and the Investor accepts this risk fully.

6. USE OF FUNDS
All investment funds will be used exclusively for property development, construction costs, professional services, regulatory compliance, and operational setup directly related to BrightWave projects. Funds will not be used for the personal benefit of any individual. A detailed fund utilisation breakdown is available upon request from the CEO.

7. TRANSPARENCY AND REPORTING
The Company commits to providing the Investor with regular updates through this portal, including construction progress milestones, financial performance reports, and distribution schedules. The Company will notify the Investor in writing of any material development that may materially affect their investment within 14 days of becoming aware of such development.

8. CONFIDENTIALITY
The Investor agrees to keep the terms of this agreement, their investment amount, and all non-public Company information strictly confidential. Disclosure of such information to competitors, media, or the public without prior written CEO approval constitutes a breach of this agreement and may attract civil liability for any resulting damages.

9. PORTAL ACCESS
Access to the Investor Portal is granted solely to the named Investor for the purpose of monitoring their investment. Access credentials must not be shared. The Company reserves the right to revoke portal access at any time, including in the event of a breach, without prejudice to the Investor's underlying financial rights.

10. FORCE MAJEURE
Neither party shall be liable for failure or delay caused by circumstances beyond their reasonable control, including natural disasters, government actions, civil unrest, pandemic, or disruptions to essential services. In such events, the Company shall notify the Investor promptly and provide a revised timeline where applicable.

11. BREACH AND REMEDIES
In the event of a material breach by the Investor — including misrepresentation, breach of confidentiality, or demand for unauthorised early repayment — the Company reserves the right to:
   (a) Suspend portal access pending resolution;
   (b) Seek legal remedies including injunctive relief and damages under Nigerian law.

In the event of a material breach by the Company — including failure to make confirmed distributions or misuse of investment funds — the Investor may:
   (a) Serve formal written notice of the breach;
   (b) Pursue civil recovery proceedings before the Kwara State High Court or the Federal High Court, as applicable;
   (c) File a complaint with the Securities and Exchange Commission (SEC) of Nigeria where appropriate.

12. TRANSFER AND ASSIGNMENT
Neither party may assign, transfer, or delegate their rights or obligations under this agreement without the prior written consent of the other party.

13. AMENDMENTS
Any variation to the terms of this agreement — including investment amount, return rate, equity percentage, or investment term — must be confirmed in writing and countersigned by both parties through the Company's authorised portal or a separately executed instrument.

14. DISPUTE RESOLUTION
Disputes shall first be addressed through good-faith negotiation within 21 days of written notice. If unresolved, the matter shall be referred to mediation or arbitration under the Arbitration and Conciliation Act Cap A18 LFN 2004. If still unresolved, disputes shall be submitted to the Kwara State High Court, Ilorin Division, or the Federal High Court, and both parties irrevocably submit to the jurisdiction of those courts.

15. GOVERNING LAW
This agreement is governed by the laws of the Federal Republic of Nigeria, including the Companies and Allied Matters Act (CAMA) 2020, the Investment and Securities Act (ISA) 2007, and the Arbitration and Conciliation Act where applicable.

16. SEVERABILITY
If any provision is found invalid or unenforceable, it shall be modified to the minimum extent necessary and the remaining provisions continue in full force.

17. ENTIRE AGREEMENT
This agreement, together with the Investor's confirmed profile details and any written amendments duly executed by both parties, constitutes the entire agreement between the parties and supersedes all prior discussions, representations, and agreements.

18. BINDING AGREEMENT
This agreement becomes binding upon the digital signatures of both the Investor and the Chief Executive Officer (Walihlah Hamza) of BrightWave Habitat Enterprise Ltd. Both parties will retain a signed copy accessible through the portal. Digital signatures are valid and legally binding in accordance with the Evidence Act 2011 of Nigeria.

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
@app.route('/hostels/phase1')
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

@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
def apple_touch_icon():
    icon_name = 'apple-touch-icon.png'
    if not os.path.exists(os.path.join('assets', 'images', icon_name)):
        if os.path.exists(os.path.join('assets', 'images', 'icon-192.png')):
            icon_name = 'icon-192.png'
        else:
            icon_name = 'brightwave-logo.png'
    resp = make_response(send_from_directory('assets/images', icon_name, mimetype='image/png'))
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp

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
const CACHE_NAME = 'brightwave-portal-v5';
const PRECACHE_ASSETS = [
    '/admin/login',
    '/assets/images/brightwave-logo.png',
    '/apple-touch-icon.png',
    '/assets/images/icon-192.png',
    '/assets/images/icon-512.png',
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(PRECACHE_ASSETS))
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

// Network-first for static assets; never cache admin pages or API calls
self.addEventListener('fetch', event => {
    if (event.request.method !== 'GET') return;
    const url = event.request.url;
    if (url.includes('/admin/')) return;
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
            .catch(() => caches.match(event.request).then(cached => cached || Response.error()))
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
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT', 'REALTOR'):
            return jsonify({"success": False, "message": "Access restricted to management roles"}), 403
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
        total_units = PropertyUnit.query.count()
        available_units = PropertyUnit.query.filter_by(status='available').count()
        occupied_units = PropertyUnit.query.filter_by(status='occupied').count()

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
            'total_units': total_units,
            'available_units': available_units,
            'occupied_units': occupied_units,
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

        admin = get_current_admin()
        if not admin or admin.role != 'CEO':
            return jsonify({"success": False, "message": "CEO access required"}), 403
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

        admin = get_current_admin()
        if not admin or admin.role != 'CEO':
            return jsonify({"success": False, "message": "CEO access required"}), 403
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
        admin = get_current_admin()
        if not admin or admin.role != 'CEO':
            return jsonify({"success": False, "message": "CEO access required"}), 403
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

@app.route('/admin/api/request-password-reset', methods=['POST'])
def request_password_reset():
    data = request.get_json() or {}
    identifier = (data.get('username') or data.get('email') or '').strip().lower()
    if not identifier:
        return jsonify({"success": False, "message": "Username or email required"}), 400
    user = Admin.query.filter(
        (Admin.username == identifier) | (Admin.email == identifier)
    ).first()
    # Always return same message to prevent user enumeration
    if not user or user.role == 'CEO':
        return jsonify({"success": True, "message": "If that account exists, a reset token has been generated."})
    # Expire any existing unused tokens
    PasswordResetToken.query.filter_by(user_id=user.id, used=False).delete()
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=24)
    prt = PasswordResetToken(user_id=user.id, token=token, expires_at=expires)
    db.session.add(prt)
    db.session.commit()
    return jsonify({"success": True, "message": "Reset request submitted. Your administrator has been notified and will share your reset link."})

@app.route('/admin/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    prt = PasswordResetToken.query.filter_by(token=token, used=False).first()
    if not prt or prt.expires_at < datetime.utcnow():
        return render_template_string("""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Invalid Link</title>
        <script src="https://cdn.tailwindcss.com"></script></head>
        <body class="bg-gray-900 text-white min-h-screen flex items-center justify-center">
        <div class="text-center"><h1 class="text-2xl font-bold text-red-400 mb-3">Link Expired or Invalid</h1>
        <p class="text-gray-400 mb-6">This password reset link is no longer valid. Please request a new one.</p>
        <a href="/admin/login" class="bg-slate-700 hover:bg-slate-600 text-white px-6 py-2 rounded-lg">Back to Login</a>
        </div></body></html>""")
    if request.method == 'POST':
        new_pw = request.form.get('password', '').strip()
        confirm_pw = request.form.get('confirm_password', '').strip()
        error = None
        if not new_pw or len(new_pw) < 8:
            error = 'Password must be at least 8 characters.'
        elif new_pw != confirm_pw:
            error = 'Passwords do not match.'
        if error:
            return render_template_string(RESET_PASSWORD_TEMPLATE, error=error, token=token)
        user = Admin.query.get(prt.user_id)
        if user:
            user.password_hash = generate_password_hash(new_pw)
            prt.used = True
            db.session.commit()
            return render_template_string("""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Password Reset</title>
            <script src="https://cdn.tailwindcss.com"></script></head>
            <body class="bg-gray-900 text-white min-h-screen flex items-center justify-center">
            <div class="text-center"><div class="w-16 h-16 bg-emerald-700 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg class="w-8 h-8 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg></div>
            <h1 class="text-2xl font-bold text-white mb-3">Password Reset Successful</h1>
            <p class="text-gray-400 mb-6">You can now log in with your new password.</p>
            <a href="/admin/login" class="bg-emerald-700 hover:bg-emerald-600 text-white px-6 py-2 rounded-lg">Go to Login</a>
            </div></body></html>""")
    return render_template_string(RESET_PASSWORD_TEMPLATE, error=None, token=token)

@app.route('/admin/api/reset-requests', methods=['GET'])
@login_required
@ceo_required
def get_reset_requests():
    tokens = PasswordResetToken.query.filter_by(used=False).filter(
        PasswordResetToken.expires_at > datetime.utcnow()
    ).order_by(PasswordResetToken.created_at.desc()).all()
    result = []
    for t in tokens:
        user = Admin.query.get(t.user_id)
        result.append({
            "id": t.id,
            "user_name": user.display_name or user.username if user else "Unknown",
            "username": user.username if user else "",
            "role": user.role if user else "",
            "reset_url": f"/admin/reset-password/{t.token}",
            "expires_at": t.expires_at.strftime("%d %b %Y, %H:%M UTC"),
            "created_at": t.created_at.strftime("%d %b %Y, %H:%M UTC"),
        })
    return jsonify(result)

@app.route('/admin/api/reset-requests/<int:req_id>', methods=['DELETE'])
@login_required
@ceo_required
def cancel_reset_request(req_id):
    prt = PasswordResetToken.query.get_or_404(req_id)
    prt.used = True
    db.session.commit()
    return jsonify({"success": True, "message": "Reset request cancelled"})

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
        current_password = data.get('currentPassword', '')
        new_password = data.get('newPassword', '')
        if not current_password:
            return jsonify({"success": False, "message": "Current password is required"}), 400
        if not new_password or len(new_password) < 8:
            return jsonify({"success": False, "message": "New password must be at least 8 characters"}), 400

        admin = Admin.query.get(session['admin_id'])
        if not admin:
            return jsonify({"success": False, "message": "Admin not found"}), 404
        if not check_password_hash(admin.password_hash, current_password):
            return jsonify({"success": False, "message": "Current password is incorrect"}), 403
        admin.password_hash = generate_password_hash(new_password)
        db.session.commit()
        return jsonify({"success": True, "message": "Password updated successfully"})
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
    all_roles = [admin.role] + (admin.secondary_roles or [])
    if 'INVESTOR' in all_roles:
        investor_profile = InvestorProfile.query.filter_by(user_id=admin.id).first()
    else:
        investor_profile = None

    ct = ContractTemplate.query.filter_by(role=admin.role).first()
    contract_title = ct.title if ct else CONTRACT_TEXTS.get(admin.role, {}).get('title', 'Agreement')
    contract_body = ct.body if ct else CONTRACT_TEXTS.get(admin.role, {}).get('body', '')

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
        contract_status=contract.status,
        contract_title=contract_title,
        contract_body=contract_body,
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
            admin = get_current_admin()
            if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'REALTOR'):
                return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Realtor"}), 403
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
            _prop_admin = get_current_admin()
            if not _prop_admin or _prop_admin.role != 'CEO':
                return jsonify({"success": False, "message": "CEO access required"}), 403
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
        _prop_admin = get_current_admin()
        if not _prop_admin or _prop_admin.role != 'CEO':
            return jsonify({"success": False, "message": "CEO access required"}), 403
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


@app.route('/admin/api/units', methods=['GET'])
@login_required
def admin_units():
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT', 'REALTOR'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, Accountant, or Realtor"}), 403
        sync_property_units_from_tenants()
        property_id = request.args.get('property_id', type=int)
        query = PropertyUnit.query
        if property_id:
            query = query.filter_by(property_id=property_id)
        units = query.order_by(PropertyUnit.property_id.asc(), PropertyUnit.sort_order.asc(), PropertyUnit.unit_code.asc()).all()
        return jsonify([serialize_property_unit(unit) for unit in units])
    except Exception as e:
        logger.error(f"Error fetching units: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/units/<int:unit_id>', methods=['PUT'])
@login_required
def admin_unit_detail(unit_id):
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "CEO or Manager access required"}), 403
        unit = PropertyUnit.query.get_or_404(unit_id)
        data = request.get_json() or {}
        if 'status' in data and data['status'] in ('available', 'reserved', 'occupied', 'maintenance'):
            unit.status = data['status']
        if 'monthly_rent' in data:
            unit.monthly_rent = float(data['monthly_rent']) if data['monthly_rent'] not in (None, '') else None
        if 'notes' in data:
            unit.notes = (data['notes'] or '').strip() or None
        unit.updated_at = datetime.utcnow()
        db.session.commit()
        sync_property_units_from_tenants()
        return jsonify({"success": True, "message": "Unit updated", "unit": serialize_property_unit(unit)})
    except Exception as e:
        logger.error(f"Error updating unit {unit_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/construction-updates', methods=['GET', 'POST'])
@login_required
def admin_construction_updates():
    try:
        if request.method == 'GET':
            admin = get_current_admin()
            if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT', 'REALTOR', 'INVESTOR'):
                return jsonify({"success": False, "message": "Access restricted to authorized roles"}), 403
            property_id = request.args.get('property_id', type=int)
            public_only = request.args.get('public_only', 'false').lower() == 'true'
            query = ConstructionUpdate.query
            if property_id:
                query = query.filter_by(property_id=property_id)
            if public_only:
                query = query.filter_by(is_public=True)
            updates = query.order_by(ConstructionUpdate.progress_percentage.asc(), ConstructionUpdate.created_at.asc()).all()
            return jsonify([serialize_construction_update(update) for update in updates])

        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "CEO or Manager access required"}), 403

        data = request.get_json() or {}
        property_id = data.get('property_id')
        title = (data.get('title') or '').strip()
        if not property_id or not title:
            return jsonify({"success": False, "message": "property_id and title are required"}), 400

        prop = Property.query.get_or_404(int(property_id))
        progress = max(0, min(100, int(data.get('progress_percentage') or 0)))
        happened_on = date_type.fromisoformat(data['happened_on']) if data.get('happened_on') else None
        update = ConstructionUpdate(
            property_id=prop.id,
            title=title,
            milestone_key=(data.get('milestone_key') or '').strip() or None,
            progress_percentage=progress,
            notes=(data.get('notes') or '').strip() or None,
            happened_on=happened_on,
            is_public=bool(data.get('is_public', True)),
            updated_by=admin.username,
        )
        db.session.add(update)
        db.session.commit()
        return jsonify({"success": True, "message": "Construction update added", "update": serialize_construction_update(update)})
    except Exception as e:
        logger.error(f"Error managing construction updates: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/construction-updates/<int:update_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_construction_update_detail(update_id):
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "CEO or Manager access required"}), 403

        update = ConstructionUpdate.query.get_or_404(update_id)
        if request.method == 'DELETE':
            db.session.delete(update)
            db.session.commit()
            return jsonify({"success": True, "message": "Construction update deleted"})

        data = request.get_json() or {}
        if 'property_id' in data and data['property_id']:
            update.property_id = int(data['property_id'])
        if 'title' in data and (data['title'] or '').strip():
            update.title = data['title'].strip()
        if 'milestone_key' in data:
            update.milestone_key = (data['milestone_key'] or '').strip() or None
        if 'progress_percentage' in data:
            update.progress_percentage = max(0, min(100, int(data['progress_percentage'] or 0)))
        if 'notes' in data:
            update.notes = (data['notes'] or '').strip() or None
        if 'happened_on' in data:
            update.happened_on = date_type.fromisoformat(data['happened_on']) if data['happened_on'] else None
        if 'is_public' in data:
            update.is_public = bool(data['is_public'])
        update.updated_by = admin.username
        update.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"success": True, "message": "Construction update saved", "update": serialize_construction_update(update)})
    except Exception as e:
        logger.error(f"Error updating construction update {update_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/inquiries', methods=['GET'])
@login_required
def admin_get_inquiries():
    """Get all property inquiries"""
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'REALTOR'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Realtor"}), 403
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
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'REALTOR'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Realtor"}), 403
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
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "Access restricted to CEO or Manager"}), 403
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
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "Access restricted to CEO or Manager"}), 403
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


@app.route('/admin/api/my-contract', methods=['GET'])
@login_required
def get_my_contract():
    admin = get_current_admin()
    if not admin or admin.role == 'CEO':
        return jsonify({"success": False, "message": "Not available"}), 403
    requested_role = (request.args.get('role') or admin.role).upper()
    if requested_role == 'CEO' or requested_role not in get_admin_roles(admin):
        return jsonify({"success": False, "message": "Requested role is not assigned to this account"}), 403
    contract = get_or_create_contract_for_role(admin, requested_role)
    ct = ContractTemplate.query.filter_by(role=requested_role).first()
    body = ct.body if ct else CONTRACT_TEXTS.get(requested_role, {}).get("body", "")
    title = ct.title if ct else CONTRACT_TEXTS.get(requested_role, {}).get("title", "Agreement")
    return jsonify({
        "success": True, "id": contract.id, "title": title, "body": body,
        "role": contract.contract_type,
        "status": contract.status,
        "user_name": admin.display_name or admin.username,
        "user_signature": contract.user_signature,
        "user_signed_at": contract.user_signed_at.strftime("%d %b %Y, %H:%M UTC") if contract.user_signed_at else None,
        "ceo_signature": contract.ceo_signature,
        "ceo_signed_at": contract.ceo_signed_at.strftime("%d %b %Y, %H:%M UTC") if contract.ceo_signed_at else None,
    })

@app.route("/admin/api/contracts/<int:contract_id>", methods=["GET"])
@login_required
def get_contract_detail(contract_id):
    admin = get_current_admin()
    if not admin or admin.role != "CEO":
        return jsonify({"success": False, "message": "CEO access required"}), 403
    contract = UserContract.query.get_or_404(contract_id)
    user = Admin.query.get(contract.user_id)
    ct = ContractTemplate.query.filter_by(role=contract.contract_type).first()
    body = ct.body if ct else CONTRACT_TEXTS.get(contract.contract_type, {}).get("body", "")
    title = ct.title if ct else CONTRACT_TEXTS.get(contract.contract_type, {}).get("title", "Agreement")
    return jsonify({
        "success": True, "id": contract.id, "title": title, "body": body,
        "status": contract.status, "role": contract.contract_type,
        "user_name": user.display_name if user else "Unknown",
        "user_signature": contract.user_signature,
        "user_signed_at": contract.user_signed_at.strftime("%d %b %Y, %H:%M UTC") if contract.user_signed_at else None,
        "ceo_signature": contract.ceo_signature,
        "ceo_signed_at": contract.ceo_signed_at.strftime("%d %b %Y, %H:%M UTC") if contract.ceo_signed_at else None,
    })

@app.route("/admin/api/completed-contracts", methods=["GET"])
@login_required
def get_completed_contracts():
    admin = get_current_admin()
    if not admin or admin.role != "CEO":
        return jsonify({"success": False, "message": "CEO access required"}), 403
    contracts = UserContract.query.filter_by(status="completed").order_by(UserContract.ceo_signed_at.desc()).all()
    result = []
    for c in contracts:
        user = Admin.query.get(c.user_id)
        result.append({
            "id": c.id, "role": c.contract_type,
            "user_name": user.display_name if user else "Unknown",
            "user_signed_at": c.user_signed_at.strftime("%d %b %Y") if c.user_signed_at else None,
            "ceo_signed_at": c.ceo_signed_at.strftime("%d %b %Y") if c.ceo_signed_at else None,
        })
    return jsonify(result)

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
            if 'username' in data and account.id != ceo.id:
                new_uname = (data['username'] or '').strip().lower()
                if new_uname and new_uname != account.username:
                    if Admin.query.filter(Admin.username == new_uname, Admin.id != account.id).first():
                        return jsonify({"success": False, "message": "Username already taken"}), 400
                    account.username = new_uname
            if 'email' in data and account.id != ceo.id:
                new_email = (data['email'] or '').strip().lower()
                if new_email and new_email != account.email:
                    if Admin.query.filter(Admin.email == new_email, Admin.id != account.id).first():
                        return jsonify({"success": False, "message": "Email already in use"}), 400
                    account.email = new_email
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
        if account.role == 'CEO':
            return jsonify({"success": False, "message": "Cannot delete CEO account"}), 403
        UserContract.query.filter_by(user_id=account.id).delete()
        InvestorProfile.query.filter_by(user_id=account.id).delete()
        db.session.delete(account)
        db.session.commit()
        return jsonify({"success": True, "message": "Account deleted"})
    except Exception as e:
        logger.error(f"Error on account {account_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== TENANT API ==========
@app.route('/admin/api/tenants', methods=['GET', 'POST'])
@login_required
def admin_tenants():
    try:
        if request.method == 'GET':
            admin = get_current_admin()
            if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
                return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403
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

        _tenant_admin = get_current_admin()
        if not _tenant_admin or not admin_has_any_role(_tenant_admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "CEO or Manager access required"}), 403
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
        sync_property_units_from_tenants()
        return jsonify({"success": True, "message": "Tenant added", "id": tenant.id})
    except Exception as e:
        logger.error(f"Error managing tenants: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/tenants/<int:tenant_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_tenant_detail(tenant_id):
    try:
        _tenant_admin = get_current_admin()
        if not _tenant_admin or not admin_has_any_role(_tenant_admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "CEO or Manager access required"}), 403
        tenant = Tenant.query.get_or_404(tenant_id)
        if request.method == 'DELETE':
            tenant.status = 'vacated'
            db.session.commit()
            sync_property_units_from_tenants()
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
        sync_property_units_from_tenants()
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
            admin = get_current_admin()
            if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
                return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403
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

        _pay_admin = get_current_admin()
        if not _pay_admin or not admin_has_any_role(_pay_admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403
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
                    'investment_term_years': p.investment_term_years,
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

@app.route('/admin/api/investors/<int:profile_id>', methods=['PUT', 'DELETE'])
@login_required
@ceo_required
def admin_investor_detail(profile_id):
    try:
        profile = InvestorProfile.query.get_or_404(profile_id)
        if request.method == 'DELETE':
            db.session.delete(profile)
            db.session.commit()
            return jsonify({"success": True, "message": "Investor profile removed"})
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
        if 'investment_term_years' in data:
            profile.investment_term_years = int(data['investment_term_years']) if data['investment_term_years'] else None
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
        all_roles = [admin.role] + (admin.secondary_roles or [])
        if 'INVESTOR' not in all_roles:
            return jsonify({"success": False, "message": "Not an investor account"}), 403
        profile = InvestorProfile.query.filter_by(user_id=admin.id).first()
        if not profile:
            return jsonify(None)
        project_property = get_investor_project_property()
        updates = []
        if project_property:
            updates = ConstructionUpdate.query.filter_by(
                property_id=project_property.id,
                is_public=True
            ).order_by(ConstructionUpdate.progress_percentage.asc(), ConstructionUpdate.created_at.asc()).all()
        return jsonify({
            'investment_type': profile.investment_type,
            'investment_amount': profile.investment_amount,
            'investment_date': profile.investment_date.isoformat() if profile.investment_date else None,
            'roi_rate': profile.roi_rate,
            'equity_percentage': profile.equity_percentage,
            'construction_start_date': profile.construction_start_date.isoformat() if profile.construction_start_date else None,
            'expected_completion_date': profile.expected_completion_date.isoformat() if profile.expected_completion_date else None,
            'total_distributed': profile.total_distributed,
            'investment_term_years': profile.investment_term_years,
            'notes': profile.notes or '',
            'project_property_id': project_property.id if project_property else None,
            'project_property_title': project_property.title if project_property else '',
            'construction_updates': [serialize_construction_update(update) for update in updates],
        })
    except Exception as e:
        logger.error(f"Error fetching investment: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

# ========== CONTRACT TEMPLATE API ==========
@app.route('/admin/api/contracts', methods=['GET'])
@login_required
def admin_contracts_get():
    admin = get_current_admin()
    if admin.role != 'CEO':
        return jsonify({"success": False, "message": "CEO only"}), 403
    templates = ContractTemplate.query.all()
    return jsonify([{
        'role': ct.role,
        'title': ct.title,
        'body': ct.body,
        'updated_at': ct.updated_at.isoformat() if ct.updated_at else None,
        'updated_by': ct.updated_by,
    } for ct in templates])

@app.route('/admin/api/contracts/<role>', methods=['PUT'])
@login_required
def admin_contract_update(role):
    admin = get_current_admin()
    if admin.role != 'CEO':
        return jsonify({"success": False, "message": "CEO only"}), 403
    if not validate_csrf_token():
        return jsonify({"success": False, "message": "Invalid CSRF token"}), 403
    data = request.get_json() or {}
    ct = ContractTemplate.query.filter_by(role=role).first()
    if not ct:
        return jsonify({"success": False, "message": "Contract not found"}), 404
    if 'title' in data:
        ct.title = data['title'].strip()
    if 'body' in data:
        ct.body = data['body']
    ct.updated_by = admin.username
    ct.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "message": "Contract saved"})

# ========== ADMIN TEMPLATES ==========

RESET_PASSWORD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reset Password - BrightWave</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white min-h-screen flex items-center justify-center p-4">
    <div class="w-full max-w-md">
        <div class="text-center mb-8">
            <img src="/assets/images/brightwave-logo.png" alt="BrightWave" class="w-14 h-14 rounded-full object-cover mx-auto mb-3 ring-2 ring-slate-600">
            <h1 class="text-xl font-bold text-white">Set New Password</h1>
            <p class="text-gray-400 text-sm mt-1">BrightWave Habitat Enterprise</p>
        </div>
        <div class="bg-gray-800 rounded-2xl p-8 shadow-2xl border border-gray-700">
            {% if error %}<div class="bg-red-900/60 border border-red-600 rounded-lg px-4 py-3 mb-5 text-sm text-red-300">{{ error }}</div>{% endif %}
            <form method="POST" class="space-y-5">
                <div>
                    <label class="block text-sm font-medium mb-2 text-gray-300">New Password</label>
                    <input type="password" name="password" required minlength="8" placeholder="At least 8 characters"
                        class="w-full px-4 py-3 bg-gray-700 border border-gray-600 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:border-blue-500">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2 text-gray-300">Confirm Password</label>
                    <input type="password" name="confirm_password" required minlength="8" placeholder="Repeat your new password"
                        class="w-full px-4 py-3 bg-gray-700 border border-gray-600 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:border-blue-500">
                </div>
                <button type="submit" class="w-full bg-emerald-700 hover:bg-emerald-600 text-white font-semibold py-3 rounded-xl transition-colors">
                    Set New Password
                </button>
            </form>
        </div>
    </div>
</body>
</html>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BrightWave Habitat Enterprise</title>
    <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
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
            <img src="/assets/images/brightwave-logo.png" alt="BrightWave" class="w-20 h-20 rounded-full object-cover mx-auto mb-4 ring-2 ring-slate-500/50 shadow-xl">
            <h1 class="text-2xl font-bold text-slate-300">BrightWave Habitat Enterprise</h1>
            <p class="text-gray-400 mt-1 text-sm">Management Portal</p>
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
            <div class="text-center pt-1">
                <button type="button" onclick="toggleForgotForm()" class="text-xs text-gray-500 hover:text-gray-300 transition-colors">Forgot password?</button>
            </div>
        </form>
        <!-- Forgot password panel -->
        <div id="forgotPanel" class="hidden mt-4 border-t border-gray-700 pt-5">
            <p class="text-xs text-gray-400 mb-3">Enter your username or email. Your administrator will share a reset link with you.</p>
            <div class="flex gap-2">
                <input type="text" id="forgotIdentifier" placeholder="Username or email" class="flex-1 px-3 py-2.5 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm placeholder-gray-500 focus:outline-none focus:border-blue-500">
                <button onclick="submitForgotPassword()" class="bg-blue-700 hover:bg-blue-600 text-white text-sm font-medium px-4 py-2 rounded-lg">Send</button>
            </div>
            <p id="forgotMsg" class="text-xs mt-2 hidden"></p>
        </div>
    </div>
    <script>
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js?v=4').catch(() => {});
        }

        function toggleForgotForm() {
            const p = document.getElementById('forgotPanel');
            p.classList.toggle('hidden');
            if (!p.classList.contains('hidden')) document.getElementById('forgotIdentifier').focus();
        }

        async function submitForgotPassword() {
            const identifier = document.getElementById('forgotIdentifier').value.trim();
            const msgEl = document.getElementById('forgotMsg');
            if (!identifier) { msgEl.textContent = 'Please enter your username or email.'; msgEl.className = 'text-xs mt-2 text-red-400'; msgEl.classList.remove('hidden'); return; }
            try {
                const res = await fetch('/admin/api/request-password-reset', {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({username: identifier})
                });
                const data = await res.json();
                msgEl.textContent = data.message || 'Request submitted.';
                msgEl.className = 'text-xs mt-2 text-emerald-400';
                msgEl.classList.remove('hidden');
                document.getElementById('forgotIdentifier').value = '';
            } catch(e) {
                msgEl.textContent = 'Error submitting request. Please try again.';
                msgEl.className = 'text-xs mt-2 text-red-400';
                msgEl.classList.remove('hidden');
            }
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
    <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#475569">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="BrightWave CEO">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
    <style>
        .ceo-nav-btn { color: #94a3b8; transition: all 0.15s; }
        .ceo-nav-btn:hover { background: rgba(71,85,105,0.5); color: #e2e8f0; }
        .ceo-nav-btn.active { background: #475569; color: #ffffff; font-weight: 600; }
        #sidebar { position: fixed; top: 0; left: 0; height: 100vh; width: 240px; transition: width 0.25s ease; overflow: hidden; z-index: 50; display: flex; flex-direction: column; background: #0f172a; border-right: 1px solid rgba(71,85,105,0.4); }
        #sidebar.collapsed { width: 60px; }
        #sidebar.collapsed .sb-label { display: none; }
        #sidebar.collapsed .sb-item { justify-content: center; padding-left: 0; padding-right: 0; }
        #sidebar.collapsed #sidebarBrand { justify-content: center; padding-left: 0; padding-right: 0; gap: 0; }
        #sidebarToggleBtn { flex-shrink: 0; transition: transform 0.25s ease; }
        #sidebar.collapsed #sidebarToggleBtn { transform: rotate(180deg); }
        #mainWrapper { margin-left: 240px; transition: margin-left 0.25s ease; min-height: 100vh; display: block; }
        #mainWrapper.sidebar-collapsed { margin-left: 60px; }
        @media (max-width: 767px) {
            #sidebar { transform: translateX(-100%); width: 240px; }
            #sidebar.mobile-open { transform: translateX(0); }
            #mainWrapper { margin-left: 0 !important; }
        }
        .scrollbar-thin::-webkit-scrollbar { width: 4px; }
        .scrollbar-thin::-webkit-scrollbar-thumb { background: #475569; border-radius: 2px; }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <script>
        const PENDING_SIGS_COUNT = {{ pending_sigs_count }};
        const USER_NAME = {{ user_name | tojson }};
    </script>

    <!-- SIDEBAR -->
    <aside id="sidebar" class="fixed top-0 left-0 h-full bg-slate-900 border-r border-slate-700/60 z-50 flex flex-col">
        <!-- Brand -->
        <div id="sidebarBrand" class="h-16 flex items-center gap-3 px-3 border-b border-slate-700/60 flex-shrink-0">
            <img src="/assets/images/brightwave-logo.png" alt="BrightWave" class="h-9 w-9 rounded-full object-cover flex-shrink-0 ring-2 ring-slate-400/40">
            <div class="sb-label min-w-0 flex-1 overflow-hidden">
                <p class="text-sm font-bold text-white leading-tight whitespace-nowrap">BrightWave</p>
                <p class="text-xs text-slate-400 truncate">CEO &middot; {{ user_name }}</p>
            </div>
            <button id="sidebarToggleBtn" onclick="toggleSidebar()" class="text-slate-400 hover:text-white p-1.5 rounded-lg hover:bg-slate-700/50 flex-shrink-0" title="Toggle sidebar">
                <i class="fas fa-chevron-left text-xs"></i>
            </button>
        </div>
        <!-- Nav items -->
        <nav class="flex-1 overflow-y-auto scrollbar-thin py-3 px-2 space-y-0.5">
            <button onclick="showSection('overviewSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-chart-line w-5 text-center flex-shrink-0"></i><span class="sb-label">Overview</span>
            </button>
            <button onclick="showSection('tenantsSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-home w-5 text-center flex-shrink-0"></i><span class="sb-label">Tenants</span>
            </button>
            <button onclick="showSection('paymentsSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-money-bill-wave w-5 text-center flex-shrink-0"></i><span class="sb-label">Payments</span>
            </button>
            <button onclick="showSection('signaturesSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-signature w-5 text-center flex-shrink-0"></i><span class="sb-label flex items-center gap-2">Signatures{% if pending_sigs_count > 0 %}<span class="bg-red-500 text-white text-xs px-1.5 py-0.5 rounded-full leading-none">{{ pending_sigs_count }}</span>{% endif %}</span>
            </button>
            <button onclick="showSection('accountsSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-users w-5 text-center flex-shrink-0"></i><span class="sb-label">Accounts</span>
            </button>
            <button onclick="showSection('investorsSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-chart-pie w-5 text-center flex-shrink-0"></i><span class="sb-label">Investors</span>
            </button>
            <button onclick="showSection('propertiesSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-building w-5 text-center flex-shrink-0"></i><span class="sb-label">Properties</span>
            </button>
            <button onclick="showSection('constructionSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-hard-hat w-5 text-center flex-shrink-0"></i><span class="sb-label">Construction</span>
            </button>
            <button onclick="showSection('contentSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-globe w-5 text-center flex-shrink-0"></i><span class="sb-label">Website</span>
            </button>
            <button onclick="showSection('teamSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-id-card w-5 text-center flex-shrink-0"></i><span class="sb-label">Our Team</span>
            </button>
            <button onclick="showSection('inquiriesSection2')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-envelope w-5 text-center flex-shrink-0"></i><span class="sb-label">Inquiries</span>
            </button>
            <button onclick="showSection('contractsSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-file-contract w-5 text-center flex-shrink-0"></i><span class="sb-label">Contracts</span>
            </button>
        </nav>
        <!-- Footer -->
        <div class="flex-shrink-0 px-2 pb-3 pt-2 border-t border-slate-700/60 space-y-0.5">
            <button id="changePasswordBtn" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-key w-5 text-center flex-shrink-0"></i><span class="sb-label">Change Password</span>
            </button>
            <a href="/admin/logout" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left block">
                <i class="fas fa-sign-out-alt w-5 text-center flex-shrink-0"></i><span class="sb-label">Logout</span>
            </a>
        </div>
    </aside>

    <!-- Mobile overlay -->
    <div id="sidebarOverlay" class="fixed inset-0 bg-black/60 z-40 hidden" onclick="closeSidebar()"></div>

    <!-- Main wrapper -->
    <div id="mainWrapper">
        <!-- Slim top bar with hamburger -->
        <header class="sticky top-0 z-30 bg-slate-900/95 backdrop-blur border-b border-slate-700/60 h-14 flex items-center px-4 gap-3">
            <button onclick="toggleSidebar()" class="text-slate-400 hover:text-white p-2 rounded-lg hover:bg-slate-700/50 transition-colors flex-shrink-0">
                <i class="fas fa-bars text-base"></i>
            </button>
            <!-- Mobile brand (hidden on desktop — sidebar handles it there) -->
            <div class="flex items-center gap-2 flex-1 md:hidden min-w-0">
                <img src="/assets/images/brightwave-logo.png" alt="BrightWave" class="h-8 w-8 rounded-full object-cover flex-shrink-0 ring-1 ring-slate-500/50">
                <div class="min-w-0">
                    <p class="text-sm font-bold text-white leading-tight truncate">BrightWave</p>
                    <p class="text-xs text-slate-400 truncate">CEO &middot; {{ user_name }}</p>
                </div>
            </div>
            <div class="flex items-center gap-2 justify-end md:flex-1">
                {% if pending_sigs_count > 0 %}
                <button onclick="showSection('signaturesSection')" class="relative bg-red-600 hover:bg-red-700 text-white text-xs font-medium py-1.5 px-3 rounded-lg transition-colors flex-shrink-0">
                    <i class="fas fa-pen-nib mr-1"></i>Signatures
                    <span class="absolute -top-1.5 -right-1.5 bg-yellow-400 text-gray-900 text-xs font-bold rounded-full w-5 h-5 flex items-center justify-center">{{ pending_sigs_count }}</span>
                </button>
                {% endif %}
            </div>
        </header>
        <main class="max-w-7xl mx-auto py-6 px-4 sm:px-6 lg:px-8">
        <!-- Change Password Form -->
        <section id="passwordForm" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Change Password</h2>
            <form id="updatePasswordForm" class="bg-gray-800 p-4 rounded-lg space-y-4 max-w-md">
                <div>
                    <label class="block text-sm font-medium mb-2">Current Password</label>
                    <input type="password" id="currentPassword" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                </div>
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
            <h2 class="text-xl font-semibold mb-6">Agreements</h2>
            <div class="bg-gray-800 rounded-xl p-6 mb-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Pending Signatures</h3>
                <div id="signaturesContent" class="space-y-4"><!-- Populated by JS --></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Completed Agreements</h3>
                <div id="completedContractsContent" class="divide-y divide-gray-700"><p class="text-gray-500 text-sm text-center py-4">Loading...</p></div>
            </div>
        </section>

        <!-- TEAM ACCOUNTS SECTION -->
        <section id="accountsSection" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Team Accounts</h2>

            <!-- Edit Account Panel (hidden until Edit clicked) -->
            <div id="editAccountPanel" class="hidden bg-gray-700 border border-slate-500/50 p-5 rounded-xl mb-4">
                <h4 class="font-semibold mb-4 text-slate-200">Editing: <span id="editAccName" class="text-white"></span></h4>
                <input type="hidden" id="editAccId">
                <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Display Name</label>
                        <input type="text" id="editAccDisplayName" placeholder="Full name" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-sm text-white placeholder-gray-500">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Username</label>
                        <input type="text" id="editAccUsername" placeholder="login username" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-sm text-white placeholder-gray-500">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Email</label>
                        <input type="email" id="editAccEmail" placeholder="email@example.com" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-sm text-white placeholder-gray-500">
                    </div>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Primary Role</label>
                        <select id="editAccRole" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-sm text-white">
                            <option value="MANAGER">Manager</option>
                            <option value="ACCOUNTANT">Accountant</option>
                            <option value="REALTOR">Realtor</option>
                            <option value="INVESTOR">Investor</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">New Password <span class="text-gray-500 font-normal">(blank = unchanged)</span></label>
                        <input type="password" id="editAccPassword" placeholder="Min 8 characters" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-sm text-white placeholder-gray-500">
                    </div>
                </div>
                <div class="mb-4">
                    <label class="block text-xs font-medium mb-1 text-gray-400">Additional Roles</label>
                    <div class="flex gap-4 flex-wrap pt-1">
                        <label class="flex items-center gap-1.5 text-sm cursor-pointer text-gray-300"><input type="checkbox" class="edit-sec-role accent-blue-500" value="MANAGER"> Manager</label>
                        <label class="flex items-center gap-1.5 text-sm cursor-pointer text-gray-300"><input type="checkbox" class="edit-sec-role accent-green-500" value="ACCOUNTANT"> Accountant</label>
                        <label class="flex items-center gap-1.5 text-sm cursor-pointer text-gray-300"><input type="checkbox" class="edit-sec-role accent-amber-500" value="REALTOR"> Realtor</label>
                        <label class="flex items-center gap-1.5 text-sm cursor-pointer text-gray-300"><input type="checkbox" class="edit-sec-role accent-emerald-500" value="INVESTOR"> Investor</label>
                    </div>
                </div>
                <div class="flex items-center gap-2 flex-wrap">
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
            <!-- Password Reset Requests -->
            <div class="bg-gray-800 p-5 rounded-xl mt-6">
                <div class="flex items-center justify-between mb-4">
                    <h3 class="font-semibold text-slate-300">Password Reset Requests</h3>
                    <button onclick="loadResetRequests()" class="text-xs text-gray-400 hover:text-white">Refresh</button>
                </div>
                <div id="resetRequestsList"><p class="text-gray-500 text-sm text-center py-3">Loading...</p></div>
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
                                <th class="py-2 text-left">Term / Completion</th>
                                <th class="py-2 text-left">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="investorsTable"></tbody>
                    </table>
                </div>
            </div>

            <!-- Investor Edit Modal -->
            <div id="invEditModal" class="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4 hidden">
                <div class="bg-gray-800 rounded-xl shadow-2xl max-w-lg w-full p-6 max-h-screen overflow-y-auto">
                    <h3 class="text-lg font-semibold text-white mb-4"><i class="fas fa-edit mr-2 text-blue-400"></i>Edit Investor Profile</h3>
                    <form id="invEditForm" class="space-y-3">
                        <div class="grid grid-cols-2 gap-3">
                            <div>
                                <label class="block text-xs font-medium text-gray-400 mb-1">Investment Amount (₦)</label>
                                <input id="invEditAmount" type="number" step="1000" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-gray-400 mb-1">Type</label>
                                <select id="invEditType" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none">
                                    <option value="DEBT">DEBT</option>
                                    <option value="EQUITY">EQUITY</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-gray-400 mb-1">ROI Rate (% p.a.)</label>
                                <input id="invEditRoi" type="number" step="0.1" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-gray-400 mb-1">Equity % (EQUITY only)</label>
                                <input id="invEditEquity" type="number" step="0.1" placeholder="Leave blank for DEBT" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-gray-400 mb-1">Investment Term (years)</label>
                                <input id="invEditTerm" type="number" min="1" max="30" placeholder="e.g. 5" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-gray-400 mb-1">Total Distributed (₦)</label>
                                <input id="invEditDist" type="number" step="1000" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-gray-400 mb-1">Investment Date</label>
                                <input id="invEditDate" type="date" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none">
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-gray-400 mb-1">Expected Completion</label>
                                <input id="invEditCompletion" type="date" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none">
                            </div>
                        </div>
                        <div>
                            <label class="block text-xs font-medium text-gray-400 mb-1">Notes</label>
                            <textarea id="invEditNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none"></textarea>
                        </div>
                        <p id="invEditMsg" class="text-xs hidden"></p>
                        <div class="flex gap-3 pt-1">
                            <button type="submit" class="flex-1 bg-blue-700 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Save Changes</button>
                            <button type="button" id="invEditCancel" class="flex-1 bg-gray-700 hover:bg-gray-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Cancel</button>
                        </div>
                    </form>
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
                <div id="tenantsContainer" class="space-y-3"></div>
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
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit</label><input type="text" id="pmtUnit" placeholder="e.g. 1A, 2B" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Period / Session</label><input type="text" id="pmtPeriod" placeholder="e.g. Jan 2026, 2025/2026" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label><input type="text" id="pmtDesc" placeholder="Optional notes" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div class="flex items-end gap-3">
                        <button type="submit" class="bg-emerald-700 hover:bg-emerald-800 text-white text-sm font-medium py-2 px-4 rounded-lg">Record Payment</button>
                        <span id="paymentMsg" class="text-sm"></span>
                    </div>
                </form>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg">
                <h3 class="font-semibold mb-3 text-slate-300">Recent Payments (last 50)</h3>
                <div id="paymentsContainer" class="space-y-3"></div>
            </div>
        </section>

        <!-- Enhanced Statistics -->
        <section id="overviewSection" class="mb-8">
            <h2 class="text-xl font-semibold mb-4">Dashboard Overview</h2>
            <div id="ceoAttentionBar" class="space-y-2 mb-5"></div>
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

        <section id="constructionSection" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Construction Updates</h2>
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <form id="ceoConstructionForm" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium mb-2">Project</label>
                        <select id="ceoConstructionProperty" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></select>
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Milestone Title</label>
                        <input type="text" id="ceoConstructionTitle" placeholder="Foundation complete" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Progress Percentage</label>
                        <input type="number" min="0" max="100" id="ceoConstructionPercent" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">Update Date</label>
                        <input type="date" id="ceoConstructionDate" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-2">Notes</label>
                        <textarea id="ceoConstructionNotes" rows="3" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"></textarea>
                    </div>
                    <div class="md:col-span-2 flex items-center gap-3 flex-wrap">
                        <button type="submit" class="bg-emerald-700 hover:bg-emerald-800 text-white font-medium py-2 px-4 rounded-lg">Post Construction Update</button>
                        <span id="ceoConstructionMsg" class="text-sm"></span>
                    </div>
                </form>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg">
                <div class="flex items-center justify-between gap-3 mb-3">
                    <h3 class="font-semibold text-slate-300">Project Timeline Feed</h3>
                    <span id="ceoConstructionHeadline" class="text-sm text-emerald-400 font-medium">0%</span>
                </div>
                <div id="ceoConstructionList" class="space-y-3"></div>
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

        <!-- CONTRACTS EDITOR SECTION -->
        <section id="contractsSection" class="hidden space-y-6">
            <div class="flex items-center justify-between mb-2">
                <div>
                    <h2 class="text-xl font-bold text-white">Contract Templates</h2>
                    <p class="text-sm text-gray-400 mt-0.5">Edit the agreement text shown to each role when they first log in. Changes take effect immediately for unsigned accounts.</p>
                </div>
            </div>
            <div id="contractsLoading" class="text-gray-400 text-sm">Loading contracts...</div>
            <div id="contractsList" class="space-y-6 hidden"></div>
        </section>
        </main>
    </div><!-- end mainWrapper -->

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
                ...options,
                headers,
            });
            if (!response.ok) throw new Error('Network response was not ok');
            return response.json();
        }

        function fmtNGN(v) { return '\u20a6' + Number(v || 0).toLocaleString('en-NG'); }

        async function loadStats() {
            try {
                const stats = await fetchData('/admin/api/stats');

                // Attention bar
                const attnBar = document.getElementById('ceoAttentionBar');
                if (attnBar) {
                    const items = [];
                    if (PENDING_SIGS_COUNT > 0) items.push(`<div class="flex items-center justify-between gap-3 bg-red-900/40 border border-red-700/50 rounded-xl px-4 py-3"><div class="flex items-center gap-3"><span class="w-2 h-2 rounded-full bg-red-400 flex-shrink-0 animate-pulse"></span><p class="text-sm text-red-200 font-medium">${PENDING_SIGS_COUNT} contract${PENDING_SIGS_COUNT > 1 ? 's' : ''} waiting for your co-signature</p></div><button onclick="showSection('signaturesSection')" class="text-xs text-red-300 border border-red-600 rounded-lg px-3 py-1.5 hover:bg-red-800/40 transition-colors flex-shrink-0">Review Now</button></div>`);
                    if (stats.new_inquiries > 0) items.push(`<div class="flex items-center justify-between gap-3 bg-amber-900/30 border border-amber-700/40 rounded-xl px-4 py-3"><div class="flex items-center gap-3"><span class="w-2 h-2 rounded-full bg-amber-400 flex-shrink-0"></span><p class="text-sm text-amber-200 font-medium">${stats.new_inquiries} new inquiry${stats.new_inquiries > 1 ? 'ies' : ''} not yet actioned</p></div><button onclick="showSection('inquiriesSection2')" class="text-xs text-amber-300 border border-amber-600 rounded-lg px-3 py-1.5 hover:bg-amber-800/30 transition-colors flex-shrink-0">View Inquiries</button></div>`);
                    if (stats.available_units > 0 && stats.active_tenants === 0) items.push(`<div class="flex items-center justify-between gap-3 bg-blue-900/30 border border-blue-700/40 rounded-xl px-4 py-3"><div class="flex items-center gap-3"><span class="w-2 h-2 rounded-full bg-blue-400 flex-shrink-0"></span><p class="text-sm text-blue-200 font-medium">${stats.available_units} unit${stats.available_units > 1 ? 's' : ''} available — no active tenants recorded</p></div><button onclick="showSection('tenantsSection')" class="text-xs text-blue-300 border border-blue-600 rounded-lg px-3 py-1.5 hover:bg-blue-800/30 transition-colors flex-shrink-0">Manage Tenants</button></div>`);
                    attnBar.innerHTML = items.join('');
                    attnBar.classList.toggle('hidden', items.length === 0);
                }

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
            document.getElementById('currentPassword').value = '';
            document.getElementById('newPassword').value = '';
            document.getElementById('passwordMessage').classList.add('hidden');
        });

        document.getElementById('updatePasswordForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const currentPassword = document.getElementById('currentPassword').value;
            const newPassword = document.getElementById('newPassword').value;
            const passwordMessage = document.getElementById('passwordMessage');
            try {
                const response = await fetchData('/admin/api/update-password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ currentPassword, newPassword })
                });
                passwordMessage.textContent = response.message;
                passwordMessage.classList.remove('hidden', 'text-red-500');
                passwordMessage.classList.add('text-green-500');
                setTimeout(() => {
                    document.getElementById('passwordForm').classList.add('hidden');
                    document.getElementById('currentPassword').value = '';
                    document.getElementById('newPassword').value = '';
                    passwordMessage.classList.add('hidden');
                }, 2000);
            } catch (error) {
                passwordMessage.textContent = error.message || 'Error updating password';
                passwordMessage.classList.remove('hidden', 'text-green-500');
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

        // ===== SIDEBAR TOGGLE =====
        let sidebarCollapsed = false;

        function toggleSidebar() {
            const sidebar = document.getElementById('sidebar');
            const mainWrapper = document.getElementById('mainWrapper');
            const overlay = document.getElementById('sidebarOverlay');
            if (window.innerWidth < 768) {
                if (sidebar.classList.contains('mobile-open')) {
                    sidebar.classList.remove('mobile-open');
                    overlay.classList.add('hidden');
                } else {
                    sidebar.classList.add('mobile-open');
                    overlay.classList.remove('hidden');
                }
            } else {
                sidebarCollapsed = !sidebarCollapsed;
                sidebar.classList.toggle('collapsed', sidebarCollapsed);
                mainWrapper.classList.toggle('sidebar-collapsed', sidebarCollapsed);
            }
        }

        function closeSidebar() {
            document.getElementById('sidebar').classList.remove('mobile-open');
            document.getElementById('sidebarOverlay').classList.add('hidden');
        }

        // ===== CEO SECTION NAVIGATION =====
        function showSection(sectionId) {
            const sections = ['overviewSection','tenantsSection','paymentsSection','signaturesSection','accountsSection','investorsSection','propertiesSection','constructionSection','contentSection','teamSection','inquiriesSection2','propertiesTableSection','contractsSection'];
            sections.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.classList.add('hidden');
            });
            const target = document.getElementById(sectionId);
            if (target) target.classList.remove('hidden');
            document.querySelectorAll('.ceo-nav-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll(`.ceo-nav-btn[onclick="showSection('${sectionId}')"]`).forEach(b => b.classList.add('active'));
            if (window.innerWidth < 768) closeSidebar();
            if (sectionId === 'signaturesSection') { loadPendingContracts(); loadCompletedContracts(); }
            if (sectionId === 'accountsSection') { loadAccounts(); loadInvestorAccountOptions(); loadResetRequests(); }
            if (sectionId === 'investorsSection') { loadInvestors(); loadInvestorAccountOptions(); }
            if (sectionId === 'tenantsSection') loadTenants();
            if (sectionId === 'paymentsSection') { loadPayments(); loadTenantOptions(); }
            if (sectionId === 'constructionSection') { loadConstructionPropertyOptions(); loadConstructionUpdates(); }
            if (sectionId === 'contractsSection') loadContracts();
            if (sectionId === 'propertiesSection') {
                const tableSection = document.getElementById('propertiesTableSection');
                if (tableSection) tableSection.classList.remove('hidden');
            }
        }

        function getConstructionSortValue(item) {
            const happened = item.happened_on ? new Date(item.happened_on).getTime() : 0;
            const created = item.created_at ? new Date(item.created_at).getTime() : 0;
            return Math.max(happened || 0, created || 0, 0);
        }

        function getConstructionLatestItem(items) {
            return items.slice().sort((a, b) => {
                const timeDiff = getConstructionSortValue(b) - getConstructionSortValue(a);
                if (timeDiff !== 0) return timeDiff;
                return (b.progress_percentage || 0) - (a.progress_percentage || 0);
            })[0] || null;
        }

        async function loadConstructionPropertyOptions() {
            try {
                const props = await fetchData('/admin/api/properties');
                const options = props.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                const ceoSel = document.getElementById('ceoConstructionProperty');
                const mgrSel = document.getElementById('mgrConstructionProperty');
                const ceoCurrent = ceoSel?.value || '';
                const mgrCurrent = mgrSel?.value || '';
                if (ceoSel) {
                    ceoSel.innerHTML = options;
                    ceoSel.value = ceoCurrent && props.some(p => String(p.id) === ceoCurrent) ? ceoCurrent : (props[0] ? String(props[0].id) : '');
                }
                if (mgrSel) {
                    mgrSel.innerHTML = options;
                    mgrSel.value = mgrCurrent && props.some(p => String(p.id) === mgrCurrent) ? mgrCurrent : (props[0] ? String(props[0].id) : '');
                }
            } catch (e) {}
        }

        function renderConstructionUpdates(items, listId, headlineId) {
            const listEl = document.getElementById(listId);
            const headlineEl = document.getElementById(headlineId);
            if (!listEl) return;
            const latest = items.length ? items[items.length - 1] : null;
            if (headlineEl) headlineEl.textContent = latest ? `${latest.progress_percentage}%` : '0%';
            if (!items.length) {
                listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-4">No construction updates yet.</p>';
                return;
            }
            listEl.innerHTML = items.slice().reverse().map(item => `
                <div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4">
                    <div class="flex items-start justify-between gap-3 mb-2">
                        <div>
                            <p class="font-semibold text-white text-sm">${item.title}</p>
                            <p class="text-xs text-gray-400 mt-0.5">${item.property_title}${item.happened_on ? ' • ' + item.happened_on : ''}</p>
                        </div>
                        <span class="text-sm font-semibold text-emerald-400">${item.progress_percentage}%</span>
                    </div>
                    ${item.notes ? `<p class="text-sm text-gray-300 leading-6">${item.notes}</p>` : ''}
                </div>
            `).join('');
        }

        async function loadConstructionUpdates(propertyId) {
            try {
                const query = propertyId ? ('?property_id=' + propertyId) : '';
                const updates = await fetchData('/admin/api/construction-updates' + query);
                renderConstructionUpdates(updates, 'ceoConstructionList', 'ceoConstructionHeadline');
                renderConstructionUpdates(updates, 'mgrConstructionList', 'mgrConstructionProgress');
            } catch (e) {
                const ceoList = document.getElementById('ceoConstructionList');
                const mgrList = document.getElementById('mgrConstructionList');
                if (ceoList) ceoList.innerHTML = '<p class="text-red-400 text-sm py-4">Error loading construction updates.</p>';
                if (mgrList) mgrList.innerHTML = '<p class="text-red-400 text-sm py-4">Error loading construction updates.</p>';
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
                    <div id="pendingCard_${c.id}" class="bg-gray-800 p-5 rounded-lg border border-yellow-600">
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
                await fetchData('/admin/api/contracts/' + contractId + '/ceo-sign', {
                    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({signature: sig})
                });
                const card = document.getElementById('pendingCard_' + contractId);
                if (card) {
                    card.innerHTML = '<div class="flex items-center gap-3"><svg class=\"w-8 h-8 text-emerald-400 flex-shrink-0\" fill=\"none\" stroke=\"currentColor\" viewBox=\"0 0 24 24\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" stroke-width=\"2\" d=\"M5 13l4 4L19 7\"/></svg><div><p class=\"font-semibold text-white\">Contract Signed</p><p class=\"text-sm text-emerald-400\">Both parties have signed. Agreement is now on file.</p></div></div>';
                    card.className = 'bg-gray-800 p-5 rounded-lg border border-emerald-600';
                }
                setTimeout(() => { loadPendingContracts(); loadCompletedContracts(); }, 2000);
            } catch (e) {
                msgEl.textContent = 'Error signing contract';
                msgEl.className = 'text-sm mt-2 text-red-400';
                msgEl.classList.remove('hidden');
            }
        }

        async function loadCompletedContracts() {
            try {
                const contracts = await fetchData('/admin/api/completed-contracts');
                const el = document.getElementById('completedContractsContent');
                if (!el) return;
                const roleColors = {CEO:'bg-purple-700',MANAGER:'bg-blue-700',ACCOUNTANT:'bg-green-700',REALTOR:'bg-amber-700',INVESTOR:'bg-emerald-700'};
                if (!contracts.length) { el.innerHTML = '<p class=\"text-gray-500 text-sm text-center py-4\">No completed agreements yet.</p>'; return; }
                el.innerHTML = contracts.map(c => '<div class=\"flex items-center justify-between py-3 border-b border-gray-700 last:border-0\"><div class=\"flex items-center gap-3\"><span class=\"text-xs px-2 py-0.5 rounded-full text-white ' + (roleColors[c.role] || 'bg-gray-600') + '\">' + c.role + '</span><div><p class=\"text-sm font-medium text-white\">' + c.user_name + '</p><p class=\"text-xs text-gray-500\">CEO signed ' + (c.ceo_signed_at || '-') + '</p></div></div><button onclick=\"viewContractById(' + c.id + ')\" class=\"text-xs text-emerald-400 hover:text-emerald-300 border border-emerald-700 rounded px-3 py-1.5\">View Agreement</button></div>').join('');
            } catch(e) { /* silent */ }
        }

        async function viewContractById(id) {
            try { const data = await fetchData('/admin/api/contracts/' + id); showContractModal(data); }
            catch (e) { alert('Could not load agreement'); }
        }

        async function viewMyContract() {
            try { const data = await fetchData('/admin/api/my-contract'); showContractModal(data); }
            catch (e) { alert('Could not load agreement'); }
        }

        function showContractModal(data) {
            document.getElementById('cvModalTitle').textContent = data.title || 'Agreement';
            document.getElementById('cvModalBody').textContent = data.body || '';
            document.getElementById('cvUserSig').textContent = data.user_signature || 'Not yet signed';
            document.getElementById('cvUserDate').textContent = data.user_signed_at ? 'Signed ' + data.user_signed_at : '';
            document.getElementById('cvCeoSig').textContent = data.ceo_signature || 'Awaiting CEO';
            document.getElementById('cvCeoDate').textContent = data.ceo_signed_at ? 'Signed ' + data.ceo_signed_at : '';
            const statusMap = {completed:'Both parties have signed — legally binding agreement on file',pending_ceo_signature:'Awaiting CEO co-signature',pending_user_signature:'Awaiting your signature'};
            document.getElementById('cvStatus').textContent = statusMap[data.status] || data.status || '';
            const modal = document.getElementById('contractViewModal');
            modal.classList.remove('hidden'); modal.classList.add('flex');
        }

        function closeContractModal() {
            const modal = document.getElementById('contractViewModal');
            modal.classList.add('hidden'); modal.classList.remove('flex');
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
                            <button onclick="editAccount(${a.id}, ${JSON.stringify(a.display_name || a.username).replace(/"/g, '&quot;')}, '${a.role}', ${JSON.stringify(a.secondary_roles || []).replace(/"/g, '&quot;')}, ${JSON.stringify(a.username).replace(/"/g, '&quot;')}, ${JSON.stringify(a.email||'').replace(/"/g, '&quot;')}, ${JSON.stringify(a.display_name||'').replace(/"/g, '&quot;')})" class="text-xs text-blue-400 hover:text-blue-300">Edit</button>
                            <button onclick="toggleAccount(${a.id}, ${!a.is_active})" class="text-xs ${a.is_active ? 'text-red-400 hover:text-red-300' : 'text-green-400 hover:text-green-300'}">${a.is_active ? 'Deactivate' : 'Activate'}</button>
<button onclick="deleteAccount(${a.id})" class="text-xs text-red-500 hover:text-red-400 ml-1">Remove</button>
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

        async function loadResetRequests() {
            const el = document.getElementById('resetRequestsList');
            if (!el) return;
            try {
                const reqs = await fetchData('/admin/api/reset-requests');
                if (!reqs.length) { el.innerHTML = '<p class="text-gray-500 text-sm text-center py-3">No pending reset requests.</p>'; return; }
                el.innerHTML = reqs.map(r => `
                    <div class="flex items-start justify-between gap-3 py-3 border-b border-gray-700 last:border-0">
                        <div class="min-w-0">
                            <p class="text-sm font-medium text-white">${r.user_name} <span class="text-xs text-gray-400 ml-1">(${r.username})</span></p>
                            <p class="text-xs text-gray-500 mt-0.5">Requested ${r.created_at} &middot; Expires ${r.expires_at}</p>
                            <div class="flex items-center gap-2 mt-2">
                                <code class="text-xs text-emerald-400 bg-gray-700 px-2 py-1 rounded break-all">${window.location.origin}${r.reset_url}</code>
                                <button onclick="navigator.clipboard.writeText('${window.location.origin}${r.reset_url}').then(()=>alert('Link copied!'))" class="text-xs text-blue-400 hover:text-blue-300 flex-shrink-0">Copy</button>
                            </div>
                        </div>
                        <button onclick="cancelResetRequest(${r.id})" class="text-xs text-red-400 hover:text-red-300 flex-shrink-0 mt-1">Cancel</button>
                    </div>`).join('');
            } catch(e) { el.innerHTML = '<p class="text-red-400 text-sm">Error loading requests</p>'; }
        }

        async function cancelResetRequest(id) {
            try {
                await fetchData('/admin/api/reset-requests/' + id, { method: 'DELETE' });
                loadResetRequests();
            } catch(e) { alert('Error cancelling request'); }
        }

        async function deleteAccount(id) {
            if (!confirm('Delete this account permanently? This cannot be undone.')) return;
            try {
                await fetchData('/admin/api/accounts/' + id, { method: 'DELETE' });
                loadAccounts();
            } catch (e) { alert('Error deleting account'); }
        }

        function editAccount(id, name, currentRole, currentSecondary, username, email, displayName) {
            document.getElementById('editAccId').value = id;
            document.getElementById('editAccName').textContent = name;
            document.getElementById('editAccRole').value = currentRole;
            document.getElementById('editAccUsername').value = username || '';
            document.getElementById('editAccEmail').value = email || '';
            document.getElementById('editAccDisplayName').value = displayName || '';
            document.getElementById('editAccPassword').value = '';
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
            const displayName = document.getElementById('editAccDisplayName').value.trim();
            const username = document.getElementById('editAccUsername').value.trim();
            const email = document.getElementById('editAccEmail').value.trim();
            const newPassword = document.getElementById('editAccPassword').value;
            const msgEl = document.getElementById('editAccMessage');
            if (newPassword && newPassword.length < 8) {
                msgEl.textContent = 'Password must be at least 8 characters';
                msgEl.className = 'text-sm ml-1 text-red-400';
                return;
            }
            const payload = { role, secondary_roles: secondaryRoles };
            if (displayName) payload.display_name = displayName;
            if (username) payload.username = username;
            if (email) payload.email = email;
            if (newPassword) payload.new_password = newPassword;
            try {
                const res = await fetchData('/admin/api/accounts/' + id, {
                    method: 'PUT', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify(payload)
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
                            <td class="py-2 pr-3 text-xs text-gray-400">${p.investment_term_years ? p.investment_term_years + ' yrs' : '—'} / ${p.expected_completion_date || 'TBD'}</td>
                            <td class="py-2">
                                <button onclick="editInvestor(${p.id}, ${JSON.stringify(p).replace(/"/g, '&quot;')})" class="text-xs text-blue-400 hover:text-blue-300 mr-2">Edit</button>
                                <button onclick="deleteInvestor(${p.id})" class="text-xs text-red-400 hover:text-red-300">Remove</button>
                            </td>
                        </tr>
                    `;
                }).join('') || '<tr><td colspan="7" class="text-gray-400 py-4 text-center">No investor profiles yet</td></tr>';
            } catch (e) {
                document.getElementById('investorsTable').innerHTML = '<tr><td colspan="7" class="text-red-400 py-2">Error loading investors</td></tr>';
            }
        }

        let editingInvestorId = null;

        function editInvestor(id, data) {
            editingInvestorId = id;
            document.getElementById('invEditAmount').value = data.investment_amount || '';
            document.getElementById('invEditType').value = data.investment_type || 'DEBT';
            document.getElementById('invEditRoi').value = data.roi_rate || 10;
            document.getElementById('invEditEquity').value = data.equity_percentage || '';
            document.getElementById('invEditTerm').value = data.investment_term_years || '';
            document.getElementById('invEditDist').value = data.total_distributed || 0;
            document.getElementById('invEditDate').value = data.investment_date || '';
            document.getElementById('invEditCompletion').value = data.expected_completion_date || '';
            document.getElementById('invEditNotes').value = data.notes || '';
            document.getElementById('invEditModal').classList.remove('hidden');
        }

        document.getElementById('invEditCancel')?.addEventListener('click', () => {
            document.getElementById('invEditModal').classList.add('hidden');
        });

        document.getElementById('invEditForm')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('invEditMsg');
            msgEl.textContent = 'Saving...';
            msgEl.className = 'text-xs text-gray-400';
            msgEl.classList.remove('hidden');
            try {
                const res = await fetchData('/admin/api/investors/' + editingInvestorId, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        investment_amount: parseFloat(document.getElementById('invEditAmount').value),
                        investment_type: document.getElementById('invEditType').value,
                        roi_rate: parseFloat(document.getElementById('invEditRoi').value),
                        equity_percentage: document.getElementById('invEditEquity').value ? parseFloat(document.getElementById('invEditEquity').value) : null,
                        investment_term_years: document.getElementById('invEditTerm').value ? parseInt(document.getElementById('invEditTerm').value) : null,
                        total_distributed: parseFloat(document.getElementById('invEditDist').value) || 0,
                        investment_date: document.getElementById('invEditDate').value || null,
                        expected_completion_date: document.getElementById('invEditCompletion').value || null,
                        notes: document.getElementById('invEditNotes').value,
                    })
                });
                msgEl.textContent = res.message || 'Saved';
                msgEl.className = 'text-xs text-emerald-400';
                loadInvestors();
                setTimeout(() => document.getElementById('invEditModal').classList.add('hidden'), 1200);
            } catch (err) {
                msgEl.textContent = err.message || 'Error saving';
                msgEl.className = 'text-xs text-red-400';
            }
        });

        async function deleteInvestor(id) {
            if (!confirm('Remove this investor profile? This cannot be undone.')) return;
            try {
                await fetchData('/admin/api/investors/' + id, { method: 'DELETE' });
                loadInvestors();
            } catch (e) { alert('Error removing investor'); }
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
                const statusColors = {active:'bg-teal-800/60 text-teal-300 border border-teal-700/40', vacated:'bg-gray-700/60 text-gray-400 border border-gray-600/40'};
                document.getElementById('tenantsContainer').innerHTML = tenants.length ? tenants.map(t => `
                    <div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4 space-y-3">
                        <div class="flex items-start justify-between gap-3">
                            <div class="min-w-0">
                                <p class="font-semibold text-white text-sm">${t.name}</p>
                                ${t.email ? `<p class="text-xs text-gray-400 mt-0.5">${t.email}</p>` : ''}
                                ${t.phone ? `<p class="text-xs text-gray-400">${t.phone}</p>` : ''}
                            </div>
                            <span class="text-xs px-2.5 py-1 rounded-full flex-shrink-0 ${statusColors[t.status] || 'bg-gray-700 text-gray-400'}">${t.status}</span>
                        </div>
                        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                            <div>
                                <p class="text-gray-500 mb-1">Property</p>
                                <p class="text-gray-300">${t.property_name || '—'}</p>
                            </div>
                            <div>
                                <p class="text-gray-500 mb-1">Unit / Room</p>
                                <p class="text-gray-300">${t.unit_number || '—'}</p>
                            </div>
                            <div>
                                <p class="text-gray-500 mb-1">Monthly Rent</p>
                                <p class="text-emerald-400 font-semibold text-sm">${t.monthly_rent ? fmtNGN(t.monthly_rent) : '—'}</p>
                            </div>
                            <div>
                                <p class="text-gray-500 mb-1">Lease Period</p>
                                <p class="text-gray-400">${t.lease_start || '—'}${t.lease_end ? ' → '+t.lease_end : ''}</p>
                            </div>
                        </div>
                        <div class="flex justify-end pt-2 border-t border-gray-600/50">
                            <button onclick="vacateTenant(${t.id})" class="text-xs text-red-400 hover:text-red-300 py-1 px-2 rounded hover:bg-red-900/30 transition-colors">${t.status === 'active' ? 'Mark Vacated' : 'Vacated'}</button>
                        </div>
                    </div>`).join('') : '<p class="text-gray-400 py-6 text-center text-sm">No tenants found</p>';
            } catch (e) {
                document.getElementById('tenantsContainer').innerHTML = '<p class="text-red-400 py-4 text-sm">Error loading tenants</p>';
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

        function parsePaymentMeta(desc) {
            if (!desc) return { unit: '', period: '', notes: desc || '' };
            const parts = desc.split(' | ');
            let unit = '', period = '', notes = [];
            for (const p of parts) {
                if (p.startsWith('Unit:')) unit = p.slice(5).trim();
                else if (p.startsWith('Period:')) period = p.slice(7).trim();
                else notes.push(p);
            }
            return { unit, period, notes: notes.join(' | ') };
        }

        async function loadPayments() {
            try {
                const payments = await fetchData('/admin/api/payments');
                const typeColors = {rent:'bg-blue-900/50 text-blue-300', deposit:'bg-purple-900/50 text-purple-300', fee:'bg-amber-900/50 text-amber-300', other:'bg-gray-700 text-gray-300'};
                document.getElementById('paymentsContainer').innerHTML = payments.length ? payments.map(p => {
                    const meta = parsePaymentMeta(p.description);
                    return `<div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4">
                        <div class="flex items-start justify-between gap-3 mb-3">
                            <div>
                                <p class="font-semibold text-white text-sm">${p.tenant_name || '—'}</p>
                                ${meta.notes ? `<p class="text-xs text-gray-400 mt-0.5">${meta.notes}</p>` : ''}
                            </div>
                            <p class="text-emerald-400 font-bold text-base flex-shrink-0">${fmtNGN(p.amount)}</p>
                        </div>
                        <div class="flex items-center gap-2 flex-wrap text-xs">
                            <span class="px-2.5 py-1 rounded-full ${typeColors[p.payment_type] || 'bg-gray-700 text-gray-300'}">${p.payment_type}</span>
                            ${meta.unit ? `<span class="px-2 py-1 rounded-full bg-slate-700 text-slate-300">Unit ${meta.unit}</span>` : ''}
                            ${meta.period ? `<span class="px-2 py-1 rounded-full bg-indigo-900/50 text-indigo-300">${meta.period}</span>` : ''}
                            <span class="text-gray-400">${p.payment_date}</span>
                            ${p.recorded_by ? `<span class="text-gray-500">by ${p.recorded_by}</span>` : ''}
                        </div>
                    </div>`;
                }).join('') : '<p class="text-gray-400 py-6 text-center text-sm">No payments recorded</p>';
            } catch (e) {
                document.getElementById('paymentsContainer').innerHTML = '<p class="text-red-400 py-4 text-sm">Error loading payments</p>';
            }
        }

        document.getElementById('addPaymentForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('paymentMsg');
            try {
                const tenantSelect = document.getElementById('pmtTenantId');
                const unit = (document.getElementById('pmtUnit')?.value || '').trim();
                const period = (document.getElementById('pmtPeriod')?.value || '').trim();
                const notes = (document.getElementById('pmtDesc')?.value || '').trim();
                const descParts = [];
                if (unit) descParts.push(`Unit:${unit}`);
                if (period) descParts.push(`Period:${period}`);
                if (notes) descParts.push(notes);
                const res = await fetchData('/admin/api/payments', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        tenant_id: tenantSelect.value || null,
                        tenant_name: document.getElementById('pmtTenantName').value,
                        amount: document.getElementById('pmtAmount').value,
                        payment_date: document.getElementById('pmtDate').value,
                        payment_type: document.getElementById('pmtType').value,
                        description: descParts.join(' | ') || null,
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

        // ===== CONTRACT TEMPLATES =====
        const ROLE_LABELS = { MANAGER: 'Property Manager', ACCOUNTANT: 'Financial Controller', REALTOR: 'Real Estate Agent', INVESTOR: 'Investor' };
        let contractsData = {};

        async function loadContracts() {
            const loadingEl = document.getElementById('contractsLoading');
            const listEl = document.getElementById('contractsList');
            try {
                const data = await fetchData('/admin/api/contracts');
                contractsData = {};
                data.forEach(c => contractsData[c.role] = c);
                listEl.innerHTML = data.map(c => `
                    <div class="bg-gray-800 rounded-xl p-6 border border-gray-700">
                        <div class="flex items-center justify-between mb-4">
                            <div>
                                <h3 class="font-semibold text-white text-lg">${ROLE_LABELS[c.role] || c.role} Agreement</h3>
                                <p class="text-xs text-gray-500 mt-0.5">Role: ${c.role}${c.updated_by ? ' &middot; Last edited by ' + c.updated_by : ''}</p>
                            </div>
                            <button onclick="saveContract('${c.role}')" class="bg-emerald-700 hover:bg-emerald-600 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors">
                                <i class="fas fa-save mr-1.5"></i>Save
                            </button>
                        </div>
                        <div class="mb-3">
                            <label class="block text-xs font-medium text-gray-400 mb-1 uppercase tracking-wide">Agreement Title</label>
                            <input id="ctTitle_${c.role}" type="text" value="${c.title.replace(/"/g, '&quot;')}" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none focus:border-slate-400">
                        </div>
                        <div>
                            <label class="block text-xs font-medium text-gray-400 mb-1 uppercase tracking-wide">Agreement Body</label>
                            <textarea id="ctBody_${c.role}" rows="20" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm font-mono leading-relaxed focus:outline-none focus:border-slate-400 resize-y">${c.body}</textarea>
                        </div>
                        <p id="ctMsg_${c.role}" class="text-xs mt-2 hidden"></p>
                    </div>
                `).join('');
                loadingEl.classList.add('hidden');
                listEl.classList.remove('hidden');
            } catch (e) {
                loadingEl.textContent = 'Error loading contracts.';
            }
        }

        async function saveContract(role) {
            const titleEl = document.getElementById('ctTitle_' + role);
            const bodyEl = document.getElementById('ctBody_' + role);
            const msgEl = document.getElementById('ctMsg_' + role);
            if (!titleEl || !bodyEl) return;
            msgEl.textContent = 'Saving...';
            msgEl.className = 'text-xs mt-2 text-gray-400';
            msgEl.classList.remove('hidden');
            try {
                const res = await fetchData('/admin/api/contracts/' + role, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title: titleEl.value.trim(), body: bodyEl.value })
                });
                msgEl.textContent = res.message || 'Saved';
                msgEl.className = 'text-xs mt-2 text-emerald-400';
            } catch (e) {
                msgEl.textContent = e.message || 'Error saving';
                msgEl.className = 'text-xs mt-2 text-red-400';
            }
        }

        async function submitConstructionUpdate(source) {
            const ids = source === 'ceo'
                ? {
                    property: 'ceoConstructionProperty',
                    title: 'ceoConstructionTitle',
                    percent: 'ceoConstructionPercent',
                    date: 'ceoConstructionDate',
                    notes: 'ceoConstructionNotes',
                    msg: 'ceoConstructionMsg',
                    form: 'ceoConstructionForm',
                  }
                : {
                    property: 'mgrConstructionProperty',
                    title: 'mgrConstructionTitle',
                    percent: 'mgrConstructionPercent',
                    date: 'mgrConstructionDate',
                    notes: 'mgrConstructionNotes',
                    msg: 'mgrConstructionMsg',
                    form: 'managerConstructionForm',
                  };
            const msgEl = document.getElementById(ids.msg);
            try {
                const res = await fetchData('/admin/api/construction-updates', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        property_id: document.getElementById(ids.property).value,
                        title: document.getElementById(ids.title).value,
                        progress_percentage: document.getElementById(ids.percent).value,
                        happened_on: document.getElementById(ids.date).value || null,
                        notes: document.getElementById(ids.notes).value,
                        is_public: true,
                    })
                });
                msgEl.textContent = res.message || 'Update posted';
                msgEl.className = 'text-sm text-emerald-400';
                document.getElementById(ids.form).reset();
                loadConstructionPropertyOptions();
                loadConstructionUpdates(document.getElementById(ids.property).value || null);
            } catch (e) {
                msgEl.textContent = e.message || 'Error posting update';
                msgEl.className = 'text-sm text-red-400';
            }
        }

        // Initialize dashboard - show overview by default
        document.addEventListener('DOMContentLoaded', () => {
            showSection('overviewSection');
            loadStats();
            loadProperties();
            loadConstructionPropertyOptions();
            loadConstructionUpdates();
            loadInquiries();
            loadMessages();
            loadSiteContent();
            loadTeamMembers();
            document.getElementById('ceoConstructionForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                await submitConstructionUpdate('ceo');
            });
        });

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js?v=4').catch(() => {});
        }

        async function viewMyContract() {
            try { const data = await fetchData('/admin/api/my-contract'); showContractModal(data); }
            catch (e) { alert('Could not load agreement. Please try again.'); }
        }

        function showContractModal(data) {
            document.getElementById('cvModalTitle').textContent = data.title || 'Agreement';
            document.getElementById('cvModalBody').textContent = data.body || '';
            document.getElementById('cvUserSig').textContent = data.user_signature || 'Not yet signed';
            document.getElementById('cvUserDate').textContent = data.user_signed_at ? 'Signed ' + data.user_signed_at : '';
            document.getElementById('cvCeoSig').textContent = data.ceo_signature || 'Awaiting CEO';
            document.getElementById('cvCeoDate').textContent = data.ceo_signed_at ? 'Signed ' + data.ceo_signed_at : '';
            const statusMap = {completed:'Both parties have signed — legally binding agreement on file',pending_ceo_signature:'Awaiting CEO co-signature',pending_user_signature:'Awaiting your signature'};
            document.getElementById('cvStatus').textContent = statusMap[data.status] || data.status || '';
            const modal = document.getElementById('contractViewModal');
            modal.classList.remove('hidden'); modal.classList.add('flex');
        }

        function closeContractModal() {
            const modal = document.getElementById('contractViewModal');
            modal.classList.add('hidden'); modal.classList.remove('flex');
        }
    </script>

    <!-- CONTRACT VIEW MODAL -->
    <div id="contractViewModal" class="fixed inset-0 bg-black bg-opacity-80 z-[100] hidden items-center justify-center p-4" onclick="if(event.target===this)closeContractModal()">
        <div class="bg-gray-800 rounded-2xl shadow-2xl max-w-2xl w-full max-h-[90vh] flex flex-col">
            <div class="p-5 border-b border-gray-700 flex justify-between items-start flex-shrink-0">
                <div>
                    <p class="text-xs text-emerald-400 uppercase tracking-wide font-medium">Signed Agreement</p>
                    <h3 id="cvModalTitle" class="text-lg font-bold text-white mt-0.5"></h3>
                </div>
                <button onclick="closeContractModal()" class="text-gray-400 hover:text-white p-1 ml-4 flex-shrink-0">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                </button>
            </div>
            <div id="cvModalBody" class="p-6 overflow-y-auto text-sm text-gray-300 leading-relaxed whitespace-pre-line" style="flex:1;max-height:45vh;overflow-y:auto;"></div>
            <div class="p-5 border-t border-gray-700 flex-shrink-0 space-y-3">
                <div class="grid grid-cols-2 gap-3">
                    <div class="bg-gray-700 rounded-lg p-3">
                        <p class="text-xs text-gray-400 mb-1">Employee / Investor Signature</p>
                        <p id="cvUserSig" class="font-semibold text-white text-sm font-mono"></p>
                        <p id="cvUserDate" class="text-xs text-gray-500 mt-0.5"></p>
                    </div>
                    <div class="bg-gray-700 rounded-lg p-3">
                        <p class="text-xs text-gray-400 mb-1">CEO Signature &#183; BrightWave</p>
                        <p id="cvCeoSig" class="font-semibold text-emerald-400 text-sm font-mono"></p>
                        <p id="cvCeoDate" class="text-xs text-gray-500 mt-0.5"></p>
                    </div>
                </div>
                <p id="cvStatus" class="text-xs text-center text-gray-500 pt-1"></p>
            </div>
        </div>
    </div>

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
    <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
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
        const USER_ROLE = {{ user_role | tojson }};
        const ALL_ROLES = {{ all_roles_json | safe }};
        const USER_NAME = {{ user_name | tojson }};
        const NEEDS_CONTRACT = {{ 'true' if needs_contract_signing else 'false' }};
        const AWAITING_CEO = {{ 'true' if awaiting_ceo_signature else 'false' }};
        const SHOW_AGREEMENT_POPUP = {{ 'true' if show_agreement_popup else 'false' }};
        const CONTRACT_ID = {{ contract_id | tojson if contract_id else 'null' }};
        const CONTRACT_STATUS = {{ contract_status | tojson }};
        const adminCsrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
        let activeRole = USER_ROLE;
    </script>

    <!-- CONTRACT SIGNING OVERLAY -->
    {% if needs_contract_signing %}
    <div id="contractOverlay" class="fixed inset-0 bg-black bg-opacity-90 z-50 flex items-center justify-center p-4">
        <div class="bg-gray-800 rounded-2xl shadow-2xl max-w-2xl w-full max-h-screen flex flex-col">
            <div class="p-6 border-b border-gray-700 flex-shrink-0">
                <div class="flex items-center gap-3 mb-1">
                    <img src="/assets/images/brightwave-logo.png" alt="BrightWave" class="w-10 h-10 rounded-full object-cover ring-2 ring-slate-500/40">
                    <div>
                        <p class="text-xs text-gray-400 uppercase tracking-wide">BrightWave Habitat Enterprise</p>
                        <h2 class="text-xl font-bold text-white">{{ contract_title }}</h2>
                    </div>
                </div>
                <p class="text-sm text-yellow-400 mt-2">Please read this agreement carefully before proceeding to your dashboard.</p>
            </div>
            <div id="contractText" class="contract-scroll p-6 overflow-y-auto flex-1 text-sm text-gray-300 leading-relaxed whitespace-pre-line" style="max-height: calc(60vh - 120px); min-height: 180px;">{{ contract_body }}<div id="contractSentinel" style="height:1px;margin-top:4px;"></div></div>
            <div id="scrollPrompt" class="text-center text-xs text-gray-500 py-2 flex-shrink-0">Please read the full agreement carefully before signing.</div>
            <div id="signatureSection" class="p-6 border-t border-gray-700 flex-shrink-0">
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
                <div class="w-full sm:w-auto overflow-x-auto">
                <div id="roleSwitcher" class="flex gap-1 bg-gray-700 p-1 rounded-lg min-w-max">
                    {% for r in all_roles %}
                    <button onclick="switchRole('{{ r }}')" id="roleBtn_{{ r }}"
                        class="role-switch-btn flex-none text-xs font-medium px-3 py-1.5 rounded-md transition-colors {% if r == user_role %}bg-slate-600 text-white{% else %}text-gray-400 hover:text-white{% endif %}">
                        {{ r }}
                    </button>
                    {% endfor %}
                </div>
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
                <div class="bg-gray-800 rounded-xl p-6">
                    <div class="flex items-center justify-between gap-3 mb-4">
                        <h3 class="font-semibold text-lg text-slate-300">Construction Progress Graph</h3>
                        <span id="invProgressHeadline" class="text-sm text-emerald-400 font-medium">0%</span>
                    </div>
                    <div id="investorProgressGraph"></div>
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
                    <div class="flex items-center justify-between gap-3 p-4 bg-gray-700 rounded-lg">
                        <div class="flex items-center gap-3">
                            <svg class="w-8 h-8 text-emerald-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                            <div><p class="font-medium text-white">Investment Agreement</p><p id="docStatus" class="text-xs text-gray-400 mt-0.5">Loading...</p></div>
                        </div>
                        <button id="viewAgreementBtn" onclick="viewMyContract()" class="hidden text-xs text-emerald-400 hover:text-emerald-300 border border-emerald-700 rounded px-3 py-1.5 flex-shrink-0">View Agreement</button>
                    </div>
                </div>
            </div>
            <div id="investorNoProfile" class="hidden bg-gray-800 rounded-xl p-8 text-center">
                <p class="text-gray-400 text-lg font-medium">Investment profile not set up yet</p>
                <p class="text-gray-500 text-sm mt-2">Your CEO will configure your investment details. Please check back shortly.</p>
            </div>

            {% elif r == 'MANAGER' %}
            <!-- MANAGER DASHBOARD -->
            <!-- Tab navigation -->
            <div class="flex gap-1 flex-wrap border-b border-gray-700 pb-1 mb-6" id="mgrTabBar">
                <button class="mgr-tab-btn px-4 py-2 rounded-t-lg text-sm font-medium transition-colors bg-slate-700 text-white" data-tab="mgrTabOverview" onclick="showMgrTab('mgrTabOverview')">Overview</button>
                <button class="mgr-tab-btn px-4 py-2 rounded-t-lg text-sm font-medium transition-colors text-gray-400 hover:text-white" data-tab="mgrTabUnits" onclick="showMgrTab('mgrTabUnits')">Units &amp; Tenants</button>
                <button class="mgr-tab-btn px-4 py-2 rounded-t-lg text-sm font-medium transition-colors text-gray-400 hover:text-white" data-tab="mgrTabInquiries" onclick="showMgrTab('mgrTabInquiries')">Inquiries <span id="mgrInquiriesBadge" class="hidden ml-1 bg-blue-600 text-white text-xs px-1.5 py-0.5 rounded-full"></span></button>
                <button class="mgr-tab-btn px-4 py-2 rounded-t-lg text-sm font-medium transition-colors text-gray-400 hover:text-white" data-tab="mgrTabConstruction" onclick="showMgrTab('mgrTabConstruction')">Construction</button>
            </div>

            <!-- OVERVIEW TAB -->
            <div id="mgrTabOverview">
                <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4 mb-6">
                    <div class="bg-gray-800 rounded-xl p-5"><p class="text-xs text-gray-400 uppercase tracking-wide mb-1">Properties</p><p id="mgr_properties" class="text-3xl font-bold">-</p></div>
                    <div class="bg-emerald-900 rounded-xl p-5"><p class="text-xs text-emerald-300 uppercase tracking-wide mb-1">Available Units</p><p id="mgr_available_units" class="text-3xl font-bold">-</p></div>
                    <div class="bg-blue-900 rounded-xl p-5"><p class="text-xs text-blue-300 uppercase tracking-wide mb-1">Open Inquiries</p><p id="mgr_inquiries" class="text-3xl font-bold">-</p></div>
                    <div class="bg-purple-900 rounded-xl p-5"><p class="text-xs text-purple-300 uppercase tracking-wide mb-1">Active Tenants</p><p id="mgr_active_tenants" class="text-3xl font-bold">-</p></div>
                </div>
                <div class="bg-gray-800 rounded-xl p-6 mb-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Properties Overview</h3>
                    <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Location</th><th class="py-2 text-left text-gray-400">Status</th></tr></thead><tbody id="mgr_propertiesTable"></tbody></table></div>
                </div>
                {% if all_roles | length > 1 %}
                <div class="bg-gray-800 rounded-xl p-6 mb-6">
                    <h3 class="font-semibold text-base mb-3 text-slate-300">Your Access Roles</h3>
                    <div class="flex flex-wrap gap-2">
                        {% for ar in all_roles %}
                        <button onclick="switchRole('{{ ar }}')" class="text-xs font-medium px-4 py-2 rounded-lg border {% if ar == 'MANAGER' %}border-teal-600 bg-teal-900/40 text-teal-300{% else %}border-gray-600 bg-gray-700 text-gray-300 hover:border-gray-500 hover:text-white{% endif %} transition-colors">
                            {{ ar }}{% if ar == 'MANAGER' %} (current){% endif %}
                        </button>
                        {% endfor %}
                    </div>
                    <p class="text-xs text-gray-500 mt-3">Switch between your roles using these buttons or the switcher at the top.</p>
                </div>
                {% endif %}
                <div class="bg-gray-800 rounded-xl p-6 mt-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Your Documents</h3>
                    <div class="flex items-center justify-between gap-3 p-4 bg-gray-700 rounded-lg">
                        <div class="flex items-center gap-3">
                            <svg class="w-8 h-8 text-emerald-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                            <div><p class="font-medium text-white">Employment Agreement</p><p id="roleDocStatus_MANAGER" class="text-xs text-gray-400 mt-0.5">Loading...</p></div>
                        </div>
                        <button id="viewRoleDocBtn_MANAGER" onclick="viewMyContract()" class="hidden text-xs text-emerald-400 hover:text-emerald-300 border border-emerald-700 rounded px-3 py-1.5 flex-shrink-0">View Agreement</button>
                    </div>
                </div>
            </div>

            <!-- UNITS & TENANTS TAB -->
            <div id="mgrTabUnits" class="hidden">
                <div class="grid grid-cols-1 xl:grid-cols-[1.1fr_0.9fr] gap-6 mb-6">
                    <div class="bg-gray-800 rounded-xl p-6">
                        <div class="flex items-center justify-between gap-3 mb-4">
                            <h3 class="font-semibold text-lg text-slate-300">Phase 1 Units</h3>
                            <span class="text-xs text-gray-500">Click status to update</span>
                        </div>
                        <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Unit</th><th class="py-2 text-left text-gray-400">Status</th><th class="py-2 text-left text-gray-400">Rent</th><th class="py-2 text-left text-gray-400">Notes</th><th class="py-2 text-left text-gray-400">Action</th></tr></thead><tbody id="mgr_unitsTable"></tbody></table></div>
                    </div>
                    <div class="bg-gray-800 rounded-xl p-6">
                        <h3 class="font-semibold text-lg mb-4 text-slate-300">Add or Update Tenant</h3>
                        <form id="managerTenantForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <input id="mgrTenantEditId" type="hidden">
                            <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Full Name *</label><input id="mgrTenantName" type="text" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Email</label><input id="mgrTenantEmail" type="email" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Phone</label><input id="mgrTenantPhone" type="text" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Property</label><input id="mgrTenantProperty" type="text" value="BrightWave Phase 1 Hostel" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit</label><select id="mgrTenantUnit" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></select></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Monthly Rent</label><input id="mgrTenantRent" type="number" step="1000" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Lease Start</label><input id="mgrTenantLeaseStart" type="date" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Lease End</label><input id="mgrTenantLeaseEnd" type="date" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label><textarea id="mgrTenantNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea></div>
                            <div class="sm:col-span-2 flex items-center gap-3 flex-wrap"><button id="mgrTenantSubmit" type="submit" class="bg-teal-700 hover:bg-teal-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Save Tenant</button><button id="mgrTenantCancelEdit" type="button" class="hidden bg-gray-700 hover:bg-gray-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Cancel Edit</button><span id="mgrTenantMsg" class="text-sm"></span></div>
                        </form>
                    </div>
                </div>
                <div class="bg-gray-800 rounded-xl p-6">
                    <div class="flex items-center justify-between gap-3 mb-4">
                        <h3 class="font-semibold text-lg text-slate-300">Active Tenants</h3>
                        <span class="text-xs text-gray-500">Manager can add and vacate tenants</span>
                    </div>
                    <div id="mgr_tenantsList" class="space-y-3"></div>
                </div>
            </div>

            <!-- INQUIRIES TAB -->
            <div id="mgrTabInquiries" class="hidden">
                <div class="bg-gray-800 rounded-xl p-6">
                    <div class="flex items-center justify-between gap-3 mb-4">
                        <h3 class="font-semibold text-lg text-slate-300">All Inquiries</h3>
                        <span class="text-xs text-gray-500">Update status to track your pipeline</span>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-sm">
                            <thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Name</th><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400 min-w-[140px]">Status</th><th class="py-2 text-left text-gray-400">Date</th></tr></thead>
                            <tbody id="mgr_inquiriesTable"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- CONSTRUCTION TAB -->
            <div id="mgrTabConstruction" class="hidden">
                <div class="bg-gray-800 rounded-xl p-6">
                    <div class="flex items-center justify-between gap-3 mb-5">
                        <h3 class="font-semibold text-lg text-slate-300">Construction Updates</h3>
                        <span id="mgrConstructionProgress" class="text-sm text-emerald-400 font-medium">0%</span>
                    </div>
                    <form id="managerConstructionForm" class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-6">
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Project</label><select id="mgrConstructionProperty" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></select></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Milestone Title</label><input id="mgrConstructionTitle" type="text" placeholder="Foundation complete" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Progress %</label><input id="mgrConstructionPercent" type="number" min="0" max="100" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Date</label><input id="mgrConstructionDate" type="date" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div class="md:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label><textarea id="mgrConstructionNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea></div>
                        <div class="md:col-span-2 flex items-center gap-3 flex-wrap"><button type="submit" class="bg-emerald-700 hover:bg-emerald-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Post Update</button><span id="mgrConstructionMsg" class="text-sm"></span></div>
                    </form>
                    <div id="mgrConstructionList" class="space-y-3"></div>
                </div>
            </div>

            {% elif r == 'ACCOUNTANT' %}
            <!-- ACCOUNTANT DASHBOARD -->
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <div class="bg-emerald-900 rounded-xl p-5"><p class="text-xs text-emerald-300 uppercase tracking-wide mb-1">Total Revenue (All Time)</p><p id="acc_total_revenue" class="text-3xl font-bold text-white">-</p></div>
                <div class="bg-teal-900 rounded-xl p-5"><p class="text-xs text-teal-300 uppercase tracking-wide mb-1">Revenue This Month</p><p id="acc_monthly_revenue" class="text-3xl font-bold text-white">-</p></div>
                <div class="bg-blue-900 rounded-xl p-5"><p class="text-xs text-blue-300 uppercase tracking-wide mb-1">Active Tenants</p><p id="acc_tenants" class="text-3xl font-bold text-white">-</p></div>
            </div>
            <div class="grid grid-cols-1 xl:grid-cols-[0.9fr_1.1fr] gap-6 mb-6">
                <div class="bg-gray-800 rounded-xl p-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Record Payment</h3>
                    <form id="accountantPaymentForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Tenant</label><select id="accPaymentTenant" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></select></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Fallback Tenant Name</label><input id="accPaymentTenantName" type="text" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Amount *</label><input id="accPaymentAmount" type="number" step="100" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Payment Date *</label><input id="accPaymentDate" type="date" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Payment Type</label><select id="accPaymentType" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="rent">Rent</option><option value="deposit">Deposit</option><option value="fee">Fee</option><option value="other">Other</option></select></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Description</label><input id="accPaymentDesc" type="text" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div class="sm:col-span-2 flex items-center gap-3 flex-wrap"><button type="submit" class="bg-emerald-700 hover:bg-emerald-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Record Payment</button><span id="accPaymentMsg" class="text-sm"></span></div>
                    </form>
                </div>
                <div class="bg-gray-800 rounded-xl p-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Recent Payments</h3>
                    <div id="acc_paymentsContainer" class="space-y-3"></div>
                </div>
            </div>
            <div class="bg-gray-800 rounded-xl p-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Occupancy Snapshot</h3>
                <div id="accUnitsSummary" class="grid grid-cols-1 sm:grid-cols-3 gap-3"></div>
            </div>
                <div class="bg-gray-800 rounded-xl p-6 mt-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Your Documents</h3>
                    <div class="flex items-center justify-between gap-3 p-4 bg-gray-700 rounded-lg">
                        <div class="flex items-center gap-3">
                            <svg class="w-8 h-8 text-emerald-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                            <div><p class="font-medium text-white">Employment Agreement</p><p id="roleDocStatus_ACCOUNTANT" class="text-xs text-gray-400 mt-0.5">Loading...</p></div>
                        </div>
                        <button id="viewRoleDocBtn_ACCOUNTANT" onclick="viewMyContract()" class="hidden text-xs text-emerald-400 hover:text-emerald-300 border border-emerald-700 rounded px-3 py-1.5 flex-shrink-0">View Agreement</button>
                    </div>
                </div>

            {% elif r == 'REALTOR' %}
            <!-- REALTOR DASHBOARD -->
            <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4 mb-6">
                <div class="bg-gray-800 rounded-xl p-5"><p class="text-xs text-gray-400 uppercase tracking-wide mb-1">Active Properties</p><p id="rel_properties" class="text-3xl font-bold">-</p></div>
                <div class="bg-emerald-900 rounded-xl p-5"><p class="text-xs text-emerald-300 uppercase tracking-wide mb-1">Available Units</p><p id="rel_available_units" class="text-3xl font-bold">-</p></div>
                <div class="bg-amber-900 rounded-xl p-5"><p class="text-xs text-amber-300 uppercase tracking-wide mb-1">Open Leads</p><p id="rel_inquiries" class="text-3xl font-bold">-</p></div>
                <div class="bg-green-900 rounded-xl p-5"><p class="text-xs text-green-300 uppercase tracking-wide mb-1">New Today</p><p id="rel_new" class="text-3xl font-bold">-</p></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-6 mb-6">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <h3 class="font-semibold text-lg text-slate-300">Units Ready To Lease</h3>
                    <span class="text-xs text-gray-500">Live availability for leasing conversations</span>
                </div>
                <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Unit</th><th class="py-2 text-left text-gray-400">Status</th><th class="py-2 text-left text-gray-400">Rent</th><th class="py-2 text-left text-gray-400">Notes</th></tr></thead><tbody id="rel_unitsTable"></tbody></table></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-6 mb-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Available Properties</h3>
                <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Location</th><th class="py-2 text-left text-gray-400">Price</th><th class="py-2 text-left text-gray-400">Status</th></tr></thead><tbody id="rel_propertiesTable"></tbody></table></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Leads / Inquiries</h3>
                <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Name</th><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Status</th><th class="py-2 text-left text-gray-400">Date</th></tr></thead><tbody id="rel_inquiriesTable"></tbody></table></div>
            </div>
                <div class="bg-gray-800 rounded-xl p-6 mt-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Your Documents</h3>
                    <div class="flex items-center justify-between gap-3 p-4 bg-gray-700 rounded-lg">
                        <div class="flex items-center gap-3">
                            <svg class="w-8 h-8 text-emerald-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                            <div><p class="font-medium text-white">Employment Agreement</p><p id="roleDocStatus_REALTOR" class="text-xs text-gray-400 mt-0.5">Loading...</p></div>
                        </div>
                        <button id="viewRoleDocBtn_REALTOR" onclick="viewMyContract()" class="hidden text-xs text-emerald-400 hover:text-emerald-300 border border-emerald-700 rounded px-3 py-1.5 flex-shrink-0">View Agreement</button>
                    </div>
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
            const response = await fetch(url, { credentials: 'include', ...options, headers });
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
            else if (role === 'MANAGER') { loadManagerDashboard(); loadRoleDocument('MANAGER'); }
            else if (role === 'ACCOUNTANT') { loadAccountantDashboard(); loadRoleDocument('ACCOUNTANT'); }
            else if (role === 'REALTOR') { loadRealtorDashboard(); loadRoleDocument('REALTOR'); }
        }

        // Contract overlay is always shown with signature section visible (scroll gate removed — server validates signature)

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
                const termYears = profile.investment_term_years || 4;
                if (type === 'DEBT') {
                    const annualReturn = amount * roi / 100;
                    const totalInterest = annualReturn * termYears;
                    const grandTotal = amount + totalInterest;
                    document.getElementById('invExpectedReturn').textContent = formatNGN(grandTotal);
                    document.getElementById('invReturnNote').textContent = `Principal ${formatNGN(amount)} + ${formatNGN(totalInterest)} interest over ${termYears} year${termYears !== 1 ? 's' : ''}`;
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

                // Distribution schedule (for DEBT: show annual payments per term)
                if (type === 'DEBT' && completion) {
                    const annualReturn = amount * roi / 100;
                    const years = Array.from({length: termYears}, (_, i) => i + 1);
                    const scheduleRows = years.map(yr => {
                        const paymentDate = new Date(completion);
                        paymentDate.setFullYear(paymentDate.getFullYear() + yr);
                        const isPast = now > paymentDate;
                        const statusClass = isPast ? 'text-emerald-400' : 'text-yellow-400';
                        const status = isPast ? 'Due' : 'Scheduled';
                        const isFinal = yr === termYears;
                        const finalPayment = isFinal ? ` + ${formatNGN(amount)} principal` : '';
                        return `<div class="flex justify-between items-center py-3 border-b border-gray-700 last:border-0 text-sm">
                            <div><span class="text-white">Year ${yr} Distribution</span>${finalPayment ? `<span class="text-xs text-emerald-400 ml-2">${finalPayment}</span>` : ''}</div>
                            <div class="text-right"><p class="font-medium text-white">${formatNGN(isFinal ? annualReturn + amount : annualReturn)}</p><p class="text-xs text-gray-400">${paymentDate.toLocaleDateString('en-GB', {month:'short',year:'numeric'})}</p></div>
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
                const docStatusMap = {
                    'completed': 'Both parties signed \u2014 Agreement on file',
                    'pending_ceo_signature': 'Awaiting CEO co-signature',
                    'pending_user_signature': 'Awaiting your signature',
                };
                document.getElementById('docStatus').textContent = docStatusMap[CONTRACT_STATUS] || 'Status unknown';
                if (CONTRACT_STATUS === 'completed') {
                    const btn = document.getElementById('viewAgreementBtn');
                    if (btn) btn.classList.remove('hidden');
                }

            } catch (e) {
                document.getElementById('investorLoading').textContent = 'Error loading investment data. Please refresh.';
            }
        }

        // ===== MANAGER DASHBOARD =====
        async function loadRoleDocument(role) {
            const statusEl = document.getElementById('roleDocStatus_' + role);
            const btnEl = document.getElementById('viewRoleDocBtn_' + role);
            if (!statusEl) return;
            const statusMap = {
                completed: 'Both parties signed — Agreement on file',
                pending_ceo_signature: 'Awaiting CEO co-signature',
                pending_user_signature: 'Awaiting your signature',
            };
            statusEl.textContent = statusMap[CONTRACT_STATUS] || 'Status unknown';
            if (CONTRACT_STATUS === 'completed' && btnEl) btnEl.classList.remove('hidden');
        }

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
                document.getElementById('acc_total_revenue').textContent = '₦' + Number(stats.total_revenue || 0).toLocaleString('en-NG');
                document.getElementById('acc_monthly_revenue').textContent = '₦' + Number(stats.monthly_revenue || 0).toLocaleString('en-NG');
                document.getElementById('acc_tenants').textContent = stats.active_tenants || 0;
                const payments = await fetchData('/admin/api/payments');
                const typeColors = {rent:'bg-blue-900/50 text-blue-300', deposit:'bg-purple-900/50 text-purple-300', fee:'bg-amber-900/50 text-amber-300', other:'bg-gray-700 text-gray-300'};
                document.getElementById('acc_paymentsContainer').innerHTML = payments.length ? payments.slice(0, 20).map(p => `
                    <div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4">
                        <div class="flex items-start justify-between gap-3 mb-2">
                            <div>
                                <p class="font-semibold text-white text-sm">${p.tenant_name || '—'}</p>
                                ${p.description ? `<p class="text-xs text-gray-400 mt-0.5">${p.description}</p>` : ''}
                            </div>
                            <p class="text-emerald-400 font-bold text-base flex-shrink-0">₦${Number(p.amount).toLocaleString('en-NG')}</p>
                        </div>
                        <div class="flex items-center gap-3 flex-wrap text-xs">
                            <span class="px-2.5 py-1 rounded-full ${typeColors[p.payment_type] || 'bg-gray-700 text-gray-300'}">${p.payment_type}</span>
                            <span class="text-gray-400">${p.payment_date}</span>
                            ${p.recorded_by ? `<span class="text-gray-500">by ${p.recorded_by}</span>` : ''}
                        </div>
                    </div>`).join('') : '<p class="text-gray-400 py-6 text-center text-sm">No payments recorded yet</p>';
            } catch (e) {
                document.getElementById('acc_paymentsContainer').innerHTML = '<p class="text-red-400 py-4 text-sm">Error loading financial data</p>';
            }
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

        function renderInvestorProgressGraph(updates) {
            const graphEl = document.getElementById('investorProgressGraph');
            const headlineEl = document.getElementById('invProgressHeadline');
            if (!graphEl) return;
            if (!updates.length) {
                if (headlineEl) headlineEl.textContent = '0%';
                graphEl.innerHTML = '<p class="text-sm text-gray-500">Construction updates will appear here once management posts them.</p>';
                return;
            }
            const latest = updates[updates.length - 1];
            if (headlineEl) headlineEl.textContent = `${latest.progress_percentage}%`;
            if (updates.length === 1) {
                graphEl.innerHTML = `<div class="space-y-3"><div class="w-full bg-gray-700 rounded-full h-3"><div class="bg-emerald-500 h-3 rounded-full" style="width:${latest.progress_percentage}%"></div></div><div class="flex justify-between text-xs text-gray-400"><span>${latest.title}</span><span>${latest.progress_percentage}%</span></div></div>`;
                return;
            }
            const width = 640;
            const height = 180;
            const points = updates.map((item, idx) => {
                const x = 24 + (idx * ((width - 48) / Math.max(updates.length - 1, 1)));
                const y = height - 24 - ((item.progress_percentage / 100) * (height - 48));
                return { x, y };
            });
            graphEl.innerHTML = `<div class="overflow-x-auto"><svg viewBox="0 0 ${width} ${height}" class="w-full min-w-[560px]"><line x1="24" y1="${height - 24}" x2="${width - 24}" y2="${height - 24}" stroke="#4b5563" stroke-width="1"></line><line x1="24" y1="24" x2="24" y2="${height - 24}" stroke="#4b5563" stroke-width="1"></line><polyline fill="none" stroke="#10b981" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" points="${points.map(p => `${p.x},${p.y}`).join(' ')}"></polyline>${points.map(p => `<circle cx="${p.x}" cy="${p.y}" r="5" fill="#111827" stroke="#10b981" stroke-width="3"></circle>`).join('')}</svg></div><div class="grid grid-cols-1 md:grid-cols-2 gap-3 mt-4">${updates.slice().reverse().map(item => `<div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-3"><div class="flex justify-between gap-3"><p class="font-medium text-white text-sm">${item.title}</p><span class="text-xs text-emerald-400 font-semibold">${item.progress_percentage}%</span></div><p class="text-xs text-gray-400 mt-1">${item.happened_on || 'Date pending'}</p>${item.notes ? `<p class="text-sm text-gray-300 mt-2">${item.notes}</p>` : ''}</div>`).join('')}</div>`;
        }

        function renderUnitsTable(targetId, units, showProperty = false) {
            const el = document.getElementById(targetId);
            if (!el) return;
            const statusClasses = { available: 'bg-emerald-900/50 text-emerald-300', reserved: 'bg-amber-900/50 text-amber-300', occupied: 'bg-blue-900/50 text-blue-300', maintenance: 'bg-red-900/50 text-red-300' };
            if (!units.length) {
                el.innerHTML = `<tr><td colspan="${showProperty ? 5 : 4}" class="text-gray-400 py-3 text-center">No units found</td></tr>`;
                return;
            }
            el.innerHTML = units.map(unit => `<tr class="border-b border-gray-700">${showProperty ? `<td class="py-2 pr-3 text-xs text-gray-300">${unit.property_title}</td>` : ''}<td class="py-2 pr-3 font-medium text-white">${unit.unit_code}</td><td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded-full ${statusClasses[unit.status] || 'bg-gray-700 text-gray-300'}">${unit.status}</span></td><td class="py-2 pr-3 text-xs text-gray-300">${unit.monthly_rent ? formatNGN(unit.monthly_rent) : '—'}</td><td class="py-2 text-xs text-gray-400">${unit.notes || '—'}</td></tr>`).join('');
        }

        function populateManagerUnitSelect(units) {
            const select = document.getElementById('mgrTenantUnit');
            if (!select) return;
            select.innerHTML = '<option value="">Select unit</option>' + units.filter(unit => unit.status === 'available' || unit.status === 'reserved').map(unit => `<option value="${unit.unit_code}">${unit.unit_code} • ${unit.status}</option>`).join('');
        }

        let roleTenantMap = {};

        function renderTenantCards(targetId, tenants) {
            const el = document.getElementById(targetId);
            if (!el) return;
            roleTenantMap = Object.fromEntries(tenants.map(t => [String(t.id), t]));
            if (!tenants.length) {
                el.innerHTML = '<p class="text-gray-400 py-4 text-center text-sm">No tenants yet</p>';
                return;
            }
            el.innerHTML = tenants.map(t => `<div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4"><div class="flex items-start justify-between gap-3"><div><p class="font-semibold text-white text-sm">${t.name}</p><p class="text-xs text-gray-400 mt-0.5">${t.property_name || 'Property pending'}${t.unit_number ? ' • ' + t.unit_number : ''}</p>${t.phone ? `<p class="text-xs text-gray-500 mt-0.5">${t.phone}</p>` : ''}</div><div class="flex gap-2"><button onclick="editRoleTenant(${t.id})" class="text-xs text-blue-400 hover:text-blue-300 border border-blue-800 px-3 py-1 rounded-lg">Edit</button><button onclick="vacateRoleTenant(${t.id})" class="text-xs text-red-400 hover:text-red-300 border border-red-800 px-3 py-1 rounded-lg">Mark Vacated</button></div></div></div>`).join('');
        }

        function editRoleTenant(id) {
            const tenant = roleTenantMap[String(id)];
            if (!tenant) return;
            document.getElementById('mgrTenantEditId').value = tenant.id;
            document.getElementById('mgrTenantName').value = tenant.name || '';
            document.getElementById('mgrTenantEmail').value = tenant.email || '';
            document.getElementById('mgrTenantPhone').value = tenant.phone || '';
            document.getElementById('mgrTenantProperty').value = tenant.property_name || 'BrightWave Phase 1 Hostel';
            const unitSelect = document.getElementById('mgrTenantUnit');
            if (tenant.unit_number && unitSelect && !Array.from(unitSelect.options).some(opt => opt.value === tenant.unit_number)) {
                const option = document.createElement('option');
                option.value = tenant.unit_number;
                option.textContent = `${tenant.unit_number} • occupied`;
                unitSelect.appendChild(option);
            }
            if (unitSelect) unitSelect.value = tenant.unit_number || '';
            document.getElementById('mgrTenantRent').value = tenant.monthly_rent || '';
            document.getElementById('mgrTenantLeaseStart').value = tenant.lease_start || '';
            document.getElementById('mgrTenantLeaseEnd').value = tenant.lease_end || '';
            document.getElementById('mgrTenantNotes').value = tenant.notes || '';
            document.getElementById('mgrTenantSubmit').textContent = 'Update Tenant';
            document.getElementById('mgrTenantCancelEdit').classList.remove('hidden');
        }

        async function loadRoleDocument(role) {
            const statusEl = document.getElementById('roleDocStatus_' + role);
            const btnEl = document.getElementById('viewRoleDocBtn_' + role);
            if (!statusEl) return;
            const statusMap = { completed: 'Both parties signed — Agreement on file', pending_ceo_signature: 'Awaiting CEO co-signature', pending_user_signature: 'Awaiting your signature' };
            statusEl.textContent = statusMap[CONTRACT_STATUS] || 'Status unknown';
            if (CONTRACT_STATUS === 'completed' && btnEl) btnEl.classList.remove('hidden');
        }

        async function loadInvestorDashboard() {
            try {
                const profile = await fetchData('/admin/api/my-investment');
                document.getElementById('investorLoading').classList.add('hidden');
                document.getElementById('investorDashboard').classList.add('hidden');
                document.getElementById('investorNoProfile').classList.add('hidden');
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
                const updates = (profile.construction_updates || []).sort((a, b) => (a.progress_percentage || 0) - (b.progress_percentage || 0));
                const latestProgress = updates.length ? updates[updates.length - 1].progress_percentage : 0;
                document.getElementById('invAmountDisplay').textContent = formatNGN(amount);
                document.getElementById('invDistributed').textContent = formatNGN(distributed);
                renderInvestorProgressGraph(updates);
                const badgeColor = type === 'DEBT' ? 'bg-blue-700' : 'bg-emerald-700';
                const summaryLabel = type === 'DEBT' ? `${roi}% Annual Return` : `${equity}% Project Equity`;
                document.getElementById('investmentSummaryCard').innerHTML = `<div class="flex justify-between items-start mb-4"><div><p class="text-xs text-gray-400 uppercase tracking-widest mb-1">Your Investment</p><p class="text-4xl font-bold text-white">${formatNGN(amount)}</p></div><span class="text-sm font-semibold px-3 py-1 rounded-full text-white ${badgeColor}">${type}</span></div><div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm"><div><p class="text-gray-400 text-xs">Return Structure</p><p class="text-white font-medium mt-0.5">${summaryLabel}</p></div><div><p class="text-gray-400 text-xs">Investment Date</p><p class="text-white font-medium mt-0.5">${profile.investment_date || 'Pending'}</p></div><div><p class="text-gray-400 text-xs">Project</p><p class="text-white font-medium mt-0.5">${profile.project_property_title || 'BrightWave Project'}</p></div><div><p class="text-gray-400 text-xs">Current Progress</p><p class="text-white font-medium mt-0.5">${latestProgress}%</p></div></div>`;
                const termYears = profile.investment_term_years || 4;
                if (type === 'DEBT') {
                    const annualReturn = amount * roi / 100;
                    const totalInterest = annualReturn * termYears;
                    document.getElementById('invExpectedReturn').textContent = formatNGN(amount + totalInterest);
                    document.getElementById('invReturnNote').textContent = `Principal ${formatNGN(amount)} + ${formatNGN(totalInterest)} interest over ${termYears} year${termYears !== 1 ? 's' : ''}`;
                } else {
                    document.getElementById('invExpectedReturn').textContent = `${equity}% ownership`;
                    document.getElementById('invReturnNote').textContent = 'Returns proportional to project revenue';
                }
                document.getElementById('projectTimeline').innerHTML = updates.length ? `<div class="mb-3"><div class="flex justify-between text-xs text-gray-400 mb-1"><span>Management-posted progress</span><span>${latestProgress}%</span></div><div class="w-full bg-gray-700 rounded-full h-3"><div class="timeline-bar bg-emerald-500 h-3 rounded-full" style="width: ${latestProgress}%"></div></div></div><div class="grid grid-cols-1 md:grid-cols-3 gap-3 mt-4">${updates.slice().reverse().slice(0, 3).map(update => `<div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4"><p class="text-xs text-gray-500">${update.happened_on || 'Pending date'}</p><p class="text-sm font-semibold text-white mt-1">${update.title}</p>${update.notes ? `<p class="text-sm text-gray-300 mt-2">${update.notes}</p>` : ''}</div>`).join('')}</div><div class="mt-3 text-xs text-gray-500 text-center">Progress is driven by updates posted from the CEO or Manager portal.</div>` : '<p class="text-gray-400 text-sm">Construction updates will appear once management starts posting milestones.</p>';
                const completion = profile.expected_completion_date ? new Date(profile.expected_completion_date) : null;
                const now = new Date();
                if (type === 'DEBT' && completion) {
                    const annualReturn = amount * roi / 100;
                    document.getElementById('distributionSchedule').innerHTML = Array.from({length: termYears}, (_, idx) => idx + 1).map(yr => { const paymentDate = new Date(completion); paymentDate.setFullYear(paymentDate.getFullYear() + yr); const isFinal = yr === termYears; const isPast = now > paymentDate; return `<div class="flex justify-between items-center py-3 border-b border-gray-700 last:border-0 text-sm"><div><span class="text-white">Year ${yr} Distribution</span>${isFinal ? `<span class="text-xs text-emerald-400 ml-2">+ principal</span>` : ''}</div><div class="text-right"><p class="font-medium text-white">${formatNGN(isFinal ? annualReturn + amount : annualReturn)}</p><p class="text-xs text-gray-400">${paymentDate.toLocaleDateString('en-GB', {month:'short',year:'numeric'})}</p></div><span class="text-xs ${isPast ? 'text-emerald-400' : 'text-yellow-400'} font-medium">${isPast ? 'Due' : 'Scheduled'}</span></div>`; }).join('');
                } else if (type === 'EQUITY') {
                    document.getElementById('distributionSchedule').innerHTML = `<p class="text-gray-400 text-sm">Equity distributions are paid from project performance after completion. Management will keep the construction graph and milestone feed current here.</p>`;
                } else {
                    document.getElementById('distributionSchedule').innerHTML = `<p class="text-gray-400 text-sm">Distribution schedule will be available once the expected completion date is set by the CEO.</p>`;
                }
                const docStatusMap = { completed: 'Both parties signed — Agreement on file', pending_ceo_signature: 'Awaiting CEO co-signature', pending_user_signature: 'Awaiting your signature' };
                document.getElementById('docStatus').textContent = docStatusMap[CONTRACT_STATUS] || 'Status unknown';
                if (CONTRACT_STATUS === 'completed') document.getElementById('viewAgreementBtn')?.classList.remove('hidden');
            } catch (e) {
                document.getElementById('investorLoading').textContent = 'Error loading investment data. Please refresh.';
            }
        }

        async function loadManagerDashboard() {
            try {
                const [stats, inquiries, props, units, tenants] = await Promise.all([fetchData('/admin/api/stats'), fetchData('/admin/api/inquiries'), fetchData('/admin/api/properties'), fetchData('/admin/api/units'), fetchData('/admin/api/tenants')]);
                document.getElementById('mgr_properties').textContent = stats.active_properties || 0;
                document.getElementById('mgr_available_units').textContent = stats.available_units || 0;
                const openCount = stats.new_inquiries || stats.total_inquiries || 0;
                document.getElementById('mgr_inquiries').textContent = openCount;
                document.getElementById('mgr_active_tenants').textContent = stats.active_tenants || 0;
                // Inquiries badge on tab
                const badge = document.getElementById('mgrInquiriesBadge');
                if (badge) { badge.textContent = openCount; badge.classList.toggle('hidden', openCount === 0); }
                // Inquiries table with status dropdown
                const inqStatuses = ['new','contacted','qualified','converted','closed'];
                document.getElementById('mgr_inquiriesTable').innerHTML = inquiries.map(i => `
                    <tr class="border-b border-gray-700">
                        <td class="py-2 pr-3 font-medium">${i.full_name}</td>
                        <td class="py-2 pr-3 text-gray-400 text-xs">${i.property_title}</td>
                        <td class="py-2 pr-3 text-xs">${i.inquiry_type}</td>
                        <td class="py-2 pr-3">
                            <select onchange="updateInquiry(${i.id}, this.value)" class="bg-gray-700 text-white text-xs px-2 py-1 rounded border border-gray-600 w-full">
                                ${inqStatuses.map(s => `<option value="${s}" ${i.status === s ? 'selected' : ''}>${s.charAt(0).toUpperCase()+s.slice(1)}</option>`).join('')}
                            </select>
                        </td>
                        <td class="py-2 text-xs text-gray-500 whitespace-nowrap">${new Date(i.created_at).toLocaleDateString()}</td>
                    </tr>`).join('') || '<tr><td colspan="5" class="text-gray-400 py-3 text-center">No inquiries yet</td></tr>';
                document.getElementById('mgr_propertiesTable').innerHTML = props.map(p => `<tr class="border-b border-gray-700"><td class="py-2 pr-3 font-medium">${p.title}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.property_type}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.location}</td><td class="py-2"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${p.construction_status || p.status}</span></td></tr>`).join('');
                // Units table with status action column
                const phase1Units = units.filter(unit => unit.property_title === 'BrightWave Phase 1 Hostel');
                const statusClasses = { available: 'bg-emerald-900/50 text-emerald-300', occupied: 'bg-blue-900/50 text-blue-300', reserved: 'bg-amber-900/50 text-amber-300', maintenance: 'bg-red-900/50 text-red-300' };
                const unitTableEl = document.getElementById('mgr_unitsTable');
                if (unitTableEl) unitTableEl.innerHTML = phase1Units.map(u => `
                    <tr class="border-b border-gray-700">
                        <td class="py-2 pr-3 font-medium text-white">${u.unit_code}</td>
                        <td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded-full ${statusClasses[u.status] || 'bg-gray-700 text-gray-300'}">${u.status}</span></td>
                        <td class="py-2 pr-3 text-xs text-gray-300">${u.monthly_rent ? formatNGN(u.monthly_rent) : '—'}</td>
                        <td class="py-2 pr-3 text-xs text-gray-400">${u.notes || '—'}</td>
                        <td class="py-2">
                            <select onchange="updateMgrUnitStatus(${u.id}, this.value)" class="bg-gray-700 text-white text-xs px-2 py-1 rounded border border-gray-600">
                                ${['available','occupied','reserved','maintenance'].map(s => `<option value="${s}" ${u.status === s ? 'selected' : ''}>${s.charAt(0).toUpperCase()+s.slice(1)}</option>`).join('')}
                            </select>
                        </td>
                    </tr>`).join('') || '<tr><td colspan="5" class="text-gray-500 py-3 text-center text-xs">No units found</td></tr>';
                populateManagerUnitSelect(phase1Units);
                renderTenantCards('mgr_tenantsList', tenants.filter(t => t.status === 'active'));
            } catch (e) { console.error('Manager dashboard error', e); }
        }

        async function loadAccountantDashboard() {
            try {
                const [stats, payments, tenants] = await Promise.all([fetchData('/admin/api/stats'), fetchData('/admin/api/payments'), fetchData('/admin/api/tenants?status=active')]);
                document.getElementById('acc_total_revenue').textContent = formatNGN(stats.total_revenue || 0);
                document.getElementById('acc_monthly_revenue').textContent = formatNGN(stats.monthly_revenue || 0);
                document.getElementById('acc_tenants').textContent = stats.active_tenants || 0;
                const typeColors = { rent: 'bg-blue-900/50 text-blue-300', deposit: 'bg-purple-900/50 text-purple-300', fee: 'bg-amber-900/50 text-amber-300', other: 'bg-gray-700 text-gray-300' };
                document.getElementById('acc_paymentsContainer').innerHTML = payments.length ? payments.slice(0, 20).map(p => `<div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4"><div class="flex items-start justify-between gap-3 mb-2"><div><p class="font-semibold text-white text-sm">${p.tenant_name || '—'}</p>${p.description ? `<p class="text-xs text-gray-400 mt-0.5">${p.description}</p>` : ''}</div><p class="text-emerald-400 font-bold text-base flex-shrink-0">${formatNGN(p.amount)}</p></div><div class="flex items-center gap-3 flex-wrap text-xs"><span class="px-2.5 py-1 rounded-full ${typeColors[p.payment_type] || 'bg-gray-700 text-gray-300'}">${p.payment_type}</span><span class="text-gray-400">${p.payment_date}</span>${p.recorded_by ? `<span class="text-gray-500">by ${p.recorded_by}</span>` : ''}</div></div>`).join('') : '<p class="text-gray-400 py-6 text-center text-sm">No payments recorded yet</p>';
                document.getElementById('accUnitsSummary').innerHTML = `<div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4"><p class="text-xs text-gray-500 uppercase tracking-wide">Available Units</p><p class="text-2xl font-bold text-emerald-400 mt-1">${stats.available_units || 0}</p></div><div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4"><p class="text-xs text-gray-500 uppercase tracking-wide">Occupied Units</p><p class="text-2xl font-bold text-blue-400 mt-1">${stats.occupied_units || 0}</p></div><div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4"><p class="text-xs text-gray-500 uppercase tracking-wide">Active Tenants</p><p class="text-2xl font-bold text-white mt-1">${tenants.length}</p></div>`;
                const tenantSelect = document.getElementById('accPaymentTenant');
                if (tenantSelect) tenantSelect.innerHTML = '<option value="">Select tenant</option>' + tenants.map(t => `<option value="${t.id}">${t.name}${t.unit_number ? ' • ' + t.unit_number : ''}</option>`).join('');
            } catch (e) {
                document.getElementById('acc_paymentsContainer').innerHTML = '<p class="text-red-400 py-4 text-sm">Error loading financial data</p>';
            }
        }

        async function loadRealtorDashboard() {
            try {
                const [stats, props, inquiries, units] = await Promise.all([fetchData('/admin/api/stats'), fetchData('/admin/api/properties'), fetchData('/admin/api/inquiries'), fetchData('/admin/api/units')]);
                document.getElementById('rel_properties').textContent = stats.active_properties || 0;
                document.getElementById('rel_available_units').textContent = stats.available_units || 0;
                document.getElementById('rel_inquiries').textContent = stats.total_inquiries || 0;
                document.getElementById('rel_new').textContent = stats.new_inquiries || 0;
                renderUnitsTable('rel_unitsTable', units.filter(unit => unit.status === 'available' || unit.status === 'reserved'), true);
                document.getElementById('rel_propertiesTable').innerHTML = props.map(p => `<tr class="border-b border-gray-700"><td class="py-2 pr-3 font-medium">${p.title}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.property_type}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.location}</td><td class="py-2 pr-3 text-xs">${p.price ? formatNGN(p.price) : (p.price_type || 'Contact')}</td><td class="py-2"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${p.construction_status || p.status}</span></td></tr>`).join('');
                document.getElementById('rel_inquiriesTable').innerHTML = inquiries.slice(0, 10).map(i => `<tr class="border-b border-gray-700"><td class="py-2 pr-3">${i.full_name}</td><td class="py-2 pr-3 text-gray-400 text-xs">${i.property_title}</td><td class="py-2 pr-3 text-xs">${i.inquiry_type}</td><td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${i.status}</span></td><td class="py-2 text-xs text-gray-500">${new Date(i.created_at).toLocaleDateString()}</td></tr>`).join('') || '<tr><td colspan="5" class="text-gray-400 py-3 text-center">No leads yet</td></tr>';
            } catch (e) {}
        }

        async function vacateRoleTenant(id) {
            if (!confirm('Mark this tenant as vacated?')) return;
            await fetchData('/admin/api/tenants/' + id, { method: 'DELETE' });
            await loadManagerDashboard();
            if (ALL_ROLES.includes('ACCOUNTANT')) await loadAccountantDashboard();
            if (ALL_ROLES.includes('REALTOR')) await loadRealtorDashboard();
        }

        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('managerTenantForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                const msgEl = document.getElementById('mgrTenantMsg');
                const editId = document.getElementById('mgrTenantEditId').value;
                const payload = {
                    name: document.getElementById('mgrTenantName').value,
                    email: document.getElementById('mgrTenantEmail').value,
                    phone: document.getElementById('mgrTenantPhone').value,
                    property_name: document.getElementById('mgrTenantProperty').value,
                    unit_number: document.getElementById('mgrTenantUnit').value,
                    monthly_rent: document.getElementById('mgrTenantRent').value,
                    lease_start: document.getElementById('mgrTenantLeaseStart').value,
                    lease_end: document.getElementById('mgrTenantLeaseEnd').value,
                    status: 'active',
                    notes: document.getElementById('mgrTenantNotes').value
                };
                try {
                    const res = await fetchData(editId ? ('/admin/api/tenants/' + editId) : '/admin/api/tenants', { method: editId ? 'PUT' : 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                    msgEl.textContent = res.message || 'Tenant saved';
                    msgEl.className = 'text-sm text-emerald-400';
                    document.getElementById('managerTenantForm').reset();
                    document.getElementById('mgrTenantProperty').value = 'BrightWave Phase 1 Hostel';
                    document.getElementById('mgrTenantEditId').value = '';
                    document.getElementById('mgrTenantSubmit').textContent = 'Save Tenant';
                    document.getElementById('mgrTenantCancelEdit').classList.add('hidden');
                    await loadManagerDashboard();
                } catch (err) {
                    msgEl.textContent = err.message || 'Error saving tenant';
                    msgEl.className = 'text-sm text-red-400';
                }
            });
            document.getElementById('mgrTenantCancelEdit')?.addEventListener('click', () => {
                document.getElementById('managerTenantForm').reset();
                document.getElementById('mgrTenantEditId').value = '';
                document.getElementById('mgrTenantProperty').value = 'BrightWave Phase 1 Hostel';
                document.getElementById('mgrTenantSubmit').textContent = 'Save Tenant';
                document.getElementById('mgrTenantCancelEdit').classList.add('hidden');
            });
            document.getElementById('accountantPaymentForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                const msgEl = document.getElementById('accPaymentMsg');
                try {
                    const res = await fetchData('/admin/api/payments', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ tenant_id: document.getElementById('accPaymentTenant').value || null, tenant_name: document.getElementById('accPaymentTenantName').value, amount: document.getElementById('accPaymentAmount').value, payment_date: document.getElementById('accPaymentDate').value, payment_type: document.getElementById('accPaymentType').value, description: document.getElementById('accPaymentDesc').value }) });
                    msgEl.textContent = res.message || 'Payment recorded';
                    msgEl.className = 'text-sm text-emerald-400';
                    document.getElementById('accountantPaymentForm').reset();
                    await loadAccountantDashboard();
                } catch (err) {
                    msgEl.textContent = err.message || 'Error recording payment';
                    msgEl.className = 'text-sm text-red-400';
                }
            });
            document.getElementById('managerConstructionForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                await submitConstructionUpdate('manager');
            });
        });

        loadConstructionPropertyOptions = async function() {
            try {
                const props = await fetchData('/admin/api/properties');
                const options = props.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                const ceoSel = document.getElementById('ceoConstructionProperty');
                const mgrSel = document.getElementById('mgrConstructionProperty');
                const ceoCurrent = ceoSel?.value || '';
                const mgrCurrent = mgrSel?.value || '';
                if (ceoSel) {
                    ceoSel.innerHTML = options;
                    ceoSel.value = ceoCurrent && props.some(p => String(p.id) === ceoCurrent) ? ceoCurrent : (props[0] ? String(props[0].id) : '');
                }
                if (mgrSel) {
                    mgrSel.innerHTML = options;
                    mgrSel.value = mgrCurrent && props.some(p => String(p.id) === mgrCurrent) ? mgrCurrent : (props[0] ? String(props[0].id) : '');
                }
            } catch (e) {}
        };

        function getConstructionSortValue(item) {
            const happened = item.happened_on ? new Date(item.happened_on).getTime() : 0;
            const created = item.created_at ? new Date(item.created_at).getTime() : 0;
            return Math.max(happened || 0, created || 0, 0);
        }

        function getConstructionLatestItem(items) {
            return items.slice().sort((a, b) => {
                const timeDiff = getConstructionSortValue(b) - getConstructionSortValue(a);
                if (timeDiff !== 0) return timeDiff;
                return (b.progress_percentage || 0) - (a.progress_percentage || 0);
            })[0] || null;
        }

        renderConstructionUpdates = function(items, listId, headlineId) {
            const listEl = document.getElementById(listId);
            const headlineEl = document.getElementById(headlineId);
            if (!listEl) return;
            const latest = getConstructionLatestItem(items);
            if (headlineEl) headlineEl.textContent = latest ? `${latest.progress_percentage}%` : '0%';
            if (!items.length) {
                listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-4">No construction updates yet.</p>';
                return;
            }
            const sortedItems = items.slice().sort((a, b) => {
                const timeDiff = getConstructionSortValue(b) - getConstructionSortValue(a);
                if (timeDiff !== 0) return timeDiff;
                return (b.progress_percentage || 0) - (a.progress_percentage || 0);
            });
            listEl.innerHTML = `
                <div class="bg-gradient-to-br from-emerald-900/40 via-slate-800 to-gray-900 border border-emerald-700/40 rounded-2xl p-5">
                    <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
                        <div>
                            <p class="text-xs uppercase tracking-[0.24em] text-emerald-300/80 mb-2">Current Site Status</p>
                            <h4 class="text-xl font-semibold text-white">${latest?.title || 'Latest update'}</h4>
                            <p class="text-sm text-gray-400 mt-1">${latest?.property_title || 'Selected project'}${latest?.happened_on ? ' • ' + latest.happened_on : ''}</p>
                        </div>
                        <div class="text-left md:text-right">
                            <p class="text-3xl font-bold text-emerald-400">${latest?.progress_percentage || 0}%</p>
                            <p class="text-xs text-gray-500 mt-1">Latest recorded progress</p>
                        </div>
                    </div>
                    <div class="mt-4 h-3 rounded-full bg-gray-700 overflow-hidden">
                        <div class="h-3 rounded-full bg-gradient-to-r from-emerald-500 to-teal-400" style="width:${latest?.progress_percentage || 0}%"></div>
                    </div>
                    ${latest?.notes ? `<p class="text-sm text-gray-300 leading-6 mt-4">${latest.notes}</p>` : ''}
                </div>
                <div class="space-y-3 mt-4">
                    ${sortedItems.map((item, idx) => `
                        <div class="relative bg-gray-700/30 border ${idx === 0 ? 'border-emerald-600/60' : 'border-gray-600/40'} rounded-2xl p-4 pl-5">
                            <div class="absolute left-0 top-4 bottom-4 w-1 rounded-full ${idx === 0 ? 'bg-emerald-500' : 'bg-gray-600'}"></div>
                            <div class="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
                                <div class="min-w-0">
                                    <div class="flex items-center gap-2 flex-wrap mb-1">
                                        <p class="font-semibold text-white text-sm">${item.title}</p>
                                        ${idx === 0 ? '<span class="text-[10px] uppercase tracking-widest px-2 py-1 rounded-full bg-emerald-900/70 text-emerald-300 border border-emerald-700/40">Latest</span>' : ''}
                                    </div>
                                    <p class="text-xs text-gray-400">${item.property_title}${item.happened_on ? ' • ' + item.happened_on : ''}</p>
                                </div>
                                <div class="flex items-center gap-3 sm:flex-col sm:items-end sm:gap-1 flex-shrink-0">
                                    <span class="text-lg font-semibold text-emerald-400">${item.progress_percentage}%</span>
                                    <span class="text-[11px] text-gray-500">Progress</span>
                                    ${USER_ROLE === 'CEO' ? `<button onclick="deleteConstructionUpdate(${item.id})" class="text-[10px] text-red-400 hover:text-red-300 border border-red-800/50 rounded px-2 py-0.5 mt-1 transition-colors">Delete</button>` : ''}
                                </div>
                            </div>
                            ${item.notes ? `<p class="text-sm text-gray-300 leading-6 mt-3">${item.notes}</p>` : ''}
                        </div>
                    `).join('')}
                </div>
            `;
        };

        loadConstructionUpdates = async function(propertyId) {
            try {
                const ceoSel = document.getElementById('ceoConstructionProperty');
                const mgrSel = document.getElementById('mgrConstructionProperty');
                const selectedId = propertyId || ceoSel?.value || mgrSel?.value || '';
                const query = selectedId ? ('?property_id=' + selectedId) : '';
                const updates = await fetchData('/admin/api/construction-updates' + query);
                renderConstructionUpdates(updates, 'ceoConstructionList', 'ceoConstructionHeadline');
                renderConstructionUpdates(updates, 'mgrConstructionList', 'mgrConstructionProgress');
            } catch (e) {
                const ceoList = document.getElementById('ceoConstructionList');
                const mgrList = document.getElementById('mgrConstructionList');
                if (ceoList) ceoList.innerHTML = '<p class="text-red-400 text-sm py-4">Error loading construction updates.</p>';
                if (mgrList) mgrList.innerHTML = '<p class="text-red-400 text-sm py-4">Error loading construction updates.</p>';
            }
        };

        const baseSubmitConstructionUpdate = submitConstructionUpdate;
        submitConstructionUpdate = async function(source) {
            const propertyId = source === 'ceo'
                ? document.getElementById('ceoConstructionProperty')?.value
                : document.getElementById('mgrConstructionProperty')?.value;
            await baseSubmitConstructionUpdate(source);
            if (source === 'ceo') {
                const ceoSel = document.getElementById('ceoConstructionProperty');
                if (ceoSel && propertyId) ceoSel.value = propertyId;
            } else {
                const mgrSel = document.getElementById('mgrConstructionProperty');
                if (mgrSel && propertyId) mgrSel.value = propertyId;
            }
            await loadConstructionUpdates(propertyId);
        };

        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('ceoConstructionProperty')?.addEventListener('change', (e) => loadConstructionUpdates(e.target.value));
            document.getElementById('mgrConstructionProperty')?.addEventListener('change', (e) => loadConstructionUpdates(e.target.value));
        });

        async function deleteConstructionUpdate(id) {
            if (!confirm('Delete this construction update? This cannot be undone.')) return;
            try {
                await fetchData('/admin/api/construction-updates/' + id, { method: 'DELETE' });
                const sel = document.getElementById('ceoConstructionProperty') || document.getElementById('mgrConstructionProperty');
                await loadConstructionUpdates(sel?.value || '');
            } catch (e) {
                alert('Error deleting update. Please try again.');
            }
        }

        function showMgrTab(tabId) {
            const tabs = ['mgrTabOverview', 'mgrTabUnits', 'mgrTabInquiries', 'mgrTabConstruction'];
            tabs.forEach(t => {
                const el = document.getElementById(t);
                if (el) el.classList.toggle('hidden', t !== tabId);
            });
            document.querySelectorAll('.mgr-tab-btn').forEach(btn => {
                const active = btn.dataset.tab === tabId;
                btn.classList.toggle('bg-slate-700', active);
                btn.classList.toggle('text-white', active);
                btn.classList.toggle('text-gray-400', !active);
            });
            if (tabId === 'mgrTabConstruction') {
                loadConstructionPropertyOptions();
                const mgrSel = document.getElementById('mgrConstructionProperty');
                loadConstructionUpdates(mgrSel?.value || '');
            }
        }

        async function updateMgrUnitStatus(unitId, newStatus) {
            try {
                await fetchData('/admin/api/units/' + unitId, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ status: newStatus })
                });
                await loadManagerDashboard();
            } catch (e) {
                alert('Error updating unit status.');
            }
        }

        async function fetchContractForRole(role) {
            return fetchData('/admin/api/my-contract?role=' + encodeURIComponent(role));
        }

        loadRoleDocument = async function(role) {
            const statusEl = document.getElementById('roleDocStatus_' + role);
            const btnEl = document.getElementById('viewRoleDocBtn_' + role);
            if (!statusEl) return;
            const statusMap = {
                completed: 'Both parties signed - Agreement on file',
                pending_ceo_signature: 'Awaiting CEO co-signature',
                pending_user_signature: 'Awaiting your signature'
            };
            try {
                const data = await fetchContractForRole(role);
                statusEl.textContent = statusMap[data.status] || 'Status unknown';
                if (btnEl) btnEl.classList.toggle('hidden', data.status !== 'completed');
            } catch (e) {
                statusEl.textContent = 'Agreement unavailable';
                if (btnEl) btnEl.classList.add('hidden');
            }
        };

        const baseLoadInvestorDashboard = loadInvestorDashboard;
        loadInvestorDashboard = async function() {
            await baseLoadInvestorDashboard();
            const docStatusEl = document.getElementById('docStatus');
            const viewBtn = document.getElementById('viewAgreementBtn');
            if (!docStatusEl) return;
            const statusMap = {
                completed: 'Both parties signed - Agreement on file',
                pending_ceo_signature: 'Awaiting CEO co-signature',
                pending_user_signature: 'Awaiting your signature'
            };
            try {
                const data = await fetchContractForRole('INVESTOR');
                docStatusEl.textContent = statusMap[data.status] || 'Status unknown';
                if (viewBtn) viewBtn.classList.toggle('hidden', data.status !== 'completed');
            } catch (e) {
                docStatusEl.textContent = 'Agreement unavailable';
                if (viewBtn) viewBtn.classList.add('hidden');
            }
        };

        async function viewMyContract(role = activeRole) {
            try { const data = await fetchContractForRole(role || activeRole); showContractModal(data); }
            catch (e) { alert('Could not load agreement. Please try again.'); }
        }

        function showContractModal(data) {
            document.getElementById('cvModalTitle').textContent = data.title || 'Agreement';
            document.getElementById('cvModalBody').textContent = data.body || '';
            document.getElementById('cvUserSig').textContent = data.user_signature || 'Not yet signed';
            document.getElementById('cvUserDate').textContent = data.user_signed_at ? 'Signed ' + data.user_signed_at : '';
            document.getElementById('cvCeoSig').textContent = data.ceo_signature || 'Awaiting CEO';
            document.getElementById('cvCeoDate').textContent = data.ceo_signed_at ? 'Signed ' + data.ceo_signed_at : '';
            const statusMap = {
                completed: 'Both parties have signed — legally binding agreement on file',
                pending_ceo_signature: 'Awaiting CEO co-signature',
                pending_user_signature: 'Awaiting your signature'
            };
            document.getElementById('cvStatus').textContent = statusMap[data.status] || data.status || '';
            const modal = document.getElementById('contractViewModal');
            modal.classList.remove('hidden');
            modal.classList.add('flex');
        }

        function closeContractModal() {
            const modal = document.getElementById('contractViewModal');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js?v=4').catch(() => {});
        }
    </script>

    <!-- CONTRACT VIEW MODAL -->
    <div id="contractViewModal" class="fixed inset-0 bg-black bg-opacity-80 z-[100] hidden items-center justify-center p-4" onclick="if(event.target===this)closeContractModal()">
        <div class="bg-gray-800 rounded-2xl shadow-2xl max-w-2xl w-full max-h-[90vh] flex flex-col">
            <div class="p-5 border-b border-gray-700 flex justify-between items-start flex-shrink-0">
                <div>
                    <p class="text-xs text-emerald-400 uppercase tracking-wide font-medium">Signed Agreement</p>
                    <h3 id="cvModalTitle" class="text-lg font-bold text-white mt-0.5"></h3>
                </div>
                <button onclick="closeContractModal()" class="text-gray-400 hover:text-white p-1 ml-4 flex-shrink-0">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                </button>
            </div>
            <div id="cvModalBody" class="contract-scroll p-6 overflow-y-auto text-sm text-gray-300 leading-relaxed whitespace-pre-line" style="flex:1;max-height:45vh;"></div>
            <div class="p-5 border-t border-gray-700 flex-shrink-0 space-y-3">
                <div class="grid grid-cols-2 gap-3">
                    <div class="bg-gray-700 rounded-lg p-3">
                        <p class="text-xs text-gray-400 mb-1">Employee / Investor Signature</p>
                        <p id="cvUserSig" class="font-semibold text-white text-sm font-mono"></p>
                        <p id="cvUserDate" class="text-xs text-gray-500 mt-0.5"></p>
                    </div>
                    <div class="bg-gray-700 rounded-lg p-3">
                        <p class="text-xs text-gray-400 mb-1">CEO Signature &#183; BrightWave</p>
                        <p id="cvCeoSig" class="font-semibold text-emerald-400 text-sm font-mono"></p>
                        <p id="cvCeoDate" class="text-xs text-gray-500 mt-0.5"></p>
                    </div>
                </div>
                <p id="cvStatus" class="text-xs text-center text-gray-500 pt-1"></p>
            </div>
        </div>
    </div>

</body>
</html>
"""

if __name__ == '__main__':
    with app.app_context():
        ensure_runtime_state()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
