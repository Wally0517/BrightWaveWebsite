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
from sqlalchemy import case, inspect, text
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
EXPENSE_RECEIPT_FOLDER = 'assets/uploads/expense-receipts'
ALLOWED_RECEIPT_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf'}
HERO_BG_FOLDER = 'assets/images/site'
VIDEO_FOLDER = 'assets/videos/site'
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'webm', 'mov', 'ogg', 'm4v'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['EXPENSE_RECEIPT_FOLDER'] = EXPENSE_RECEIPT_FOLDER
app.config['HERO_BG_FOLDER'] = HERO_BG_FOLDER
app.config['VIDEO_FOLDER'] = VIDEO_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB (for video uploads)

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXPENSE_RECEIPT_FOLDER, exist_ok=True)
os.makedirs(HERO_BG_FOLDER, exist_ok=True)
os.makedirs(VIDEO_FOLDER, exist_ok=True)

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
    monthly_salary = db.Column(db.Float, default=0.0)  # flat fee for ACCOUNTANT / PA; 0 for commission-only roles
    has_seen_tour = db.Column(db.Boolean, default=False)  # first-login onboarding tour shown?

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
    capital_budget = db.Column(db.Float, nullable=True)
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
    inquiry_notes = db.Column(db.Text, nullable=True)
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
    draft_value = db.Column(db.Text, nullable=True)
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
    roi_rate = db.Column(db.Float, default=3.5)          # annual % for DEBT investors
    equity_percentage = db.Column(db.Float, nullable=True)  # % project ownership for EQUITY
    construction_start_date = db.Column(db.Date, nullable=True)
    expected_completion_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    total_distributed = db.Column(db.Float, default=0.0)
    investment_term_years = db.Column(db.Integer, nullable=True)  # e.g. 3, 5, 10
    property_id = db.Column(db.Integer, db.ForeignKey('property.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship('Admin', backref='investor_profile_rel', foreign_keys=[user_id])
    property = db.relationship('Property', foreign_keys=[property_id])

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


class ProjectExpense(db.Model):
    __tablename__ = 'project_expense'
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('property.id'), nullable=False)
    expense_date = db.Column(db.Date, nullable=False, default=date_type.today)
    category = db.Column(db.String(30), nullable=False, default='materials')
    item_name = db.Column(db.String(160), nullable=False)
    payee_name = db.Column(db.String(160), nullable=True)
    quantity = db.Column(db.Float, nullable=True)
    unit_cost = db.Column(db.Float, nullable=True)
    amount = db.Column(db.Float, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    receipt_path = db.Column(db.String(255), nullable=True)
    approval_status = db.Column(db.String(30), nullable=False, default='pending')
    approval_note = db.Column(db.Text, nullable=True)
    approved_by = db.Column(db.String(80), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    recorded_by = db.Column(db.String(80), nullable=True)
    is_paid = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    property = db.relationship('Property', backref='expenses')


class VendorContact(db.Model):
    __tablename__ = 'vendor_contact'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), unique=True, nullable=False)
    contact_type = db.Column(db.String(30), nullable=False, default='supplier')
    phone = db.Column(db.String(40), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class MaintenanceRecord(db.Model):
    __tablename__ = 'maintenance_record'
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('property.id'), nullable=False)
    maintenance_date = db.Column(db.Date, nullable=False, default=date_type.today)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(40), nullable=False, default='general')
    description = db.Column(db.Text, nullable=True)
    vendor_name = db.Column(db.String(160), nullable=True)
    cost = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='completed')
    recorded_by = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    property = db.relationship('Property', backref='maintenance_records')

class PropertyUnitType(db.Model):
    __tablename__ = 'property_unit_type'
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('property.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    annual_price = db.Column(db.Float, default=0.0)
    total_count = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    property = db.relationship('Property', backref='unit_types')

class Tenant(db.Model):
    __tablename__ = 'tenant'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    property_name = db.Column(db.String(150), nullable=True)
    unit_number = db.Column(db.String(30), nullable=True)
    unit_type_id = db.Column(db.Integer, db.ForeignKey('property_unit_type.id'), nullable=True)
    lease_start = db.Column(db.Date, nullable=True)
    lease_end = db.Column(db.Date, nullable=True)
    monthly_rent = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default='active')
    notes = db.Column(db.Text, nullable=True)
    serviced_by_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    unit_type = db.relationship('PropertyUnitType', backref='tenants', foreign_keys=[unit_type_id])
    serviced_by = db.relationship('Admin', foreign_keys=[serviced_by_id])

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


class PayrollPayment(db.Model):
    __tablename__ = 'payroll_payment'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=False)
    period_year = db.Column(db.Integer, nullable=False)
    period_month = db.Column(db.Integer, nullable=False)  # 1-12
    amount = db.Column(db.Float, nullable=False)
    kind = db.Column(db.String(20), nullable=False, default='commission')  # 'commission' | 'salary'
    source_tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    paid_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_by = db.Column(db.String(80), nullable=True)
    user = db.relationship('Admin', foreign_keys=[user_id])


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


class PendingSignup(db.Model):
    __tablename__ = 'pending_signup'
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    phone = db.Column(db.String(40), nullable=True)
    role = db.Column(db.String(30), nullable=False)  # INVESTOR | MANAGER | ACCOUNTANT | REALTOR | PA
    password_hash = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending | approved | rejected
    role_data = db.Column(db.JSON, default=dict)  # investor: {amount, type, term_years}; staff: {experience, availability}
    rejection_reason = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by = db.Column(db.String(80), nullable=True)
    created_admin_id = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=True)


DEFAULT_SITE_CONTENT = {
    'home.hero_badge': 'Trusted property for students, families, and investors',
    'home.hero_title': 'Property opportunities in Nigeria, presented with real proof and clear process.',
    'home.hero_subtitle': 'BrightWave Habitat Enterprise serves clients looking for live student accommodation, credible land opportunities, family homes, and long-term estate projects, starting with an active flagship already operating in Malete, Kwara State.',
    'home.about_intro': 'BrightWave Habitat Enterprise is a Nigerian property business focused on student accommodation, land opportunities, residential homes, and estate growth, with BrightWave Hostel Phase 1 in Malete as the first live proof of delivery.',
    'home.hero_bg_path': '',
    'home.video_url': '',
    'home.video_section_title': 'See BrightWave in Action',
    'home.video_section_enabled': 'false',
    'home.announcement_text': '',
    'home.announcement_enabled': 'false',
    'home.services_heading': 'Our Services',
    'home.services_lead': 'BrightWave operates across student accommodation, land transactions, homes, and estate development, with each offer presented according to its real stage and readiness.',
    'site.contact_email': 'brightwavehabitat@gmail.com',
    'site.contact_phone': '+234 803 766 9462',
    'site.contact_address': 'Malete, Kwara State, Nigeria',
    'site.footer_company_desc': 'Building trusted property opportunities across Nigeria with clarity, credibility, and a focus on real delivery.',
    'site.wa_number_hostel': '2348037669462',
    'site.wa_number_land': '2349038402914',
    'site.wa_number_home': '2349075381905',
    'site.social_facebook': 'https://www.facebook.com/share/19BvJAymc3/?mibextid=wwXIfr',
    'site.social_twitter': 'https://x.com/brightwave__?s=21',
    'site.social_instagram': 'https://www.instagram.com/brightwave___?igsh=MXN3NHAzdGU1a2ppaQ%3D&utm_-source=qr',
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


def allowed_receipt_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_RECEIPT_EXTENSIONS

def ensure_cms_baseline():
    """Create CMS tables and seed defaults lazily for existing deployments."""
    db.create_all()
    ensure_runtime_schema_updates()
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


def ensure_runtime_schema_updates():
    inspector = inspect(db.engine)
    dialect = db.engine.dialect.name
    text_type = 'TEXT'
    string_type = 'VARCHAR'
    timestamp_type = 'TIMESTAMP' if dialect == 'postgresql' else 'DATETIME'
    float_type = 'DOUBLE PRECISION' if dialect == 'postgresql' else 'REAL'

    property_columns = {column['name'] for column in inspector.get_columns('property')} if inspector.has_table('property') else set()
    if 'capital_budget' not in property_columns:
        db.session.execute(text(f'ALTER TABLE property ADD COLUMN capital_budget {float_type}'))

    expense_columns = {column['name'] for column in inspector.get_columns('project_expense')} if inspector.has_table('project_expense') else set()
    if expense_columns:
        if 'receipt_path' not in expense_columns:
            db.session.execute(text(f'ALTER TABLE project_expense ADD COLUMN receipt_path {string_type}(255)'))
        if 'approval_status' not in expense_columns:
            db.session.execute(text("ALTER TABLE project_expense ADD COLUMN approval_status VARCHAR(30) DEFAULT 'pending'"))
        if 'approval_note' not in expense_columns:
            db.session.execute(text(f'ALTER TABLE project_expense ADD COLUMN approval_note {text_type}'))
        if 'approved_by' not in expense_columns:
            db.session.execute(text(f'ALTER TABLE project_expense ADD COLUMN approved_by {string_type}(80)'))
        if 'approved_at' not in expense_columns:
            db.session.execute(text(f'ALTER TABLE project_expense ADD COLUMN approved_at {timestamp_type}'))
        db.session.execute(text("UPDATE project_expense SET approval_status = 'pending' WHERE approval_status IS NULL OR approval_status = ''"))
        if 'is_paid' not in expense_columns:
            db.session.execute(text('ALTER TABLE project_expense ADD COLUMN is_paid BOOLEAN NOT NULL DEFAULT 0'))

    investor_columns = {column['name'] for column in inspector.get_columns('investor_profile')} if inspector.has_table('investor_profile') else set()
    if investor_columns and 'property_id' not in investor_columns:
        db.session.execute(text('ALTER TABLE investor_profile ADD COLUMN property_id INTEGER REFERENCES property(id)'))

    inquiry_columns = {column['name'] for column in inspector.get_columns('property_inquiry')} if inspector.has_table('property_inquiry') else set()
    if inquiry_columns and 'inquiry_notes' not in inquiry_columns:
        db.session.execute(text('ALTER TABLE property_inquiry ADD COLUMN inquiry_notes TEXT'))

    # Rename legacy "Hostel" property titles to "Apartment" branding
    if inspector.has_table('property'):
        db.session.execute(text("UPDATE property SET title = 'BrightWave Phase 1 Apartment' WHERE title = 'BrightWave Phase 1 Hostel'"))
        db.session.execute(text("UPDATE property SET title = 'BrightWave Apartment Phase 2' WHERE title = 'BrightWave Hostel Phase 2'"))
        db.session.execute(text("UPDATE property SET title = 'BrightWave Apartment Phase 3' WHERE title = 'BrightWave Hostel Phase 3'"))

    # Clear placeholder capital_budget values from seed data on planning/pending properties
    # that have no recorded expenses — these were example values not set by the user
    if inspector.has_table('property') and inspector.has_table('project_expense'):
        db.session.execute(text("""
            UPDATE property SET capital_budget = NULL
            WHERE construction_status IN ('planning', 'pending', 'not_started')
            AND capital_budget IS NOT NULL
            AND id NOT IN (SELECT DISTINCT property_id FROM project_expense WHERE property_id IS NOT NULL)
        """))

    if not inspector.has_table('maintenance_record'):
        db.create_all()

    content_columns = {column['name'] for column in inspector.get_columns('site_content')} if inspector.has_table('site_content') else set()
    if content_columns and 'draft_value' not in content_columns:
        db.session.execute(text('ALTER TABLE site_content ADD COLUMN draft_value TEXT'))

    admin_columns = {column['name'] for column in inspector.get_columns('admin')} if inspector.has_table('admin') else set()
    if admin_columns and 'monthly_salary' not in admin_columns:
        db.session.execute(text(f'ALTER TABLE admin ADD COLUMN monthly_salary {float_type} DEFAULT 0'))
    if admin_columns and 'has_seen_tour' not in admin_columns:
        db.session.execute(text('ALTER TABLE admin ADD COLUMN has_seen_tour BOOLEAN DEFAULT 0'))

    tenant_columns = {column['name'] for column in inspector.get_columns('tenant')} if inspector.has_table('tenant') else set()
    if tenant_columns and 'serviced_by_id' not in tenant_columns:
        db.session.execute(text('ALTER TABLE tenant ADD COLUMN serviced_by_id INTEGER REFERENCES admin(id)'))

    if not inspector.has_table('payroll_payment'):
        db.create_all()

    if not inspector.has_table('pending_signup'):
        db.create_all()

    db.session.commit()

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


def serialize_payment_record(payment):
    return {
        'id': payment.id,
        'tenant_id': payment.tenant_id,
        'tenant_name': payment.tenant_name or '',
        'amount': payment.amount,
        'payment_date': payment.payment_date.isoformat() if payment.payment_date else '',
        'payment_type': payment.payment_type,
        'description': payment.description or '',
        'recorded_by': payment.recorded_by or '',
        'created_at': payment.created_at.strftime('%Y-%m-%d %H:%M') if payment.created_at else '',
    }


def serialize_project_expense(expense):
    return {
        'id': expense.id,
        'property_id': expense.property_id,
        'property_title': expense.property.title if expense.property else '',
        'expense_date': expense.expense_date.isoformat() if expense.expense_date else '',
        'category': expense.category,
        'item_name': expense.item_name,
        'payee_name': expense.payee_name or '',
        'quantity': expense.quantity,
        'unit_cost': expense.unit_cost,
        'amount': expense.amount,
        'notes': expense.notes or '',
        'receipt_path': expense.receipt_path or '',
        'approval_status': expense.approval_status or 'pending',
        'approval_note': expense.approval_note or '',
        'approved_by': expense.approved_by or '',
        'approved_at': expense.approved_at.strftime('%Y-%m-%d %H:%M') if expense.approved_at else '',
        'recorded_by': expense.recorded_by or '',
        'is_paid': bool(expense.is_paid),
        'created_at': expense.created_at.strftime('%Y-%m-%d %H:%M') if expense.created_at else '',
        'updated_at': expense.updated_at.strftime('%Y-%m-%d %H:%M') if expense.updated_at else '',
    }


def serialize_vendor_contact(vendor):
    return {
        'id': vendor.id,
        'name': vendor.name,
        'contact_type': vendor.contact_type,
        'phone': vendor.phone or '',
        'notes': vendor.notes or '',
        'is_active': vendor.is_active,
    }


def expense_can_be_approved_by(admin):
    return bool(admin and admin_has_any_role(admin, 'CEO'))


def add_years_safe(base_date, years):
    if not base_date:
        return None
    try:
        return base_date.replace(year=base_date.year + years)
    except ValueError:
        return base_date.replace(month=2, day=28, year=base_date.year + years)


def build_debt_distribution_schedule(amount, roi_rate, term_years, expected_completion_date=None):
    amount = float(amount or 0)
    roi_rate = float(roi_rate or 0)
    term_years = int(term_years or 0)
    if amount <= 0 or term_years <= 0:
        return {
            'distribution_model': 'annual_principal_plus_roi',
            'annual_principal_component': 0.0,
            'annual_roi_amount': 0.0,
            'projected_total_roi': 0.0,
            'projected_total_payout': 0.0,
            'schedule': [],
        }

    annual_principal_component = round(amount / term_years, 2)
    annual_roi_amount = round(amount * roi_rate / 100, 2)
    remaining_principal = round(amount, 2)
    schedule = []

    for year in range(1, term_years + 1):
        opening_principal = remaining_principal
        principal_component = annual_principal_component if year < term_years else round(remaining_principal, 2)
        total_payout = round(principal_component + annual_roi_amount, 2)
        remaining_principal = round(max(remaining_principal - principal_component, 0), 2)
        due_date = add_years_safe(expected_completion_date, year) if expected_completion_date else None
        schedule.append({
            'year': year,
            'opening_principal': round(opening_principal, 2),
            'principal_component': round(principal_component, 2),
            'roi_component': annual_roi_amount,
            'total_payout': total_payout,
            'remaining_principal': remaining_principal,
            'due_date': due_date.isoformat() if due_date else None,
        })

    return {
        'distribution_model': 'annual_principal_plus_roi',
        'annual_principal_component': annual_principal_component,
        'annual_roi_amount': annual_roi_amount,
        'projected_total_roi': round(annual_roi_amount * term_years, 2),
        'projected_total_payout': round(sum(item['total_payout'] for item in schedule), 2),
        'schedule': schedule,
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
        title='BrightWave Phase 1 Apartment',
        description='Modern 10-room self-contained apartment near KWASU with private bathrooms, kitchens, 24/7 security, and solar power.',
        property_type='hostel',
        location='Malete, Kwara State, Nigeria',
        price=None, price_type='Now Open - Contact for Rates',
        total_rooms=10, available_rooms=10,
        amenities=['Private Bathroom','Private Kitchen','24/7 Security','Solar Power','CCTV','Water Supply','Parking Space'],
        images=['images/phase1/phase1-main-entrance.jpg', 'images/phase1/phase1-aerial-topview.jpg'],
        construction_status='completed', completion_date=datetime(2026, 3, 25).date(),
        featured=True, status='active', capital_budget=25000000
    )

    phase2 = Property(
        title='BrightWave Apartment Phase 2',
        description='30-room modern apartment complex with enhanced amenities.',
        property_type='hostel', location='Malete, Kwara State',
        price=480000, price_type='per session',
        total_rooms=20, available_rooms=20,
        amenities=['Self-contained rooms','24/7 Security & CCTV','Solar power backup','Recreation facilities','Study Areas','Common Spaces'],
        images=['images/hostels/brightwave-phase2-render.jpg'],
        construction_status='planning', completion_date=datetime(2027, 6, 30).date(),
        featured=False, status='active', capital_budget=None
    )

    phase3 = Property(
        title='BrightWave Apartment Phase 3',
        description='40-room premium apartment complex with gym, library, and recreational facilities.',
        property_type='hostel', location='GreenCity, Malete, Kwara State',
        price=520000, price_type='per session',
        total_rooms=40, available_rooms=40,
        amenities=['Self-contained rooms','24/7 Security & CCTV','Solar power backup','Gym','Library','Recreation facilities'],
        images=['images/hostels/brightwave-phase3-concept.jpg'],
        construction_status='pending', completion_date=datetime(2028, 12, 31).date(),
        featured=False, status='active', capital_budget=None
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
    phase1 = Property.query.filter_by(title='BrightWave Phase 1 Apartment').first()
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
        'BrightWave Apartment Phase 2',
        'BrightWave Apartment Phase 3',
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

def ensure_unit_type_migrations():
    """Add new columns/tables to existing DB when the schema has evolved."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)

    def has_column(table, column):
        try:
            return any(c['name'] == column for c in insp.get_columns(table))
        except Exception:
            return False

    pending = []
    if insp.has_table('tenant') and not has_column('tenant', 'unit_type_id'):
        pending.append('ALTER TABLE tenant ADD COLUMN unit_type_id INTEGER')

    for stmt in pending:
        try:
            with db.engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            logger.warning(f"Unit-type migration skipped ({stmt}): {e}")


def seed_default_unit_types():
    """Seed initial unit types for hostel/apartment properties (idempotent — runs only on empty)."""
    if PropertyUnitType.query.first():
        return

    phase_seeds = {
        1: [
            {
                'name': 'Self-Contained Room (Ensuite)',
                'description': 'Ensuite apartment-style room with private bathroom and kitchen.',
                'annual_price': 0.0,
                'total_count': 10,
            },
        ],
        2: [
            {
                'name': 'Self-Contained Room (Ensuite)',
                'description': 'Single room with private ensuite bathroom.',
                'annual_price': 0.0,
                'total_count': 0,
            },
            {
                'name': 'Room + Parlour Apartment (Ensuite)',
                'description': 'Self-contained apartment with separate parlour and ensuite.',
                'annual_price': 0.0,
                'total_count': 0,
            },
        ],
        3: [
            {
                'name': 'Self-Contained Room (Ensuite)',
                'description': 'Single room with private ensuite bathroom.',
                'annual_price': 0.0,
                'total_count': 0,
            },
            {
                'name': 'Room + Parlour Apartment (Ensuite)',
                'description': 'Self-contained apartment with separate parlour and ensuite.',
                'annual_price': 0.0,
                'total_count': 0,
            },
        ],
    }

    created = False
    for phase, types in phase_seeds.items():
        phase_token = f'Phase {phase}'
        prop = Property.query.filter(Property.title.like(f'%{phase_token}%')).first()
        if not prop:
            continue
        for t in types:
            db.session.add(PropertyUnitType(property_id=prop.id, **t))
            created = True
    if created:
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.warning(f"seed_default_unit_types commit failed: {e}")


def initialize_app_state(include_sample_data=False, bootstrap_admin=False):
    """Run one-time database initialization outside the web worker startup path."""
    db.create_all()
    ensure_unit_type_migrations()
    ensure_cms_baseline()
    seed_contract_templates()
    if include_sample_data:
        init_sample_data()
    seed_default_units()
    seed_default_construction_updates()
    reconcile_property_catalog()
    sync_property_units_from_tenants()
    seed_default_unit_types()
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
   — 10% of the gross rent value per residential or apartment unit successfully leased or let.
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
— Distributions are paid annually, commencing after project completion and first revenue generation.
— Each annual debt distribution combines a fixed principal repayment portion and the agreed annual ROI amount, based on the Investor's confirmed term and profile.
— The annual principal repayment portion is calculated by dividing the invested principal across the agreed investment term, unless a different structure is confirmed in writing by the CEO.
— The annual return rate for founding investors in the current phase is 3.5% per annum. This rate reflects the early-stage nature of the business and ensures long-term sustainability for both the Company and its investors. The CEO reserves the right to revise the rate upward in future investment rounds as the Company scales and revenues increase, with any revision to be confirmed in writing prior to the signing of any new agreement.
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
    # Serve from repo root first (180x180 copy of icon-192), fall back to assets
    if os.path.exists('apple-touch-icon.png'):
        resp = make_response(send_from_directory('.', 'apple-touch-icon.png', mimetype='image/png'))
    elif os.path.exists(os.path.join('assets', 'images', 'icon-192.png')):
        resp = make_response(send_from_directory('assets/images', 'icon-192.png', mimetype='image/png'))
    else:
        resp = make_response(send_from_directory('assets/images', 'brightwave-logo.png', mimetype='image/png'))
    resp.headers['Cache-Control'] = 'public, max-age=3600, must-revalidate'
    resp.headers['ETag'] = 'brightwave-icon-v3'
    return resp

@app.route('/favicon.ico')
def favicon_ico():
    if os.path.exists('favicon-32x32.png'):
        resp = make_response(send_from_directory('.', 'favicon-32x32.png', mimetype='image/x-icon'))
    else:
        resp = make_response(send_from_directory('assets/images', 'icon-192.png', mimetype='image/x-icon'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/favicon-32x32.png')
def favicon_32():
    if os.path.exists('favicon-32x32.png'):
        resp = make_response(send_from_directory('.', 'favicon-32x32.png', mimetype='image/png'))
    else:
        resp = make_response(send_from_directory('assets/images', 'icon-192.png', mimetype='image/png'))
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp

@app.route('/favicon-16x16.png')
def favicon_16():
    if os.path.exists('favicon-16x16.png'):
        resp = make_response(send_from_directory('.', 'favicon-16x16.png', mimetype='image/png'))
    else:
        resp = make_response(send_from_directory('assets/images', 'icon-192.png', mimetype='image/png'))
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

@app.route('/site.webmanifest')
def public_pwa_manifest():
    manifest = {
        "id": "/",
        "name": "BrightWave Habitat Enterprise",
        "short_name": "BrightWave",
        "description": "BrightWave Habitat Enterprise official website",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#0f172a",
        "theme_color": "#0f172a",
        "icons": [
            {"src": "/apple-touch-icon.png?v=2", "sizes": "180x180", "type": "image/png"},
            {"src": "/assets/images/icon-192.png?v=2", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/assets/images/icon-512.png?v=2", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ]
    }
    from flask import Response
    return Response(json.dumps(manifest), mimetype='application/manifest+json')

@app.route('/sw.js')
def service_worker():
    sw_code = """
const CACHE_NAME = 'brightwave-portal-v6';
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
        if request.args.get('preview') == '1' and session.get('admin_id'):
            return jsonify({
                item.slug: item.draft_value if item.draft_value is not None else item.value
                for item in SiteContent.query.order_by(SiteContent.slug.asc()).all()
            })
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
    """Get enhanced dashboard statistics, optionally filtered by property_id"""
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT', 'REALTOR'):
            return jsonify({"success": False, "message": "Access restricted to management roles"}), 403
        from sqlalchemy import func as sqlfunc
        ensure_cms_baseline()

        filter_prop_id = request.args.get('property_id', type=int)
        filter_prop = Property.query.get(filter_prop_id) if filter_prop_id else None
        filter_prop_title = filter_prop.title if filter_prop else None

        # --- Property-level counts (always portfolio-wide) ---
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

        # --- Tenant/unit stats (filterable by property) ---
        tenant_q = Tenant.query
        if filter_prop_title:
            tenant_q = tenant_q.filter(Tenant.property_name.ilike(f'%{filter_prop_title}%'))
        active_tenants = tenant_q.filter_by(status='active').count()
        total_tenants = tenant_q.count()
        reserved_tenants = tenant_q.filter_by(status='reserved').count()
        vacated_tenants = tenant_q.filter_by(status='vacated').count()

        unit_q = PropertyUnit.query
        if filter_prop_id:
            unit_q = unit_q.filter_by(property_id=filter_prop_id)
        total_units = unit_q.count()
        available_units = unit_q.filter_by(status='available').count()
        occupied_units = unit_q.filter_by(status='occupied').count()

        # --- Revenue stats (filterable via tenant property_name join) ---
        now = datetime.utcnow()
        month_start = date_type(now.year, now.month, 1)

        def _rev_q(extra_filters=None):
            q = db.session.query(sqlfunc.sum(PaymentRecord.amount))
            if filter_prop_title:
                q = q.join(Tenant, PaymentRecord.tenant_id == Tenant.id).filter(
                    Tenant.property_name.ilike(f'%{filter_prop_title}%')
                )
            if extra_filters:
                for f in extra_filters:
                    q = q.filter(f)
            return q.scalar() or 0

        monthly_revenue = _rev_q([PaymentRecord.payment_date >= month_start])
        total_revenue = _rev_q()

        # --- Capital/expense stats (filterable by property_id) ---
        def _exp_q(approval=None, extra_filters=None):
            q = db.session.query(sqlfunc.sum(ProjectExpense.amount))
            if filter_prop_id:
                q = q.filter(ProjectExpense.property_id == filter_prop_id)
            if approval:
                q = q.filter(ProjectExpense.approval_status == approval)
            if extra_filters:
                for f in extra_filters:
                    q = q.filter(f)
            return q.scalar() or 0

        # total_capital_spent = approved only — only sanctioned spending counts
        approved_capital_spent = _exp_q('approved')
        total_capital_spent = approved_capital_spent
        monthly_capital_spent = _exp_q('approved', extra_filters=[ProjectExpense.expense_date >= month_start])
        pending_capital_spent = _exp_q('pending')
        rejected_capital_spent = _exp_q('rejected')

        if filter_prop:
            total_capital_budget = filter_prop.capital_budget or 0
        else:
            total_capital_budget = db.session.query(sqlfunc.sum(Property.capital_budget)).scalar() or 0
        capital_budget_remaining = total_capital_budget - approved_capital_spent

        # --- Monthly trend (last 24 months, filterable) ---
        from calendar import monthrange as _mrange
        monthly_trend = []
        for _i in range(23, -1, -1):
            _mo = now.replace(day=1) - timedelta(days=_i * 28)
            _yr, _mo_n = _mo.year, _mo.month
            _ms = date_type(_yr, _mo_n, 1)
            _me = date_type(_yr, _mo_n, _mrange(_yr, _mo_n)[1])
            _rev = _rev_q([PaymentRecord.payment_date >= _ms, PaymentRecord.payment_date <= _me])
            _cap = _exp_q('approved', extra_filters=[ProjectExpense.expense_date >= _ms, ProjectExpense.expense_date <= _me])
            monthly_trend.append({'month': _ms.strftime("%b '%y"), 'revenue': float(_rev), 'capital': float(_cap)})

        # --- Yearly trend (all years from first record) ---
        _earliest_pay = db.session.query(sqlfunc.min(PaymentRecord.payment_date)).scalar()
        _earliest_exp = db.session.query(sqlfunc.min(ProjectExpense.expense_date)).scalar()
        if filter_prop_id:
            _earliest_exp = db.session.query(sqlfunc.min(ProjectExpense.expense_date)).filter(ProjectExpense.property_id == filter_prop_id).scalar()
        _start_yr = now.year
        if _earliest_pay: _start_yr = min(_start_yr, _earliest_pay.year)
        if _earliest_exp: _start_yr = min(_start_yr, _earliest_exp.year)
        yearly_trend = []
        for _yr in range(_start_yr, now.year + 1):
            _ys = date_type(_yr, 1, 1)
            _ye = date_type(_yr, 12, 31)
            _yrev = _rev_q([PaymentRecord.payment_date >= _ys, PaymentRecord.payment_date <= _ye])
            _ycap = _exp_q('approved', extra_filters=[ProjectExpense.expense_date >= _ys, ProjectExpense.expense_date <= _ye])
            yearly_trend.append({'year': str(_yr), 'revenue': float(_yrev), 'capital': float(_ycap)})

        # --- Recent data (filterable) ---
        recent_inquiries = PropertyInquiry.query.order_by(PropertyInquiry.created_at.desc()).limit(5).all()
        recent_messages = ContactMessage.query.order_by(ContactMessage.created_at.desc()).limit(5).all()
        rec_pay_q = PaymentRecord.query
        if filter_prop_title:
            rec_pay_q = rec_pay_q.join(Tenant, PaymentRecord.tenant_id == Tenant.id).filter(
                Tenant.property_name.ilike(f'%{filter_prop_title}%')
            )
        recent_payments = rec_pay_q.order_by(PaymentRecord.created_at.desc()).limit(5).all()
        recent_tenants = tenant_q.order_by(Tenant.created_at.desc()).limit(5).all()
        rec_exp_q = ProjectExpense.query
        if filter_prop_id:
            rec_exp_q = rec_exp_q.filter(ProjectExpense.property_id == filter_prop_id)
        recent_expenses = rec_exp_q.order_by(ProjectExpense.expense_date.desc(), ProjectExpense.created_at.desc()).limit(5).all()

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
            'monthly_revenue': float(monthly_revenue),
            'total_revenue': float(total_revenue),
            'monthly_capital_spent': float(monthly_capital_spent),
            'total_capital_spent': float(total_capital_spent),
            'approved_capital_spent': float(approved_capital_spent),
            'pending_capital_spent': float(pending_capital_spent),
            'rejected_capital_spent': float(rejected_capital_spent),
            'total_capital_budget': float(total_capital_budget),
            'capital_budget_remaining': float(capital_budget_remaining),
            'monthly_trend': monthly_trend,
            'yearly_trend': yearly_trend,
            'filtered_property': {'id': filter_prop.id, 'title': filter_prop.title} if filter_prop else None,
            'tenant_status': {
                'active': active_tenants,
                'reserved': reserved_tenants,
                'vacated': vacated_tenants,
            },
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
                } for t in recent_tenants],
                'expenses': [{
                    'id': e.id,
                    'property_title': e.property.title if e.property else '',
                    'item_name': e.item_name,
                    'category': e.category,
                    'amount': e.amount,
                    'expense_date': e.expense_date.strftime('%Y-%m-%d') if e.expense_date else ''
                } for e in recent_expenses]
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
            return jsonify({
                item.slug: item.draft_value if item.draft_value is not None else item.value
                for item in SiteContent.query.order_by(SiteContent.slug.asc()).all()
            })

        admin = get_current_admin()
        if not admin or admin.role != 'CEO':
            return jsonify({"success": False, "message": "CEO access required"}), 403
        data = request.get_json() or {}
        for slug in DEFAULT_SITE_CONTENT.keys():
            if slug in data:
                existing = SiteContent.query.filter_by(slug=slug).first()
                if existing:
                    existing.draft_value = str(data.get(slug, '')).strip()
                else:
                    db.session.add(SiteContent(slug=slug, value=str(data.get(slug, '')).strip(), draft_value=str(data.get(slug, '')).strip()))
        db.session.commit()
        return jsonify({"success": True, "message": "Saved as draft — click Publish to go live"})
    except Exception as e:
        logger.error(f"Error updating site content: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/site-content/upload-hero-bg', methods=['POST'])
@login_required
def upload_hero_bg():
    try:
        admin = get_current_admin()
        if not admin or admin.role != 'CEO':
            return jsonify({"success": False, "message": "CEO access required"}), 403
        if 'file' not in request.files:
            return jsonify({"success": False, "message": "No file provided"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "message": "No file selected"}), 400
        if file and allowed_file(file.filename):
            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = secure_filename(f"hero_bg_{int(time())}.{ext}")
            file_path = os.path.join(app.config['HERO_BG_FOLDER'], filename)
            file.save(file_path)
            path_value = f"images/site/{filename}"
            existing = SiteContent.query.filter_by(slug='home.hero_bg_path').first()
            if existing:
                existing.draft_value = path_value
            else:
                db.session.add(SiteContent(slug='home.hero_bg_path', value='', draft_value=path_value))
            db.session.commit()
            return jsonify({"success": True, "path": path_value, "message": "Saved as draft — click Publish to go live"})
        return jsonify({"success": False, "message": "Invalid file type. Use PNG, JPG, WEBP, or GIF."}), 400
    except Exception as e:
        logger.error(f"Error uploading hero background: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/site-content/upload-video', methods=['POST'])
@login_required
def upload_site_video():
    try:
        admin = get_current_admin()
        if not admin or admin.role != 'CEO':
            return jsonify({"success": False, "message": "CEO access required"}), 403
        if 'file' not in request.files:
            return jsonify({"success": False, "message": "No file provided"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "message": "No file selected"}), 400
        ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        if ext not in ALLOWED_VIDEO_EXTENSIONS:
            return jsonify({"success": False, "message": "Invalid file type. Use MP4, WEBM, MOV, or OGG."}), 400
        filename = secure_filename(f"site_video_{int(time())}.{ext}")
        file_path = os.path.join(app.config['VIDEO_FOLDER'], filename)
        file.save(file_path)
        video_url = f"/assets/videos/site/{filename}"
        existing = SiteContent.query.filter_by(slug='home.video_url').first()
        if existing:
            existing.draft_value = video_url
        else:
            db.session.add(SiteContent(slug='home.video_url', value='', draft_value=video_url))
        db.session.commit()
        return jsonify({"success": True, "url": video_url, "message": "Video saved as draft — click Publish to go live"})
    except Exception as e:
        logger.error(f"Error uploading site video: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/site-content/draft-status', methods=['GET'])
@login_required
def site_content_draft_status():
    try:
        count = SiteContent.query.filter(SiteContent.draft_value.isnot(None)).count()
        return jsonify({"pending": count})
    except Exception as e:
        logger.error(f"Error fetching draft status: {str(e)}")
        return jsonify({"pending": 0})

@app.route('/admin/api/site-content/publish', methods=['POST'])
@login_required
def publish_site_content():
    try:
        admin = get_current_admin()
        if not admin or admin.role != 'CEO':
            return jsonify({"success": False, "message": "CEO access required"}), 403
        items = SiteContent.query.filter(SiteContent.draft_value.isnot(None)).all()
        count = len(items)
        for item in items:
            item.value = item.draft_value
            item.draft_value = None
        db.session.commit()
        return jsonify({"success": True, "message": f"Published {count} change(s) to the live website", "published": count})
    except Exception as e:
        logger.error(f"Error publishing site content: {str(e)}")
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


@app.route('/admin/api/upload-expense-receipt', methods=['POST'])
@login_required
def upload_expense_receipt():
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403
        if 'file' not in request.files:
            return jsonify({"success": False, "message": "No file provided"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "message": "No file selected"}), 400
        if file and allowed_receipt_file(file.filename):
            filename = secure_filename(f"{int(time())}_{file.filename}")
            file_path = os.path.join(app.config['EXPENSE_RECEIPT_FOLDER'], filename)
            file.save(file_path)
            return jsonify({"success": True, "filename": f"uploads/expense-receipts/{filename}"})
        return jsonify({"success": False, "message": "Invalid receipt file type"}), 400
    except Exception as e:
        logger.error(f"Error uploading expense receipt: {str(e)}")
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


# ========== PUBLIC SIGNUP ==========
SIGNUP_ALLOWED_ROLES = {'INVESTOR', 'MANAGER', 'ACCOUNTANT', 'REALTOR', 'PA'}


@app.route('/signup', methods=['GET'])
def public_signup_page():
    return render_template_string(SIGNUP_TEMPLATE)


@app.route('/api/signup', methods=['POST'])
@limiter.limit("5 per hour")
def api_public_signup():
    try:
        data = request.get_json() or {}
        full_name = (data.get('full_name') or '').strip()
        email = (data.get('email') or '').strip().lower()
        phone = (data.get('phone') or '').strip() or None
        role = (data.get('role') or '').strip().upper()
        password = data.get('password') or ''

        if not full_name or not email or not role or not password:
            return jsonify({"success": False, "message": "All required fields must be filled"}), 400
        if role not in SIGNUP_ALLOWED_ROLES:
            return jsonify({"success": False, "message": "Invalid role"}), 400
        if '@' not in email or '.' not in email.split('@')[-1]:
            return jsonify({"success": False, "message": "Invalid email"}), 400
        if len(password) < 8:
            return jsonify({"success": False, "message": "Password must be at least 8 characters"}), 400

        # Reject if email already used by an Admin or pending signup
        if Admin.query.filter_by(email=email).first():
            return jsonify({"success": False, "message": "An account with this email already exists. Please log in instead."}), 409
        existing_pending = PendingSignup.query.filter_by(email=email).first()
        if existing_pending and existing_pending.status == 'pending':
            return jsonify({"success": False, "message": "You already have a pending request under review."}), 409

        # Role-specific data
        role_data = {}
        if role == 'INVESTOR':
            try:
                amount = float(data.get('investment_amount') or 0)
            except (TypeError, ValueError):
                amount = 0.0
            if amount <= 0:
                return jsonify({"success": False, "message": "Please enter an intended investment amount"}), 400
            inv_type = (data.get('investment_type') or 'DEBT').strip().upper()
            if inv_type not in ('DEBT', 'EQUITY'):
                inv_type = 'DEBT'
            try:
                term_years = int(data.get('term_years') or 0) or None
            except (TypeError, ValueError):
                term_years = None
            role_data = {
                'investment_amount': amount,
                'investment_type': inv_type,
                'term_years': term_years,
                'notes': (data.get('notes') or '').strip() or None,
            }
        else:
            role_data = {
                'experience': (data.get('experience') or '').strip() or None,
                'availability': (data.get('availability') or '').strip() or None,
                'notes': (data.get('notes') or '').strip() or None,
            }

        # Replace any prior rejected request from same email
        if existing_pending and existing_pending.status == 'rejected':
            db.session.delete(existing_pending)
            db.session.flush()

        signup = PendingSignup(
            full_name=full_name,
            email=email,
            phone=phone,
            role=role,
            password_hash=generate_password_hash(password),
            role_data=role_data,
            ip_address=(request.headers.get('X-Forwarded-For') or request.remote_addr or '')[:64] or None,
        )
        db.session.add(signup)
        db.session.commit()
        return jsonify({
            "success": True,
            "message": "Request submitted. The CEO will review it shortly — you'll be able to log in once approved.",
        })
    except IntegrityError:
        db.session.rollback()
        return jsonify({"success": False, "message": "A request with this email is already pending."}), 409
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error submitting signup: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


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
            has_seen_tour=bool(admin.has_seen_tour),
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
        has_seen_tour=bool(admin.has_seen_tour),
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
                'capital_budget': prop.capital_budget,
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
                capital_budget=float(data['capital_budget']) if data.get('capital_budget') not in (None, '') else None,
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
            if 'capital_budget' in data and not data.get('title'):
                property.capital_budget = float(data['capital_budget']) if data.get('capital_budget') not in (None, '') else None
                db.session.commit()
                return jsonify({"success": True, "message": "Budget updated"})
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
            property.capital_budget = float(data['capital_budget']) if data.get('capital_budget') not in (None, '') else None
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


@app.route('/admin/api/project-expenses', methods=['GET', 'POST'])
@login_required
def admin_project_expenses():
    try:
        admin = get_current_admin()
        if request.method == 'GET':
            if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
                return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403
            property_id = request.args.get('property_id', type=int)
            approval_status = (request.args.get('approval_status') or '').strip().lower()
            has_receipt = (request.args.get('has_receipt') or '').strip().lower()
            category_filter = (request.args.get('category') or '').strip().lower()
            summary_query = ProjectExpense.query
            if property_id:
                summary_query = summary_query.filter_by(property_id=property_id)
            query = summary_query
            if approval_status in {'pending', 'approved', 'rejected'}:
                query = query.filter_by(approval_status=approval_status)
            if has_receipt in {'1', 'true', 'yes'}:
                query = query.filter(ProjectExpense.receipt_path.isnot(None), ProjectExpense.receipt_path != '')
            if category_filter in {'materials', 'labour', 'transport', 'equipment', 'permits', 'land', 'other'}:
                query = query.filter_by(category=category_filter)
            expenses = query.order_by(ProjectExpense.expense_date.desc(), ProjectExpense.created_at.desc()).all()
            total_amount = round(sum(exp.amount or 0 for exp in expenses), 2)
            by_category = {}
            for exp in expenses:
                by_category[exp.category] = round(by_category.get(exp.category, 0) + (exp.amount or 0), 2)
            approval_totals = {'pending': 0.0, 'approved': 0.0, 'rejected': 0.0}
            for exp in summary_query.all():
                key = exp.approval_status or 'pending'
                if key in approval_totals:
                    approval_totals[key] = round(approval_totals[key] + (exp.amount or 0), 2)
            budget_total = None
            budget_remaining = None
            over_budget = False
            if property_id:
                prop = Property.query.get(property_id)
                if prop and prop.capital_budget is not None:
                    budget_total = round(prop.capital_budget, 2)
                    budget_remaining = round(budget_total - approval_totals['approved'], 2)
                    over_budget = budget_remaining < 0
            return jsonify({
                'expenses': [serialize_project_expense(expense) for expense in expenses],
                'total_amount': total_amount,
                'by_category': by_category,
                'approval_totals': approval_totals,
                'budget_total': budget_total,
                'budget_remaining': budget_remaining,
                'over_budget': over_budget,
            })

        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403

        data = request.get_json() or {}
        property_id = data.get('property_id')
        item_name = (data.get('item_name') or '').strip()
        amount = data.get('amount')
        category = (data.get('category') or 'materials').strip() or 'materials'
        if not property_id or amount in (None, ''):
            return jsonify({"success": False, "message": "property_id and amount are required"}), 400
        if not item_name:
            payee_hint = (data.get('payee_name') or '').strip()
            auto_names = {
                'labour': f"Labour{' — ' + payee_hint if payee_hint else ''}",
                'transport': 'Transport',
                'permits': 'Permit / Fee',
                'other': 'Other expense',
            }
            item_name = auto_names.get(category)
            if not item_name:
                return jsonify({"success": False, "message": "Item / Purchase name is required for this category"}), 400

        prop = Property.query.get_or_404(int(property_id))
        payee_name = (data.get('payee_name') or '').strip() or None
        expense = ProjectExpense(
            property_id=prop.id,
            expense_date=date_type.fromisoformat(data['expense_date']) if data.get('expense_date') else date_type.today(),
            category=(data.get('category') or 'materials').strip() or 'materials',
            item_name=item_name,
            payee_name=payee_name,
            quantity=float(data['quantity']) if data.get('quantity') not in (None, '') else None,
            unit_cost=float(data['unit_cost']) if data.get('unit_cost') not in (None, '') else None,
            amount=float(amount),
            notes=(data.get('notes') or '').strip() or None,
            receipt_path=(data.get('receipt_path') or '').strip() or None,
            approval_status='approved' if expense_can_be_approved_by(admin) else 'pending',
            approved_by=(admin.display_name or admin.username) if expense_can_be_approved_by(admin) else None,
            approved_at=datetime.utcnow() if expense_can_be_approved_by(admin) else None,
            recorded_by=admin.display_name or admin.username,
        )
        db.session.add(expense)
        if payee_name:
            existing_vendor = VendorContact.query.filter_by(name=payee_name).first()
            if not existing_vendor:
                db.session.add(VendorContact(
                    name=payee_name,
                    contact_type='worker' if expense.category == 'labour' else 'supplier',
                    is_active=True,
                ))
        db.session.commit()
        return jsonify({"success": True, "message": "Project expense recorded", "expense": serialize_project_expense(expense)})
    except Exception as e:
        logger.error(f"Error managing project expenses: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/project-expenses/<int:expense_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_project_expense_detail(expense_id):
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403

        expense = ProjectExpense.query.get_or_404(expense_id)
        if request.method == 'DELETE':
            db.session.delete(expense)
            db.session.commit()
            return jsonify({"success": True, "message": "Project expense removed"})

        data = request.get_json() or {}
        if 'property_id' in data and data['property_id']:
            expense.property_id = int(data['property_id'])
        if 'expense_date' in data:
            expense.expense_date = date_type.fromisoformat(data['expense_date']) if data['expense_date'] else expense.expense_date
        if 'category' in data:
            expense.category = (data['category'] or 'materials').strip() or 'materials'
        if 'item_name' in data and (data['item_name'] or '').strip():
            expense.item_name = data['item_name'].strip()
        if 'payee_name' in data:
            expense.payee_name = (data['payee_name'] or '').strip() or None
        if 'quantity' in data:
            expense.quantity = float(data['quantity']) if data['quantity'] not in (None, '') else None
        if 'unit_cost' in data:
            expense.unit_cost = float(data['unit_cost']) if data['unit_cost'] not in (None, '') else None
        if 'amount' in data and data['amount'] not in (None, ''):
            expense.amount = float(data['amount'])
        if 'notes' in data:
            expense.notes = (data['notes'] or '').strip() or None
        if 'receipt_path' in data:
            expense.receipt_path = (data['receipt_path'] or '').strip() or None
        if 'is_paid' in data:
            expense.is_paid = bool(data['is_paid'])
        requested_status = (data.get('approval_status') or '').strip().lower()
        if requested_status:
            if requested_status not in {'pending', 'approved', 'rejected'}:
                return jsonify({"success": False, "message": "Invalid approval_status"}), 400
            if not expense_can_be_approved_by(admin):
                return jsonify({"success": False, "message": "Only CEO or Accountant can change approval status"}), 403
            expense.approval_status = requested_status
            expense.approval_note = (data.get('approval_note') or '').strip() or None
            if requested_status == 'approved':
                expense.approved_by = admin.display_name or admin.username
                expense.approved_at = datetime.utcnow()
            else:
                expense.approved_by = None
                expense.approved_at = None
        elif not expense_can_be_approved_by(admin):
            expense.approval_status = 'pending'
            expense.approval_note = None
            expense.approved_by = None
            expense.approved_at = None
        expense.recorded_by = admin.display_name or admin.username
        if expense.payee_name:
            existing_vendor = VendorContact.query.filter_by(name=expense.payee_name).first()
            if not existing_vendor:
                db.session.add(VendorContact(
                    name=expense.payee_name,
                    contact_type='worker' if expense.category == 'labour' else 'supplier',
                    is_active=True,
                ))
        expense.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"success": True, "message": "Project expense updated", "expense": serialize_project_expense(expense)})
    except Exception as e:
        logger.error(f"Error updating project expense {expense_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/vendors', methods=['GET', 'POST'])
@login_required
def admin_vendors():
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403
        if request.method == 'GET':
            vendors = VendorContact.query.filter_by(is_active=True).order_by(VendorContact.name.asc()).all()
            return jsonify([serialize_vendor_contact(vendor) for vendor in vendors])

        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({"success": False, "message": "Vendor name is required"}), 400
        existing = VendorContact.query.filter_by(name=name).first()
        if existing:
            return jsonify({"success": True, "message": "Vendor already exists", "vendor": serialize_vendor_contact(existing)})
        vendor = VendorContact(
            name=name,
            contact_type=(data.get('contact_type') or 'supplier').strip() or 'supplier',
            phone=(data.get('phone') or '').strip() or None,
            notes=(data.get('notes') or '').strip() or None,
            is_active=True,
        )
        db.session.add(vendor)
        db.session.commit()
        return jsonify({"success": True, "message": "Vendor saved", "vendor": serialize_vendor_contact(vendor)})
    except Exception as e:
        logger.error(f"Error managing vendors: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/maintenance', methods=['GET', 'POST'])
@login_required
def admin_maintenance():
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "Access restricted"}), 403
        if request.method == 'GET':
            property_id = request.args.get('property_id')
            q = MaintenanceRecord.query
            if property_id:
                q = q.filter_by(property_id=int(property_id))
            records = q.order_by(MaintenanceRecord.maintenance_date.desc(), MaintenanceRecord.id.desc()).all()
            return jsonify([{
                'id': r.id,
                'property_id': r.property_id,
                'property_title': r.property.title if r.property else '',
                'maintenance_date': r.maintenance_date.isoformat() if r.maintenance_date else '',
                'title': r.title,
                'category': r.category,
                'description': r.description or '',
                'vendor_name': r.vendor_name or '',
                'cost': r.cost,
                'status': r.status,
                'recorded_by': r.recorded_by or '',
                'created_at': r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '',
            } for r in records])
        data = request.get_json() or {}
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({"success": False, "message": "Title is required"}), 400
        property_id = data.get('property_id')
        if not property_id:
            return jsonify({"success": False, "message": "Property is required"}), 400
        record = MaintenanceRecord(
            property_id=int(property_id),
            maintenance_date=date_type.fromisoformat(data['maintenance_date']) if data.get('maintenance_date') else date_type.today(),
            title=title,
            category=(data.get('category') or 'general').strip(),
            description=(data.get('description') or '').strip() or None,
            vendor_name=(data.get('vendor_name') or '').strip() or None,
            cost=float(data['cost']) if data.get('cost') else None,
            status=(data.get('status') or 'completed').strip(),
            recorded_by=admin.username,
        )
        db.session.add(record)
        db.session.commit()
        return jsonify({"success": True, "message": "Maintenance record saved", "id": record.id})
    except Exception as e:
        logger.error(f"Error managing maintenance: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/maintenance/<int:record_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_maintenance_record(record_id):
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "Access restricted"}), 403
        record = MaintenanceRecord.query.get_or_404(record_id)
        if request.method == 'DELETE':
            db.session.delete(record)
            db.session.commit()
            return jsonify({"success": True, "message": "Record deleted"})
        data = request.get_json() or {}
        if 'title' in data: record.title = (data['title'] or '').strip()
        if 'category' in data: record.category = (data['category'] or 'general').strip()
        if 'description' in data: record.description = (data['description'] or '').strip() or None
        if 'vendor_name' in data: record.vendor_name = (data['vendor_name'] or '').strip() or None
        if 'cost' in data: record.cost = float(data['cost']) if data['cost'] else None
        if 'status' in data: record.status = (data['status'] or 'completed').strip()
        if 'maintenance_date' in data and data['maintenance_date']:
            record.maintenance_date = date_type.fromisoformat(data['maintenance_date'])
        if 'property_id' in data and data['property_id']:
            record.property_id = int(data['property_id'])
        db.session.commit()
        return jsonify({"success": True, "message": "Record updated"})
    except Exception as e:
        logger.error(f"Error updating maintenance record: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/inquiries', methods=['GET', 'POST'])
@login_required
def admin_get_inquiries():
    """Get all property inquiries"""
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'REALTOR'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Realtor"}), 403
        if request.method == 'POST':
            data = request.get_json() or {}
            if not data.get('full_name') or not data.get('phone'):
                return jsonify({"success": False, "message": "Name and phone are required"}), 400
            prop = None
            if data.get('property_id'):
                prop = Property.query.get(int(data['property_id']))
            inq = PropertyInquiry(
                property_id=prop.id if prop else None,
                full_name=data['full_name'].strip(),
                email=(data.get('email') or '').strip() or 'manual@entry.local',
                phone=data['phone'].strip(),
                inquiry_type=data.get('inquiry_type', 'general'),
                budget_range=(data.get('budget_range') or '').strip() or None,
                preferred_move_date=date_type.fromisoformat(data['preferred_move_date']) if data.get('preferred_move_date') else None,
                message=(data.get('message') or '').strip() or 'Manually added by staff',
                status=data.get('status', 'new'),
                priority=data.get('priority', 'medium'),
                inquiry_notes=(data.get('inquiry_notes') or '').strip() or None,
            )
            db.session.add(inq)
            db.session.commit()
            return jsonify({"success": True, "message": "Lead added", "id": inq.id})
        inquiries = PropertyInquiry.query.order_by(PropertyInquiry.created_at.desc()).all()
        return jsonify([{
            'id': inquiry.id,
            'property_id': inquiry.property_id,
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
            'inquiry_notes': inquiry.inquiry_notes or '',
            'created_at': inquiry.created_at.isoformat()
        } for inquiry in inquiries])
    except Exception as e:
        logger.error(f"Error fetching inquiries: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

@app.route('/admin/api/inquiries/<int:inquiry_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_update_inquiry(inquiry_id):
    """Update or delete an inquiry"""
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'REALTOR'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Realtor"}), 403
        inquiry = PropertyInquiry.query.get_or_404(inquiry_id)
        if request.method == 'DELETE':
            db.session.delete(inquiry)
            db.session.commit()
            return jsonify({"success": True, "message": "Lead removed"})
        data = request.get_json() or {}
        if 'status' in data: inquiry.status = data['status']
        if 'priority' in data: inquiry.priority = data['priority']
        if 'inquiry_notes' in data: inquiry.inquiry_notes = (data['inquiry_notes'] or '').strip() or None
        if 'full_name' in data: inquiry.full_name = data['full_name'].strip()
        if 'phone' in data: inquiry.phone = data['phone'].strip()
        if 'email' in data: inquiry.email = data['email'].strip()
        if 'budget_range' in data: inquiry.budget_range = data['budget_range'].strip() or None
        if 'preferred_move_date' in data:
            inquiry.preferred_move_date = date_type.fromisoformat(data['preferred_move_date']) if data['preferred_move_date'] else None
        if 'message' in data: inquiry.message = (data['message'] or '').strip() or inquiry.message
        if 'property_id' in data and data['property_id']:
            inquiry.property_id = int(data['property_id'])
        inquiry.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"success": True, "message": "Lead updated"})
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

# ========== SIGNUP APPROVALS API ==========
def _serialize_pending_signup(s):
    return {
        'id': s.id,
        'full_name': s.full_name,
        'email': s.email,
        'phone': s.phone or '',
        'role': s.role,
        'status': s.status,
        'role_data': s.role_data or {},
        'rejection_reason': s.rejection_reason or '',
        'ip_address': s.ip_address or '',
        'created_at': s.created_at.strftime('%Y-%m-%d %H:%M UTC') if s.created_at else None,
        'reviewed_at': s.reviewed_at.strftime('%Y-%m-%d %H:%M UTC') if s.reviewed_at else None,
        'reviewed_by': s.reviewed_by or '',
        'created_admin_id': s.created_admin_id,
    }


def _generate_username_for_signup(s):
    base = ''.join(ch for ch in (s.email.split('@')[0] or s.full_name).lower() if ch.isalnum()) or 'user'
    base = base[:60]
    candidate = base
    n = 1
    while Admin.query.filter_by(username=candidate).first():
        n += 1
        candidate = f"{base}{n}"
        if n > 50:
            candidate = f"{base}{secrets.token_hex(3)}"
            break
    return candidate


@app.route('/admin/api/signups')
@login_required
@ceo_required
def admin_signups_list():
    try:
        status = (request.args.get('status') or 'pending').lower()
        q = PendingSignup.query
        if status and status != 'all':
            q = q.filter_by(status=status)
        rows = q.order_by(PendingSignup.created_at.desc()).all()
        pending_count = PendingSignup.query.filter_by(status='pending').count()
        return jsonify({'success': True, 'signups': [_serialize_pending_signup(s) for s in rows], 'pending_count': pending_count})
    except Exception as e:
        logger.error(f"Error listing signups: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/signups/<int:signup_id>/approve', methods=['POST'])
@login_required
@ceo_required
def admin_signup_approve(signup_id):
    try:
        ceo = get_current_admin()
        signup = PendingSignup.query.get_or_404(signup_id)
        if signup.status != 'pending':
            return jsonify({"success": False, "message": f"Signup already {signup.status}"}), 400

        # Sanity: email collision can occur if an Admin was manually created in the gap
        if Admin.query.filter_by(email=signup.email).first():
            return jsonify({"success": False, "message": "An Admin with this email already exists"}), 409

        username = _generate_username_for_signup(signup)
        new_admin = Admin(
            username=username,
            email=signup.email,
            password_hash=signup.password_hash,
            role=signup.role if signup.role != 'PA' else 'ACCOUNTANT',  # PA stored as ACCOUNTANT-class for now
            secondary_roles=[],
            display_name=signup.full_name,
            is_active=True,
            has_signed_contract=False,
            monthly_salary=0.0,
        )
        # PA isn't a top-level role in valid_roles list; tag it via secondary_roles for visibility
        if signup.role == 'PA':
            new_admin.secondary_roles = ['PA']
        db.session.add(new_admin)
        db.session.flush()  # get new_admin.id

        # Investor: create InvestorProfile from role_data
        if signup.role == 'INVESTOR':
            rd = signup.role_data or {}
            try:
                amount = float(rd.get('investment_amount') or 0)
            except (TypeError, ValueError):
                amount = 0.0
            inv_type = (rd.get('investment_type') or 'DEBT').upper()
            term = rd.get('term_years')
            try:
                term = int(term) if term else None
            except (TypeError, ValueError):
                term = None
            profile = InvestorProfile(
                user_id=new_admin.id,
                investment_type=inv_type,
                investment_amount=amount,
                investment_term_years=term,
                notes=rd.get('notes') or None,
            )
            db.session.add(profile)

        # Staff: kick off contract signing
        if signup.role in ('MANAGER', 'ACCOUNTANT', 'REALTOR', 'PA', 'INVESTOR'):
            contract_type = signup.role if signup.role != 'PA' else 'ACCOUNTANT'
            contract = UserContract(user_id=new_admin.id, contract_type=contract_type, status='pending_user_signature')
            db.session.add(contract)

        signup.status = 'approved'
        signup.reviewed_at = datetime.utcnow()
        signup.reviewed_by = ceo.display_name or ceo.username if ceo else None
        signup.created_admin_id = new_admin.id
        db.session.commit()
        return jsonify({"success": True, "message": f"Approved. {signup.full_name} can now log in as @{username}.", "admin_id": new_admin.id, "username": username})
    except IntegrityError:
        db.session.rollback()
        return jsonify({"success": False, "message": "Username/email collision while creating account"}), 409
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error approving signup {signup_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/signups/<int:signup_id>/reject', methods=['POST'])
@login_required
@ceo_required
def admin_signup_reject(signup_id):
    try:
        ceo = get_current_admin()
        signup = PendingSignup.query.get_or_404(signup_id)
        if signup.status != 'pending':
            return jsonify({"success": False, "message": f"Signup already {signup.status}"}), 400
        data = request.get_json() or {}
        signup.status = 'rejected'
        signup.rejection_reason = (data.get('reason') or '').strip() or None
        signup.reviewed_at = datetime.utcnow()
        signup.reviewed_by = ceo.display_name or ceo.username if ceo else None
        db.session.commit()
        return jsonify({"success": True, "message": "Signup rejected."})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error rejecting signup {signup_id}: {str(e)}")
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
                    'monthly_salary': float(a.monthly_salary or 0),
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

        try:
            monthly_salary_val = float(data.get('monthly_salary') or 0)
            if monthly_salary_val < 0:
                monthly_salary_val = 0.0
        except (TypeError, ValueError):
            monthly_salary_val = 0.0

        new_admin = Admin(
            username=data['username'].strip(),
            email=data['email'].strip().lower(),
            password_hash=generate_password_hash(data['password']),
            role=data['role'],
            secondary_roles=secondary,
            display_name=(data.get('display_name') or '').strip() or None,
            monthly_salary=monthly_salary_val,
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
            if 'monthly_salary' in data:
                try:
                    sal = float(data['monthly_salary'] or 0)
                    account.monthly_salary = sal if sal >= 0 else 0.0
                except (TypeError, ValueError):
                    pass
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

# ========== PROPERTY UNIT TYPE API ==========
def _serialize_unit_type(ut):
    occupied = Tenant.query.filter_by(unit_type_id=ut.id, status='active').count()
    return {
        'id': ut.id,
        'property_id': ut.property_id,
        'property_title': ut.property.title if ut.property else '',
        'name': ut.name,
        'description': ut.description or '',
        'annual_price': ut.annual_price or 0,
        'total_count': ut.total_count or 0,
        'occupied_count': occupied,
        'available_count': max(0, (ut.total_count or 0) - occupied),
        'is_active': ut.is_active,
        'updated_at': ut.updated_at.isoformat() if ut.updated_at else None,
    }


@app.route('/admin/api/unit-types', methods=['GET', 'POST'])
@login_required
def admin_unit_types():
    try:
        if request.method == 'GET':
            prop_id = request.args.get('property_id', type=int)
            q = PropertyUnitType.query
            if prop_id:
                q = q.filter_by(property_id=prop_id)
            unit_types = q.order_by(PropertyUnitType.property_id.asc(), PropertyUnitType.name.asc()).all()
            return jsonify([_serialize_unit_type(ut) for ut in unit_types])

        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "CEO or Manager access required"}), 403
        data = request.get_json() or {}
        if not data.get('property_id') or not data.get('name'):
            return jsonify({"success": False, "message": "property_id and name are required"}), 400
        prop = Property.query.get(int(data['property_id']))
        if not prop:
            return jsonify({"success": False, "message": "Property not found"}), 404
        try:
            annual_price = float(data.get('annual_price') or 0)
            total_count = int(data.get('total_count') or 0)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid annual_price or total_count"}), 400
        ut = PropertyUnitType(
            property_id=prop.id,
            name=data['name'].strip(),
            description=(data.get('description') or '').strip() or None,
            annual_price=annual_price,
            total_count=total_count,
            is_active=bool(data.get('is_active', True)),
        )
        db.session.add(ut)
        db.session.commit()
        return jsonify({"success": True, "message": "Unit type added", "id": ut.id})
    except Exception as e:
        logger.error(f"Error managing unit types: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/unit-types/<int:unit_type_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_unit_type_detail(unit_type_id):
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "CEO or Manager access required"}), 403
        ut = PropertyUnitType.query.get_or_404(unit_type_id)

        if request.method == 'DELETE':
            in_use = Tenant.query.filter_by(unit_type_id=ut.id, status='active').count()
            if in_use:
                return jsonify({
                    "success": False,
                    "message": f"Cannot delete: {in_use} active tenant(s) assigned. Reassign them first."
                }), 409
            db.session.delete(ut)
            db.session.commit()
            return jsonify({"success": True, "message": "Unit type deleted"})

        data = request.get_json() or {}
        if 'property_id' in data and data['property_id']:
            prop = Property.query.get(int(data['property_id']))
            if not prop:
                return jsonify({"success": False, "message": "Property not found"}), 404
            ut.property_id = prop.id
        if 'name' in data:
            ut.name = (data['name'] or '').strip() or ut.name
        if 'description' in data:
            ut.description = (data['description'] or '').strip() or None
        if data.get('annual_price') is not None:
            try:
                ut.annual_price = float(data['annual_price'])
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": "Invalid annual_price"}), 400
        if data.get('total_count') is not None:
            try:
                ut.total_count = int(data['total_count'])
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": "Invalid total_count"}), 400
        if 'is_active' in data:
            ut.is_active = bool(data['is_active'])
        db.session.commit()
        return jsonify({"success": True, "message": "Unit type updated"})
    except Exception as e:
        logger.error(f"Error on unit type {unit_type_id}: {str(e)}")
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
                'unit_type_id': t.unit_type_id,
                'unit_type_name': t.unit_type.name if t.unit_type else '',
                'lease_start': t.lease_start.isoformat() if t.lease_start else '',
                'lease_end': t.lease_end.isoformat() if t.lease_end else '',
                'monthly_rent': t.monthly_rent or 0,
                'status': t.status,
                'notes': t.notes or '',
                'serviced_by_id': t.serviced_by_id,
                'serviced_by_name': (t.serviced_by.display_name or t.serviced_by.username) if t.serviced_by else '',
                'created_at': t.created_at.strftime('%Y-%m-%d'),
            } for t in tenants])

        _tenant_admin = get_current_admin()
        if not _tenant_admin or not admin_has_any_role(_tenant_admin, 'CEO', 'MANAGER'):
            return jsonify({"success": False, "message": "CEO or Manager access required"}), 403
        data = request.get_json() or {}
        if not data.get('name'):
            return jsonify({"success": False, "message": "Tenant name is required"}), 400

        # When unit_type is selected, derive property_name + rent default (if not provided)
        unit_type_id = data.get('unit_type_id') or None
        property_name = (data.get('property_name') or '').strip() or None
        rent_val = data.get('monthly_rent')
        if unit_type_id:
            ut = PropertyUnitType.query.get(int(unit_type_id))
            if ut:
                property_name = property_name or (ut.property.title if ut.property else None)
                if rent_val in (None, '', 0, '0'):
                    rent_val = ut.annual_price

        serviced_by_id = data.get('serviced_by_id') or None
        tenant = Tenant(
            name=data['name'].strip(),
            email=(data.get('email') or '').strip() or None,
            phone=(data.get('phone') or '').strip() or None,
            property_name=property_name,
            unit_number=(data.get('unit_number') or '').strip() or None,
            unit_type_id=int(unit_type_id) if unit_type_id else None,
            lease_start=date_type.fromisoformat(data['lease_start']) if data.get('lease_start') else None,
            lease_end=date_type.fromisoformat(data['lease_end']) if data.get('lease_end') else None,
            monthly_rent=float(rent_val or 0),
            status=data.get('status', 'active'),
            notes=(data.get('notes') or '').strip() or None,
            serviced_by_id=int(serviced_by_id) if serviced_by_id else None,
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
            if request.args.get('hard') == '1':
                if not admin_has_any_role(_tenant_admin, 'CEO'):
                    return jsonify({"success": False, "message": "CEO access required"}), 403
                db.session.delete(tenant)
                db.session.commit()
                sync_property_units_from_tenants()
                return jsonify({"success": True, "message": "Tenant removed"})
            tenant.status = 'vacated'
            db.session.commit()
            sync_property_units_from_tenants()
            return jsonify({"success": True, "message": "Tenant marked as vacated"})
        data = request.get_json() or {}
        for field in ['name', 'email', 'phone', 'property_name', 'unit_number', 'status', 'notes']:
            if field in data:
                setattr(tenant, field, (data[field] or '').strip() or None if field != 'status' else data[field])
        if 'unit_type_id' in data:
            new_id = data['unit_type_id']
            tenant.unit_type_id = int(new_id) if new_id else None
            if new_id:
                ut = PropertyUnitType.query.get(int(new_id))
                if ut and not tenant.property_name:
                    tenant.property_name = ut.property.title if ut.property else tenant.property_name
        if data.get('monthly_rent') is not None:
            tenant.monthly_rent = float(data['monthly_rent'])
        if data.get('lease_start'):
            tenant.lease_start = date_type.fromisoformat(data['lease_start'])
        if data.get('lease_end'):
            tenant.lease_end = date_type.fromisoformat(data['lease_end'])
        if 'serviced_by_id' in data:
            sb = data.get('serviced_by_id')
            tenant.serviced_by_id = int(sb) if sb else None
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
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403

        if request.method == 'GET':
            payments = PaymentRecord.query.order_by(PaymentRecord.created_at.desc()).limit(50).all()
            return jsonify([serialize_payment_record(p) for p in payments])

        data = request.get_json() or {}
        if not data.get('amount'):
            return jsonify({"success": False, "message": "Amount is required"}), 400
        tenant_name = data.get('tenant_name', '').strip()
        tenant_id = data.get('tenant_id') or None
        if tenant_id:
            t = Tenant.query.get(int(tenant_id))
            if t:
                tenant_name = t.name
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


@app.route('/admin/api/payments/<int:payment_id>', methods=['PUT', 'DELETE'])
@login_required
def admin_payment_detail(payment_id):
    try:
        admin = get_current_admin()
        if not admin or not admin_has_any_role(admin, 'CEO', 'MANAGER', 'ACCOUNTANT'):
            return jsonify({"success": False, "message": "Access restricted to CEO, Manager, or Accountant"}), 403

        payment = PaymentRecord.query.get_or_404(payment_id)
        if request.method == 'DELETE':
            db.session.delete(payment)
            db.session.commit()
            return jsonify({"success": True, "message": "Payment removed"})

        data = request.get_json() or {}
        if 'amount' in data:
            payment.amount = float(data['amount'])
        if 'payment_date' in data and data['payment_date']:
            payment.payment_date = date_type.fromisoformat(data['payment_date'])
        if 'payment_type' in data:
            payment.payment_type = (data['payment_type'] or 'rent').strip() or 'rent'
        if 'description' in data:
            payment.description = (data['description'] or '').strip() or None

        tenant_id = data.get('tenant_id')
        tenant_name = (data.get('tenant_name') or '').strip()
        if tenant_id:
            tenant = Tenant.query.get(int(tenant_id))
            payment.tenant_id = int(tenant_id)
            payment.tenant_name = tenant.name if tenant else (tenant_name or payment.tenant_name)
        elif 'tenant_id' in data:
            payment.tenant_id = None
            payment.tenant_name = tenant_name or None
        elif tenant_name:
            payment.tenant_name = tenant_name

        payment.recorded_by = admin.display_name or admin.username
        db.session.commit()
        return jsonify({"success": True, "message": "Payment updated", "payment": serialize_payment_record(payment)})
    except Exception as e:
        logger.error(f"Error updating payment {payment_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


# ========== PAYROLL ==========
# Pricing model: the Yearly Rent stored on a tenant is the GROSS total the tenant pays
# (base + 10% markup bundled). The 10% markup is the Manager/Realtor commission —
# funded by the tenant, not deducted from company revenue.
#   base       = total / 1.10
#   commission = total - base   (== total / 11)
# Accountant / PA / any other role earns the flat `Admin.monthly_salary` for the month.
PAYROLL_COMMISSION_RATE = 0.10
PAYROLL_COMMISSION_ROLES = {'MANAGER', 'REALTOR'}


def _user_qualifies_for_commission(user):
    if not user:
        return False
    if (user.role or '').upper() in PAYROLL_COMMISSION_ROLES:
        return True
    for sr in (user.secondary_roles or []):
        if (sr or '').upper() in PAYROLL_COMMISSION_ROLES:
            return True
    return False


def compute_user_payroll(user, year, month):
    """Return commission + salary breakdown for a single user in a (year, month)."""
    from sqlalchemy import extract

    commission_lines = []
    commission_total = 0.0
    if _user_qualifies_for_commission(user):
        tenants = Tenant.query.filter(
            Tenant.serviced_by_id == user.id,
            extract('year', Tenant.created_at) == year,
            extract('month', Tenant.created_at) == month,
        ).all()
        for t in tenants:
            total_paid = float(t.monthly_rent or 0)  # legacy field name; stores gross yearly total
            base = total_paid / (1 + PAYROLL_COMMISSION_RATE)
            commission = total_paid - base
            commission_total += commission
            commission_lines.append({
                'tenant_id': t.id,
                'tenant_name': t.name,
                'unit_number': t.unit_number,
                'annual_rent': total_paid,
                'base_rent': base,
                'commission': commission,
            })

    salary = float(user.monthly_salary or 0)

    payments_q = (
        PayrollPayment.query
        .filter(PayrollPayment.user_id == user.id,
                PayrollPayment.period_year == year,
                PayrollPayment.period_month == month)
        .order_by(PayrollPayment.paid_at.desc())
        .all()
    )
    payment_history = [{
        'id': p.id,
        'amount': float(p.amount or 0),
        'kind': p.kind,
        'notes': p.notes or '',
        'paid_by': p.paid_by or '',
        'paid_at': p.paid_at.isoformat() if p.paid_at else None,
    } for p in payments_q]
    already_paid = float(sum(p['amount'] for p in payment_history))

    total_earned = commission_total + salary
    return {
        'user_id': user.id,
        'username': user.username,
        'display_name': user.display_name or user.username,
        'role': user.role,
        'secondary_roles': user.secondary_roles or [],
        'qualifies_for_commission': _user_qualifies_for_commission(user),
        'commission_lines': commission_lines,
        'commission_total': commission_total,
        'salary': salary,
        'total_earned': total_earned,
        'already_paid': already_paid,
        'outstanding': max(total_earned - already_paid, 0.0),
        'payment_history': payment_history,
    }


@app.route('/admin/api/payroll/summary')
@login_required
@ceo_required
def admin_payroll_summary():
    try:
        today = date_type.today()
        try:
            year = int(request.args.get('year', today.year))
            month = int(request.args.get('month', today.month))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid year or month"}), 400
        if not (1 <= month <= 12):
            return jsonify({"success": False, "message": "Month must be 1-12"}), 400

        users = Admin.query.filter_by(is_active=True).order_by(Admin.role.asc(), Admin.username.asc()).all()
        # CEO and INVESTOR roles are not on payroll
        rows = [compute_user_payroll(u, year, month) for u in users if (u.role or '').upper() not in ('CEO', 'INVESTOR')]

        totals = {
            'commission_total': sum(r['commission_total'] for r in rows),
            'salary_total': sum(r['salary'] for r in rows),
            'earned_total': sum(r['total_earned'] for r in rows),
            'paid_total': sum(r['already_paid'] for r in rows),
            'outstanding_total': sum(r['outstanding'] for r in rows),
        }
        return jsonify({'success': True, 'year': year, 'month': month, 'rows': rows, 'totals': totals})
    except Exception as e:
        logger.error(f"Error in payroll summary: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/payroll/pay', methods=['POST'])
@login_required
@ceo_required
def admin_payroll_pay():
    try:
        admin = get_current_admin()
        data = request.get_json() or {}
        user_id = data.get('user_id')
        amount = data.get('amount')
        if not user_id or amount in (None, ''):
            return jsonify({"success": False, "message": "user_id and amount are required"}), 400
        try:
            user_id = int(user_id)
            amount = float(amount)
            year = int(data.get('year') or date_type.today().year)
            month = int(data.get('month') or date_type.today().month)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid numeric value"}), 400
        if amount <= 0:
            return jsonify({"success": False, "message": "Amount must be positive"}), 400
        if not (1 <= month <= 12):
            return jsonify({"success": False, "message": "Month must be 1-12"}), 400

        user = Admin.query.get(user_id)
        if not user:
            return jsonify({"success": False, "message": "User not found"}), 404

        kind = (data.get('kind') or 'commission').strip().lower()
        if kind not in ('commission', 'salary'):
            kind = 'commission'

        payment = PayrollPayment(
            user_id=user.id,
            period_year=year,
            period_month=month,
            amount=amount,
            kind=kind,
            source_tenant_id=int(data['source_tenant_id']) if data.get('source_tenant_id') else None,
            notes=(data.get('notes') or '').strip() or None,
            paid_by=admin.display_name or admin.username if admin else None,
        )
        db.session.add(payment)
        db.session.commit()
        return jsonify({"success": True, "message": "Payroll payment recorded", "id": payment.id})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error recording payroll payment: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/payroll/history')
@login_required
@ceo_required
def admin_payroll_history():
    try:
        try:
            year = int(request.args.get('year', date_type.today().year))
            month = int(request.args.get('month', date_type.today().month))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid year or month"}), 400

        payments = (
            PayrollPayment.query
            .filter(PayrollPayment.period_year == year, PayrollPayment.period_month == month)
            .order_by(PayrollPayment.paid_at.desc())
            .all()
        )
        out = []
        for p in payments:
            u = p.user
            out.append({
                'id': p.id,
                'user_id': p.user_id,
                'username': u.username if u else None,
                'display_name': (u.display_name or u.username) if u else None,
                'amount': p.amount,
                'kind': p.kind,
                'source_tenant_id': p.source_tenant_id,
                'notes': p.notes,
                'paid_by': p.paid_by,
                'paid_at': p.paid_at.isoformat() if p.paid_at else None,
            })
        return jsonify({'success': True, 'year': year, 'month': month, 'payments': out})
    except Exception as e:
        logger.error(f"Error fetching payroll history: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/payroll/pay/<int:payment_id>', methods=['PUT', 'DELETE'])
@login_required
@ceo_required
def admin_payroll_pay_detail(payment_id):
    try:
        payment = PayrollPayment.query.get_or_404(payment_id)
        if request.method == 'DELETE':
            db.session.delete(payment)
            db.session.commit()
            return jsonify({"success": True, "message": "Payment removed"})

        data = request.get_json() or {}
        if 'amount' in data:
            try:
                amt = float(data['amount'])
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": "Invalid amount"}), 400
            if amt <= 0:
                return jsonify({"success": False, "message": "Amount must be positive"}), 400
            payment.amount = amt
        if 'kind' in data:
            kind = (data['kind'] or '').strip().lower()
            if kind in ('commission', 'salary'):
                payment.kind = kind
        if 'notes' in data:
            payment.notes = (data['notes'] or '').strip() or None
        if 'period_year' in data or 'period_month' in data:
            try:
                y = int(data.get('period_year', payment.period_year))
                m = int(data.get('period_month', payment.period_month))
                if not (1 <= m <= 12):
                    raise ValueError
                payment.period_year = y
                payment.period_month = m
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": "Invalid period"}), 400
        db.session.commit()
        return jsonify({"success": True, "message": "Payment updated"})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error editing payroll payment {payment_id}: {str(e)}")
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
                    'property_id': p.property_id,
                    'property_title': p.property.title if p.property else '',
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
            roi_rate=float(data.get('roi_rate') or 3.5),
            equity_percentage=float(data['equity_percentage']) if data.get('equity_percentage') else None,
            construction_start_date=parse_date(data.get('construction_start_date')),
            expected_completion_date=parse_date(data.get('expected_completion_date')),
            notes=(data.get('notes') or '').strip() or None,
            property_id=int(data['property_id']) if data.get('property_id') else None,
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
        if 'property_id' in data:
            profile.property_id = int(data['property_id']) if data['property_id'] else None
        profile.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"success": True, "message": "Investor profile updated"})
    except Exception as e:
        logger.error(f"Error updating investor profile {profile_id}: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500

def _serialize_investor_profile(profile):
    project_assigned = bool(profile.property_id)
    project_property = profile.property if project_assigned else get_investor_project_property()
    updates = []
    if project_property:
        updates = ConstructionUpdate.query.filter_by(
            property_id=project_property.id,
            is_public=True
        ).order_by(ConstructionUpdate.progress_percentage.asc(), ConstructionUpdate.created_at.asc()).all()
    debt_schedule = build_debt_distribution_schedule(
        profile.investment_amount,
        profile.roi_rate,
        profile.investment_term_years,
        profile.expected_completion_date,
    ) if profile.investment_type == 'DEBT' else None
    return {
        'id': profile.id,
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
        'project_assigned': project_assigned,
        'project_property_id': project_property.id if project_property else None,
        'project_property_title': project_property.title if project_property else '',
        'project_property_price': project_property.price if project_property else None,
        'project_property_total_rooms': project_property.total_rooms if project_property else None,
        'project_property_construction_status': project_property.construction_status if project_property else None,
        'construction_updates': [serialize_construction_update(u) for u in updates],
        'distribution_model': debt_schedule['distribution_model'] if debt_schedule else 'equity_variable',
        'annual_principal_component': debt_schedule['annual_principal_component'] if debt_schedule else None,
        'annual_roi_amount': debt_schedule['annual_roi_amount'] if debt_schedule else None,
        'projected_total_roi': debt_schedule['projected_total_roi'] if debt_schedule else None,
        'projected_total_payout': debt_schedule['projected_total_payout'] if debt_schedule else None,
        'payout_schedule': debt_schedule['schedule'] if debt_schedule else [],
    }

@app.route('/admin/api/me/mark-tour-seen', methods=['POST'])
@login_required
def mark_tour_seen():
    try:
        admin = get_current_admin()
        if not admin:
            return jsonify({"success": False, "message": "Not authenticated"}), 401
        admin.has_seen_tour = True
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error marking tour seen: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error"}), 500


@app.route('/admin/api/my-investment', methods=['GET'])
@login_required
def my_investment():
    try:
        admin = get_current_admin()
        all_roles = [admin.role] + (admin.secondary_roles or [])
        if 'INVESTOR' not in all_roles:
            return jsonify({"success": False, "message": "Not an investor account"}), 403
        profiles = InvestorProfile.query.filter_by(user_id=admin.id).all()
        if not profiles:
            return jsonify([])
        return jsonify([_serialize_investor_profile(p) for p in profiles])
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
            <div class="text-center pt-1 flex items-center justify-center gap-3">
                <button type="button" onclick="toggleForgotForm()" class="text-xs text-gray-500 hover:text-gray-300 transition-colors">Forgot password?</button>
                <span class="text-gray-700">·</span>
                <a href="/signup" class="text-xs text-gray-500 hover:text-gray-300 transition-colors">New here? Request access</a>
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

SIGNUP_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Request Access — BrightWave Habitat</title>
    <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
    <meta name="theme-color" content="#475569">
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white min-h-screen flex items-center justify-center py-10 px-4">
    <div class="max-w-xl w-full bg-gray-800 p-8 rounded-xl shadow-2xl">
        <div class="text-center mb-6">
            <img src="/assets/images/brightwave-logo.png" alt="BrightWave" class="w-16 h-16 rounded-full object-cover mx-auto mb-3 ring-2 ring-slate-500/50">
            <h1 class="text-xl font-bold text-slate-200">Request Access</h1>
            <p class="text-gray-400 mt-1 text-sm">Tell us who you are. The CEO reviews each request before activation.</p>
        </div>

        <form id="signupForm" class="space-y-4">
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                    <label class="block text-xs font-medium mb-1 text-gray-400">Full Name *</label>
                    <input type="text" id="su_name" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm focus:outline-none focus:border-slate-500">
                </div>
                <div>
                    <label class="block text-xs font-medium mb-1 text-gray-400">Email *</label>
                    <input type="email" id="su_email" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm focus:outline-none focus:border-slate-500">
                </div>
                <div>
                    <label class="block text-xs font-medium mb-1 text-gray-400">Phone</label>
                    <input type="text" id="su_phone" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm focus:outline-none focus:border-slate-500">
                </div>
                <div>
                    <label class="block text-xs font-medium mb-1 text-gray-400">I am joining as *</label>
                    <select id="su_role" required onchange="suSwitchRole()" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm focus:outline-none focus:border-slate-500">
                        <option value="">-- Select role --</option>
                        <option value="INVESTOR">Investor</option>
                        <option value="MANAGER">Manager</option>
                        <option value="ACCOUNTANT">Accountant</option>
                        <option value="REALTOR">Realtor</option>
                        <option value="PA">Personal Assistant (PA)</option>
                    </select>
                </div>
                <div class="sm:col-span-2">
                    <label class="block text-xs font-medium mb-1 text-gray-400">Password *</label>
                    <input type="password" id="su_password" required minlength="8" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm focus:outline-none focus:border-slate-500">
                    <p class="text-[10px] text-gray-500 mt-1">Min 8 characters. You'll use this to log in once approved.</p>
                </div>
            </div>

            <!-- INVESTOR fields -->
            <div id="suInvestorFields" class="hidden border-t border-gray-700 pt-4">
                <p class="text-xs text-emerald-400 font-medium uppercase tracking-wide mb-3">Investor Details</p>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Intended Investment (₦) *</label>
                        <input type="number" id="su_invAmount" min="0" step="10000" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm focus:outline-none focus:border-slate-500">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Type</label>
                        <select id="su_invType" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="DEBT">Debt (fixed return)</option>
                            <option value="EQUITY">Equity (project share)</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Term (years)</label>
                        <input type="number" id="su_invTerm" min="1" max="20" placeholder="e.g. 3" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                </div>
            </div>

            <!-- STAFF fields -->
            <div id="suStaffFields" class="hidden border-t border-gray-700 pt-4">
                <p class="text-xs text-amber-400 font-medium uppercase tracking-wide mb-3">Background</p>
                <div class="space-y-3">
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Prior experience</label>
                        <textarea id="su_experience" rows="2" placeholder="Brief on your relevant background" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea>
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Availability</label>
                        <input type="text" id="su_availability" placeholder="e.g. Mon–Fri full-time" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                </div>
            </div>

            <div>
                <label class="block text-xs font-medium mb-1 text-gray-400">Anything else? (optional)</label>
                <textarea id="su_notes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea>
            </div>

            <button type="submit" class="w-full bg-slate-600 hover:bg-slate-700 text-white font-medium py-2.5 px-4 rounded-lg focus:outline-none transition-colors">Submit Request</button>
            <p id="suMessage" class="text-sm text-center hidden"></p>
        </form>

        <p class="text-xs text-gray-500 text-center mt-6">
            Already approved? <a href="/admin/login" class="text-slate-300 hover:text-white underline">Log in</a>
        </p>
    </div>

    <script>
        function suSwitchRole() {
            const role = document.getElementById('su_role').value;
            document.getElementById('suInvestorFields').classList.toggle('hidden', role !== 'INVESTOR');
            document.getElementById('suStaffFields').classList.toggle('hidden', !role || role === 'INVESTOR');
        }

        document.getElementById('signupForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const msg = document.getElementById('suMessage');
            msg.classList.remove('hidden');
            msg.textContent = 'Submitting...';
            msg.className = 'text-sm text-center text-gray-400';
            const role = document.getElementById('su_role').value;
            const payload = {
                full_name: document.getElementById('su_name').value,
                email: document.getElementById('su_email').value,
                phone: document.getElementById('su_phone').value,
                role: role,
                password: document.getElementById('su_password').value,
                notes: document.getElementById('su_notes').value,
            };
            if (role === 'INVESTOR') {
                payload.investment_amount = document.getElementById('su_invAmount').value;
                payload.investment_type = document.getElementById('su_invType').value;
                payload.term_years = document.getElementById('su_invTerm').value;
            } else {
                payload.experience = document.getElementById('su_experience').value;
                payload.availability = document.getElementById('su_availability').value;
            }
            try {
                const res = await fetch('/api/signup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
                const data = await res.json();
                if (!res.ok || !data.success) {
                    msg.textContent = data.message || 'Submission failed.';
                    msg.className = 'text-sm text-center text-red-400';
                    return;
                }
                msg.textContent = data.message;
                msg.className = 'text-sm text-center text-emerald-400';
                document.getElementById('signupForm').reset();
                suSwitchRole();
            } catch(err) {
                msg.textContent = 'Network error. Please try again.';
                msg.className = 'text-sm text-center text-red-400';
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
    <link rel="shortcut icon" href="/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#475569">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="BrightWave CEO">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        .ceo-nav-btn { color: #94a3b8; transition: all 0.15s; }
        .ceo-nav-btn:hover { background: rgba(71,85,105,0.5); color: #e2e8f0; }
        .ceo-nav-btn.active { background: #475569; color: #ffffff; font-weight: 600; }
        #sidebar { position: fixed; top: 0; left: 0; height: 100vh; width: 240px; transition: width 0.25s ease, transform 0.25s ease; overflow: hidden; z-index: 50; display: flex; flex-direction: column; background: #0f172a; border-right: 1px solid rgba(71,85,105,0.4); }
        #sidebar.collapsed { width: 60px; }
        #sidebar.collapsed .sb-label { display: none; }
        #sidebar.collapsed .sb-item { justify-content: center; padding-left: 0; padding-right: 0; }
        #sidebar.collapsed #sidebarBrand { justify-content: center; padding-left: 0; padding-right: 0; gap: 0; }
        #sidebarToggleBtn { flex-shrink: 0; transition: transform 0.25s ease; }
        #sidebar.collapsed #sidebarToggleBtn { transform: rotate(180deg); }
        #sidebarPinBtn { transition: color 0.15s; }
        #mainWrapper { margin-left: 240px; transition: margin-left 0.25s ease; min-height: 100vh; display: block; }
        #mainWrapper.sidebar-collapsed { margin-left: 60px; }
        @media (max-width: 767px) {
            #sidebar { transform: translateX(-100%); width: 240px; }
            #sidebar.mobile-open { transform: translateX(0); }
            #mainWrapper { margin-left: 0 !important; }
            #mainWrapper > main { padding-left: 1rem !important; padding-right: 1rem !important; }
            .mobile-stack { flex-direction: column !important; align-items: stretch !important; }
            .mobile-full { width: 100% !important; min-width: 0 !important; }
            .attention-bar-item { flex-wrap: wrap; }
        }
        .scrollbar-thin::-webkit-scrollbar { width: 4px; }
        .scrollbar-thin::-webkit-scrollbar-thumb { background: #475569; border-radius: 2px; }
        /* prevent any child from blowing out the horizontal layout */
        *, *::before, *::after { box-sizing: border-box; }
        body { overflow-x: hidden; }
        #mainWrapper { max-width: 100vw; overflow-x: hidden; }
        /* compact tables on mobile */
        @media (max-width: 640px) {
            table { font-size: 0.75rem; }
            td, th { padding-top: 0.35rem !important; padding-bottom: 0.35rem !important; }
        }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen overflow-x-hidden">

    <!-- Loading splash -->
    <div id="bwLoader" style="position:fixed;inset:0;z-index:9999;background:#0f172a;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:24px;transition:opacity 0.45s ease;pointer-events:all">
        <div style="display:flex;flex-direction:column;align-items:center;gap:14px">
            <div style="position:relative;width:80px;height:80px">
                <img src="/assets/images/brightwave-logo.png" alt="BrightWave" id="bwLoaderImg"
                     style="width:80px;height:80px;border-radius:50%;object-fit:cover;box-shadow:0 0 0 3px rgba(20,184,166,0.35),0 16px 40px rgba(0,0,0,0.6)"
                     onerror="this.style.display='none';document.getElementById('bwLoaderIcon').style.display='flex'">
                <div id="bwLoaderIcon" style="display:none;width:80px;height:80px;border-radius:20px;background:linear-gradient(135deg,#0d9488,#0e7490);align-items:center;justify-content:center;box-shadow:0 16px 40px rgba(0,0,0,0.5)">
                    <svg style="width:40px;height:40px" fill="none" stroke="white" stroke-width="1.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 21h19.5m-18-18v18m10.5-18v18m6-13.5V21M6.75 6.75h.75m-.75 3h.75m-.75 3h.75m3-6h.75m-.75 3h.75m-.75 3h.75M6.75 21v-3.375c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21M3 3h12m-.75 4.5H21m-3.75 3.75h.008v.008h-.008v-.008Zm0 3h.008v.008h-.008v-.008Zm0 3h.008v.008h-.008v-.008Z"/></svg>
                </div>
            </div>
            <div style="text-align:center">
                <p style="color:#f1f5f9;font-size:20px;font-weight:700;letter-spacing:0.04em;margin:0;font-family:system-ui,sans-serif">BrightWave</p>
                <p style="color:#14b8a6;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;margin:3px 0 0;font-family:system-ui,sans-serif">Habitat Enterprise</p>
            </div>
        </div>
        <div style="width:36px;height:36px;border:2.5px solid rgba(20,184,166,0.15);border-top-color:#14b8a6;border-radius:50%;animation:bwSpin 0.75s linear infinite"></div>
        <style>@keyframes bwSpin{to{transform:rotate(360deg)}}</style>
    </div>
    <script>
        const _bwLoaderStart = Date.now();
        let _bwLoaderDone = false;
        function dismissLoader() {
            if (_bwLoaderDone) return;
            _bwLoaderDone = true;
            const delay = Math.max(0, 700 - (Date.now() - _bwLoaderStart));
            setTimeout(() => {
                const el = document.getElementById('bwLoader');
                if (!el) return;
                el.style.opacity = '0';
                el.style.pointerEvents = 'none';
                setTimeout(() => el && el.remove(), 460);
            }, delay);
        }
    </script>

    <script>
        const PENDING_SIGS_COUNT = {{ pending_sigs_count }};
        const USER_ROLE = 'CEO';
        const ALL_ROLES = ['CEO'];
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
            <button id="sidebarPinBtn" onclick="toggleSidebarPin()" class="sb-label text-slate-400 hover:text-white p-1.5 rounded-lg hover:bg-slate-700/50 flex-shrink-0 transition-colors" title="Pin sidebar open">
                <i id="sidebarPinIcon" class="fas fa-thumbtack text-xs opacity-40"></i>
            </button>
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
            <button onclick="showSection('unitTypesSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-th-large w-5 text-center flex-shrink-0"></i><span class="sb-label">Unit Types</span>
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
            <button onclick="showSection('payrollSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-hand-holding-usd w-5 text-center flex-shrink-0"></i><span class="sb-label">Payroll</span>
            </button>
            <button onclick="showSection('approvalsSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-user-check w-5 text-center flex-shrink-0"></i><span class="sb-label flex items-center gap-2">Approvals <span id="approvalsBadge" class="hidden bg-red-500 text-white text-xs px-1.5 py-0.5 rounded-full leading-none">0</span></span>
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
            <button onclick="showSection('capitalSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-calculator w-5 text-center flex-shrink-0"></i><span class="sb-label">Capital Calculation</span>
            </button>
            <button onclick="showSection('maintenanceSection')" class="ceo-nav-btn sb-item w-full px-3 py-2.5 rounded-lg flex items-center gap-3 text-sm text-left">
                <i class="fas fa-wrench w-5 text-center flex-shrink-0"></i><span class="sb-label">Maintenance</span>
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

    <!-- Full-page website preview modal -->
    <div id="websitePreviewModal" class="fixed inset-0 z-[999] hidden flex flex-col" style="background:#0f172a">
        <div class="flex items-center justify-between gap-2 px-3 py-2.5 bg-slate-900 border-b border-slate-700/60 flex-shrink-0">
            <div class="flex items-center gap-2 min-w-0">
                <div class="flex gap-1 flex-shrink-0">
                    <span class="w-3 h-3 rounded-full bg-red-500/70"></span>
                    <span class="w-3 h-3 rounded-full bg-amber-500/70"></span>
                    <span class="w-3 h-3 rounded-full bg-emerald-500/70"></span>
                </div>
                <span class="hidden sm:inline text-xs text-slate-400 font-mono bg-slate-800 border border-slate-700 rounded px-2 py-1 truncate max-w-[180px]">Draft Preview</span>
            </div>
            <div class="flex items-center gap-1.5 flex-shrink-0">
                <button onclick="document.getElementById('websitePreviewFrame').src = document.getElementById('websitePreviewFrame').src" class="flex items-center gap-1.5 text-xs text-slate-300 hover:text-white bg-slate-700 hover:bg-slate-600 px-2.5 py-1.5 rounded-lg transition-colors" title="Refresh">
                    <i class="fas fa-rotate-right"></i><span class="hidden sm:inline">Refresh</span>
                </button>
                <a href="https://www.brightwavehabitat.com/" target="_blank" rel="noopener" class="flex items-center gap-1.5 text-xs text-slate-300 hover:text-white bg-slate-700 hover:bg-slate-600 px-2.5 py-1.5 rounded-lg transition-colors" title="Open in Tab">
                    <i class="fas fa-external-link-alt"></i><span class="hidden sm:inline">Open in Tab</span>
                </a>
                <button onclick="closeWebsitePreview()" class="flex items-center gap-1.5 text-xs text-white bg-red-700 hover:bg-red-600 px-2.5 py-1.5 rounded-lg transition-colors" title="Close">
                    <i class="fas fa-times"></i><span class="hidden sm:inline">Close</span>
                </button>
            </div>
        </div>
        <div class="flex-1 relative">
            <div id="previewLoadingSpinner" class="absolute inset-0 flex items-center justify-center bg-slate-900 z-10">
                <div class="text-center">
                    <div class="w-10 h-10 border-4 border-slate-600 border-t-blue-400 rounded-full animate-spin mx-auto mb-3"></div>
                    <p class="text-slate-400 text-sm">Loading website preview…</p>
                    <p class="text-slate-600 text-xs mt-1">Showing the currently saved version</p>
                </div>
            </div>
            <iframe id="websitePreviewFrame" src="" class="w-full h-full border-0" onload="document.getElementById('previewLoadingSpinner').classList.add('hidden')"></iframe>
        </div>
    </div>

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
        <main class="max-w-7xl mx-auto py-4 sm:py-6 px-3 sm:px-6 lg:px-8">
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
                        <select id="editAccRole" onchange="updateSalaryVisibility('editAccRole','editAccSalaryWrap')" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-sm text-white">
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
                    <div id="editAccSalaryWrap">
                        <label class="block text-xs font-medium mb-1 text-gray-400">Monthly Salary (₦) <span class="text-gray-500 font-normal">payroll flat fee</span></label>
                        <input type="number" id="editAccMonthlySalary" min="0" step="1000" placeholder="0" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-sm text-white placeholder-gray-500">
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
                        <select id="accRole" required onchange="updateSalaryVisibility('accRole','accSalaryWrap')" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
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
                    <div id="accSalaryWrap">
                        <label class="block text-xs font-medium mb-1 text-gray-400">Monthly Salary (₦) <span class="text-gray-500">payroll</span></label>
                        <input type="number" id="accMonthlySalary" min="0" step="1000" value="0" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                        <p class="text-[10px] text-gray-500 mt-1">Flat fee for Accountant / PA. Leave 0 for commission-only roles.</p>
                    </div>
                    <div class="flex items-end md:col-span-2">
                        <button type="submit" class="bg-slate-600 hover:bg-slate-700 text-white text-sm font-medium py-2 px-4 rounded-lg w-full">Create Account</button>
                    </div>
                </form>
                <p id="accountMessage" class="text-sm mt-2 hidden"></p>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg">
                <h3 class="font-semibold mb-3 text-slate-300">All Accounts</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm min-w-[720px]">
                        <thead>
                            <tr class="border-b border-gray-600">
                                <th class="py-2 text-left">Name</th>
                                <th class="py-2 text-left">Username</th>
                                <th class="py-2 text-left">Email</th>
                                <th class="py-2 text-left">Primary Role</th>
                                <th class="py-2 text-left">Also</th>
                                <th class="py-2 text-left">Salary/mo</th>
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

        <!-- PAYROLL SECTION -->
        <section id="payrollSection" class="mb-8 hidden">
            <div class="flex items-start justify-between gap-4 mb-4 flex-wrap">
                <div>
                    <h2 class="text-xl font-semibold">Payroll</h2>
                    <p class="text-xs text-gray-400 mt-1">Manager/Realtor: 10% commission per unit they serviced this month. Accountant/PA: flat monthly salary set on Accounts tab.</p>
                </div>
                <div class="flex items-end gap-2">
                    <div>
                        <label class="block text-[11px] font-medium text-gray-400 mb-1">Year</label>
                        <select id="payrollYear" class="bg-gray-700 border border-gray-600 text-sm rounded-lg px-3 py-1.5 text-white"></select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-medium text-gray-400 mb-1">Month</label>
                        <select id="payrollMonth" class="bg-gray-700 border border-gray-600 text-sm rounded-lg px-3 py-1.5 text-white"></select>
                    </div>
                    <button onclick="loadPayroll()" class="bg-slate-600 hover:bg-slate-700 text-white text-sm font-medium py-1.5 px-3 rounded-lg">Refresh</button>
                </div>
            </div>

            <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-5">
                <div class="bg-gray-800 p-3 rounded-lg"><p class="text-[10px] text-gray-500 uppercase tracking-wide">Commission</p><p id="pr_commTotal" class="text-lg font-semibold text-amber-400">—</p></div>
                <div class="bg-gray-800 p-3 rounded-lg"><p class="text-[10px] text-gray-500 uppercase tracking-wide">Salaries</p><p id="pr_salTotal" class="text-lg font-semibold text-blue-400">—</p></div>
                <div class="bg-gray-800 p-3 rounded-lg"><p class="text-[10px] text-gray-500 uppercase tracking-wide">Total Earned</p><p id="pr_earnTotal" class="text-lg font-semibold text-emerald-400">—</p></div>
                <div class="bg-gray-800 p-3 rounded-lg"><p class="text-[10px] text-gray-500 uppercase tracking-wide">Paid</p><p id="pr_paidTotal" class="text-lg font-semibold text-gray-300">—</p></div>
                <div class="bg-gray-800 p-3 rounded-lg"><p class="text-[10px] text-gray-500 uppercase tracking-wide">Outstanding</p><p id="pr_outTotal" class="text-lg font-semibold text-red-400">—</p></div>
            </div>

            <div class="bg-gray-800 p-4 rounded-lg">
                <div class="overflow-x-auto">
                    <table class="w-full text-sm min-w-[760px]">
                        <thead>
                            <tr class="border-b border-gray-600">
                                <th class="py-2 text-left">User</th>
                                <th class="py-2 text-left">Role</th>
                                <th class="py-2 text-right">Commission</th>
                                <th class="py-2 text-right">Salary</th>
                                <th class="py-2 text-right">Total Earned</th>
                                <th class="py-2 text-right">Paid</th>
                                <th class="py-2 text-right">Outstanding</th>
                                <th class="py-2 text-left pl-3">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="payrollTable"><tr><td colspan="8" class="py-6 text-center text-gray-500">Loading...</td></tr></tbody>
                    </table>
                </div>
            </div>

            <!-- Pay Modal -->
            <div id="payrollPayModal" class="hidden fixed inset-0 bg-black/70 z-50 items-center justify-center p-4" style="display:none;">
                <div class="bg-gray-800 border border-gray-700 rounded-xl p-5 max-w-md w-full">
                    <h3 class="text-lg font-semibold text-white mb-3">Record Payroll Payment</h3>
                    <p id="prPayWho" class="text-sm text-gray-400 mb-3"></p>
                    <input type="hidden" id="prPayUserId">
                    <div class="grid grid-cols-2 gap-3 mb-3">
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">Amount (₦) *</label>
                            <input type="number" id="prPayAmount" step="1000" min="0" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                        </div>
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">Kind</label>
                            <select id="prPayKind" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                                <option value="commission">Commission</option>
                                <option value="salary">Salary</option>
                            </select>
                        </div>
                    </div>
                    <div class="mb-3">
                        <label class="block text-xs text-gray-400 mb-1">Notes</label>
                        <textarea id="prPayNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea>
                    </div>
                    <p id="prPayMsg" class="text-sm hidden mb-2"></p>
                    <div class="flex gap-2">
                        <button onclick="submitPayrollPayment()" class="flex-1 bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-medium py-2 rounded-lg">Record Payment</button>
                        <button onclick="closePayrollPayModal()" class="flex-1 bg-gray-700 hover:bg-gray-600 text-white text-sm font-medium py-2 rounded-lg">Cancel</button>
                    </div>
                </div>
            </div>
        </section>

        <!-- APPROVALS SECTION -->
        <section id="approvalsSection" class="mb-8 hidden">
            <div class="flex items-start justify-between gap-4 mb-4 flex-wrap">
                <div>
                    <h2 class="text-xl font-semibold">Signup Approvals</h2>
                    <p class="text-xs text-gray-400 mt-1">Self-service signups land here. Approve to activate the account (their password is preserved); reject to discard.</p>
                </div>
                <div class="flex items-end gap-2">
                    <select id="approvalsStatusFilter" onchange="loadApprovals()" class="bg-gray-700 border border-gray-600 text-sm rounded-lg px-3 py-1.5 text-white">
                        <option value="pending">Pending</option>
                        <option value="approved">Approved</option>
                        <option value="rejected">Rejected</option>
                        <option value="all">All</option>
                    </select>
                    <button onclick="loadApprovals()" class="bg-slate-600 hover:bg-slate-700 text-white text-sm font-medium py-1.5 px-3 rounded-lg">Refresh</button>
                </div>
            </div>
            <div id="approvalsContainer" class="space-y-3"><p class="text-gray-500 text-sm text-center py-6">Loading...</p></div>
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
                        <label class="block text-xs font-medium mb-1 text-gray-400">Project / Property *</label>
                        <select id="invPropertyId" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="">Select project...</option>
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
                        <input type="number" id="invRoi" value="3.5" step="0.5" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
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
                    <table class="w-full text-sm min-w-[800px]">
                        <thead>
                            <tr class="border-b border-gray-600">
                                <th class="py-2 text-left">Investor</th>
                                <th class="py-2 text-left">Project</th>
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
                        <div class="col-span-2">
                            <label class="block text-xs font-medium text-gray-400 mb-1">Project / Property</label>
                            <select id="invEditPropertyId" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none">
                                <option value="">— no specific project —</option>
                            </select>
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
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Property</label>
                        <select id="tnProperty" onchange="tnOnPropertyChange(); tnUtRefreshForProperty();" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white">
                            <option value="">-- Select property --</option>
                        </select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit Type <span class="text-gray-500">(optional)</span></label>
                        <select id="tnUnitType" onchange="tnOnUnitTypeChange()" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white">
                            <option value="">-- Select unit type --</option>
                        </select>
                        <p class="text-[10px] text-gray-500 mt-1">Selecting a type auto-fills the yearly rent.</p>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit / Room No.</label>
                        <select id="tnUnit" onchange="tnOnUnitChange()" disabled class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white opacity-60">
                            <option value="">-- Select property first --</option>
                        </select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Yearly Rent (₦)</label><input type="number" id="tnRent" step="1000" placeholder="Auto-fills from unit type" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Lease Start</label><input type="date" id="tnLeaseStart" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Lease End</label><input type="date" id="tnLeaseEnd" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Status</label>
                        <select id="tnStatus" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="active">Active</option><option value="vacated">Vacated</option>
                        </select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Serviced By <span class="text-gray-500">(payroll)</span></label>
                        <select id="tnServicedBy" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white">
                            <option value="">-- Not tracked --</option>
                        </select>
                        <p class="text-[10px] text-gray-500 mt-1">Yearly Rent is the gross (base + 10%). Manager/Realtor earns the 10% portion; company keeps the base.</p>
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

        <!-- UNIT TYPES SECTION -->
        <section id="unitTypesSection" class="mb-8 hidden">
            <div class="flex items-start justify-between gap-4 mb-4 flex-wrap">
                <div>
                    <h2 class="text-xl font-semibold">Unit Types</h2>
                    <p class="text-xs text-gray-400 mt-1">Define what each property offers and the yearly price per type. Tenants get auto-priced when assigned to a type.</p>
                </div>
            </div>
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="font-semibold mb-3 text-slate-300">Add Unit Type</h3>
                <form id="addUnitTypeForm" class="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Property *</label>
                        <select id="utProperty" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white">
                            <option value="">-- Select property --</option>
                        </select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit Type Name *</label>
                        <input type="text" id="utName" required placeholder="e.g. Self-Contained Room (Ensuite)"
                               class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Yearly Price (₦)</label>
                        <input type="number" id="utPrice" step="1000" min="0"
                               class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Total Units</label>
                        <input type="number" id="utCount" min="0"
                               class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div class="md:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Description</label>
                        <input type="text" id="utDesc" placeholder="e.g. Ensuite room with private bathroom and kitchen"
                               class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div class="flex items-end gap-3">
                        <button type="submit" class="bg-teal-700 hover:bg-teal-800 text-white text-sm font-medium py-2 px-4 rounded-lg">Add Unit Type</button>
                        <span id="utMsg" class="text-sm"></span>
                    </div>
                </form>
            </div>
            <div id="unitTypesContainer" class="space-y-4"></div>
        </section>

        <!-- EDIT UNIT TYPE MODAL -->
        <div id="utEditModal" class="fixed inset-0 z-50 bg-black/70 hidden items-center justify-center p-4" style="display:none;">
            <div class="bg-gray-800 rounded-xl shadow-2xl w-full max-w-md p-5 border border-gray-700">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-lg font-semibold text-white">Edit Unit Type</h3>
                    <button type="button" onclick="closeUtEdit()" class="text-gray-400 hover:text-white"><i class="fas fa-times"></i></button>
                </div>
                <form id="utEditForm" class="space-y-3">
                    <input type="hidden" id="utEditId">
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Property</label>
                        <select id="utEditProperty" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Name</label>
                        <input type="text" id="utEditName" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Description</label>
                        <input type="text" id="utEditDesc" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Yearly Price (₦)</label>
                            <input type="number" id="utEditPrice" step="1000" min="0" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                        </div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Total Units</label>
                            <input type="number" id="utEditCount" min="0" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                        </div>
                    </div>
                    <p id="utEditMsg" class="text-sm hidden"></p>
                    <div class="flex gap-3 pt-1">
                        <button type="submit" class="flex-1 bg-blue-700 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Save</button>
                        <button type="button" onclick="closeUtEdit()" class="flex-1 bg-gray-700 hover:bg-gray-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Cancel</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- EDIT TENANT MODAL -->
        <div id="tenantEditModal" class="fixed inset-0 z-50 bg-black/70 hidden items-center justify-center p-4" style="display:none;">
            <div class="bg-gray-800 rounded-xl shadow-2xl w-full max-w-xl p-5 border border-gray-700 max-h-[90vh] overflow-y-auto">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-lg font-semibold text-white">Edit Tenant</h3>
                    <button type="button" onclick="closeTenantEdit()" class="text-gray-400 hover:text-white"><i class="fas fa-times"></i></button>
                </div>
                <form id="tenantEditForm" class="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <input type="hidden" id="tnEditId">
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Full Name</label>
                        <input type="text" id="tnEditName" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Email</label>
                        <input type="email" id="tnEditEmail" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Phone</label>
                        <input type="text" id="tnEditPhone" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Property</label>
                        <select id="tnEditProperty" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit Type</label>
                        <select id="tnEditUnitType" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit / Room No.</label>
                        <input type="text" id="tnEditUnit" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Yearly Rent (₦)</label>
                        <input type="number" id="tnEditRent" step="1000" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Status</label>
                        <select id="tnEditStatus" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="active">Active</option><option value="vacated">Vacated</option>
                        </select>
                    </div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Lease Start</label>
                        <input type="date" id="tnEditLeaseStart" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div><label class="block text-xs font-medium mb-1 text-gray-400">Lease End</label>
                        <input type="date" id="tnEditLeaseEnd" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                    <div class="md:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Serviced By <span class="text-gray-500">(payroll)</span></label>
                        <select id="tnEditServicedBy" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white">
                            <option value="">-- Not tracked --</option>
                        </select></div>
                    <div class="md:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label>
                        <textarea id="tnEditNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea></div>
                    <p id="tnEditMsg" class="text-sm hidden md:col-span-2"></p>
                    <div class="md:col-span-2 flex gap-3 pt-1">
                        <button type="submit" class="flex-1 bg-blue-700 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Save Changes</button>
                        <button type="button" onclick="closeTenantEdit()" class="flex-1 bg-gray-700 hover:bg-gray-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Cancel</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- PAYMENTS SECTION -->
        <section id="paymentsSection" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-4">Payments</h2>
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="font-semibold mb-3 text-slate-300">Record Payment</h3>
                <form id="addPaymentForm" class="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <input type="hidden" id="pmtEditId">
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Tenant</label>
                        <select id="pmtTenantId" onchange="pmtOnTenantChange()" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <option value="">-- Select tenant --</option>
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
                    <div class="flex items-end gap-3 flex-wrap">
                        <button type="submit" id="pmtSubmitBtn" class="bg-emerald-700 hover:bg-emerald-800 text-white text-sm font-medium py-2 px-4 rounded-lg">Record Payment</button>
                        <button type="button" id="pmtCancelBtn" onclick="cancelCeoPaymentEdit()" class="hidden bg-gray-600 hover:bg-gray-500 text-white text-sm font-medium py-2 px-4 rounded-lg">Cancel Edit</button>
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
            <!-- Project filter bar -->
            <div class="flex items-center gap-3 mb-4 flex-wrap">
                <div class="flex items-center gap-2 flex-1 min-w-0">
                    <i class="fas fa-filter text-slate-400 text-xs flex-shrink-0"></i>
                    <span class="text-xs text-slate-400 font-medium uppercase tracking-wide flex-shrink-0">View</span>
                    <select id="ceoProjectFilter" onchange="ceoApplyProjectFilter()" class="bg-slate-800 border border-slate-600 text-sm text-white rounded-lg px-3 py-1.5 flex-1 min-w-0 max-w-xs focus:outline-none focus:border-teal-500">
                        <option value="">All Projects (Portfolio)</option>
                    </select>
                </div>
                <span id="ceoFilterBadge" class="hidden text-xs bg-teal-900/60 border border-teal-700/40 text-teal-300 px-2.5 py-1 rounded-full flex-shrink-0"></span>
            </div>
            <!-- Charts row -->
            <div class="grid grid-cols-1 lg:grid-cols-7 gap-4 mb-6">
                <div class="lg:col-span-3 bg-gray-800/80 border border-gray-700/50 rounded-xl p-4 sm:p-5">
                    <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
                        <h3 class="text-sm font-semibold text-slate-300 flex items-center gap-2"><i class="fas fa-chart-bar text-teal-400 text-xs"></i> Revenue vs Spend</h3>
                        <div class="flex items-center gap-0.5 bg-slate-900/70 rounded-lg p-0.5">
                            <button onclick="ceoSetPeriod('3M')" id="ceoPeriod_3M" class="text-xs px-2.5 py-1 rounded-md font-medium transition-colors text-slate-400 hover:text-white">3M</button>
                            <button onclick="ceoSetPeriod('6M')" id="ceoPeriod_6M" class="text-xs px-2.5 py-1 rounded-md font-medium transition-colors bg-slate-600 text-white">6M</button>
                            <button onclick="ceoSetPeriod('12M')" id="ceoPeriod_12M" class="text-xs px-2.5 py-1 rounded-md font-medium transition-colors text-slate-400 hover:text-white">12M</button>
                            <button onclick="ceoSetPeriod('all')" id="ceoPeriod_all" class="text-xs px-2.5 py-1 rounded-md font-medium transition-colors text-slate-400 hover:text-white">All</button>
                        </div>
                    </div>
                    <div class="relative" style="height:190px"><canvas id="ceoRevenueChart"></canvas></div>
                </div>
                <div class="lg:col-span-2 bg-gray-800/80 border border-gray-700/50 rounded-xl p-4 sm:p-5">
                    <div class="flex items-center justify-between mb-3">
                        <h3 class="text-sm font-semibold text-slate-300 flex items-center gap-2"><i class="fas fa-circle-dot text-purple-400 text-xs"></i> Tenant Status</h3>
                    </div>
                    <div class="relative" style="height:190px"><canvas id="ceoTenantDonut"></canvas></div>
                </div>
                <div class="lg:col-span-2 bg-gray-800/80 border border-gray-700/50 rounded-xl p-4 sm:p-5">
                    <div class="flex items-center justify-between mb-3">
                        <h3 class="text-sm font-semibold text-slate-300 flex items-center gap-2"><i class="fas fa-receipt text-amber-400 text-xs"></i> Expense Categories</h3>
                    </div>
                    <div class="relative" style="height:190px">
                        <canvas id="ceoCategoryDonut"></canvas>
                        <div id="ceoCatEmpty" class="hidden absolute inset-0 flex flex-col items-center justify-center gap-1">
                            <i class="fas fa-receipt text-slate-600 text-2xl"></i>
                            <p class="text-xs text-slate-500 text-center">No expenses recorded yet</p>
                            <p class="text-[10px] text-slate-600 text-center">Add expenses in the Expenses tab</p>
                        </div>
                    </div>
                </div>
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
                            <option value="hostel">Apartment</option>
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
                    <div>
                        <label class="block text-sm font-medium mb-2">Capital Budget (₦)</label>
                        <input type="number" id="capital_budget" step="1000" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg">
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
            <!-- Project selector + form -->
            <div class="bg-gray-800 rounded-xl p-5 mb-4">
                <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
                    <div>
                        <p id="ceoConstrFormLabel" class="font-semibold text-slate-300 text-sm">Post New Update</p>
                        <p class="text-xs text-gray-500 mt-0.5">Add or edit a milestone on the selected project</p>
                    </div>
                    <div class="flex items-center gap-2 flex-wrap">
                        <label class="text-xs text-gray-400">Project:</label>
                        <select id="ceoConstructionProperty" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm w-full sm:w-auto sm:min-w-[180px]"></select>
                    </div>
                </div>
                <form id="ceoConstructionForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <input type="hidden" id="ceoConstructionEditId">
                    <div class="sm:col-span-2">
                        <label class="block text-xs font-medium mb-1 text-gray-400">Milestone Title *</label>
                        <input type="text" id="ceoConstructionTitle" placeholder="e.g. Foundation complete, Finishing stage" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Progress % (0–100)</label>
                        <input type="number" min="0" max="100" id="ceoConstructionPercent" placeholder="e.g. 85" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-medium mb-1 text-gray-400">Date</label>
                        <input type="date" id="ceoConstructionDate" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div class="sm:col-span-2">
                        <label class="block text-xs font-medium mb-1 text-gray-400">Notes</label>
                        <textarea id="ceoConstructionNotes" rows="2" placeholder="Optional detail about this milestone" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea>
                    </div>
                    <div class="sm:col-span-2 flex items-center gap-3 flex-wrap">
                        <button type="submit" id="ceoConstrSubmitBtn" class="bg-emerald-700 hover:bg-emerald-800 text-white font-medium py-2 px-5 rounded-lg text-sm">Post Update</button>
                        <button type="button" id="ceoConstrCancelBtn" class="hidden bg-gray-600 hover:bg-gray-500 text-white font-medium py-2 px-4 rounded-lg text-sm" onclick="cancelConstructionEdit('ceo')">Cancel Edit</button>
                        <span id="ceoConstructionMsg" class="text-sm"></span>
                    </div>
                </form>
            </div>
            <!-- Timeline list -->
            <div class="bg-gray-800 rounded-xl p-5">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <h3 class="font-semibold text-slate-300">Project Timeline</h3>
                    <span id="ceoConstructionHeadline" class="text-sm text-emerald-400 font-medium">0%</span>
                </div>
                <div id="ceoConstructionList" class="space-y-3"></div>
            </div>
        </section>

        <section id="capitalSection" class="mb-8 hidden">
            <h2 class="text-xl font-semibold mb-2">Capital Calculation</h2>
            <p class="text-sm text-gray-400 mb-5">Track all project spend — materials, labour, logistics, permits, and more. CEO and accountant approvals count against committed capital.</p>
            <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                <div class="bg-emerald-900 rounded-xl p-3 overflow-hidden"><p class="text-xs text-emerald-300 uppercase tracking-wide mb-1 truncate">Approved Spend</p><p id="capApprovedTotal" class="text-lg font-bold text-white truncate">₦0</p></div>
                <div class="bg-amber-900 rounded-xl p-3 overflow-hidden"><p class="text-xs text-amber-300 uppercase tracking-wide mb-1 truncate">Pending Approval</p><p id="capPendingTotal" class="text-lg font-bold text-white truncate">₦0</p></div>
                <div class="bg-red-900 rounded-xl p-3 overflow-hidden"><p class="text-xs text-red-300 uppercase tracking-wide mb-1 truncate">Rejected</p><p id="capRejectedTotal" class="text-lg font-bold text-white truncate">₦0</p></div>
                <div class="bg-cyan-900 rounded-xl p-3 overflow-hidden"><p class="text-xs text-cyan-300 uppercase tracking-wide mb-1 truncate">Budget Remaining</p><p id="capBudgetRemaining" class="text-lg font-bold text-white truncate">—</p></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-4 mb-4 flex flex-col gap-3">
                <div class="flex flex-col sm:flex-row sm:items-center gap-3 flex-wrap">
                    <label class="text-sm text-gray-400 flex-shrink-0">Project:</label>
                    <select id="ceoCapitalProperty" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm w-full sm:w-auto sm:min-w-[220px]"></select>
                    <div class="flex items-center gap-3 flex-wrap">
                        <span class="text-xs text-gray-400">Capital Budget: <span id="capBudgetTotal" class="text-white font-semibold">—</span></span>
                        <button type="button" onclick="toggleBudgetEdit()" class="text-xs text-amber-400 hover:text-amber-300 border border-amber-700/50 rounded px-2.5 py-1 transition-colors">Set / Edit Budget</button>
                    </div>
                </div>
                <div id="budgetEditRow" class="hidden flex-col sm:flex-row items-start sm:items-center gap-3 flex-wrap">
                    <label class="text-xs text-gray-400 flex-shrink-0">New Capital Budget (₦):</label>
                    <input type="number" id="budgetEditInput" step="100000" placeholder="e.g. 25000000" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm w-full sm:w-48">
                    <button type="button" onclick="saveBudget()" class="bg-amber-700 hover:bg-amber-600 text-white text-xs font-medium py-2 px-4 rounded-lg">Save Budget</button>
                    <button type="button" onclick="toggleBudgetEdit()" class="text-xs text-gray-400 hover:text-white py-2">Cancel</button>
                    <span id="budgetEditMsg" class="text-xs"></span>
                </div>
                <p class="text-xs text-gray-600">Approve / Reject buttons appear on each expense card once recorded below.</p>
            </div>
            <div class="grid grid-cols-1 xl:grid-cols-[0.95fr_1.05fr] gap-4">
                <div class="bg-gray-800 rounded-xl p-5">
                    <div class="flex items-center justify-between gap-3 mb-4">
                        <div>
                            <h3 class="font-semibold text-slate-300">Record Expense</h3>
                            <p class="text-xs text-gray-500 mt-0.5">Cement, iron rods, bricklayer wages, diesel, permits...</p>
                        </div>
                        <div class="text-right">
                            <p class="text-xs text-gray-500 uppercase tracking-wide">Total Recorded</p>
                            <p id="ceoExpenseTotal" class="text-lg font-bold text-amber-300">₦0</p>
                        </div>
                    </div>
                    <form id="ceoExpenseForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <input type="hidden" id="ceoExpenseEditId">
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Expense Date *</label><input type="date" id="ceoExpenseDate" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Category *</label><select id="ceoExpenseCategory" onchange="updateExpenseForm('ceo')" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="materials">Materials</option><option value="labour">Labour</option><option value="transport">Transport</option><option value="equipment">Equipment</option><option value="permits">Permits</option><option value="other">Other</option></select></div>
                        <div class="sm:col-span-2" id="ceoExpenseItemRow"><label id="ceoExpenseItemLabel" class="block text-xs font-medium mb-1 text-gray-400">Item / Purchase *</label><input type="text" id="ceoExpenseItem" placeholder="Cement, iron rods, electrical fittings..." class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div class="sm:col-span-2 hidden" id="ceoExpenseLabourHint"><p class="text-xs text-amber-300 bg-amber-900/30 border border-amber-700/40 rounded-lg px-3 py-2">Labour entry — fill in the worker name in <strong>Paid To</strong> and the total <strong>Amount</strong>. No item or quantity needed.</p></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400" id="ceoExpensePayeeLabel">Paid To / Supplier</label><input list="expensePayeeOptions" type="text" id="ceoExpensePayee" placeholder="Supplier or worker name" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Amount (₦) *</label><input type="number" id="ceoExpenseAmount" step="100" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div id="ceoExpenseQtyRow"><label class="block text-xs font-medium mb-1 text-gray-400">Quantity</label><input type="number" id="ceoExpenseQuantity" step="0.01" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div id="ceoExpenseUnitRow"><label class="block text-xs font-medium mb-1 text-gray-400">Unit Cost (₦)</label><input type="number" id="ceoExpenseUnitCost" step="0.01" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Receipt / Proof</label><input type="file" id="ceoExpenseReceipt" accept="image/*,.pdf" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm file:mr-3 file:rounded file:border-0 file:bg-gray-600 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-white hover:file:bg-gray-500"><p class="text-[11px] text-gray-500 mt-1">Optional. Upload invoice, receipt, transfer slip, or proof of payment.</p></div>
                        <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label><textarea id="ceoExpenseNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea></div>
                        <div class="sm:col-span-2 flex items-center gap-3 flex-wrap"><button type="submit" id="ceoExpenseSubmitBtn" class="bg-amber-700 hover:bg-amber-600 text-white font-medium py-2 px-5 rounded-lg text-sm">Save Expense</button><button type="button" id="ceoExpenseCancelBtn" class="hidden bg-gray-600 hover:bg-gray-500 text-white font-medium py-2 px-4 rounded-lg text-sm" onclick="ceoCancelExpenseEdit()">Cancel Edit</button><span id="ceoExpenseMsg" class="text-sm"></span></div>
                        <p class="sm:col-span-2 text-[11px] text-gray-500">CEO and accountant approvals determine what counts against committed capital.</p>
                    </form>
                </div>
                <div class="bg-gray-800 rounded-xl p-5">
                    <div class="flex items-center justify-between gap-3 mb-4">
                        <h3 class="font-semibold text-slate-300">Recorded Expenses</h3>
                        <div id="ceoExpenseBreakdown" class="text-xs text-gray-400 text-right"></div>
                    </div>
                    <div class="flex items-center gap-2 flex-wrap mb-4">
                        <select id="ceoExpenseStatusFilter" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-xs"><option value="">All statuses</option><option value="pending">Pending</option><option value="approved">Approved</option><option value="rejected">Rejected</option></select>
                        <select id="ceoExpenseCategoryFilter" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-xs"><option value="">All categories</option><option value="materials">Materials</option><option value="labour">Labour</option><option value="transport">Transport</option><option value="equipment">Equipment</option><option value="permits">Permits</option><option value="land">Land</option><option value="other">Other</option></select>
                        <label class="inline-flex items-center gap-2 text-xs text-gray-400 px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"><input type="checkbox" id="ceoExpenseReceiptOnly" class="rounded border-gray-500 bg-gray-800">Receipts only</label>
                    </div>
                    <div id="ceoExpenseList" class="space-y-3"></div>
                </div>
            </div>
        </section>

        <!-- MAINTENANCE SECTION -->
        <section id="maintenanceSection" class="mb-8 hidden">
            <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
                <h2 class="text-xl font-semibold">Maintenance Log</h2>
                <button onclick="ceoToggleMaintenanceForm()" class="bg-teal-700 hover:bg-teal-600 text-white text-sm font-medium px-4 py-2 rounded-lg flex items-center gap-2"><i class="fas fa-plus text-xs"></i> Record Maintenance</button>
            </div>
            <!-- Filters -->
            <div class="flex flex-wrap gap-3 mb-4 items-center">
                <select id="ceoMaintProperty" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="">All properties</option></select>
                <select id="ceoMaintCategory" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-xs"><option value="">All categories</option><option value="general">General</option><option value="plumbing">Plumbing</option><option value="electrical">Electrical</option><option value="painting">Painting</option><option value="roofing">Roofing</option><option value="cleaning">Cleaning</option><option value="security">Security</option><option value="other">Other</option></select>
                <button onclick="loadMaintenanceRecords()" class="px-3 py-2 bg-gray-700 hover:bg-gray-600 text-white text-xs rounded-lg">Filter</button>
                <span id="ceoMaintSummary" class="text-xs text-gray-400 ml-auto"></span>
            </div>
            <!-- Add/Edit form -->
            <div id="ceoMaintForm" class="hidden bg-gray-800 rounded-xl p-4 mb-4 border border-gray-700">
                <h3 class="text-sm font-semibold text-gray-300 mb-3" id="ceoMaintFormTitle">New Maintenance Record</h3>
                <input type="hidden" id="ceoMaintEditId">
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
                    <div>
                        <label class="block text-xs text-gray-400 mb-1">Property *</label>
                        <select id="ceoMaintFormProperty" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="">Select property</option></select>
                    </div>
                    <div>
                        <label class="block text-xs text-gray-400 mb-1">Date *</label>
                        <input type="date" id="ceoMaintDate" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white">
                    </div>
                    <div class="sm:col-span-2">
                        <label class="block text-xs text-gray-400 mb-1">Title *</label>
                        <input type="text" id="ceoMaintTitle" placeholder="e.g. Fixed roof leak in unit 3A" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-500">
                    </div>
                    <div>
                        <label class="block text-xs text-gray-400 mb-1">Category</label>
                        <select id="ceoMaintFormCategory" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="general">General</option><option value="plumbing">Plumbing</option><option value="electrical">Electrical</option><option value="painting">Painting</option><option value="roofing">Roofing</option><option value="cleaning">Cleaning</option><option value="security">Security</option><option value="other">Other</option></select>
                    </div>
                    <div>
                        <label class="block text-xs text-gray-400 mb-1">Status</label>
                        <select id="ceoMaintStatus" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="completed">Completed</option><option value="in_progress">In Progress</option><option value="scheduled">Scheduled</option></select>
                    </div>
                    <div>
                        <label class="block text-xs text-gray-400 mb-1">Vendor / Contractor</label>
                        <input type="text" id="ceoMaintVendor" placeholder="Vendor or contractor name" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-500">
                    </div>
                    <div>
                        <label class="block text-xs text-gray-400 mb-1">Cost (₦)</label>
                        <input type="number" id="ceoMaintCost" placeholder="0" min="0" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-500">
                    </div>
                    <div class="sm:col-span-2">
                        <label class="block text-xs text-gray-400 mb-1">Notes / Description</label>
                        <textarea id="ceoMaintDesc" rows="2" placeholder="Any additional details..." class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-500 resize-none"></textarea>
                    </div>
                </div>
                <div class="flex items-center gap-3">
                    <button onclick="ceoSubmitMaintenance()" class="bg-teal-700 hover:bg-teal-600 text-white text-sm font-medium px-4 py-2 rounded-lg">Save Record</button>
                    <button onclick="ceoToggleMaintenanceForm(true)" class="bg-gray-700 hover:bg-gray-600 text-white text-sm font-medium px-4 py-2 rounded-lg">Cancel</button>
                    <span id="ceoMaintMsg" class="text-sm ml-2"></span>
                </div>
            </div>
            <!-- Records list -->
            <div id="ceoMaintList" class="space-y-3"><p class="text-gray-500 text-sm text-center py-8">Loading...</p></div>
        </section>

        <section id="contentSection" class="mb-8 hidden">
            <!-- Header row -->
            <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
                <h2 class="text-xl font-semibold">Website Content Management</h2>
                <div class="flex items-center gap-2 flex-wrap">
                    <button type="button" onclick="openWebsitePreview()" class="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors">
                        <i class="fas fa-eye text-xs"></i> Preview Draft
                    </button>
                    <a href="https://www.brightwavehabitat.com/" target="_blank" rel="noopener noreferrer" class="flex items-center gap-2 bg-blue-700 hover:bg-blue-600 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors">
                        <i class="fas fa-external-link-alt text-xs"></i> Open Live Site
                    </a>
                </div>
            </div>
            <!-- Publish banner -->
            <div id="publishBanner" class="hidden bg-amber-900/60 border border-amber-600 rounded-lg px-4 py-3 mb-5 flex flex-wrap items-center justify-between gap-3">
                <div class="flex items-center gap-2 text-amber-300 text-sm">
                    <i class="fas fa-exclamation-circle"></i>
                    <span>You have <strong id="draftPendingCount">0</strong> unpublished change(s). Save as draft keeps them private; Publish pushes everything live.</span>
                </div>
                <button type="button" onclick="publishWebsite()" id="publishBtn" class="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white font-semibold py-2 px-5 rounded-lg text-sm transition-colors">
                    <i class="fas fa-rocket text-xs"></i> Publish to Website
                </button>
            </div>

            <!-- Announcement Banner -->
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="text-base font-semibold text-slate-300 mb-3 flex items-center gap-2"><i class="fas fa-bullhorn text-emerald-400"></i> Site Announcement Banner</h3>
                <p class="text-xs text-gray-400 mb-3">When enabled, a banner appears at the very top of every page on the website. Use it for promotions, news, or important announcements.</p>
                <form id="announcementForm" class="grid grid-cols-1 gap-3">
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">Banner Text</label>
                        <input type="text" id="home_announcement_text" placeholder="e.g. Phase 2 bookings now open — enquire today" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div class="flex flex-wrap items-center gap-4">
                        <label class="flex items-center gap-2 cursor-pointer">
                            <input type="checkbox" id="home_announcement_enabled" class="accent-emerald-500 w-4 h-4">
                            <span class="text-sm font-medium text-gray-300">Show banner on website</span>
                        </label>
                        <button type="submit" class="bg-emerald-700 hover:bg-emerald-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Save Draft</button>
                        <span id="announcementMessage" class="text-sm"></span>
                    </div>
                </form>
            </div>

            <!-- Hero Background Image -->
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="text-base font-semibold text-slate-300 mb-3 flex items-center gap-2"><i class="fas fa-image text-blue-400"></i> Hero Background Image</h3>
                <p class="text-xs text-gray-400 mb-3">Upload a photo to use as the homepage hero background. It will appear as the first slide behind the hero text. Recommended: landscape JPG/WEBP, min 1920×1080.</p>
                <div id="heroBgPreviewWrap" class="mb-3 hidden">
                    <p class="text-xs text-gray-400 mb-1">Current hero background:</p>
                    <img id="heroBgPreview" src="" alt="Hero background preview" class="h-28 w-full object-cover rounded-lg border border-gray-600">
                    <p id="heroBgCurrentPath" class="text-xs text-gray-500 mt-1"></p>
                </div>
                <div class="flex items-end gap-3 flex-wrap">
                    <div class="flex-1 min-w-[200px]">
                        <label class="block text-sm font-medium mb-1 text-gray-300">Select Image File</label>
                        <input type="file" id="heroBgFileInput" accept="image/*" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <button type="button" onclick="uploadHeroBg()" class="bg-blue-700 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg text-sm whitespace-nowrap">Upload &amp; Set</button>
                    <button type="button" onclick="clearHeroBg()" class="bg-gray-600 hover:bg-gray-500 text-white font-medium py-2 px-4 rounded-lg text-sm whitespace-nowrap">Clear Background</button>
                </div>
                <span id="heroBgMessage" class="block mt-2 text-sm"></span>
            </div>

            <!-- Video Section -->
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="text-base font-semibold text-slate-300 mb-3 flex items-center gap-2"><i class="fas fa-video text-pink-400"></i> Homepage Video Section</h3>
                <form id="videoSectionForm" class="grid grid-cols-1 gap-4">
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">Section Title</label>
                        <input type="text" id="home_video_section_title" placeholder="See BrightWave in Action" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <!-- Video source tabs -->
                    <div>
                        <div class="flex gap-2 mb-2">
                            <button type="button" id="videoTabUrl" onclick="switchVideoTab('url')" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-pink-700 text-white">Paste URL</button>
                            <button type="button" id="videoTabUpload" onclick="switchVideoTab('upload')" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-gray-600 text-gray-300">Upload from Device</button>
                        </div>
                        <div id="videoPanelUrl">
                            <label class="block text-xs text-gray-400 mb-1">YouTube embed URL or direct MP4 link</label>
                            <input type="text" id="home_video_url" placeholder="https://www.youtube.com/embed/VIDEO_ID" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                        </div>
                        <div id="videoPanelUpload" class="hidden">
                            <label class="block text-xs text-gray-400 mb-1">Select a video from your device (MP4, WEBM, MOV — max 200 MB)</label>
                            <input type="file" id="videoFileInput" accept="video/mp4,video/webm,video/quicktime,video/ogg,.mp4,.webm,.mov,.ogg,.m4v" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                            <div class="mt-2 flex flex-wrap items-center gap-2">
                                <button type="button" onclick="uploadSiteVideo()" class="bg-pink-700 hover:bg-pink-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Upload Video</button>
                                <span id="videoUploadMessage" class="text-sm"></span>
                            </div>
                            <div id="videoUploadProgress" class="hidden mt-2">
                                <div class="bg-gray-600 rounded-full h-2"><div id="videoProgressBar" class="bg-pink-500 h-2 rounded-full transition-all" style="width:0%"></div></div>
                                <p id="videoProgressText" class="text-xs text-gray-400 mt-1">Uploading…</p>
                            </div>
                        </div>
                    </div>
                    <div class="flex flex-wrap items-center gap-4">
                        <label class="flex items-center gap-2 cursor-pointer">
                            <input type="checkbox" id="home_video_section_enabled" class="accent-emerald-500 w-4 h-4">
                            <span class="text-sm font-medium text-gray-300">Show video section on homepage</span>
                        </label>
                        <button type="submit" class="bg-pink-700 hover:bg-pink-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Save Draft</button>
                        <span id="videoSectionMessage" class="text-sm"></span>
                    </div>
                </form>
            </div>

            <!-- Text Content -->
            <div class="bg-gray-800 p-4 rounded-lg">
                <h3 class="text-base font-semibold text-slate-300 mb-3 flex items-center gap-2"><i class="fas fa-pen text-amber-400"></i> Page Text Content</h3>
                <form id="siteContentForm" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">Home Hero Badge</label>
                        <input type="text" id="home_hero_badge" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">Home Hero Title</label>
                        <input type="text" id="home_hero_title" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-1 text-gray-300">Home Hero Subtitle</label>
                        <textarea id="home_hero_subtitle" rows="3" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea>
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-1 text-gray-300">Home About Intro</label>
                        <textarea id="home_about_intro" rows="3" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea>
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-1 text-gray-300">About Page Hero Subtitle</label>
                        <textarea id="about_hero_subtitle" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea>
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-1 text-gray-300">About Page Intro Body</label>
                        <textarea id="about_intro_body" rows="4" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">About Team Heading</label>
                        <input type="text" id="about_team_heading" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">About Team Subheading</label>
                        <input type="text" id="about_team_subheading" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div class="md:col-span-2 flex flex-wrap items-center gap-3">
                        <button type="submit" class="bg-slate-600 hover:bg-slate-700 text-white font-medium py-2 px-4 rounded-lg">Save Draft</button>
                        <button type="button" id="saveAndPreviewBtn" class="flex items-center gap-2 bg-blue-700 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg">
                            <i class="fas fa-eye text-xs"></i> Save &amp; Preview Draft
                        </button>
                        <span id="siteContentMessage" class="text-sm"></span>
                    </div>
                </form>
            </div>

            <!-- Contact Info & Social Links -->
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="text-base font-semibold text-slate-300 mb-3 flex items-center gap-2"><i class="fas fa-address-book text-rose-400"></i> Contact Info &amp; Social Links</h3>
                <p class="text-xs text-gray-400 mb-3">Used in the website footer, WhatsApp speed-dial buttons, and anywhere contact details appear.</p>
                <form id="contactInfoForm" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">Contact Email</label>
                        <input type="email" id="site_contact_email" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="brightwavehabitat@gmail.com">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">Contact Phone</label>
                        <input type="text" id="site_contact_phone" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="+234 803 766 9462">
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-1 text-gray-300">Address</label>
                        <input type="text" id="site_contact_address" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="Malete, Kwara State, Nigeria">
                    </div>
                    <div class="md:col-span-2">
                        <label class="block text-sm font-medium mb-1 text-gray-300">Footer Company Description</label>
                        <textarea id="site_footer_company_desc" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="Building trusted property opportunities across Nigeria…"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300"><i class="fab fa-whatsapp text-emerald-400 mr-1"></i> WhatsApp — Student Hostels</label>
                        <input type="text" id="site_wa_number_hostel" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="2348037669462 (no + sign)">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300"><i class="fab fa-whatsapp text-emerald-400 mr-1"></i> WhatsApp — Land Purchase</label>
                        <input type="text" id="site_wa_number_land" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="2349038402914">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300"><i class="fab fa-whatsapp text-emerald-400 mr-1"></i> WhatsApp — Residential</label>
                        <input type="text" id="site_wa_number_home" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="2349075381905">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300"><i class="fab fa-facebook text-blue-400 mr-1"></i> Facebook URL</label>
                        <input type="url" id="site_social_facebook" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="https://facebook.com/...">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300"><i class="fab fa-x-twitter text-gray-300 mr-1"></i> X (Twitter) URL</label>
                        <input type="url" id="site_social_twitter" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="https://x.com/...">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300"><i class="fab fa-instagram text-pink-400 mr-1"></i> Instagram URL</label>
                        <input type="url" id="site_social_instagram" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm" placeholder="https://instagram.com/...">
                    </div>
                    <div class="md:col-span-2 flex flex-wrap items-center gap-3">
                        <button type="submit" class="bg-rose-700 hover:bg-rose-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Save Draft</button>
                        <span id="contactInfoMessage" class="text-sm"></span>
                    </div>
                </form>
            </div>

            <!-- Services Section Text -->
            <div class="bg-gray-800 p-4 rounded-lg mb-4">
                <h3 class="text-base font-semibold text-slate-300 mb-3 flex items-center gap-2"><i class="fas fa-concierge-bell text-purple-400"></i> Services Section</h3>
                <form id="servicesSectionForm" class="grid grid-cols-1 gap-4">
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">Services Heading</label>
                        <input type="text" id="home_services_heading" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1 text-gray-300">Services Lead Text</label>
                        <textarea id="home_services_lead" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea>
                    </div>
                    <div class="flex items-center gap-3">
                        <button type="submit" class="bg-purple-700 hover:bg-purple-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Save Draft</button>
                        <span id="servicesSectionMessage" class="text-sm"></span>
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
            <div class="bg-gray-800 p-4 rounded-lg">
                <div class="overflow-x-auto"><table class="w-full text-sm min-w-[640px]">
                    <thead>
                        <tr class="border-b border-gray-600">
                            <th class="py-2 text-left">Title</th>
                            <th class="py-2 text-left">Type</th>
                            <th class="py-2 text-left">Location</th>
                            <th class="py-2 text-left">Status</th>
                            <th class="py-2 text-left">Budget</th>
                            <th class="py-2 text-left">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="propertiesTable">
                        <!-- Properties will be populated by JavaScript -->
                    </tbody>
                </table></div>
            </div>
        </section>

        <!-- Inquiries and Messages Tabs -->
        <section id="inquiriesSection2" class="hidden">
            <div class="flex flex-wrap gap-2 mb-4">
                <button id="inquiriesTab" class="bg-slate-600 text-white px-4 py-2 rounded-lg text-sm">Property Inquiries</button>
                <button id="messagesTab" class="bg-gray-600 text-white px-4 py-2 rounded-lg text-sm">Contact Messages</button>
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
        function fmtCompact(v) {
            const n = Number(v || 0);
            if (n >= 1e9) return '\u20a6' + (n/1e9).toFixed(1).replace(/\.0$/,'') + 'B';
            if (n >= 1e6) return '\u20a6' + (n/1e6).toFixed(1).replace(/\.0$/,'') + 'M';
            if (n >= 1e3) return '\u20a6' + Math.round(n/1e3) + 'K';
            return fmtNGN(v);
        }

        function countUp(elId, target, fmt) {
            const el = document.getElementById(elId);
            if (!el) return;
            const num = parseFloat(target) || 0;
            if (!num) { el.textContent = fmt ? fmt(0) : '0'; return; }
            const dur = 700, t0 = performance.now();
            const step = ts => {
                const p = Math.min((ts - t0) / dur, 1);
                const v = num * (1 - Math.pow(1 - p, 3));
                el.textContent = fmt ? fmt(v) : Math.round(v);
                if (p < 1) requestAnimationFrame(step); else el.textContent = fmt ? fmt(num) : num;
            };
            requestAnimationFrame(step);
        }

        function _trendSlice(monthly, yearly, period) {
            if (period === 'all') return { labels: yearly.map(y => y.year), revenue: yearly.map(y => y.revenue), capital: yearly.map(y => y.capital) };
            const n = period === '3M' ? 3 : period === '6M' ? 6 : 12;
            const s = monthly.slice(-n);
            return { labels: s.map(m => m.month), revenue: s.map(m => m.revenue), capital: s.map(m => m.capital) };
        }
        function _setPeriodBtns(prefix, active) {
            ['3M','6M','12M','all'].forEach(x => {
                const btn = document.getElementById(prefix + x);
                if (btn) btn.className = 'text-xs px-2.5 py-1 rounded-md font-medium transition-colors ' + (x === active ? 'bg-slate-600 text-white' : 'text-slate-400 hover:text-white');
            });
        }
        window._ceoTrendPeriod = window._ceoTrendPeriod || '6M';
        function ceoSetPeriod(p) {
            window._ceoTrendPeriod = p;
            _setPeriodBtns('ceoPeriod_', p);
            const full = window._ceoTrendDataFull || { monthly: [], yearly: [] };
            const d = _trendSlice(full.monthly, full.yearly, p);
            if (window._ceoRevChart) { window._ceoRevChart.data.labels = d.labels; window._ceoRevChart.data.datasets[0].data = d.revenue; window._ceoRevChart.data.datasets[1].data = d.capital; window._ceoRevChart.update(); }
        }

        function ceoApplyProjectFilter() {
            const pid = document.getElementById('ceoProjectFilter').value;
            loadStats(pid ? parseInt(pid) : null);
        }

        async function ceoPopulateProjectFilter() {
            try {
                const props = await fetchData('/admin/api/properties');
                const sel = document.getElementById('ceoProjectFilter');
                if (!sel) return;
                props.forEach(p => {
                    const opt = document.createElement('option');
                    opt.value = p.id;
                    opt.textContent = p.title;
                    sel.appendChild(opt);
                });
            } catch(e) { console.warn('Could not load properties for filter', e); }
        }

        async function loadStats(filterPropertyId) {
            try {
                const qs = filterPropertyId ? ('?property_id=' + filterPropertyId) : '';
                const stats = await fetchData('/admin/api/stats' + qs);
                // Update filter badge
                const badge = document.getElementById('ceoFilterBadge');
                if (badge) {
                    if (stats.filtered_property) {
                        badge.textContent = stats.filtered_property.title;
                        badge.classList.remove('hidden');
                    } else {
                        badge.classList.add('hidden');
                    }
                }

                // Attention bar
                const attnBar = document.getElementById('ceoAttentionBar');
                if (attnBar) {
                    const items = [];
                    if (PENDING_SIGS_COUNT > 0) items.push(`<div class="flex flex-wrap items-center justify-between gap-2 bg-red-900/40 border border-red-700/50 rounded-xl px-3 sm:px-4 py-3"><div class="flex items-center gap-2 min-w-0"><span class="w-2 h-2 rounded-full bg-red-400 flex-shrink-0 animate-pulse"></span><p class="text-sm text-red-200 font-medium">${PENDING_SIGS_COUNT} contract${PENDING_SIGS_COUNT > 1 ? 's' : ''} waiting for your co-signature</p></div><button onclick="showSection('signaturesSection')" class="text-xs text-red-300 border border-red-600 rounded-lg px-3 py-1.5 hover:bg-red-800/40 transition-colors flex-shrink-0">Review Now</button></div>`);
                    if (stats.new_inquiries > 0) items.push(`<div class="flex flex-wrap items-center justify-between gap-2 bg-amber-900/30 border border-amber-700/40 rounded-xl px-3 sm:px-4 py-3"><div class="flex items-center gap-2 min-w-0"><span class="w-2 h-2 rounded-full bg-amber-400 flex-shrink-0"></span><p class="text-sm text-amber-200 font-medium">${stats.new_inquiries} new inquiry${stats.new_inquiries > 1 ? 'ies' : ''} not yet actioned</p></div><button onclick="showSection('inquiriesSection2')" class="text-xs text-amber-300 border border-amber-600 rounded-lg px-3 py-1.5 hover:bg-amber-800/30 transition-colors flex-shrink-0">View Inquiries</button></div>`);
                    if (stats.available_units > 0 && stats.active_tenants === 0) items.push(`<div class="flex flex-wrap items-center justify-between gap-2 bg-blue-900/30 border border-blue-700/40 rounded-xl px-3 sm:px-4 py-3"><div class="flex items-center gap-2 min-w-0"><span class="w-2 h-2 rounded-full bg-blue-400 flex-shrink-0"></span><p class="text-sm text-blue-200 font-medium">${stats.available_units} unit${stats.available_units > 1 ? 's' : ''} available — no active tenants recorded</p></div><button onclick="showSection('tenantsSection')" class="text-xs text-blue-300 border border-blue-600 rounded-lg px-3 py-1.5 hover:bg-blue-800/30 transition-colors flex-shrink-0">Manage Tenants</button></div>`);
                    attnBar.innerHTML = items.join('');
                    attnBar.classList.toggle('hidden', items.length === 0);
                }

                // Business metrics row
                document.getElementById('businessStats').innerHTML = `
                    <div class="bg-teal-800/60 border border-teal-700/40 p-4 rounded-xl flex items-start gap-3 shadow overflow-hidden">
                        <div class="w-9 h-9 bg-teal-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-users text-teal-300 text-sm"></i>
                        </div>
                        <div class="min-w-0">
                            <p class="text-xs text-teal-400 font-medium uppercase tracking-wide truncate">Active Tenants</p>
                            <p class="text-xl sm:text-2xl font-bold text-white mt-0.5">${stats.active_tenants}</p>
                            <p class="text-xs text-teal-300 mt-0.5 truncate">${stats.total_tenants} total recorded</p>
                        </div>
                    </div>
                    <div class="bg-emerald-800/60 border border-emerald-700/40 p-4 rounded-xl flex items-start gap-3 shadow overflow-hidden">
                        <div class="w-9 h-9 bg-emerald-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-money-bill-wave text-emerald-300 text-sm"></i>
                        </div>
                        <div class="min-w-0 overflow-hidden">
                            <p class="text-xs text-emerald-400 font-medium uppercase tracking-wide truncate">This Month Revenue</p>
                            <p class="text-lg sm:text-xl font-bold text-white mt-0.5 truncate">${fmtCompact(stats.monthly_revenue)}</p>
                            <p class="text-xs text-emerald-300 mt-0.5 truncate">All time: ${fmtCompact(stats.total_revenue)}</p>
                        </div>
                    </div>
                    <div class="bg-slate-700/80 border border-slate-600/40 p-4 rounded-xl flex items-start gap-3 shadow overflow-hidden">
                        <div class="w-9 h-9 bg-slate-600 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-building text-slate-300 text-sm"></i>
                        </div>
                        <div class="min-w-0">
                            <p class="text-xs text-slate-400 font-medium uppercase tracking-wide truncate">Properties</p>
                            <p class="text-xl sm:text-2xl font-bold text-white mt-0.5">${stats.total_properties}</p>
                            <p class="text-xs text-slate-400 mt-0.5 truncate">${stats.active_properties} active &bull; Apt:${stats.property_breakdown.hostels} Land:${stats.property_breakdown.land_plots} Res:${stats.property_breakdown.residential}</p>
                        </div>
                    </div>
                    <div class="bg-amber-800/60 border border-amber-700/40 p-4 rounded-xl flex items-start gap-3 shadow overflow-hidden">
                        <div class="w-9 h-9 bg-amber-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-hammer text-amber-300 text-sm"></i>
                        </div>
                        <div class="min-w-0 overflow-hidden">
                            <p class="text-xs text-amber-400 font-medium uppercase tracking-wide truncate">Capital Spent</p>
                            <p class="text-lg sm:text-xl font-bold text-white mt-0.5 truncate">${fmtCompact(stats.approved_capital_spent)}</p>
                            <p class="text-xs text-amber-300 mt-0.5 truncate">This month: ${fmtCompact(stats.monthly_capital_spent)}${stats.pending_capital_spent > 0 ? ' · <span class="text-yellow-400">Pending: ' + fmtCompact(stats.pending_capital_spent) + '</span>' : ''}</p>
                        </div>
                    </div>
                    <div class="bg-cyan-800/60 border border-cyan-700/40 p-4 rounded-xl flex items-start gap-3 shadow overflow-hidden">
                        <div class="w-9 h-9 bg-cyan-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-scale-balanced text-cyan-300 text-sm"></i>
                        </div>
                        <div class="min-w-0 overflow-hidden">
                            <p class="text-xs text-cyan-400 font-medium uppercase tracking-wide truncate">Budget Position</p>
                            <p class="text-lg sm:text-xl font-bold ${stats.capital_budget_remaining < 0 ? 'text-red-400' : 'text-white'} mt-0.5 truncate">${stats.total_capital_budget > 0 ? fmtCompact(Math.abs(stats.capital_budget_remaining)) + (stats.capital_budget_remaining < 0 ? ' over' : ' left') : '—'}</p>
                            <p class="text-xs text-cyan-300 mt-0.5 truncate">Budget: ${fmtCompact(stats.total_capital_budget)} · Approved: ${fmtCompact(stats.approved_capital_spent)}</p>
                        </div>
                    </div>
                `;

                // Website metrics row
                document.getElementById('stats').innerHTML = `
                    <div class="bg-green-800/60 border border-green-700/40 p-4 rounded-xl flex items-start gap-3 shadow overflow-hidden">
                        <div class="w-9 h-9 bg-green-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-search text-green-300 text-sm"></i>
                        </div>
                        <div class="min-w-0">
                            <p class="text-xs text-green-400 font-medium uppercase tracking-wide truncate">Inquiries</p>
                            <p class="text-xl sm:text-2xl font-bold text-white mt-0.5">${stats.total_inquiries}</p>
                            <p class="text-xs text-green-300 mt-0.5">${stats.new_inquiries} new</p>
                        </div>
                    </div>
                    <div class="bg-blue-800/60 border border-blue-700/40 p-4 rounded-xl flex items-start gap-3 shadow overflow-hidden">
                        <div class="w-9 h-9 bg-blue-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-envelope text-blue-300 text-sm"></i>
                        </div>
                        <div class="min-w-0">
                            <p class="text-xs text-blue-400 font-medium uppercase tracking-wide truncate">Messages</p>
                            <p class="text-xl sm:text-2xl font-bold text-white mt-0.5">${stats.contact_messages}</p>
                            <p class="text-xs text-blue-300 mt-0.5">${stats.new_messages} new</p>
                        </div>
                    </div>
                    <div class="bg-purple-800/60 border border-purple-700/40 p-4 rounded-xl flex items-start gap-3 shadow overflow-hidden">
                        <div class="w-9 h-9 bg-purple-700/70 rounded-lg flex items-center justify-center flex-shrink-0">
                            <i class="fas fa-users text-purple-300 text-sm"></i>
                        </div>
                        <div class="min-w-0">
                            <p class="text-xs text-purple-400 font-medium uppercase tracking-wide truncate">Active Team</p>
                            <p class="text-xl sm:text-2xl font-bold text-white mt-0.5">${stats.active_team_members}</p>
                            <p class="text-xs text-purple-300 mt-0.5">On About page</p>
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
                            <div class="overflow-x-auto"><table class="w-full text-sm min-w-[280px]">
                                <thead><tr class="border-b border-gray-700"><th class="py-1 text-left text-gray-500 font-medium">Tenant</th><th class="py-1 text-left text-gray-500 font-medium">Amount</th><th class="py-1 text-left text-gray-500 font-medium">Date</th></tr></thead>
                                <tbody>${stats.recent_activity.payments.length ? stats.recent_activity.payments.map(p => `
                                    <tr class="border-b border-gray-700/50">
                                        <td class="py-2 text-white">${p.tenant_name}</td>
                                        <td class="py-2 text-emerald-400 font-medium">${fmtNGN(p.amount)}</td>
                                        <td class="py-2 text-gray-400 text-xs">${p.payment_date}</td>
                                    </tr>`).join('') : noRows}</tbody>
                            </table></div>
                        </div>
                        <div>
                            <h4 class="text-xs font-semibold text-blue-400 uppercase tracking-wide mb-3 flex items-center gap-1.5"><i class="fas fa-users"></i> Recent Tenants</h4>
                            <div class="overflow-x-auto"><table class="w-full text-sm min-w-[260px]">
                                <thead><tr class="border-b border-gray-700"><th class="py-1 text-left text-gray-500 font-medium">Name</th><th class="py-1 text-left text-gray-500 font-medium">Property</th><th class="py-1 text-left text-gray-500 font-medium">Status</th></tr></thead>
                                <tbody>${stats.recent_activity.tenants.length ? stats.recent_activity.tenants.map(t => `
                                    <tr class="border-b border-gray-700/50">
                                        <td class="py-2 text-white">${t.name}</td>
                                        <td class="py-2 text-gray-400 text-xs">${t.property_name || '—'}</td>
                                        <td class="py-2"><span class="text-xs px-2 py-0.5 rounded-full ${t.status === 'active' ? 'bg-teal-800 text-teal-300' : 'bg-gray-700 text-gray-400'}">${t.status}</span></td>
                                    </tr>`).join('') : noRows}</tbody>
                            </table></div>
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
                // Charts
                window._ceoTrendDataFull = { monthly: stats.monthly_trend || [], yearly: stats.yearly_trend || [] };
                const _ceoD = _trendSlice(window._ceoTrendDataFull.monthly, window._ceoTrendDataFull.yearly, window._ceoTrendPeriod);
                _setPeriodBtns('ceoPeriod_', window._ceoTrendPeriod);
                const rcCtx = document.getElementById('ceoRevenueChart');
                if (rcCtx) {
                    if (window._ceoRevChart) window._ceoRevChart.destroy();
                    window._ceoRevChart = new Chart(rcCtx, {
                        type: 'bar',
                        data: {
                            labels: _ceoD.labels,
                            datasets: [
                                { label: 'Revenue', data: _ceoD.revenue, backgroundColor: 'rgba(45,212,191,0.7)', borderColor: '#2dd4bf', borderWidth: 1, borderRadius: 4 },
                                { label: 'Capital Spent', data: _ceoD.capital, backgroundColor: 'rgba(251,146,60,0.6)', borderColor: '#fb923c', borderWidth: 1, borderRadius: 4 }
                            ]
                        },
                        options: {
                            responsive: true, maintainAspectRatio: false,
                            plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
                            scales: {
                                x: { ticks: { color: '#64748b', font: { size: 11 }, maxRotation: 45 }, grid: { color: 'rgba(255,255,255,0.04)' } },
                                y: { ticks: { color: '#64748b', font: { size: 11 }, callback: v => v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? Math.round(v/1e3)+'K' : v }, grid: { color: 'rgba(255,255,255,0.06)' } }
                            }
                        }
                    });
                }
                const ts = stats.tenant_status || {};
                const tdCtx = document.getElementById('ceoTenantDonut');
                if (tdCtx) {
                    if (window._ceoTenantChart) window._ceoTenantChart.destroy();
                    const tdTotal = (ts.active||0) + (ts.reserved||0) + (ts.vacated||0);
                    window._ceoTenantChart = new Chart(tdCtx, {
                        type: 'doughnut',
                        data: {
                            labels: ['Active', 'Reserved', 'Vacated'],
                            datasets: [{ data: [ts.active||0, ts.reserved||0, ts.vacated||0], backgroundColor: ['rgba(45,212,191,0.8)','rgba(250,204,21,0.8)','rgba(100,116,139,0.7)'], borderWidth: 0, hoverOffset: 4 }]
                        },
                        options: {
                            responsive: true, maintainAspectRatio: false, cutout: '68%',
                            plugins: {
                                legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 11 }, padding: 12, boxWidth: 10 } },
                                tooltip: { callbacks: { label: ctx => ctx.label + ': ' + ctx.raw + (tdTotal ? ' (' + Math.round(ctx.raw/tdTotal*100) + '%)' : '') } }
                            }
                        }
                    });
                }
                // Expense categories donut
                try {
                    const expData = await fetchData('/admin/api/project-expenses' + qs);
                    const byCat = expData.by_category || {};
                    const catEntries = Object.entries(byCat).sort((a,b) => b[1]-a[1]).slice(0,6);
                    const catCtx = document.getElementById('ceoCategoryDonut');
                    const catEmpty = document.getElementById('ceoCatEmpty');
                    if (catEntries.length && catCtx) {
                        if (catEmpty) catEmpty.classList.add('hidden');
                        catCtx.style.display = '';
                        if (window._ceoCatChart) window._ceoCatChart.destroy();
                        const catColors = ['rgba(251,191,36,0.85)','rgba(20,184,166,0.85)','rgba(99,102,241,0.85)','rgba(249,115,22,0.85)','rgba(239,68,68,0.85)','rgba(100,116,139,0.75)'];
                        const catTotal = catEntries.reduce((s, e) => s + e[1], 0);
                        window._ceoCatChart = new Chart(catCtx, {
                            type: 'doughnut',
                            data: {
                                labels: catEntries.map(e => e[0].replace(/_/g,' ').replace(/\b\w/g, c => c.toUpperCase())),
                                datasets: [{ data: catEntries.map(e => e[1]), backgroundColor: catColors, borderWidth: 0, hoverOffset: 4 }]
                            },
                            options: {
                                responsive: true, maintainAspectRatio: false, cutout: '65%',
                                plugins: {
                                    legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 10 }, padding: 10, boxWidth: 9 } },
                                    tooltip: { callbacks: { label: ctx => ctx.label + ': ' + fmtCompact(ctx.raw) + (catTotal ? ' (' + Math.round(ctx.raw/catTotal*100) + '%)' : '') } }
                                }
                            }
                        });
                    } else {
                        if (window._ceoCatChart) { window._ceoCatChart.destroy(); window._ceoCatChart = null; }
                        if (catCtx) catCtx.style.display = 'none';
                        if (catEmpty) catEmpty.classList.remove('hidden');
                    }
                } catch(e) { console.error('ceoCategoryDonut error', e); }
            } catch (error) {
                console.error('Error loading stats:', error);
            } finally {
                dismissLoader();
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
                        }">${prop.property_type === 'hostel' ? 'Apartment' : prop.property_type.charAt(0).toUpperCase() + prop.property_type.slice(1)}</span></td>
                        <td class="py-2">${prop.location}</td>
                        <td class="py-2">${prop.construction_status || 'N/A'}</td>
                        <td class="py-2">${prop.capital_budget ? fmtNGN(prop.capital_budget) : '—'}</td>
                        <td class="py-2 flex items-center gap-3">
                            <button onclick="editPropertyBudget(${prop.id}, ${prop.capital_budget || 0})" class="text-amber-400 hover:text-amber-300 text-xs">Edit Budget</button>
                            <button onclick="deleteProperty(${prop.id})" class="text-red-400 hover:underline text-xs">Delete</button>
                        </td>
                    </tr>
                `).join('');
            } catch (error) {
                console.error('Error loading properties:', error);
            }
        }

        async function editPropertyBudget(propId, currentBudget) {
            const amount = prompt('Set Capital Budget (₦) for this project:', currentBudget || '');
            if (amount === null) return;
            if (isNaN(amount) || Number(amount) < 0) { alert('Enter a valid amount'); return; }
            try {
                await fetchData('/admin/api/properties/' + propId, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ capital_budget: Number(amount) })
                });
                loadProperties();
            } catch (err) { alert(err.message || 'Error saving budget'); }
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
                // Hero background preview
                const heroBgPath = content['home.hero_bg_path'] || '';
                const heroBgPreviewWrap = document.getElementById('heroBgPreviewWrap');
                if (heroBgPath && heroBgPreviewWrap) {
                    heroBgPreviewWrap.classList.remove('hidden');
                    document.getElementById('heroBgPreview').src = '/assets/' + heroBgPath;
                    document.getElementById('heroBgCurrentPath').textContent = heroBgPath;
                } else if (heroBgPreviewWrap) {
                    heroBgPreviewWrap.classList.add('hidden');
                }
                // Video section
                const videoUrl = content['home.video_url'] || '';
                const videoTitle = content['home.video_section_title'] || '';
                const videoEnabled = content['home.video_section_enabled'] === 'true';
                const vuEl = document.getElementById('home_video_url');
                const vtEl = document.getElementById('home_video_section_title');
                const veEl = document.getElementById('home_video_section_enabled');
                if (vuEl) vuEl.value = videoUrl;
                if (vtEl) vtEl.value = videoTitle;
                if (veEl) veEl.checked = videoEnabled;
                // Announcement banner
                const annText = content['home.announcement_text'] || '';
                const annEnabled = content['home.announcement_enabled'] === 'true';
                const annEl = document.getElementById('home_announcement_text');
                const annEnEl = document.getElementById('home_announcement_enabled');
                if (annEl) annEl.value = annText;
                if (annEnEl) annEnEl.checked = annEnabled;
                // Contact & social
                const setVal = (id, key) => { const el = document.getElementById(id); if (el) el.value = content[key] || ''; };
                setVal('site_contact_email', 'site.contact_email');
                setVal('site_contact_phone', 'site.contact_phone');
                setVal('site_contact_address', 'site.contact_address');
                setVal('site_footer_company_desc', 'site.footer_company_desc');
                setVal('site_wa_number_hostel', 'site.wa_number_hostel');
                setVal('site_wa_number_land', 'site.wa_number_land');
                setVal('site_wa_number_home', 'site.wa_number_home');
                setVal('site_social_facebook', 'site.social_facebook');
                setVal('site_social_twitter', 'site.social_twitter');
                setVal('site_social_instagram', 'site.social_instagram');
                // Services
                setVal('home_services_heading', 'home.services_heading');
                setVal('home_services_lead', 'home.services_lead');
                loadDraftStatus();
            } catch (error) {
                console.error('Error loading site content:', error);
            }
        }

        async function loadDraftStatus() {
            try {
                const data = await fetchData('/admin/api/site-content/draft-status');
                const banner = document.getElementById('publishBanner');
                const count = document.getElementById('draftPendingCount');
                if (banner && count) {
                    if (data.pending > 0) {
                        count.textContent = data.pending;
                        banner.classList.remove('hidden');
                    } else {
                        banner.classList.add('hidden');
                    }
                }
            } catch (e) { /* ignore */ }
        }

        async function publishWebsite() {
            const btn = document.getElementById('publishBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin text-xs"></i> Publishing…'; }
            try {
                const data = await fetchData('/admin/api/site-content/publish', { method: 'POST' });
                if (data.success) {
                    const banner = document.getElementById('publishBanner');
                    if (banner) banner.classList.add('hidden');
                    alert(data.message || 'Published successfully!');
                } else {
                    alert(data.message || 'Publish failed.');
                }
            } catch (e) {
                alert('Error publishing changes.');
            } finally {
                if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-rocket text-xs"></i> Publish to Website'; }
            }
        }

        function openWebsitePreview() {
            const modal = document.getElementById('websitePreviewModal');
            const frame = document.getElementById('websitePreviewFrame');
            const spinner = document.getElementById('previewLoadingSpinner');
            if (!modal || !frame) return;
            spinner.classList.remove('hidden');
            frame.src = '/?preview=1';
            modal.classList.remove('hidden');
            document.body.style.overflow = 'hidden';
        }

        function closeWebsitePreview() {
            const modal = document.getElementById('websitePreviewModal');
            const frame = document.getElementById('websitePreviewFrame');
            if (!modal) return;
            modal.classList.add('hidden');
            frame.src = '';
            document.body.style.overflow = '';
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeWebsitePreview();
        });

        function switchVideoTab(tab) {
            const isUrl = tab === 'url';
            document.getElementById('videoPanelUrl').classList.toggle('hidden', !isUrl);
            document.getElementById('videoPanelUpload').classList.toggle('hidden', isUrl);
            document.getElementById('videoTabUrl').className = isUrl
                ? 'px-3 py-1.5 rounded-lg text-xs font-medium bg-pink-700 text-white'
                : 'px-3 py-1.5 rounded-lg text-xs font-medium bg-gray-600 text-gray-300';
            document.getElementById('videoTabUpload').className = !isUrl
                ? 'px-3 py-1.5 rounded-lg text-xs font-medium bg-pink-700 text-white'
                : 'px-3 py-1.5 rounded-lg text-xs font-medium bg-gray-600 text-gray-300';
        }

        async function uploadSiteVideo() {
            const fileInput = document.getElementById('videoFileInput');
            const msgEl = document.getElementById('videoUploadMessage');
            const progressWrap = document.getElementById('videoUploadProgress');
            const progressBar = document.getElementById('videoProgressBar');
            const progressText = document.getElementById('videoProgressText');
            if (!fileInput.files.length) {
                msgEl.textContent = 'Select a video file first.';
                msgEl.className = 'text-sm text-amber-400';
                return;
            }
            const file = fileInput.files[0];
            if (file.size > 200 * 1024 * 1024) {
                msgEl.textContent = 'File too large (max 200 MB).';
                msgEl.className = 'text-sm text-red-400';
                return;
            }
            msgEl.textContent = '';
            progressWrap.classList.remove('hidden');
            progressBar.style.width = '0%';
            progressText.textContent = 'Uploading…';
            const formData = new FormData();
            formData.append('file', file);
            try {
                await new Promise((resolve, reject) => {
                    const xhr = new XMLHttpRequest();
                    xhr.open('POST', '/admin/api/site-content/upload-video');
                    xhr.setRequestHeader('X-CSRF-Token', adminCsrfToken);
                    xhr.withCredentials = true;
                    xhr.upload.onprogress = (e) => {
                        if (e.lengthComputable) {
                            const pct = Math.round((e.loaded / e.total) * 100);
                            progressBar.style.width = pct + '%';
                            progressText.textContent = `Uploading… ${pct}%`;
                        }
                    };
                    xhr.onload = () => {
                        const result = JSON.parse(xhr.responseText);
                        if (result.success) {
                            progressBar.style.width = '100%';
                            progressText.textContent = 'Upload complete!';
                            document.getElementById('home_video_url').value = result.url;
                            // Auto-enable the video section checkbox
                            const enabledCheck = document.getElementById('home_video_section_enabled');
                            if (enabledCheck) enabledCheck.checked = true;
                            msgEl.textContent = 'Video uploaded! Now click "Save Draft" below.';
                            msgEl.className = 'text-sm text-green-400 font-medium';
                            fileInput.value = '';
                            loadDraftStatus();
                            resolve();
                        } else {
                            reject(new Error(result.message || 'Upload failed'));
                        }
                    };
                    xhr.onerror = () => reject(new Error('Network error'));
                    xhr.send(formData);
                });
            } catch (e) {
                msgEl.textContent = e.message || 'Upload error.';
                msgEl.className = 'text-sm text-red-400';
                progressWrap.classList.add('hidden');
            }
        }

        async function uploadHeroBg() {
            const fileInput = document.getElementById('heroBgFileInput');
            const msgEl = document.getElementById('heroBgMessage');
            if (!fileInput.files.length) { msgEl.textContent = 'Please select an image file first.'; msgEl.className = 'block mt-2 text-sm text-amber-400'; return; }
            msgEl.textContent = 'Uploading…'; msgEl.className = 'block mt-2 text-sm text-gray-400';
            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            try {
                const response = await fetch('/admin/api/site-content/upload-hero-bg', { method: 'POST', credentials: 'include', headers: { 'X-CSRF-Token': adminCsrfToken }, body: formData });
                const result = await response.json();
                if (result.success) {
                    msgEl.textContent = result.message || 'Saved as draft — click Publish to go live.';
                    msgEl.className = 'block mt-2 text-sm text-amber-400';
                    const wrap = document.getElementById('heroBgPreviewWrap');
                    wrap.classList.remove('hidden');
                    document.getElementById('heroBgPreview').src = '/assets/' + result.path;
                    document.getElementById('heroBgCurrentPath').textContent = result.path;
                    fileInput.value = '';
                    loadDraftStatus();
                } else {
                    msgEl.textContent = result.message || 'Upload failed.';
                    msgEl.className = 'block mt-2 text-sm text-red-400';
                }
            } catch (e) {
                msgEl.textContent = 'Upload error. Check connection.'; msgEl.className = 'block mt-2 text-sm text-red-400';
            }
        }

        async function clearHeroBg() {
            if (!confirm('Remove the custom hero background? The site will fall back to the default photo slideshow.')) return;
            try {
                await fetchData('/admin/api/site-content', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ 'home.hero_bg_path': '' }) });
                document.getElementById('heroBgPreviewWrap').classList.add('hidden');
                const msgEl = document.getElementById('heroBgMessage');
                msgEl.textContent = 'Cleared (draft) — Publish to apply to live site.'; msgEl.className = 'block mt-2 text-sm text-amber-400';
                loadDraftStatus();
            } catch (e) {
                const msgEl = document.getElementById('heroBgMessage');
                msgEl.textContent = 'Error clearing background.'; msgEl.className = 'block mt-2 text-sm text-red-400';
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
                capital_budget: parseFloat(document.getElementById('capital_budget').value) || null,
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
                message.className = 'ml-3 text-sm text-amber-400';
                loadDraftStatus();
            } catch (error) {
                message.textContent = 'Error saving draft';
                message.className = 'ml-3 text-sm text-red-400';
            }
        });

        document.getElementById('videoSectionForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const message = document.getElementById('videoSectionMessage');
            try {
                const payload = {
                    'home.video_url': document.getElementById('home_video_url').value.trim(),
                    'home.video_section_title': document.getElementById('home_video_section_title').value.trim(),
                    'home.video_section_enabled': document.getElementById('home_video_section_enabled').checked ? 'true' : 'false'
                };
                const response = await fetchData('/admin/api/site-content', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                message.textContent = response.message;
                message.className = 'ml-3 text-sm text-amber-400';
                loadDraftStatus();
            } catch (error) {
                message.textContent = 'Error saving draft';
                message.className = 'ml-3 text-sm text-red-400';
            }
        });

        document.getElementById('announcementForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const message = document.getElementById('announcementMessage');
            try {
                const payload = {
                    'home.announcement_text': document.getElementById('home_announcement_text').value.trim(),
                    'home.announcement_enabled': document.getElementById('home_announcement_enabled').checked ? 'true' : 'false'
                };
                const response = await fetchData('/admin/api/site-content', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                message.textContent = response.message;
                message.className = 'ml-3 text-sm text-amber-400';
                loadDraftStatus();
            } catch (error) {
                message.textContent = 'Error saving draft';
                message.className = 'ml-3 text-sm text-red-400';
            }
        });

        document.getElementById('contactInfoForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const message = document.getElementById('contactInfoMessage');
            try {
                const g = id => (document.getElementById(id)?.value || '').trim();
                const payload = {
                    'site.contact_email': g('site_contact_email'),
                    'site.contact_phone': g('site_contact_phone'),
                    'site.contact_address': g('site_contact_address'),
                    'site.footer_company_desc': g('site_footer_company_desc'),
                    'site.wa_number_hostel': g('site_wa_number_hostel'),
                    'site.wa_number_land': g('site_wa_number_land'),
                    'site.wa_number_home': g('site_wa_number_home'),
                    'site.social_facebook': g('site_social_facebook'),
                    'site.social_twitter': g('site_social_twitter'),
                    'site.social_instagram': g('site_social_instagram'),
                };
                const response = await fetchData('/admin/api/site-content', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                message.textContent = response.message;
                message.className = 'ml-3 text-sm text-amber-400';
                loadDraftStatus();
            } catch (error) {
                message.textContent = 'Error saving draft';
                message.className = 'ml-3 text-sm text-red-400';
            }
        });

        document.getElementById('servicesSectionForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const message = document.getElementById('servicesSectionMessage');
            try {
                const payload = {
                    'home.services_heading': (document.getElementById('home_services_heading')?.value || '').trim(),
                    'home.services_lead': (document.getElementById('home_services_lead')?.value || '').trim(),
                };
                const response = await fetchData('/admin/api/site-content', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                message.textContent = response.message;
                message.className = 'ml-3 text-sm text-amber-400';
                loadDraftStatus();
            } catch (error) {
                message.textContent = 'Error saving draft';
                message.className = 'ml-3 text-sm text-red-400';
            }
        });

        // Save & Preview button
        const savePreviewBtn = document.getElementById('saveAndPreviewBtn');
        if (savePreviewBtn) {
            savePreviewBtn.addEventListener('click', async () => {
                // Trigger the form save first
                const form = document.getElementById('siteContentForm');
                if (form) {
                    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                    // Short delay to let the save complete, then open preview
                    setTimeout(openWebsitePreview, 600);
                }
            });
        }

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
        // v2 key — resets any old pinned state from previous session
        let sidebarPinned = localStorage.getItem('ceo_sidebar_pinned_v2') === 'true';

        function applyPinState() {
            const pinBtn = document.getElementById('sidebarPinBtn');
            const pinIcon = document.getElementById('sidebarPinIcon');
            if (pinIcon) {
                pinIcon.className = sidebarPinned
                    ? 'fas fa-thumbtack text-xs text-emerald-400'
                    : 'fas fa-thumbtack text-xs text-slate-500';
            }
            if (pinBtn) {
                pinBtn.title = sidebarPinned ? 'Unpin sidebar (auto-close on)' : 'Pin sidebar open';
                pinBtn.className = pinBtn.className.replace(/ring-[^\s]+/g, '').trim();
                if (sidebarPinned) pinBtn.classList.add('ring-1', 'ring-emerald-500/50');
            }
        }

        function toggleSidebarPin() {
            sidebarPinned = !sidebarPinned;
            localStorage.setItem('ceo_sidebar_pinned_v2', sidebarPinned);
            applyPinState();
            // If just pinned while collapsed, expand the sidebar
            if (sidebarPinned && sidebarCollapsed) {
                sidebarCollapsed = false;
                document.getElementById('sidebar').classList.remove('collapsed');
                document.getElementById('mainWrapper').classList.remove('sidebar-collapsed');
            }
        }

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

        function collapseSidebarOnNav() {
            if (window.innerWidth >= 768 && !sidebarPinned && !sidebarCollapsed) {
                sidebarCollapsed = true;
                document.getElementById('sidebar').classList.add('collapsed');
                document.getElementById('mainWrapper').classList.add('sidebar-collapsed');
            }
        }

        function closeSidebar() {
            document.getElementById('sidebar').classList.remove('mobile-open');
            document.getElementById('sidebarOverlay').classList.add('hidden');
        }

        // Apply pin state on load
        applyPinState();

        // ===== CEO SECTION NAVIGATION =====
        function showSection(sectionId, skipCollapse = false) {
            const sections = ['overviewSection','tenantsSection','unitTypesSection','paymentsSection','signaturesSection','accountsSection','payrollSection','approvalsSection','investorsSection','propertiesSection','constructionSection','capitalSection','maintenanceSection','contentSection','teamSection','inquiriesSection2','propertiesTableSection','contractsSection'];
            sections.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.classList.add('hidden');
            });
            const target = document.getElementById(sectionId);
            if (target) target.classList.remove('hidden');
            document.querySelectorAll('.ceo-nav-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll(`.ceo-nav-btn[onclick="showSection('${sectionId}')"]`).forEach(b => b.classList.add('active'));
            if (!skipCollapse) {
                if (window.innerWidth < 768) closeSidebar(); else collapseSidebarOnNav();
            }
            if (sectionId === 'signaturesSection') { loadPendingContracts(); loadCompletedContracts(); }
            if (sectionId === 'accountsSection') { loadAccounts(); loadInvestorAccountOptions(); loadResetRequests(); }
            if (sectionId === 'payrollSection') { loadPayroll(); }
            if (sectionId === 'approvalsSection') { loadApprovals(); }
            if (sectionId === 'investorsSection') { loadInvestors(); loadInvestorAccountOptions(); loadInvestorPropertyDropdowns(); }
            if (sectionId === 'tenantsSection') { loadTenants(); tnPopulatePropertyDropdown(); tnUtRefreshForProperty(); fillServicedBySelect(document.getElementById('tnServicedBy')); }
            if (sectionId === 'unitTypesSection') { loadPropertiesForUnitTypeForm(); loadUnitTypes(); }
            if (sectionId === 'paymentsSection') { loadPayments(); loadTenantOptions(); }
            if (sectionId === 'constructionSection') { loadConstructionPropertyOptions(); loadConstructionUpdates(); }
            if (sectionId === 'capitalSection') { loadCapitalPropertyOptions(); }
            if (sectionId === 'maintenanceSection') { loadMaintenancePropertyOptions(); loadMaintenanceRecords(); }
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
                loadConstructionUpdates(ceoSel?.value || mgrSel?.value || '');
            } catch (e) {}
        }

        async function loadCapitalPropertyOptions() {
            try {
                const props = await fetchData('/admin/api/properties');
                const options = props.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                ['ceoCapitalProperty', 'mgrCapitalProperty'].forEach(id => {
                    const sel = document.getElementById(id);
                    if (!sel) return;
                    const cur = sel.value;
                    sel.innerHTML = options;
                    sel.value = cur && props.some(p => String(p.id) === cur) ? cur : (props[0] ? String(props[0].id) : '');
                });
                if (typeof loadProjectExpenses === 'function') {
                    await loadProjectExpenses('ceo');
                    await loadProjectExpenses('mgr');
                } else {
                    await ceoLoadExpenses();
                }
            } catch (e) {}
        }

        function toggleBudgetEdit() {
            const row = document.getElementById('budgetEditRow');
            const isHidden = row.classList.contains('hidden');
            row.classList.toggle('hidden', !isHidden);
            row.style.display = isHidden ? 'flex' : 'none';
            if (isHidden) {
                const current = document.getElementById('capBudgetTotal')?.textContent || '';
                const num = current.replace(/[₦,]/g, '').trim();
                if (num && !isNaN(num)) document.getElementById('budgetEditInput').value = num;
                document.getElementById('budgetEditInput').focus();
                document.getElementById('budgetEditMsg').textContent = '';
            }
        }

        async function saveBudget() {
            const propId = document.getElementById('ceoCapitalProperty')?.value;
            const amount = document.getElementById('budgetEditInput')?.value;
            const msgEl = document.getElementById('budgetEditMsg');
            if (!propId) { msgEl.textContent = 'Select a project first.'; msgEl.className = 'text-xs text-red-400'; return; }
            if (!amount || isNaN(amount) || Number(amount) < 0) { msgEl.textContent = 'Enter a valid amount.'; msgEl.className = 'text-xs text-red-400'; return; }
            try {
                await fetchData('/admin/api/properties/' + propId, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ capital_budget: Number(amount) })
                });
                msgEl.textContent = 'Budget saved!';
                msgEl.className = 'text-xs text-emerald-400';
                if (typeof loadProjectExpenses === 'function') {
                    await loadProjectExpenses('ceo');
                } else {
                    await ceoLoadExpenses();
                }
                setTimeout(toggleBudgetEdit, 1000);
            } catch (err) {
                msgEl.textContent = err.message || 'Error saving budget';
                msgEl.className = 'text-xs text-red-400';
            }
        }

        // ── MAINTENANCE (CEO) ─────────────────────────────────────────
        let _ceoMaintCache = [];

        async function loadMaintenancePropertyOptions() {
            try {
                const props = await fetchData('/admin/api/properties');
                const allOpt = '<option value="">All properties</option>';
                const opts = props.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                ['ceoMaintProperty', 'ceoMaintFormProperty', 'mgrMaintProperty', 'mgrMaintFormProperty'].forEach(id => {
                    const el = document.getElementById(id);
                    if (!el) return;
                    const cur = el.value;
                    el.innerHTML = (id.includes('Form') ? '' : allOpt) + opts;
                    if (cur && props.some(p => String(p.id) === cur)) el.value = cur;
                });
            } catch(e) {}
        }

        async function loadMaintenanceRecords(prefix) {
            prefix = prefix || 'ceo';
            const listEl = document.getElementById(prefix + 'MaintList');
            const summaryEl = document.getElementById(prefix + 'MaintSummary');
            if (!listEl) return;
            const propId = document.getElementById(prefix + 'MaintProperty')?.value || '';
            const cat = document.getElementById(prefix + 'MaintCategory')?.value || '';
            const params = new URLSearchParams();
            if (propId) params.set('property_id', propId);
            const qs = params.toString() ? '?' + params.toString() : '';
            try {
                listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-6">Loading...</p>';
                const records = await fetchData('/admin/api/maintenance' + qs);
                const filtered = cat ? records.filter(r => r.category === cat) : records;
                _ceoMaintCache = filtered;
                const totalCost = filtered.reduce((s, r) => s + (r.cost || 0), 0);
                if (summaryEl) summaryEl.textContent = filtered.length + ' record' + (filtered.length !== 1 ? 's' : '') + (totalCost ? ' · Total cost: ' + formatNGN(totalCost) : '');
                if (!filtered.length) { listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-8">No maintenance records found.</p>'; return; }
                const statusColors = { completed: 'bg-emerald-900/60 text-emerald-300', in_progress: 'bg-amber-900/60 text-amber-300', scheduled: 'bg-blue-900/60 text-blue-300' };
                const statusLabels = { completed: 'Completed', in_progress: 'In Progress', scheduled: 'Scheduled' };
                listEl.innerHTML = filtered.map(r => {
                    const sc = statusColors[r.status] || 'bg-gray-700 text-gray-300';
                    const sl = statusLabels[r.status] || r.status;
                    return `<div class="rounded-xl border border-gray-700/70 bg-gray-700/30 p-4"><div class="flex items-start justify-between gap-3"><div class="min-w-0"><div class="flex items-center gap-2 flex-wrap"><p class="font-semibold text-white text-sm">${r.title}</p><span class="px-2 py-0.5 rounded-full text-[11px] ${sc}">${sl}</span><span class="px-2 py-0.5 rounded-full text-[11px] bg-gray-700 text-gray-400">${r.category}</span></div><p class="text-xs text-gray-400 mt-1">${r.property_title || ''} · ${r.maintenance_date || ''}${r.vendor_name ? ' · ' + r.vendor_name : ''}</p>${r.description ? `<p class="text-xs text-gray-500 mt-1">${r.description}</p>` : ''}</div><div class="text-right flex-shrink-0">${r.cost ? '<p class="text-sm font-bold text-amber-300">' + formatNGN(r.cost) + '</p>' : ''}<p class="text-xs text-gray-500 mt-1">${r.recorded_by || ''}</p></div></div><div class="flex items-center gap-3 mt-3 text-xs"><button onclick="ceoEditMaint(${r.id},'${prefix}')" class="text-blue-400 hover:text-blue-300">Edit</button><button onclick="ceoDeleteMaint(${r.id},'${prefix}')" class="text-red-400 hover:text-red-300">Remove</button></div></div>`;
                }).join('');
            } catch(e) { listEl.innerHTML = '<p class="text-red-400 text-sm text-center py-6">Error loading records.</p>'; }
        }

        function ceoToggleMaintenanceForm(hide, prefix) {
            prefix = prefix || 'ceo';
            const form = document.getElementById(prefix + 'MaintForm');
            if (!form) return;
            if (hide) {
                form.classList.add('hidden');
                document.getElementById(prefix + 'MaintEditId').value = '';
                form.querySelectorAll('input,textarea,select').forEach(el => { if (el.type !== 'hidden') el.value = el.tagName === 'SELECT' ? el.options[0]?.value || '' : ''; });
                document.getElementById(prefix + 'MaintFormTitle').textContent = 'New Maintenance Record';
                const msg = document.getElementById(prefix + 'MaintMsg');
                if (msg) msg.textContent = '';
            } else {
                form.classList.remove('hidden');
                const d = new Date(); const pad = n => String(n).padStart(2,'0');
                const today = d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate());
                const dateEl = document.getElementById(prefix + 'MaintDate');
                if (dateEl && !dateEl.value) dateEl.value = today;
                form.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }

        async function ceoSubmitMaintenance(prefix) {
            prefix = prefix || 'ceo';
            const editId = document.getElementById(prefix + 'MaintEditId').value;
            const msgEl = document.getElementById(prefix + 'MaintMsg');
            const payload = {
                property_id: document.getElementById(prefix + 'MaintFormProperty')?.value || '',
                maintenance_date: document.getElementById(prefix + 'MaintDate')?.value || '',
                title: document.getElementById(prefix + 'MaintTitle')?.value?.trim() || '',
                category: document.getElementById(prefix + 'MaintFormCategory')?.value || 'general',
                status: document.getElementById(prefix + 'MaintStatus')?.value || 'completed',
                vendor_name: document.getElementById(prefix + 'MaintVendor')?.value?.trim() || '',
                cost: document.getElementById(prefix + 'MaintCost')?.value || '',
                description: document.getElementById(prefix + 'MaintDesc')?.value?.trim() || '',
            };
            if (!payload.title) { if (msgEl) { msgEl.textContent = 'Title is required'; msgEl.className = 'text-sm text-red-400'; } return; }
            if (!payload.property_id) { if (msgEl) { msgEl.textContent = 'Select a property'; msgEl.className = 'text-sm text-red-400'; } return; }
            try {
                if (msgEl) { msgEl.textContent = 'Saving...'; msgEl.className = 'text-sm text-gray-400'; }
                await fetchData(editId ? '/admin/api/maintenance/' + editId : '/admin/api/maintenance', {
                    method: editId ? 'PUT' : 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify(payload)
                });
                ceoToggleMaintenanceForm(true, prefix);
                await loadMaintenanceRecords(prefix);
            } catch(err) { if (msgEl) { msgEl.textContent = err.message || 'Error saving record'; msgEl.className = 'text-sm text-red-400'; } }
        }

        function ceoEditMaint(id, prefix) {
            prefix = prefix || 'ceo';
            const r = _ceoMaintCache.find(x => x.id === id);
            if (!r) return;
            document.getElementById(prefix + 'MaintEditId').value = r.id;
            document.getElementById(prefix + 'MaintFormProperty').value = r.property_id;
            document.getElementById(prefix + 'MaintDate').value = r.maintenance_date || '';
            document.getElementById(prefix + 'MaintTitle').value = r.title || '';
            document.getElementById(prefix + 'MaintFormCategory').value = r.category || 'general';
            document.getElementById(prefix + 'MaintStatus').value = r.status || 'completed';
            document.getElementById(prefix + 'MaintVendor').value = r.vendor_name || '';
            document.getElementById(prefix + 'MaintCost').value = r.cost || '';
            document.getElementById(prefix + 'MaintDesc').value = r.description || '';
            document.getElementById(prefix + 'MaintFormTitle').textContent = 'Edit Record';
            ceoToggleMaintenanceForm(false, prefix);
        }

        async function ceoDeleteMaint(id, prefix) {
            prefix = prefix || 'ceo';
            if (!confirm('Remove this maintenance record?')) return;
            try {
                await fetchData('/admin/api/maintenance/' + id, { method: 'DELETE' });
                await loadMaintenanceRecords(prefix);
            } catch(e) { alert(e.message || 'Error deleting record'); }
        }

        window.expenseCache = window.expenseCache || { ceo: [], mgr: [], acc: [] };
        window.expPage = window.expPage || { ceo: 1, mgr: 1, acc: 1 };
        const _CEO_PAGE_SIZE = 20;

        function ceoExpenseCard(exp) {
            const st = exp.approval_status || 'pending';
            const stCls = st === 'approved' ? 'bg-emerald-900/60 text-emerald-300' : st === 'rejected' ? 'bg-rose-900/60 text-rose-300' : 'bg-amber-900/60 text-amber-300';
            const stLbl = st === 'approved' ? 'Approved' : st === 'rejected' ? 'Rejected' : 'Pending';
            const paidBadge = exp.is_paid ? '<span class="px-2 py-0.5 rounded-full text-[11px] bg-emerald-900/60 text-emerald-300">Paid</span>' : '<span class="px-2 py-0.5 rounded-full text-[11px] bg-orange-900/60 text-orange-300">Unpaid</span>';
            const receiptHtml = exp.receipt_path ? `<p class="mt-2 flex items-center gap-3"><a href="/assets/${exp.receipt_path}" target="_blank" class="text-xs text-cyan-300 hover:text-cyan-200 underline">View receipt</a><a href="/assets/${exp.receipt_path}" download class="text-xs text-cyan-400 hover:text-cyan-300 underline">Download</a></p>` : '';
            const approvalBtns = `${st !== 'approved' ? `<button onclick="ceoApproveExpense(${exp.id},'approved')" class="text-emerald-400 hover:text-emerald-300 text-xs font-medium">Approve</button>` : ''}${st !== 'rejected' ? `<button onclick="ceoApproveExpense(${exp.id},'rejected')" class="text-rose-400 hover:text-rose-300 text-xs font-medium">Reject</button>` : ''}`;
            const paidToggle = `<button onclick="ceoTogglePaid(${exp.id},${exp.is_paid})" class="${exp.is_paid ? 'text-orange-400 hover:text-orange-300' : 'text-emerald-400 hover:text-emerald-300'} text-xs font-medium">${exp.is_paid ? 'Mark Unpaid' : 'Mark Paid'}</button>`;
            return `<div class="rounded-xl border ${exp.is_paid ? 'border-emerald-800/50' : 'border-gray-700/70'} bg-gray-700/30 p-4"><div class="flex items-start justify-between gap-3"><div class="min-w-0"><div class="flex items-center gap-2 flex-wrap"><p class="font-semibold text-white text-sm">${exp.item_name}</p><span class="px-2 py-0.5 rounded-full text-[11px] ${stCls}">${stLbl}</span>${paidBadge}</div><p class="text-xs text-gray-400 mt-1">${exp.payee_name || 'No payee'} &middot; ${exp.category} &middot; ${exp.expense_date || ''}</p>${exp.notes ? `<p class="text-xs text-gray-500 mt-2">${exp.notes}</p>` : ''}${receiptHtml}${exp.approved_by ? `<p class="text-[11px] text-gray-500 mt-2">Approved by ${exp.approved_by}</p>` : ''}</div><div class="text-right flex-shrink-0"><p class="text-base font-bold text-amber-300">${formatNGN(exp.amount)}</p></div></div><div class="flex items-center justify-between gap-3 mt-3 text-xs flex-wrap"><div class="text-gray-500">${exp.quantity ? 'Qty '+exp.quantity : ''}${exp.quantity && exp.unit_cost ? ' &middot; ' : ''}${exp.unit_cost ? 'Unit '+formatNGN(exp.unit_cost) : ''}</div><div class="flex items-center gap-3 flex-wrap">${paidToggle}<button onclick="ceoCopyEditExpense(${exp.id})" class="text-blue-400 hover:text-blue-300 text-xs font-medium">Edit</button><button onclick="ceoDeleteExpense(${exp.id})" class="text-red-400 hover:text-red-300 text-xs font-medium">Remove</button>${approvalBtns}</div></div></div>`;
        }

        function renderExpensePage(prefix) {
            const listEl = document.getElementById(prefix + 'ExpenseList');
            if (!listEl) return;
            const all = (window.expenseCache[prefix]) || [];
            const total = all.length;
            const totalPages = Math.max(1, Math.ceil(total / _CEO_PAGE_SIZE));
            if (window.expPage[prefix] > totalPages) window.expPage[prefix] = totalPages;
            const start = (window.expPage[prefix] - 1) * _CEO_PAGE_SIZE;
            const page = all.slice(start, start + _CEO_PAGE_SIZE);
            if (!total) { listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-6">No expenses recorded for this project yet.</p>'; return; }
            const cards = page.map(exp => ceoExpenseCard(exp)).join('');
            const prev = window.expPage[prefix] > 1 ? `<button onclick="window.expPage['${prefix}']--;renderExpensePage('${prefix}')" class="px-3 py-1 rounded bg-gray-700 hover:bg-gray-600 text-white text-xs">&#8592; Prev</button>` : '';
            const next = window.expPage[prefix] < totalPages ? `<button onclick="window.expPage['${prefix}']++;renderExpensePage('${prefix}')" class="px-3 py-1 rounded bg-gray-700 hover:bg-gray-600 text-white text-xs">Next &#8594;</button>` : '';
            const pager = totalPages > 1 ? `<div class="flex items-center justify-between mt-4 text-xs text-gray-400"><span>Showing ${start+1}&ndash;${Math.min(start+_CEO_PAGE_SIZE,total)} of ${total}</span><div class="flex items-center gap-3">${prev}<span>Page ${window.expPage[prefix]} / ${totalPages}</span>${next}</div></div>` : `<p class="text-xs text-gray-500 mt-3 text-right">${total} expense${total!==1?'s':''} total</p>`;
            listEl.innerHTML = cards + pager;
        }

        async function ceoTogglePaid(id, current) {
            try {
                await fetchData('/admin/api/project-expenses/' + id, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ is_paid: !current }) });
                await ceoLoadExpenses(undefined, true);
            } catch(e) { alert(e.message || 'Error updating payment status'); }
        }

        async function ceoLoadExpenses(propertyId, preservePage) {
            const selectedId = propertyId || document.getElementById('ceoCapitalProperty')?.value || '';
            const listEl = document.getElementById('ceoExpenseList');
            const totalEl = document.getElementById('ceoExpenseTotal');
            const breakdownEl = document.getElementById('ceoExpenseBreakdown');
            if (!listEl) return;
            const filters = {
                status: document.getElementById('ceoExpenseStatusFilter')?.value || '',
                receiptsOnly: !!document.getElementById('ceoExpenseReceiptOnly')?.checked,
                category: document.getElementById('ceoExpenseCategoryFilter')?.value || '',
            };
            const params = new URLSearchParams();
            if (selectedId) params.set('property_id', selectedId);
            if (filters.status) params.set('approval_status', filters.status);
            if (filters.receiptsOnly) params.set('has_receipt', 'true');
            if (filters.category) params.set('category', filters.category);
            const qs = params.toString() ? '?' + params.toString() : '';
            try {
                const data = await fetchData('/admin/api/project-expenses' + qs);
                window.expenseCache.ceo = data.expenses || [];
                _ceoCachedExpenses = window.expenseCache.ceo;
                const approvalTotals = data.approval_totals || {};
                const el = (id) => document.getElementById(id);
                if (el('capApprovedTotal')) el('capApprovedTotal').textContent = fmtCompact(approvalTotals.approved || 0);
                if (el('capPendingTotal')) el('capPendingTotal').textContent = fmtCompact(approvalTotals.pending || 0);
                if (el('capRejectedTotal')) el('capRejectedTotal').textContent = fmtCompact(approvalTotals.rejected || 0);
                if (el('capBudgetRemaining')) el('capBudgetRemaining').textContent = data.budget_remaining != null ? fmtCompact(Math.abs(data.budget_remaining)) + (data.over_budget ? ' over' : ' left') : '—';
                if (el('capBudgetTotal')) el('capBudgetTotal').textContent = data.budget_total != null ? fmtCompact(data.budget_total) : '—';
                if (totalEl) totalEl.textContent = formatNGN(data.total_amount || 0);
                const parts = [];
                if (data.budget_total != null) parts.push('Budget ' + formatNGN(data.budget_total));
                if (data.budget_remaining != null) parts.push((data.over_budget ? 'Over by ' : 'Left ') + formatNGN(Math.abs(data.budget_remaining)));
                if (approvalTotals.approved) parts.push('Approved ' + formatNGN(approvalTotals.approved));
                if (approvalTotals.pending) parts.push('Pending ' + formatNGN(approvalTotals.pending));
                if (approvalTotals.rejected) parts.push('Rejected ' + formatNGN(approvalTotals.rejected));
                if (breakdownEl) breakdownEl.textContent = parts.length ? parts.join(' · ') : 'No expenses yet';
                if (!preservePage) window.expPage.ceo = 1;
                renderExpensePage('ceo');
            } catch (err) {
                if (listEl) listEl.innerHTML = '<p class="text-red-400 text-sm text-center py-6">Error loading expenses.</p>';
            }
        }

        async function ceoApproveExpense(id, status) {
            if (!confirm('Mark expense as ' + status + '?')) return;
            try {
                await fetchData('/admin/api/project-expenses/' + id, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ approval_status: status })
                });
                await ceoLoadExpenses(undefined, true);
                await loadStats();
            } catch (err) { alert(err.message || 'Error updating status'); }
        }

        async function ceoDeleteExpense(id) {
            if (!confirm('Permanently remove this expense?')) return;
            try {
                await fetchData('/admin/api/project-expenses/' + id, { method: 'DELETE' });
                await ceoLoadExpenses(undefined, true);
                await loadStats();
            } catch (err) { alert(err.message || 'Error deleting expense'); }
        }

        let _ceoCachedExpenses = [];

        function ceoCopyEditExpense(id) {
            const exp = _ceoCachedExpenses.find(e => e.id === id);
            if (!exp) return;
            document.getElementById('ceoExpenseEditId').value = exp.id;
            document.getElementById('ceoExpenseDate').value = exp.expense_date || '';
            document.getElementById('ceoExpenseCategory').value = exp.category || 'materials';
            updateExpenseForm('ceo');
            document.getElementById('ceoExpenseItem').value = exp.item_name || '';
            document.getElementById('ceoExpensePayee').value = exp.payee_name || '';
            document.getElementById('ceoExpenseAmount').value = exp.amount || '';
            document.getElementById('ceoExpenseQuantity').value = exp.quantity ?? '';
            document.getElementById('ceoExpenseUnitCost').value = exp.unit_cost ?? '';
            document.getElementById('ceoExpenseNotes').value = exp.notes || '';
            document.getElementById('ceoExpenseSubmitBtn').textContent = 'Save Changes';
            document.getElementById('ceoExpenseCancelBtn').classList.remove('hidden');
            document.getElementById('ceoExpenseForm')?.scrollIntoView({ behavior: 'smooth' });
        }

        function updateExpenseForm(prefix) {
            const cat = document.getElementById(prefix + 'ExpenseCategory')?.value || 'materials';
            const isLabour = cat === 'labour';
            const showQty = cat === 'materials' || cat === 'equipment';
            document.getElementById(prefix + 'ExpenseItemRow')?.classList.toggle('hidden', isLabour);
            document.getElementById(prefix + 'ExpenseLabourHint')?.classList.toggle('hidden', !isLabour);
            document.getElementById(prefix + 'ExpenseQtyRow')?.classList.toggle('hidden', !showQty);
            document.getElementById(prefix + 'ExpenseUnitRow')?.classList.toggle('hidden', !showQty);
            const lbl = document.getElementById(prefix + 'ExpenseItemLabel');
            if (lbl) lbl.textContent = {materials:'Item / Purchase *', transport:'Route / Purpose', permits:'Permit / Fee Description', equipment:'Equipment / Item *', other:'Description'}[cat] || 'Item / Purchase *';
            const payeeLbl = document.getElementById(prefix + 'ExpensePayeeLabel');
            if (payeeLbl) payeeLbl.textContent = isLabour ? 'Worker / Paid To *' : 'Paid To / Supplier';
        }

        function ceoCancelExpenseEdit() {
            document.getElementById('ceoExpenseEditId').value = '';
            document.getElementById('ceoExpenseForm')?.reset();
            document.getElementById('ceoExpenseSubmitBtn').textContent = 'Save Expense';
            document.getElementById('ceoExpenseCancelBtn').classList.add('hidden');
            updateExpenseForm('ceo');
        }

        async function ceoUploadReceipt() {
            const fileInput = document.getElementById('ceoExpenseReceipt');
            if (!fileInput || !fileInput.files || !fileInput.files.length) return '';
            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            const response = await fetch('/admin/api/upload-expense-receipt', {
                method: 'POST',
                credentials: 'include',
                headers: { 'X-CSRF-Token': adminCsrfToken },
                body: formData
            });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.message || 'Receipt upload failed');
            return result.filename || '';
        }

        async function ceoSubmitExpense(e) {
            e.preventDefault();
            const editId = document.getElementById('ceoExpenseEditId').value;
            const msgEl = document.getElementById('ceoExpenseMsg');
            const propertyId = document.getElementById('ceoCapitalProperty')?.value || '';
            if (!propertyId) { msgEl.textContent = 'Select a project first.'; msgEl.className = 'text-sm text-red-400'; return; }
            const payload = {
                property_id: propertyId,
                expense_date: document.getElementById('ceoExpenseDate').value,
                category: document.getElementById('ceoExpenseCategory').value,
                item_name: document.getElementById('ceoExpenseItem').value,
                payee_name: document.getElementById('ceoExpensePayee').value,
                amount: document.getElementById('ceoExpenseAmount').value,
                quantity: document.getElementById('ceoExpenseQuantity').value,
                unit_cost: document.getElementById('ceoExpenseUnitCost').value,
                notes: document.getElementById('ceoExpenseNotes').value,
            };
            try {
                const receiptPath = await ceoUploadReceipt();
                if (receiptPath) payload.receipt_path = receiptPath;
                const res = await fetchData(editId ? ('/admin/api/project-expenses/' + editId) : '/admin/api/project-expenses', {
                    method: editId ? 'PUT' : 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify(payload)
                });
                msgEl.textContent = res.message || 'Expense saved';
                msgEl.className = 'text-sm text-emerald-400';
                ceoCancelExpenseEdit();
                await ceoLoadExpenses(propertyId);
                await loadStats();
            } catch (err) {
                msgEl.textContent = err.message || 'Error saving expense';
                msgEl.className = 'text-sm text-red-400';
            }
        }

        function renderConstructionUpdates(items, listId, headlineId) {
            const listEl = document.getElementById(listId);
            const headlineEl = document.getElementById(headlineId);
            if (!listEl) return;
            const latest = getConstructionLatestItem(items);
            if (headlineEl) headlineEl.textContent = latest ? `${latest.progress_percentage}%` : '0%';
            if (!items.length) {
                listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-6">No updates yet. Post the first milestone above.</p>';
                return;
            }
            const isCeoList = listId === 'ceoConstructionList';
            const source = isCeoList ? 'ceo' : 'mgr';
            const sortedItems = items.slice().sort((a, b) => {
                const timeDiff = getConstructionSortValue(b) - getConstructionSortValue(a);
                if (timeDiff !== 0) return timeDiff;
                return (b.progress_percentage || 0) - (a.progress_percentage || 0);
            });
            listEl.innerHTML = `
                <div class="bg-gradient-to-br from-emerald-900/40 via-slate-800 to-gray-900 border border-emerald-700/40 rounded-2xl p-4 sm:p-5 mb-4">
                    <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                        <div class="min-w-0">
                            <p class="text-xs uppercase tracking-widest text-emerald-300/70 mb-1">Current Site Status</p>
                            <h4 class="text-lg sm:text-xl font-semibold text-white leading-snug">${latest?.title || 'Latest update'}</h4>
                            <p class="text-sm text-gray-400 mt-1">${latest?.property_title || ''}${latest?.happened_on ? ' · ' + latest.happened_on : ''}</p>
                        </div>
                        <div class="flex-shrink-0">
                            <p class="text-3xl font-bold text-emerald-400">${latest?.progress_percentage || 0}%</p>
                            <p class="text-xs text-gray-500 mt-0.5">Latest progress</p>
                        </div>
                    </div>
                    <div class="mt-4 h-2.5 rounded-full bg-gray-700 overflow-hidden">
                        <div class="h-2.5 rounded-full bg-gradient-to-r from-emerald-500 to-teal-400 transition-all duration-700" style="width:${latest?.progress_percentage || 0}%"></div>
                    </div>
                    ${latest?.notes ? `<p class="text-sm text-gray-300 leading-relaxed mt-3">${latest.notes}</p>` : ''}
                </div>
                <div class="space-y-2">
                    ${sortedItems.map((item, idx) => `
                        <div class="relative bg-gray-700/30 border ${idx === 0 ? 'border-emerald-600/50' : 'border-gray-600/30'} rounded-xl p-3 sm:p-4 pl-4 sm:pl-5">
                            <div class="absolute left-0 top-3 bottom-3 w-1 rounded-full ${idx === 0 ? 'bg-emerald-500' : 'bg-gray-600'}"></div>
                            <div class="flex items-start justify-between gap-2">
                                <div class="min-w-0 flex-1">
                                    <div class="flex items-center gap-2 flex-wrap">
                                        <p class="font-semibold text-white text-sm">${item.title}</p>
                                        ${idx === 0 ? '<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full bg-emerald-900/70 text-emerald-300 border border-emerald-700/40">Latest</span>' : ''}
                                    </div>
                                    <p class="text-xs text-gray-400 mt-0.5">${item.happened_on ? item.happened_on + ' · ' : ''}${item.progress_percentage}% complete</p>
                                    ${item.notes ? `<p class="text-xs text-gray-300 mt-1.5 leading-relaxed">${item.notes}</p>` : ''}
                                </div>
                                <div class="flex items-center gap-1.5 flex-shrink-0">
                                    <button onclick="editConstructionUpdate(${item.id},'${(item.title||'').replace(/'/g,"\\'")}',${item.progress_percentage},'${item.happened_on||''}','${(item.notes||'').replace(/'/g,"\\'").replace(/\\n/g,' ')}',${item.property_id},'${source}')" class="text-xs text-blue-400 hover:text-blue-300 border border-blue-800/50 rounded px-2 py-1 transition-colors">Edit</button>
                                    <button onclick="deleteConstructionUpdate(${item.id},'${source}')" class="text-xs text-red-400 hover:text-red-300 border border-red-800/50 rounded px-2 py-1 transition-colors">Delete</button>
                                </div>
                            </div>
                        </div>
                    `).join('')}
                </div>
            `;
        }

        async function loadConstructionUpdates(propertyId) {
            try {
                const ceoSel = document.getElementById('ceoConstructionProperty');
                const selectedId = propertyId || ceoSel?.value || '';
                const query = selectedId ? ('?property_id=' + selectedId) : '';
                const updates = await fetchData('/admin/api/construction-updates' + query);
                renderConstructionUpdates(updates, 'ceoConstructionList', 'ceoConstructionHeadline');
            } catch (e) {
                const ceoList = document.getElementById('ceoConstructionList');
                if (ceoList) ceoList.innerHTML = '<p class="text-red-400 text-sm py-4">Error loading construction updates.</p>';
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

        function downloadContract() {
            const title = document.getElementById('cvModalTitle')?.textContent || 'Agreement';
            const body = document.getElementById('cvModalBody')?.textContent || '';
            const userSig = document.getElementById('cvUserSig')?.textContent || '';
            const userDate = document.getElementById('cvUserDate')?.textContent || '';
            const ceoSig = document.getElementById('cvCeoSig')?.textContent || '';
            const ceoDate = document.getElementById('cvCeoDate')?.textContent || '';
            const status = document.getElementById('cvStatus')?.textContent || '';
            const content = `${title}\n${'='.repeat(title.length)}\n\n${body}\n\n${'—'.repeat(40)}\nEmployee / Investor Signature: ${userSig}\n${userDate}\n\nCEO Signature (BrightWave): ${ceoSig}\n${ceoDate}\n\nStatus: ${status}`;
            const blob = new Blob([content], { type: 'text/plain' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = title.replace(/[^a-z0-9]/gi, '_') + '.txt';
            a.click();
            URL.revokeObjectURL(a.href);
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
                    const salaryCell = (a.monthly_salary && a.monthly_salary > 0)
                        ? `<span class="text-emerald-400 text-xs">${fmtNGN(a.monthly_salary)}</span>`
                        : '<span class="text-xs text-gray-600">—</span>';
                    return `
                    <tr class="border-b border-gray-700 hover:bg-gray-750">
                        <td class="py-2 pr-3">${a.display_name || '-'}</td>
                        <td class="py-2 pr-3 text-gray-300">${a.username}</td>
                        <td class="py-2 pr-3 text-gray-400 text-xs">${a.email}</td>
                        <td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded-full text-white ${roleColors[a.role] || 'bg-gray-600'}">${a.role}</span></td>
                        <td class="py-2 pr-3">${secondary || '<span class="text-xs text-gray-600">—</span>'}</td>
                        <td class="py-2 pr-3">${salaryCell}</td>
                        <td class="py-2 pr-3 text-xs ${statusColors[a.contract_status] || 'text-gray-500'}">${statusLabels[a.contract_status] || a.contract_status}</td>
                        <td class="py-2 pr-3"><span class="text-xs ${a.is_active ? 'text-green-400' : 'text-red-400'}">${a.is_active ? 'Active' : 'Inactive'}</span></td>
                        <td class="py-2 flex gap-2 flex-wrap">
                            <button onclick='editAccount(${JSON.stringify(a).replace(/'/g, "&#39;")})' class="text-xs text-blue-400 hover:text-blue-300">Edit</button>
                            <button onclick="toggleAccount(${a.id}, ${!a.is_active})" class="text-xs ${a.is_active ? 'text-red-400 hover:text-red-300' : 'text-green-400 hover:text-green-300'}">${a.is_active ? 'Deactivate' : 'Activate'}</button>
<button onclick="deleteAccount(${a.id})" class="text-xs text-red-500 hover:text-red-400 ml-1">Remove</button>
                        </td>
                    </tr>`;
                }).join('');
            } catch (e) {
                document.getElementById('accountsTable').innerHTML = '<tr><td colspan="9" class="text-red-400 py-2">Error loading accounts</td></tr>';
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

        function updateSalaryVisibility(roleSelectId, wrapId) {
            const role = (document.getElementById(roleSelectId)?.value || '').toUpperCase();
            const wrap = document.getElementById(wrapId);
            if (!wrap) return;
            const commissionRole = (role === 'MANAGER' || role === 'REALTOR');
            wrap.classList.toggle('hidden', commissionRole);
            // Zero out the salary so it isn't sent if hidden
            if (commissionRole) {
                const inp = wrap.querySelector('input');
                if (inp) inp.value = 0;
            }
        }

        function editAccount(a) {
            const currentSecondary = a.secondary_roles || [];
            document.getElementById('editAccId').value = a.id;
            document.getElementById('editAccName').textContent = a.display_name || a.username;
            document.getElementById('editAccRole').value = a.role;
            document.getElementById('editAccUsername').value = a.username || '';
            document.getElementById('editAccEmail').value = a.email || '';
            document.getElementById('editAccDisplayName').value = a.display_name || '';
            document.getElementById('editAccPassword').value = '';
            const salaryEl = document.getElementById('editAccMonthlySalary');
            if (salaryEl) salaryEl.value = a.monthly_salary || 0;
            document.querySelectorAll('.edit-sec-role').forEach(cb => {
                cb.checked = currentSecondary.includes(cb.value);
            });
            updateSalaryVisibility('editAccRole', 'editAccSalaryWrap');
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
            const salaryEl = document.getElementById('editAccMonthlySalary');
            if (salaryEl && salaryEl.value !== '') payload.monthly_salary = salaryEl.value;
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
                        monthly_salary: document.getElementById('accMonthlySalary')?.value || 0,
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

        let _investorPropertiesCache = [];

        async function loadInvestorPropertyDropdowns() {
            try {
                _investorPropertiesCache = await fetchData('/admin/api/properties');
                const opts = '<option value="">— no specific project —</option>' + _investorPropertiesCache.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                ['invPropertyId', 'invEditPropertyId'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.innerHTML = (id === 'invPropertyId' ? '<option value="">Select project...</option>' : '<option value="">— no specific project —</option>') + _investorPropertiesCache.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                });
            } catch (e) {}
        }

        async function loadInvestors() {
            try {
                const investors = await fetchData('/admin/api/investors');
                document.getElementById('investorsTable').innerHTML = investors.map(p => {
                    const annualReturn = p.investment_type === 'DEBT' ? (p.investment_amount * p.roi_rate / 100) : null;
                    const annualPrincipal = p.investment_type === 'DEBT' && p.investment_term_years
                        ? (p.investment_amount / p.investment_term_years)
                        : null;
                    const display = p.investment_type === 'DEBT'
                        ? `${formatNGN(annualPrincipal || 0)} principal + ${formatNGN(annualReturn || 0)} ROI / yr`
                        : `${p.equity_percentage || '?'}% equity`;
                    return `
                        <tr class="border-b border-gray-700 hover:bg-gray-750">
                            <td class="py-2 pr-3">
                                <p class="font-medium">${p.investor_name}</p>
                                <p class="text-xs text-gray-400">${p.investor_email}</p>
                            </td>
                            <td class="py-2 pr-3 text-xs text-gray-300">${p.property_title || '<span class="text-gray-600">—</span>'}</td>
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
                }).join('') || '<tr><td colspan="8" class="text-gray-400 py-4 text-center">No investor profiles yet</td></tr>';
            } catch (e) {
                document.getElementById('investorsTable').innerHTML = '<tr><td colspan="8" class="text-red-400 py-2">Error loading investors</td></tr>';
            }
        }

        let editingInvestorId = null;

        function editInvestor(id, data) {
            editingInvestorId = id;
            document.getElementById('invEditAmount').value = data.investment_amount || '';
            document.getElementById('invEditType').value = data.investment_type || 'DEBT';
            document.getElementById('invEditRoi').value = data.roi_rate || 3.5;
            document.getElementById('invEditEquity').value = data.equity_percentage || '';
            document.getElementById('invEditTerm').value = data.investment_term_years || '';
            document.getElementById('invEditDist').value = data.total_distributed || 0;
            document.getElementById('invEditDate').value = data.investment_date || '';
            document.getElementById('invEditCompletion').value = data.expected_completion_date || '';
            document.getElementById('invEditNotes').value = data.notes || '';
            const propSel = document.getElementById('invEditPropertyId');
            if (propSel) propSel.value = data.property_id ? String(data.property_id) : '';
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
                        property_id: document.getElementById('invEditPropertyId')?.value || null,
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
                        property_id: document.getElementById('invPropertyId')?.value || null,
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
                        <div class="grid grid-cols-2 sm:grid-cols-6 gap-3 text-xs">
                            <div>
                                <p class="text-gray-500 mb-1">Property</p>
                                <p class="text-gray-300">${t.property_name || '—'}</p>
                            </div>
                            <div>
                                <p class="text-gray-500 mb-1">Unit Type</p>
                                <p class="text-gray-300">${t.unit_type_name || '—'}</p>
                            </div>
                            <div>
                                <p class="text-gray-500 mb-1">Unit / Room</p>
                                <p class="text-gray-300">${t.unit_number || '—'}</p>
                            </div>
                            <div>
                                <p class="text-gray-500 mb-1">Yearly Rent</p>
                                <p class="text-emerald-400 font-semibold text-sm">${t.monthly_rent ? fmtNGN(t.monthly_rent) : '—'}</p>
                            </div>
                            <div>
                                <p class="text-gray-500 mb-1">Lease Period</p>
                                <p class="text-gray-400">${t.lease_start || '—'}${t.lease_end ? ' → '+t.lease_end : ''}</p>
                            </div>
                            <div>
                                <p class="text-gray-500 mb-1">Serviced By</p>
                                <p class="text-gray-300">${t.serviced_by_name || '—'}</p>
                            </div>
                        </div>
                        <div class="flex justify-between items-center pt-2 border-t border-gray-600/50">
                            <button onclick='openTenantEdit(${JSON.stringify(t).replace(/'/g,"&#39;")})' class="text-xs text-blue-400 hover:text-blue-300 py-1 px-2 rounded hover:bg-blue-900/30 transition-colors"><i class="fas fa-pen mr-1"></i>Edit</button>
                            <div class="flex gap-2">
                                <button onclick="vacateTenant(${t.id})" class="text-xs text-amber-400 hover:text-amber-300 py-1 px-2 rounded hover:bg-amber-900/30 transition-colors">${t.status === 'active' ? 'Mark Vacated' : 'Vacated'}</button>
                                <button onclick="hardDeleteTenant(${t.id})" class="text-xs text-red-400 hover:text-red-300 py-1 px-2 rounded hover:bg-red-900/30 transition-colors">Remove</button>
                            </div>
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
                        unit_type_id: document.getElementById('tnUnitType')?.value || null,
                        unit_number: document.getElementById('tnUnit').value,
                        monthly_rent: document.getElementById('tnRent').value,
                        lease_start: document.getElementById('tnLeaseStart').value,
                        lease_end: document.getElementById('tnLeaseEnd').value,
                        status: document.getElementById('tnStatus').value,
                        notes: document.getElementById('tnNotes').value,
                        serviced_by_id: document.getElementById('tnServicedBy')?.value || null,
                    })
                });
                msgEl.textContent = res.message;
                msgEl.className = 'text-sm text-green-400';
                document.getElementById('addTenantForm').reset();
                const _unitSel = document.getElementById('tnUnit');
                if (_unitSel) { _unitSel.innerHTML = '<option value="">-- Select property first --</option>'; _unitSel.disabled = true; }
                const _utSel = document.getElementById('tnUnitType');
                if (_utSel) _utSel.innerHTML = '<option value="">-- Select unit type --</option>';
                loadTenants();
                loadStats();
            } catch (err) {
                msgEl.textContent = err.message || 'Error adding tenant';
                msgEl.className = 'text-sm text-red-400';
            }
        });

        // ===== UNIT TYPES (cache + UI) =====
        let _unitTypesCache = [];
        let _propertiesCacheForUt = [];

        async function fetchUnitTypesAndCache() {
            try { _unitTypesCache = await fetchData('/admin/api/unit-types'); }
            catch (e) { _unitTypesCache = []; }
        }

        async function fetchPropertiesForUt() {
            try {
                const props = await fetchData('/admin/api/properties');
                _propertiesCacheForUt = props.filter(p => p.status === 'active');
            } catch (e) { _propertiesCacheForUt = []; }
        }

        function fillUtPropertyOptions(selectEl) {
            if (!selectEl) return;
            const current = selectEl.value;
            selectEl.innerHTML = '<option value="">-- Select property --</option>' +
                _propertiesCacheForUt.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
            if (current) selectEl.value = current;
        }

        async function loadPropertiesForUnitTypeForm() {
            await fetchPropertiesForUt();
            fillUtPropertyOptions(document.getElementById('utProperty'));
        }

        // Repopulate tnUnitType based on currently-selected tnProperty (which uses property TITLE as value, ID is in data-id)
        async function tnUtRefreshForProperty() {
            await fetchUnitTypesAndCache();
            const propSel = document.getElementById('tnProperty');
            const utSel = document.getElementById('tnUnitType');
            if (!propSel || !utSel) return;
            const propId = propSel.selectedOptions[0]?.dataset.id;
            const filtered = propId ? _unitTypesCache.filter(u => String(u.property_id) === String(propId)) : [];
            utSel.innerHTML = '<option value="">-- Select unit type --</option>' +
                filtered.map(u => `<option value="${u.id}" data-price="${u.annual_price || 0}">${u.name}${u.annual_price ? ' — ' + fmtNGN(u.annual_price) + '/yr' : ''}</option>`).join('');
        }

        function tnOnUnitTypeChange() {
            const sel = document.getElementById('tnUnitType');
            const price = sel.selectedOptions[0]?.dataset.price;
            const rentEl = document.getElementById('tnRent');
            if (price && rentEl && (!rentEl.value || rentEl.value === '0')) rentEl.value = price;
        }

        async function loadUnitTypes() {
            try {
                await Promise.all([fetchPropertiesForUt(), fetchUnitTypesAndCache()]);
                const container = document.getElementById('unitTypesContainer');
                if (!_unitTypesCache.length) {
                    container.innerHTML = '<div class="bg-gray-800 p-6 rounded-lg text-gray-400 text-center text-sm">No unit types yet. Add one above to start.</div>';
                    return;
                }
                const grouped = {};
                _unitTypesCache.forEach(u => {
                    const key = u.property_title || 'Unassigned';
                    (grouped[key] = grouped[key] || []).push(u);
                });
                container.innerHTML = Object.entries(grouped).map(([propTitle, items]) => `
                    <div class="bg-gray-800 p-4 rounded-lg">
                        <h3 class="font-semibold text-slate-200 mb-3 flex items-center gap-2"><i class="fas fa-building text-gray-400"></i>${propTitle}</h3>
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                            ${items.map(u => `
                                <div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4">
                                    <div class="flex items-start justify-between gap-3 mb-2">
                                        <p class="font-semibold text-white text-sm">${u.name}</p>
                                        <p class="text-emerald-400 font-bold text-sm flex-shrink-0">${u.annual_price ? fmtNGN(u.annual_price) : '\u20a60'}<span class="text-[10px] text-gray-400 font-normal">/yr</span></p>
                                    </div>
                                    ${u.description ? `<p class="text-xs text-gray-400 mb-3">${u.description}</p>` : ''}
                                    <div class="flex items-center gap-3 text-xs mb-3">
                                        <span class="px-2 py-1 rounded-full bg-blue-900/40 text-blue-300 border border-blue-700/40">${u.occupied_count}/${u.total_count} occupied</span>
                                        <span class="text-gray-500">${u.available_count} available</span>
                                    </div>
                                    <div class="flex justify-end gap-2 pt-2 border-t border-gray-600/50">
                                        <button onclick='openUtEdit(${JSON.stringify(u).replace(/'/g,"&#39;")})' class="text-xs text-blue-400 hover:text-blue-300 py-1 px-2 rounded hover:bg-blue-900/30"><i class="fas fa-pen mr-1"></i>Edit</button>
                                        <button onclick="deleteUnitType(${u.id})" class="text-xs text-red-400 hover:text-red-300 py-1 px-2 rounded hover:bg-red-900/30"><i class="fas fa-trash mr-1"></i>Delete</button>
                                    </div>
                                </div>
                            `).join('')}
                        </div>
                    </div>`).join('');
            } catch (e) {
                document.getElementById('unitTypesContainer').innerHTML = '<p class="text-red-400 py-4 text-sm">Error loading unit types</p>';
            }
        }

        document.getElementById('addUnitTypeForm')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('utMsg');
            try {
                const res = await fetchData('/admin/api/unit-types', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        property_id: document.getElementById('utProperty').value,
                        name: document.getElementById('utName').value,
                        description: document.getElementById('utDesc').value,
                        annual_price: document.getElementById('utPrice').value || 0,
                        total_count: document.getElementById('utCount').value || 0,
                    })
                });
                msgEl.textContent = res.message;
                msgEl.className = 'text-sm text-green-400';
                document.getElementById('addUnitTypeForm').reset();
                loadUnitTypes();
            } catch (err) {
                msgEl.textContent = err.message || 'Error adding unit type';
                msgEl.className = 'text-sm text-red-400';
            }
        });

        function openUtEdit(u) {
            document.getElementById('utEditId').value = u.id;
            fillUtPropertyOptions(document.getElementById('utEditProperty'));
            document.getElementById('utEditProperty').value = u.property_id;
            document.getElementById('utEditName').value = u.name || '';
            document.getElementById('utEditDesc').value = u.description || '';
            document.getElementById('utEditPrice').value = u.annual_price || 0;
            document.getElementById('utEditCount').value = u.total_count || 0;
            document.getElementById('utEditMsg').classList.add('hidden');
            const modal = document.getElementById('utEditModal');
            modal.style.display = 'flex';
            modal.classList.remove('hidden');
        }

        function closeUtEdit() {
            const modal = document.getElementById('utEditModal');
            modal.style.display = 'none';
            modal.classList.add('hidden');
        }

        document.getElementById('utEditForm')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('utEditMsg');
            msgEl.classList.remove('hidden');
            msgEl.textContent = 'Saving...';
            msgEl.className = 'text-xs text-gray-400';
            try {
                const id = document.getElementById('utEditId').value;
                const res = await fetchData('/admin/api/unit-types/' + id, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        property_id: document.getElementById('utEditProperty').value,
                        name: document.getElementById('utEditName').value,
                        description: document.getElementById('utEditDesc').value,
                        annual_price: document.getElementById('utEditPrice').value || 0,
                        total_count: document.getElementById('utEditCount').value || 0,
                    })
                });
                msgEl.textContent = res.message || 'Saved';
                msgEl.className = 'text-xs text-emerald-400';
                loadUnitTypes();
                setTimeout(closeUtEdit, 900);
            } catch (err) {
                msgEl.textContent = err.message || 'Error saving';
                msgEl.className = 'text-xs text-red-400';
            }
        });

        async function deleteUnitType(id) {
            if (!confirm('Delete this unit type? This cannot be undone.')) return;
            try {
                await fetchData('/admin/api/unit-types/' + id, { method: 'DELETE' });
                loadUnitTypes();
            } catch (e) { alert(e.message || 'Error deleting unit type'); }
        }

        // ===== TENANT EDIT (modal opens from card Edit button) =====
        function openTenantEdit(t) {
            (async () => {
                await Promise.all([fetchPropertiesForUt(), fetchUnitTypesAndCache()]);

                // Build prop select with title as value (matches add form pattern)
                const propSel = document.getElementById('tnEditProperty');
                propSel.innerHTML = '<option value="">-- Select property --</option>' +
                    _propertiesCacheForUt.map(p => `<option value="${p.title}" data-id="${p.id}">${p.title}</option>`).join('');

                if (t.property_name) propSel.value = t.property_name;

                const refreshEditUt = () => {
                    const propId = propSel.selectedOptions[0]?.dataset.id;
                    const utSel = document.getElementById('tnEditUnitType');
                    const filtered = propId ? _unitTypesCache.filter(u => String(u.property_id) === String(propId)) : [];
                    utSel.innerHTML = '<option value="">-- Select unit type --</option>' +
                        filtered.map(u => `<option value="${u.id}" data-price="${u.annual_price || 0}">${u.name}${u.annual_price ? ' — ' + fmtNGN(u.annual_price) + '/yr' : ''}</option>`).join('');
                    if (t.unit_type_id) utSel.value = t.unit_type_id;
                };
                refreshEditUt();
                propSel.onchange = refreshEditUt;

                document.getElementById('tnEditId').value = t.id;
                document.getElementById('tnEditName').value = t.name || '';
                document.getElementById('tnEditEmail').value = t.email || '';
                document.getElementById('tnEditPhone').value = t.phone || '';
                document.getElementById('tnEditUnit').value = t.unit_number || '';
                document.getElementById('tnEditRent').value = t.monthly_rent || 0;
                document.getElementById('tnEditStatus').value = t.status || 'active';
                document.getElementById('tnEditLeaseStart').value = t.lease_start || '';
                document.getElementById('tnEditLeaseEnd').value = t.lease_end || '';
                document.getElementById('tnEditNotes').value = t.notes || '';
                await fillServicedBySelect(document.getElementById('tnEditServicedBy'), t.serviced_by_id);
                document.getElementById('tnEditMsg').classList.add('hidden');
                const modal = document.getElementById('tenantEditModal');
                modal.style.display = 'flex';
                modal.classList.remove('hidden');
            })();
        }

        function closeTenantEdit() {
            const modal = document.getElementById('tenantEditModal');
            modal.style.display = 'none';
            modal.classList.add('hidden');
        }

        document.getElementById('tenantEditForm')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('tnEditMsg');
            msgEl.classList.remove('hidden');
            msgEl.textContent = 'Saving...';
            msgEl.className = 'text-xs text-gray-400 md:col-span-2';
            try {
                const id = document.getElementById('tnEditId').value;
                const res = await fetchData('/admin/api/tenants/' + id, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({
                        name: document.getElementById('tnEditName').value,
                        email: document.getElementById('tnEditEmail').value,
                        phone: document.getElementById('tnEditPhone').value,
                        property_name: document.getElementById('tnEditProperty').value,
                        unit_type_id: document.getElementById('tnEditUnitType').value || null,
                        unit_number: document.getElementById('tnEditUnit').value,
                        monthly_rent: document.getElementById('tnEditRent').value || 0,
                        status: document.getElementById('tnEditStatus').value,
                        lease_start: document.getElementById('tnEditLeaseStart').value,
                        lease_end: document.getElementById('tnEditLeaseEnd').value,
                        notes: document.getElementById('tnEditNotes').value,
                        serviced_by_id: document.getElementById('tnEditServicedBy').value || null,
                    })
                });
                msgEl.textContent = res.message || 'Saved';
                msgEl.className = 'text-xs text-emerald-400 md:col-span-2';
                loadTenants(document.getElementById('tnFilterStatus').value);
                loadStats();
                setTimeout(closeTenantEdit, 900);
            } catch (err) {
                msgEl.textContent = err.message || 'Error saving';
                msgEl.className = 'text-xs text-red-400 md:col-span-2';
            }
        });

        // ===== PAYROLL TAB =====
        const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        let _payrollLastRows = [];

        function _initPayrollPickers() {
            const yearSel = document.getElementById('payrollYear');
            const monthSel = document.getElementById('payrollMonth');
            if (!yearSel || yearSel.options.length) return;
            const now = new Date();
            const startYear = now.getFullYear() - 2;
            for (let y = now.getFullYear() + 1; y >= startYear; y--) {
                const opt = document.createElement('option');
                opt.value = y; opt.textContent = y;
                if (y === now.getFullYear()) opt.selected = true;
                yearSel.appendChild(opt);
            }
            for (let m = 1; m <= 12; m++) {
                const opt = document.createElement('option');
                opt.value = m; opt.textContent = MONTH_NAMES[m-1];
                if (m === now.getMonth() + 1) opt.selected = true;
                monthSel.appendChild(opt);
            }
        }

        async function loadPayroll() {
            _initPayrollPickers();
            const year = document.getElementById('payrollYear').value;
            const month = document.getElementById('payrollMonth').value;
            const tbody = document.getElementById('payrollTable');
            tbody.innerHTML = '<tr><td colspan="8" class="py-6 text-center text-gray-500">Loading...</td></tr>';
            try {
                const res = await fetchData(`/admin/api/payroll/summary?year=${year}&month=${month}`);
                if (!res.success) throw new Error(res.message || 'Failed');
                _payrollLastRows = res.rows || [];
                document.getElementById('pr_commTotal').textContent = fmtNGN(res.totals.commission_total);
                document.getElementById('pr_salTotal').textContent = fmtNGN(res.totals.salary_total);
                document.getElementById('pr_earnTotal').textContent = fmtNGN(res.totals.earned_total);
                document.getElementById('pr_paidTotal').textContent = fmtNGN(res.totals.paid_total);
                document.getElementById('pr_outTotal').textContent = fmtNGN(res.totals.outstanding_total);

                if (!_payrollLastRows.length) {
                    tbody.innerHTML = '<tr><td colspan="8" class="py-6 text-center text-gray-500">No payroll-eligible staff for this month</td></tr>';
                    return;
                }
                tbody.innerHTML = _payrollLastRows.map((r, idx) => {
                    const linesId = `prLines_${r.user_id}`;
                    const histId = `prHist_${r.user_id}`;
                    const hasLines = r.commission_lines && r.commission_lines.length;
                    const hasHistory = r.payment_history && r.payment_history.length;
                    const linesToggle = hasLines
                        ? `<button onclick="document.getElementById('${linesId}').classList.toggle('hidden')" class="text-xs text-blue-400 hover:text-blue-300 ml-1">[${r.commission_lines.length}]</button>`
                        : '';
                    const histToggle = hasHistory
                        ? `<button onclick="document.getElementById('${histId}').classList.toggle('hidden')" class="text-xs text-blue-400 hover:text-blue-300 ml-1">[${r.payment_history.length}]</button>`
                        : '';
                    const linesRow = hasLines
                        ? `<tr id="${linesId}" class="hidden"><td colspan="8" class="bg-gray-900/40 px-4 py-3">
                                <p class="text-xs text-gray-400 mb-2">Breakdown — Yearly Rent stored on tenant is the gross total (base + 10% markup). Manager earns the markup; company keeps the base.</p>
                                <table class="w-full text-xs"><thead><tr class="text-gray-500"><th class="text-left py-1">Tenant</th><th class="text-left py-1">Unit</th><th class="text-right py-1">Tenant Pays</th><th class="text-right py-1">Base (company)</th><th class="text-right py-1">Commission (10%)</th></tr></thead>
                                <tbody>${r.commission_lines.map(l => `<tr class="border-t border-gray-700/40"><td class="py-1 text-gray-300">${l.tenant_name}</td><td class="py-1 text-gray-400">${l.unit_number || '—'}</td><td class="py-1 text-right text-gray-300">${fmtNGN(l.annual_rent)}</td><td class="py-1 text-right text-emerald-400">${fmtNGN(l.base_rent)}</td><td class="py-1 text-right text-amber-400">${fmtNGN(l.commission)}</td></tr>`).join('')}</tbody></table>
                           </td></tr>`
                        : '';
                    const histRow = hasHistory
                        ? `<tr id="${histId}" class="hidden"><td colspan="8" class="bg-gray-900/40 px-4 py-3">
                                <p class="text-xs text-gray-400 mb-2">Payment history for this month — edit a mistake or remove an entry:</p>
                                <table class="w-full text-xs"><thead><tr class="text-gray-500"><th class="text-left py-1">Paid</th><th class="text-left py-1">Kind</th><th class="text-right py-1">Amount</th><th class="text-left py-1 pl-3">Notes</th><th class="text-left py-1">By</th><th class="text-left py-1 pl-3">Actions</th></tr></thead>
                                <tbody>${r.payment_history.map(p => `<tr class="border-t border-gray-700/40">
                                    <td class="py-1 text-gray-400">${(p.paid_at || '').slice(0,10)}</td>
                                    <td class="py-1 text-gray-300">${p.kind}</td>
                                    <td class="py-1 text-right text-emerald-400">${fmtNGN(p.amount)}</td>
                                    <td class="py-1 text-gray-400 pl-3 max-w-[200px] truncate" title="${(p.notes||'').replace(/"/g,'&quot;')}">${p.notes || '—'}</td>
                                    <td class="py-1 text-gray-500 text-[11px]">${p.paid_by || '—'}</td>
                                    <td class="py-1 pl-3"><button onclick="editPayrollPayment(${p.id}, ${p.amount}, '${p.kind}', '${(p.notes||'').replace(/'/g,"\\'").replace(/"/g,'&quot;')}')" class="text-blue-400 hover:text-blue-300 mr-2">Edit</button><button onclick="deletePayrollPayment(${p.id})" class="text-red-400 hover:text-red-300">Delete</button></td>
                                </tr>`).join('')}</tbody></table>
                           </td></tr>`
                        : '';
                    const outstandingColor = r.outstanding > 0 ? 'text-red-400' : 'text-gray-500';
                    const payDisabled = r.outstanding <= 0 ? 'opacity-40 pointer-events-none' : '';
                    return `
                        <tr class="border-b border-gray-700">
                            <td class="py-2 pr-3 text-gray-200">${r.display_name}</td>
                            <td class="py-2 pr-3 text-gray-400 text-xs">${r.role}</td>
                            <td class="py-2 pr-3 text-right text-amber-400">${fmtNGN(r.commission_total)}${linesToggle}</td>
                            <td class="py-2 pr-3 text-right text-blue-400">${fmtNGN(r.salary)}</td>
                            <td class="py-2 pr-3 text-right text-emerald-400 font-semibold">${fmtNGN(r.total_earned)}</td>
                            <td class="py-2 pr-3 text-right text-gray-300">${fmtNGN(r.already_paid)}${histToggle}</td>
                            <td class="py-2 pr-3 text-right ${outstandingColor} font-semibold">${fmtNGN(r.outstanding)}</td>
                            <td class="py-2 pl-3">
                                <button onclick='openPayrollPayModal(${idx})' class="text-xs bg-emerald-700 hover:bg-emerald-600 text-white px-3 py-1 rounded ${payDisabled}">Pay</button>
                            </td>
                        </tr>
                        ${linesRow}
                        ${histRow}
                    `;
                }).join('');
            } catch (e) {
                tbody.innerHTML = `<tr><td colspan="8" class="py-6 text-center text-red-400">${e.message || 'Error loading payroll'}</td></tr>`;
            }
        }

        function openPayrollPayModal(idx) {
            const row = _payrollLastRows[idx];
            if (!row) return;
            document.getElementById('prPayUserId').value = row.user_id;
            document.getElementById('prPayWho').textContent = `${row.display_name} — outstanding ${fmtNGN(row.outstanding)}`;
            document.getElementById('prPayAmount').value = Math.round(row.outstanding);
            document.getElementById('prPayKind').value = row.commission_total > 0 ? 'commission' : 'salary';
            document.getElementById('prPayNotes').value = '';
            const msg = document.getElementById('prPayMsg');
            msg.classList.add('hidden'); msg.textContent = '';
            const modal = document.getElementById('payrollPayModal');
            modal.style.display = 'flex';
            modal.classList.remove('hidden');
        }

        function closePayrollPayModal() {
            const modal = document.getElementById('payrollPayModal');
            modal.style.display = 'none';
            modal.classList.add('hidden');
        }

        async function editPayrollPayment(id, currentAmount, currentKind, currentNotes) {
            const newAmountStr = prompt('New amount (₦):', String(currentAmount));
            if (newAmountStr === null) return;
            const newAmount = parseFloat(newAmountStr);
            if (!(newAmount > 0)) { alert('Amount must be positive'); return; }
            const newKindStr = prompt('Kind (commission or salary):', currentKind);
            if (newKindStr === null) return;
            const newKind = newKindStr.trim().toLowerCase();
            if (newKind !== 'commission' && newKind !== 'salary') { alert('Kind must be commission or salary'); return; }
            const newNotesStr = prompt('Notes (optional):', currentNotes || '');
            if (newNotesStr === null) return;
            try {
                await fetchData('/admin/api/payroll/pay/' + id, {
                    method: 'PUT', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ amount: newAmount, kind: newKind, notes: newNotesStr })
                });
                loadPayroll();
            } catch (e) { alert(e.message || 'Error editing payment'); }
        }

        async function deletePayrollPayment(id) {
            if (!confirm('Permanently remove this payment record? This will increase the outstanding balance.')) return;
            try {
                await fetchData('/admin/api/payroll/pay/' + id, { method: 'DELETE' });
                loadPayroll();
            } catch (e) { alert(e.message || 'Error removing payment'); }
        }

        async function submitPayrollPayment() {
            const user_id = document.getElementById('prPayUserId').value;
            const amount = document.getElementById('prPayAmount').value;
            const kind = document.getElementById('prPayKind').value;
            const notes = document.getElementById('prPayNotes').value;
            const year = document.getElementById('payrollYear').value;
            const month = document.getElementById('payrollMonth').value;
            const msg = document.getElementById('prPayMsg');
            msg.classList.remove('hidden');
            msg.textContent = 'Saving...';
            msg.className = 'text-sm text-gray-400';
            try {
                const res = await fetchData('/admin/api/payroll/pay', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ user_id, amount, kind, notes, year, month })
                });
                msg.textContent = res.message || 'Recorded';
                msg.className = 'text-sm text-emerald-400';
                setTimeout(() => { closePayrollPayModal(); loadPayroll(); }, 700);
            } catch (e) {
                msg.textContent = e.message || 'Error recording payment';
                msg.className = 'text-sm text-red-400';
            }
        }

        // ===== SIGNUP APPROVALS =====
        const APPROVAL_ROLE_COLORS = { INVESTOR:'bg-emerald-700', MANAGER:'bg-blue-700', ACCOUNTANT:'bg-green-700', REALTOR:'bg-amber-700', PA:'bg-purple-700' };

        async function loadApprovals() {
            const container = document.getElementById('approvalsContainer');
            const status = document.getElementById('approvalsStatusFilter').value;
            container.innerHTML = '<p class="text-gray-500 text-sm text-center py-6">Loading...</p>';
            try {
                const res = await fetchData('/admin/api/signups?status=' + encodeURIComponent(status));
                if (!res.success) throw new Error(res.message || 'Failed');
                const badge = document.getElementById('approvalsBadge');
                if (badge) {
                    badge.textContent = res.pending_count;
                    badge.classList.toggle('hidden', !res.pending_count);
                }
                if (!res.signups.length) {
                    container.innerHTML = `<p class="text-gray-500 text-sm text-center py-6">No ${status} signups.</p>`;
                    return;
                }
                container.innerHTML = res.signups.map(s => renderApprovalCard(s)).join('');
            } catch (e) {
                container.innerHTML = `<p class="text-red-400 text-sm py-3">${e.message || 'Error loading signups'}</p>`;
            }
        }

        function renderApprovalCard(s) {
            const roleColor = APPROVAL_ROLE_COLORS[s.role] || 'bg-gray-600';
            const rd = s.role_data || {};
            let detailsHtml = '';
            if (s.role === 'INVESTOR') {
                detailsHtml = `
                    <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs mt-2">
                        <div><p class="text-gray-500">Intended Amount</p><p class="text-emerald-400 font-semibold">${rd.investment_amount ? fmtNGN(rd.investment_amount) : '—'}</p></div>
                        <div><p class="text-gray-500">Type</p><p class="text-gray-300">${rd.investment_type || '—'}</p></div>
                        <div><p class="text-gray-500">Term (yrs)</p><p class="text-gray-300">${rd.term_years || '—'}</p></div>
                        <div><p class="text-gray-500">Submitted</p><p class="text-gray-400 text-[11px]">${s.created_at || '—'}</p></div>
                    </div>`;
            } else {
                detailsHtml = `
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs mt-2">
                        <div><p class="text-gray-500">Experience</p><p class="text-gray-300">${rd.experience || '—'}</p></div>
                        <div><p class="text-gray-500">Availability</p><p class="text-gray-300">${rd.availability || '—'}</p></div>
                    </div>
                    <p class="text-[11px] text-gray-500 mt-2">Submitted ${s.created_at || '—'}</p>`;
            }
            const notesHtml = rd.notes ? `<p class="text-xs text-gray-400 mt-2 italic">"${rd.notes}"</p>` : '';

            let actionsHtml = '';
            if (s.status === 'pending') {
                actionsHtml = `
                    <div class="flex gap-2 pt-3 border-t border-gray-700/50 mt-3">
                        <button onclick="approveSignup(${s.id})" class="flex-1 bg-emerald-700 hover:bg-emerald-600 text-white text-sm font-medium py-1.5 rounded-lg"><i class="fas fa-check mr-1"></i>Approve</button>
                        <button onclick="rejectSignup(${s.id})" class="flex-1 bg-red-700 hover:bg-red-600 text-white text-sm font-medium py-1.5 rounded-lg"><i class="fas fa-times mr-1"></i>Reject</button>
                    </div>`;
            } else if (s.status === 'approved') {
                actionsHtml = `<p class="text-xs text-emerald-400 mt-3 pt-3 border-t border-gray-700/50">✓ Approved ${s.reviewed_at || ''} by ${s.reviewed_by || '—'}</p>`;
            } else if (s.status === 'rejected') {
                actionsHtml = `<p class="text-xs text-red-400 mt-3 pt-3 border-t border-gray-700/50">✕ Rejected ${s.reviewed_at || ''} by ${s.reviewed_by || '—'}${s.rejection_reason ? ' — ' + s.rejection_reason : ''}</p>`;
            }

            return `
                <div class="bg-gray-800 border border-gray-700/60 rounded-xl p-4">
                    <div class="flex items-start justify-between gap-3 flex-wrap">
                        <div class="min-w-0">
                            <p class="font-semibold text-white">${s.full_name} <span class="text-xs ml-1 px-2 py-0.5 rounded-full text-white ${roleColor}">${s.role}</span></p>
                            <p class="text-xs text-gray-400 mt-0.5">${s.email}${s.phone ? ' · ' + s.phone : ''}</p>
                        </div>
                        <span class="text-[10px] uppercase tracking-wide px-2 py-0.5 rounded ${s.status === 'pending' ? 'bg-amber-900/50 text-amber-300' : s.status === 'approved' ? 'bg-emerald-900/50 text-emerald-300' : 'bg-red-900/50 text-red-300'}">${s.status}</span>
                    </div>
                    ${detailsHtml}
                    ${notesHtml}
                    ${actionsHtml}
                </div>`;
        }

        async function approveSignup(id) {
            if (!confirm('Approve this signup? This creates an active account immediately.')) return;
            try {
                const res = await fetchData('/admin/api/signups/' + id + '/approve', { method: 'POST' });
                alert(res.message || 'Approved');
                loadApprovals();
                if (typeof loadAccounts === 'function') loadAccounts();
            } catch (e) { alert(e.message || 'Error approving'); }
        }

        async function rejectSignup(id) {
            const reason = prompt('Reason for rejection (optional, shown to no one but you):', '');
            if (reason === null) return;  // cancelled
            try {
                const res = await fetchData('/admin/api/signups/' + id + '/reject', {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ reason })
                });
                alert(res.message || 'Rejected');
                loadApprovals();
            } catch (e) { alert(e.message || 'Error rejecting'); }
        }

        // Poll pending-count once at dashboard load (best-effort, doesn't block)
        (function pollApprovalsBadge() {
            try {
                fetch('/admin/api/signups?status=pending').then(r => r.json()).then(d => {
                    if (!d || !d.success) return;
                    const badge = document.getElementById('approvalsBadge');
                    if (badge) { badge.textContent = d.pending_count; badge.classList.toggle('hidden', !d.pending_count); }
                }).catch(() => {});
            } catch (e) {}
        })();

        // ===== PAYROLL HELPERS (shared across tenant form + payroll tab) =====
        let _servicedByCache = null;
        async function fetchServicedByCandidates() {
            if (_servicedByCache) return _servicedByCache;
            try {
                const accts = await fetchData('/admin/api/accounts');
                _servicedByCache = (accts || []).filter(a => {
                    const r = (a.role || '').toUpperCase();
                    const sr = (a.secondary_roles || []).map(x => (x || '').toUpperCase());
                    return r === 'MANAGER' || r === 'REALTOR' || sr.includes('MANAGER') || sr.includes('REALTOR');
                });
            } catch (e) { _servicedByCache = []; }
            return _servicedByCache;
        }
        async function fillServicedBySelect(selectEl, selectedId) {
            if (!selectEl) return;
            const cands = await fetchServicedByCandidates();
            selectEl.innerHTML = '<option value="">-- Not tracked --</option>' +
                cands.map(a => `<option value="${a.id}">${(a.display_name || a.username)} — ${a.role}</option>`).join('');
            if (selectedId != null) selectEl.value = String(selectedId);
        }

        // ===== PAYMENTS =====
        window._ceoPmtTenantMap = {};

        async function loadTenantOptions() {
            try {
                const tenants = await fetchData('/admin/api/tenants?status=active');
                window._ceoPmtTenantMap = {};
                tenants.forEach(t => { window._ceoPmtTenantMap[t.id] = t; });
                const sel = document.getElementById('pmtTenantId');
                if (!sel) return;
                sel.innerHTML = '<option value="">-- Select tenant --</option>' + tenants.map(t =>
                    `<option value="${t.id}">${t.name}${t.property_name ? ' — ' + t.property_name : ''}${t.unit_number ? ' / ' + t.unit_number : ''}</option>`
                ).join('');
            } catch (e) {}
        }

        function pmtOnTenantChange() {
            const sel = document.getElementById('pmtTenantId');
            const tid = parseInt(sel.value);
            if (!tid) return;
            const t = window._ceoPmtTenantMap[tid];
            if (!t) return;
            const unitEl = document.getElementById('pmtUnit');
            const amtEl = document.getElementById('pmtAmount');
            const nameEl = document.getElementById('pmtTenantName');
            if (unitEl && t.unit_number) unitEl.value = t.unit_number;
            if (amtEl && t.monthly_rent) amtEl.value = t.monthly_rent;
            if (nameEl) nameEl.value = '';
        }

        async function tnPopulatePropertyDropdown() {
            try {
                const props = await fetchData('/admin/api/properties');
                const sel = document.getElementById('tnProperty');
                if (!sel) return;
                sel.innerHTML = '<option value="">-- Select property --</option>' +
                    props.map(p => `<option value="${p.title}" data-id="${p.id}">${p.title}</option>`).join('');
            } catch(e) {}
        }

        async function tnOnPropertyChange() {
            const propSel = document.getElementById('tnProperty');
            const unitSel = document.getElementById('tnUnit');
            const rentEl = document.getElementById('tnRent');
            const propId = propSel.selectedOptions[0] && propSel.selectedOptions[0].dataset.id;
            unitSel.innerHTML = '<option value="">-- Loading units... --</option>';
            unitSel.disabled = true;
            unitSel.className = unitSel.className.replace('opacity-60','') + ' opacity-60';
            if (rentEl) rentEl.value = '';
            if (!propId) { unitSel.innerHTML = '<option value="">-- Select property first --</option>'; return; }
            try {
                const units = await fetchData('/admin/api/units?property_id=' + propId);
                const opts = units.map(u => {
                    // Price comes from Unit Type now, so only show room number + status (if not available)
                    const label = u.unit_code + (u.status !== 'available' ? ' (' + u.status + ')' : '');
                    return `<option value="${u.unit_code}" data-status="${u.status}">${label}</option>`;
                });
                unitSel.innerHTML = '<option value="">-- Select unit --</option>' + opts.join('');
                unitSel.disabled = false;
                unitSel.className = unitSel.className.replace('opacity-60','');
            } catch(e) { unitSel.innerHTML = '<option value="">-- Error loading units --</option>'; }
        }

        function tnOnUnitChange() {
            // Rent now flows from Unit Type only — no auto-fill on physical unit selection
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

        let ceoPaymentsCache = [];

        async function loadPayments() {
            try {
                const payments = await fetchData('/admin/api/payments');
                ceoPaymentsCache = payments;
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
                        <div class="flex items-center justify-between gap-2 flex-wrap text-xs">
                            <div class="flex items-center gap-2 flex-wrap">
                                <span class="px-2.5 py-1 rounded-full ${typeColors[p.payment_type] || 'bg-gray-700 text-gray-300'}">${p.payment_type}</span>
                                ${meta.unit ? `<span class="px-2 py-1 rounded-full bg-slate-700 text-slate-300">Unit ${meta.unit}</span>` : ''}
                                ${meta.period ? `<span class="px-2 py-1 rounded-full bg-indigo-900/50 text-indigo-300">${meta.period}</span>` : ''}
                                <span class="text-gray-400">${p.payment_date}</span>
                                ${p.recorded_by ? `<span class="text-gray-500">by ${p.recorded_by}</span>` : ''}
                            </div>
                            <div class="flex items-center gap-3">
                                <button type="button" onclick="startCeoPaymentEdit(${p.id})" class="text-blue-400 hover:text-blue-300 font-medium">Edit</button>
                                <button type="button" onclick="deleteCeoPayment(${p.id})" class="text-red-400 hover:text-red-300 font-medium">Remove</button>
                            </div>
                        </div>
                    </div>`;
                }).join('') : '<p class="text-gray-400 py-6 text-center text-sm">No payments recorded</p>';
            } catch (e) {
                document.getElementById('paymentsContainer').innerHTML = '<p class="text-red-400 py-4 text-sm">Error loading payments</p>';
            }
        }

        function startCeoPaymentEdit(paymentId) {
            const p = ceoPaymentsCache.find(x => x.id === paymentId);
            if (!p) return;
            const meta = parsePaymentMeta(p.description);
            document.getElementById('pmtEditId').value = p.id;
            document.getElementById('pmtTenantId').value = p.tenant_id || '';
            document.getElementById('pmtTenantName').value = p.tenant_name || '';
            document.getElementById('pmtAmount').value = p.amount || '';
            document.getElementById('pmtDate').value = p.payment_date || '';
            document.getElementById('pmtType').value = p.payment_type || 'rent';
            document.getElementById('pmtUnit').value = meta.unit || '';
            document.getElementById('pmtPeriod').value = meta.period || '';
            document.getElementById('pmtDesc').value = meta.notes || '';
            document.getElementById('pmtSubmitBtn').textContent = 'Save Changes';
            document.getElementById('pmtCancelBtn').classList.remove('hidden');
            document.getElementById('addPaymentForm').scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        function cancelCeoPaymentEdit() {
            document.getElementById('pmtEditId').value = '';
            document.getElementById('addPaymentForm').reset();
            document.getElementById('pmtSubmitBtn').textContent = 'Record Payment';
            document.getElementById('pmtCancelBtn').classList.add('hidden');
        }

        async function deleteCeoPayment(paymentId) {
            if (!confirm('Remove this payment record?')) return;
            try {
                await fetchData('/admin/api/payments/' + paymentId, { method: 'DELETE' });
                cancelCeoPaymentEdit();
                loadPayments();
                loadStats();
            } catch (err) { alert(err.message || 'Error removing payment'); }
        }

        async function hardDeleteTenant(id) {
            if (!confirm('Permanently remove this tenant record? This cannot be undone.')) return;
            try {
                await fetchData('/admin/api/tenants/' + id + '?hard=1', { method: 'DELETE' });
                loadTenants(document.getElementById('tnFilterStatus').value);
                loadStats();
            } catch (e) { alert(e.message || 'Error removing tenant'); }
        }

        document.getElementById('addPaymentForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const msgEl = document.getElementById('paymentMsg');
            const editId = document.getElementById('pmtEditId')?.value || '';
            try {
                const tenantSelect = document.getElementById('pmtTenantId');
                const unit = (document.getElementById('pmtUnit')?.value || '').trim();
                const period = (document.getElementById('pmtPeriod')?.value || '').trim();
                const notes = (document.getElementById('pmtDesc')?.value || '').trim();
                const descParts = [];
                if (unit) descParts.push(`Unit:${unit}`);
                if (period) descParts.push(`Period:${period}`);
                if (notes) descParts.push(notes);
                const payload = {
                    tenant_id: tenantSelect.value || null,
                    tenant_name: document.getElementById('pmtTenantName').value,
                    amount: document.getElementById('pmtAmount').value,
                    payment_date: document.getElementById('pmtDate').value,
                    payment_type: document.getElementById('pmtType').value,
                    description: descParts.join(' | ') || null,
                };
                const url = editId ? '/admin/api/payments/' + editId : '/admin/api/payments';
                const method = editId ? 'PUT' : 'POST';
                const res = await fetchData(url, { method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                msgEl.textContent = res.message;
                msgEl.className = 'text-sm text-green-400';
                cancelCeoPaymentEdit();
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
            const prefix = source === 'ceo' ? 'ceo' : 'mgr';
            const ids = source === 'ceo'
                ? { property: 'ceoConstructionProperty', title: 'ceoConstructionTitle', percent: 'ceoConstructionPercent', date: 'ceoConstructionDate', notes: 'ceoConstructionNotes', msg: 'ceoConstructionMsg', form: 'ceoConstructionForm', editId: 'ceoConstructionEditId' }
                : { property: 'mgrConstructionProperty', title: 'mgrConstructionTitle', percent: 'mgrConstructionPercent', date: 'mgrConstructionDate', notes: 'mgrConstructionNotes', msg: 'mgrConstructionMsg', form: 'managerConstructionForm', editId: 'mgrConstructionEditId' };
            const msgEl = document.getElementById(ids.msg);
            const editId = document.getElementById(ids.editId)?.value || '';
            const propId = document.getElementById(ids.property)?.value;
            try {
                const payload = {
                    property_id: propId,
                    title: document.getElementById(ids.title).value,
                    progress_percentage: document.getElementById(ids.percent).value,
                    happened_on: document.getElementById(ids.date).value || null,
                    notes: document.getElementById(ids.notes).value,
                    is_public: true,
                };
                const url = editId ? `/admin/api/construction-updates/${editId}` : '/admin/api/construction-updates';
                const method = editId ? 'PUT' : 'POST';
                const res = await fetchData(url, { method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                msgEl.textContent = res.message || (editId ? 'Update saved' : 'Update posted');
                msgEl.className = 'text-sm text-emerald-400';
                cancelConstructionEdit(source);
                loadConstructionPropertyOptions();
                loadConstructionUpdates(document.getElementById(ids.property).value || null);
            } catch (e) {
                msgEl.textContent = e.message || 'Error posting update';
                msgEl.className = 'text-sm text-red-400';
            }
        }

        // Initialize dashboard - show overview by default
        document.addEventListener('DOMContentLoaded', () => {
            showSection('overviewSection', true);
            ceoPopulateProjectFilter();
            loadStats();
            loadProperties();
            loadConstructionPropertyOptions();
            loadInquiries();
            loadMessages();
            loadSiteContent();
            loadTeamMembers();
            document.getElementById('ceoConstructionProperty')?.addEventListener('change', (e) => loadConstructionUpdates(e.target.value));
            document.getElementById('ceoConstructionForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                await submitConstructionUpdate('ceo');
            });
            document.getElementById('ceoCapitalProperty')?.addEventListener('change', () => ceoLoadExpenses());
            document.getElementById('ceoExpenseStatusFilter')?.addEventListener('change', () => ceoLoadExpenses());
            document.getElementById('ceoExpenseReceiptOnly')?.addEventListener('change', () => ceoLoadExpenses());
            document.getElementById('ceoExpenseForm')?.addEventListener('submit', ceoSubmitExpense);
            loadCapitalPropertyOptions();
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

        function downloadContract() {
            const title = document.getElementById('cvModalTitle')?.textContent || 'Agreement';
            const body = document.getElementById('cvModalBody')?.textContent || '';
            const userSig = document.getElementById('cvUserSig')?.textContent || '';
            const userDate = document.getElementById('cvUserDate')?.textContent || '';
            const ceoSig = document.getElementById('cvCeoSig')?.textContent || '';
            const ceoDate = document.getElementById('cvCeoDate')?.textContent || '';
            const status = document.getElementById('cvStatus')?.textContent || '';
            const refNum = 'BWH-' + Date.now().toString(36).toUpperCase().slice(-8);
            const printDate = new Date().toLocaleDateString('en-GB', {day:'2-digit',month:'long',year:'numeric'});
            const safeBody = body.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            const html = `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>${title}</title><style>
@page{size:A4;margin:22mm 20mm 22mm 20mm}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Times New Roman',serif;color:#000;background:#fff;font-size:11pt;line-height:1.65}
.hdr{padding-bottom:12pt;margin-bottom:16pt;display:flex;align-items:flex-start;justify-content:space-between}
.co-name{font-size:22pt;font-weight:bold;letter-spacing:1px;line-height:1.1}
.co-sub{font-size:8pt;letter-spacing:2.5px;text-transform:uppercase;color:#333;margin-top:3pt}
.co-addr{font-size:8pt;color:#555;margin-top:4pt}
.doc-title{text-align:center;margin:16pt 0 14pt}
.doc-title h1{font-size:14pt;text-transform:uppercase;letter-spacing:2.5px;padding-bottom:5pt;display:inline-block}
.meta{display:flex;justify-content:space-between;font-size:8.5pt;color:#444;margin-bottom:16pt;padding:7pt 0;background:#fff}
.btext{white-space:pre-wrap;font-size:10.5pt;line-height:1.8;text-align:justify;margin-bottom:26pt}
.sig-hd{font-size:9.5pt;text-transform:uppercase;letter-spacing:1.8px;padding-bottom:4pt;margin-bottom:14pt}
.sig-grid{display:grid;grid-template-columns:1fr 1fr;gap:28pt;margin-bottom:28pt}
.sig-box{padding-top:9pt}
.sig-lbl{font-size:7.5pt;text-transform:uppercase;letter-spacing:1px;color:#555;margin-bottom:4pt}
.sig-name{font-size:14pt;font-style:italic;font-family:'Brush Script MT','Segoe Script',cursive;margin-bottom:3pt;min-height:22pt}
.sig-date{font-size:8.5pt;color:#333}
.status-badge{display:inline-block;border:0.8pt solid #888;padding:5pt 14pt;font-size:8.5pt;letter-spacing:1px;margin-top:10pt}
.stamp-area{margin-top:36pt}
.ftr{margin-top:28pt;padding-top:7pt;display:flex;justify-content:space-between;font-size:7.5pt;color:#888}
@media print{.no-print{display:none}}
</style></head><body>
<div class="hdr">
  <div>
    <div class="co-name">BrightWave</div>
    <div class="co-sub">Habitat Enterprise</div>
    <div class="co-addr">Kwara State, Nigeria &nbsp;&middot;&nbsp; brightwavehabitat@gmail.com</div>
  </div>
  <div style="text-align:right;font-size:8pt;color:#555;line-height:1.9">
    <div><strong>Ref:</strong> ${refNum}</div>
    <div><strong>Issued:</strong> ${printDate}</div>
  </div>
</div>
<div class="doc-title"><h1>${title}</h1></div>
<div class="meta">
  <span><strong>Document Reference:</strong> ${refNum}</span>
  <span><strong>Date Issued:</strong> ${printDate}</span>
  <span><strong>Jurisdiction:</strong> Kwara State, Nigeria</span>
</div>
<div class="btext">${safeBody}</div>
<div class="sig-hd">Signatures &amp; Execution</div>
<div class="sig-grid">
  <div class="sig-box">
    <div class="sig-lbl">Employee / Investor Signature</div>
    <div class="sig-name">${userSig || '&nbsp;'}</div>
    <div class="sig-date">${userDate || 'Date: ____________________________'}</div>
  </div>
  <div class="sig-box">
    <div class="sig-lbl">Chief Executive Officer &middot; BrightWave Habitat Enterprise</div>
    <div class="sig-name">${ceoSig || '&nbsp;'}</div>
    <div class="sig-date">${ceoDate || 'Date: ____________________________'}</div>
  </div>
</div>
<div class="stamp-area"></div>
<div style="text-align:center;margin-top:10pt"><span class="status-badge">${status || 'EXECUTED AGREEMENT'}</span></div>
<div class="ftr">
  <span>BrightWave Habitat Enterprise &nbsp;&middot;&nbsp; Kwara State, Nigeria</span>
  <span>Ref: ${refNum} &nbsp;&middot;&nbsp; Generated ${printDate}</span>
</div>
<script>window.onload=function(){window.print();}<\/script>
</body></html>`;
            const w = window.open('','_blank','width=820,height=1000');
            if (w) { w.document.write(html); w.document.close(); }
        }

        async function deleteConstructionUpdate(id, source) {
            if (!confirm('Delete this construction update? This cannot be undone.')) return;
            try {
                await fetchData('/admin/api/construction-updates/' + id, { method: 'DELETE' });
                const sel = document.getElementById('ceoConstructionProperty');
                await loadConstructionUpdates(sel?.value || '');
            } catch (e) {
                alert('Error deleting update. Please try again.');
            }
        }

        function editConstructionUpdate(id, title, pct, date, notes, propertyId, source) {
            const editIdEl = document.getElementById('ceoConstructionEditId');
            const titleEl = document.getElementById('ceoConstructionTitle');
            const pctEl = document.getElementById('ceoConstructionPercent');
            const dateEl = document.getElementById('ceoConstructionDate');
            const notesEl = document.getElementById('ceoConstructionNotes');
            const labelEl = document.getElementById('ceoConstrFormLabel');
            const submitBtn = document.getElementById('ceoConstrSubmitBtn');
            const cancelBtn = document.getElementById('ceoConstrCancelBtn');
            const propertySel = document.getElementById('ceoConstructionProperty');
            if (!editIdEl) return;
            editIdEl.value = id;
            if (titleEl) titleEl.value = title;
            if (pctEl) pctEl.value = pct;
            if (dateEl) dateEl.value = date;
            if (notesEl) notesEl.value = notes;
            if (propertySel) propertySel.value = String(propertyId);
            if (labelEl) labelEl.textContent = 'Edit Update';
            if (submitBtn) submitBtn.textContent = 'Save Changes';
            if (cancelBtn) cancelBtn.classList.remove('hidden');
            titleEl?.focus();
        }

        function cancelConstructionEdit(source) {
            const editIdEl = document.getElementById('ceoConstructionEditId');
            const titleEl = document.getElementById('ceoConstructionTitle');
            const pctEl = document.getElementById('ceoConstructionPercent');
            const dateEl = document.getElementById('ceoConstructionDate');
            const notesEl = document.getElementById('ceoConstructionNotes');
            const labelEl = document.getElementById('ceoConstrFormLabel');
            const submitBtn = document.getElementById('ceoConstrSubmitBtn');
            const cancelBtn = document.getElementById('ceoConstrCancelBtn');
            const msgEl = document.getElementById('ceoConstructionMsg');
            if (editIdEl) editIdEl.value = '';
            if (titleEl) titleEl.value = '';
            if (pctEl) pctEl.value = '';
            if (dateEl) dateEl.value = '';
            if (notesEl) notesEl.value = '';
            if (labelEl) labelEl.textContent = 'Post New Update';
            if (submitBtn) submitBtn.textContent = 'Post Update';
            if (cancelBtn) cancelBtn.classList.add('hidden');
            if (msgEl) msgEl.textContent = '';
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
                <div class="flex justify-center gap-3 pt-2">
                    <button onclick="downloadContract()" class="bg-slate-700 hover:bg-slate-600 text-white text-xs font-medium py-2 px-4 rounded-lg flex items-center gap-2"><i class="fas fa-download"></i> Download PDF</button>
                    <button onclick="closeContractModal()" class="bg-gray-700 hover:bg-gray-600 text-white text-xs font-medium py-2 px-4 rounded-lg">Close</button>
                </div>
            </div>
        </div>
    </div>

    <!-- ====== FIRST-LOGIN ONBOARDING TOUR (Shepherd.js) ====== -->
    <script>
    (function() {
        const HAS_SEEN_TOUR = {{ has_seen_tour | tojson }};
        if (HAS_SEEN_TOUR) return;

        function loadCSS(href) {
            return new Promise((resolve, reject) => {
                const l = document.createElement('link');
                l.rel = 'stylesheet'; l.href = href;
                l.onload = resolve; l.onerror = reject;
                document.head.appendChild(l);
            });
        }
        function loadJS(src) {
            return new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = src; s.onload = resolve; s.onerror = reject;
                document.head.appendChild(s);
            });
        }

        async function markSeen() {
            try {
                await fetch('/admin/api/me/mark-tour-seen', {
                    method: 'POST', credentials: 'include',
                    headers: { 'X-CSRF-Token': adminCsrfToken || '' },
                });
            } catch (e) { /* non-blocking */ }
        }

        async function startCEOTour() {
            await loadCSS('https://cdn.jsdelivr.net/npm/shepherd.js@11.2.0/dist/css/shepherd.css');
            await loadJS('https://cdn.jsdelivr.net/npm/shepherd.js@11.2.0/dist/js/shepherd.min.js');
            const tour = new Shepherd.Tour({
                useModalOverlay: true,
                defaultStepOptions: {
                    cancelIcon: { enabled: true },
                    classes: 'shadow-md bg-gray-800 text-white',
                    scrollTo: { behavior: 'smooth', block: 'center' },
                },
            });
            const nextBtn = { text: 'Next', action() { return tour.next(); } };
            const skipBtn = { text: 'Skip tour', classes: 'shepherd-button-secondary', action() { return tour.cancel(); } };
            const doneBtn = { text: 'Got it', action() { return tour.complete(); } };

            tour.addStep({
                id: 'welcome', title: 'Welcome back, Wally',
                text: 'Quick 4-step tour of the new bits. You can skip any time.',
                buttons: [skipBtn, nextBtn],
            });
            tour.addStep({
                id: 'approvals', title: 'Approvals',
                text: 'New signups land here. Approve to activate an account; reject with a note. The red badge shows how many are waiting.',
                attachTo: { element: 'button[onclick*="approvalsSection"]', on: 'right' },
                buttons: [skipBtn, nextBtn],
            });
            tour.addStep({
                id: 'payroll', title: 'Payroll',
                text: 'Tracks 10% commission per unit serviced by a Manager/Realtor + flat salary for Accountant/PA. CEO and Investors are excluded automatically.',
                attachTo: { element: 'button[onclick*="payrollSection"]', on: 'right' },
                buttons: [skipBtn, nextBtn],
            });
            tour.addStep({
                id: 'tenants', title: 'Tenants → Serviced By',
                text: 'When you add a tenant, set "Serviced By" to the Manager/Realtor who closed it. That feeds Payroll automatically.',
                attachTo: { element: 'button[onclick*="tenantsSection"]', on: 'right' },
                buttons: [skipBtn, doneBtn],
            });
            tour.on('complete', markSeen);
            tour.on('cancel', markSeen);
            setTimeout(() => tour.start(), 600);
        }

        if (USER_ROLE === 'CEO') {
            document.addEventListener('DOMContentLoaded', startCEOTour);
        }
    })();
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
    <link rel="shortcut icon" href="/favicon.ico">
    <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#475569">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="BrightWave">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        .contract-scroll::-webkit-scrollbar { width: 6px; }
        .contract-scroll::-webkit-scrollbar-track { background: #374151; }
        .contract-scroll::-webkit-scrollbar-thumb { background: #6B7280; border-radius: 3px; }
        .timeline-bar { transition: width 0.8s ease; }
        *, *::before, *::after { box-sizing: border-box; }
        body { overflow-x: hidden; }
        /* tab bar scrolls on mobile */
        #mgrTabBar { scrollbar-width: none; }
        #mgrTabBar::-webkit-scrollbar { display: none; }
        /* compact mobile padding */
        @media (max-width: 640px) {
            main { padding-left: 0.75rem !important; padding-right: 0.75rem !important; }
            table { font-size: 0.72rem; }
            td, th { padding-top: 0.3rem !important; padding-bottom: 0.3rem !important; }
            .mobile-card-stack { flex-direction: column !important; }
        }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen overflow-x-hidden">

    <!-- Loading splash -->
    <div id="bwLoader" style="position:fixed;inset:0;z-index:9999;background:#0f172a;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:24px;transition:opacity 0.45s ease;pointer-events:all">
        <div style="display:flex;flex-direction:column;align-items:center;gap:14px">
            <div style="position:relative;width:80px;height:80px">
                <img src="/assets/images/brightwave-logo.png" alt="BrightWave" id="bwLoaderImg"
                     style="width:80px;height:80px;border-radius:50%;object-fit:cover;box-shadow:0 0 0 3px rgba(20,184,166,0.35),0 16px 40px rgba(0,0,0,0.6)"
                     onerror="this.style.display='none';document.getElementById('bwLoaderIcon').style.display='flex'">
                <div id="bwLoaderIcon" style="display:none;width:80px;height:80px;border-radius:20px;background:linear-gradient(135deg,#0d9488,#0e7490);align-items:center;justify-content:center;box-shadow:0 16px 40px rgba(0,0,0,0.5)">
                    <svg style="width:40px;height:40px" fill="none" stroke="white" stroke-width="1.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 21h19.5m-18-18v18m10.5-18v18m6-13.5V21M6.75 6.75h.75m-.75 3h.75m-.75 3h.75m3-6h.75m-.75 3h.75m-.75 3h.75M6.75 21v-3.375c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21M3 3h12m-.75 4.5H21m-3.75 3.75h.008v.008h-.008v-.008Zm0 3h.008v.008h-.008v-.008Zm0 3h.008v.008h-.008v-.008Z"/></svg>
                </div>
            </div>
            <div style="text-align:center">
                <p style="color:#f1f5f9;font-size:20px;font-weight:700;letter-spacing:0.04em;margin:0;font-family:system-ui,sans-serif">BrightWave</p>
                <p style="color:#14b8a6;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;margin:3px 0 0;font-family:system-ui,sans-serif">Habitat Enterprise</p>
            </div>
        </div>
        <div style="width:36px;height:36px;border:2.5px solid rgba(20,184,166,0.15);border-top-color:#14b8a6;border-radius:50%;animation:bwSpin 0.75s linear infinite"></div>
        <style>@keyframes bwSpin{to{transform:rotate(360deg)}}</style>
    </div>
    <script>
        const _bwLoaderStart = Date.now();
        let _bwLoaderDone = false;
        function dismissLoader() {
            if (_bwLoaderDone) return;
            _bwLoaderDone = true;
            const delay = Math.max(0, 700 - (Date.now() - _bwLoaderStart));
            setTimeout(() => {
                const el = document.getElementById('bwLoader');
                if (!el) return;
                el.style.opacity = '0';
                el.style.pointerEvents = 'none';
                setTimeout(() => el && el.remove(), 460);
            }, delay);
        }
    </script>

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
        <div class="max-w-7xl mx-auto py-3 px-3 sm:px-6 lg:px-8">
            <!-- Top row: brand + logout -->
            <div class="flex items-center justify-between gap-2 mb-2">
                <div class="min-w-0">
                    <p class="text-xs text-gray-500 uppercase tracking-widest hidden sm:block">BrightWave Habitat Enterprise</p>
                    <h1 id="portalTitle" class="text-base sm:text-xl font-bold text-slate-300 truncate">
                        {% if user_role == 'MANAGER' %}Property Manager Portal
                        {% elif user_role == 'ACCOUNTANT' %}Finance Portal
                        {% elif user_role == 'REALTOR' %}Realtor Portal
                        {% elif user_role == 'INVESTOR' %}Investor Portal
                        {% else %}{{ user_role }} Portal{% endif %}
                    </h1>
                </div>
                <div class="flex items-center gap-2 flex-shrink-0">
                    <div class="text-right hidden sm:block">
                        <p class="text-sm font-medium text-white">{{ user_name }}</p>
                        <p id="activeRoleLabel" class="text-xs text-gray-400">{{ user_role }}</p>
                    </div>
                    {% if awaiting_ceo_signature %}
                    <span class="bg-yellow-600 text-white text-xs px-2 py-1 rounded-full hidden sm:inline">Awaiting CEO</span>
                    {% endif %}
                    <a href="/admin/logout" class="text-gray-400 hover:text-white text-xs sm:text-sm border border-gray-600 rounded-lg px-2 sm:px-3 py-1.5 flex-shrink-0">Logout</a>
                </div>
            </div>
            {% if all_roles | length > 1 %}
            <!-- Role switcher row — scrollable on mobile -->
            <div class="overflow-x-auto -mx-1">
                <div id="roleSwitcher" class="flex gap-1 bg-gray-700/60 p-1 rounded-lg min-w-max mx-1">
                    {% for r in all_roles %}
                    <button onclick="switchRole('{{ r }}')" id="roleBtn_{{ r }}"
                        class="role-switch-btn flex-none text-xs font-medium px-3 py-1.5 rounded-md transition-colors {% if r == user_role %}bg-slate-600 text-white{% else %}text-gray-400 hover:text-white{% endif %}">
                        {{ r }}
                    </button>
                    {% endfor %}
                </div>
            </div>
            {% endif %}
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

    <main class="max-w-7xl mx-auto py-4 sm:py-6 px-3 sm:px-6 lg:px-8">

        {% for r in all_roles %}
        <div id="roleSection_{{ r }}" class="{% if r != user_role %}hidden{% endif %}">

            {% if r == 'INVESTOR' %}
            <!-- INVESTOR DASHBOARD — Premium -->
            <!-- Loading state -->
            <div id="investorLoading" class="flex flex-col items-center justify-center py-20 gap-4">
                <div class="w-12 h-12 border-4 border-slate-600 border-t-emerald-500 rounded-full animate-spin"></div>
                <p class="text-slate-400 text-sm">Loading your investment data…</p>
            </div>

            <!-- Main dashboard -->
            <div id="investorDashboard" class="hidden space-y-4 sm:space-y-6 pb-20 md:pb-0">

                <!-- Pending property assignment banner (visible only when CEO hasn't assigned a property yet) -->
                <div id="invPendingAssignment" class="hidden bg-amber-900/30 border border-amber-700/40 rounded-2xl p-4 flex items-start gap-3">
                    <div class="w-9 h-9 bg-amber-900/60 border border-amber-700/40 rounded-lg flex items-center justify-center flex-shrink-0">
                        <i class="fas fa-hourglass-half text-amber-400 text-sm"></i>
                    </div>
                    <div class="min-w-0">
                        <p class="font-semibold text-amber-300 text-sm">Property assignment pending</p>
                        <p class="text-xs text-amber-100/70 mt-1">The CEO will set up your project shortly after your onboarding chat. The figures below are based on the current flagship project until then.</p>
                    </div>
                </div>

                <!-- Project selector (shown only when investor has multiple projects) -->
                <div id="invProjectSelector" class="hidden bg-gray-800 border border-gray-700/60 rounded-2xl p-4">
                    <p class="text-xs text-gray-400 uppercase tracking-wide mb-3 font-medium">Your Investments</p>
                    <div id="invProjectTabs" class="flex flex-wrap gap-2"></div>
                </div>

                <!-- TAB NAV (top, visible md+) -->
                <div class="hidden md:flex items-center gap-1 bg-gray-800/60 border border-gray-700/40 rounded-xl p-1">
                    <button data-inv-tab="overview" onclick="switchInvestorScreen('overview')" class="inv-tab-btn flex-1 text-sm font-medium py-2 px-3 rounded-lg transition-colors text-white bg-slate-700"><i class="fas fa-home mr-2"></i>Overview</button>
                    <button data-inv-tab="returns" onclick="switchInvestorScreen('returns')" class="inv-tab-btn flex-1 text-sm font-medium py-2 px-3 rounded-lg transition-colors text-gray-400 hover:text-white"><i class="fas fa-chart-line mr-2"></i>Returns</button>
                    <button data-inv-tab="project" onclick="switchInvestorScreen('project')" class="inv-tab-btn flex-1 text-sm font-medium py-2 px-3 rounded-lg transition-colors text-gray-400 hover:text-white"><i class="fas fa-hard-hat mr-2"></i>Project</button>
                    <button data-inv-tab="profile" onclick="switchInvestorScreen('profile')" class="inv-tab-btn flex-1 text-sm font-medium py-2 px-3 rounded-lg transition-colors text-gray-400 hover:text-white"><i class="fas fa-user mr-2"></i>Profile</button>
                </div>

                <!-- === SCREEN: OVERVIEW === -->
                <div class="inv-screen" data-inv-screen="overview">

                <!-- 1. HERO / WELCOME CARD -->
                <div class="relative overflow-hidden bg-gradient-to-br from-slate-900 via-slate-800 to-gray-900 border border-slate-600/60 rounded-2xl p-5 sm:p-8">
                    <div class="absolute -top-16 -right-16 w-56 h-56 bg-emerald-500/5 rounded-full pointer-events-none"></div>
                    <div class="absolute -bottom-12 -left-12 w-40 h-40 bg-blue-500/5 rounded-full pointer-events-none"></div>
                    <div class="relative">
                        <div class="flex items-center justify-between gap-3 mb-4 sm:mb-6">
                            <div class="flex items-center gap-3">
                                <img src="/assets/images/brightwave-logo.png" alt="BrightWave" class="w-10 h-10 sm:w-12 sm:h-12 rounded-full ring-2 ring-slate-500/40 object-cover flex-shrink-0">
                                <div>
                                    <p class="text-xs text-slate-400 uppercase tracking-widest leading-tight">BrightWave Habitat Enterprise</p>
                                    <p class="text-sm font-semibold text-slate-200 leading-tight">Investor Portal</p>
                                </div>
                            </div>
                            <span id="invTypeBadge" class="text-xs font-bold px-3 py-1.5 rounded-full flex-shrink-0"></span>
                        </div>
                        <h2 class="text-2xl sm:text-3xl font-bold text-white mb-1 break-words">Welcome back, <span id="invWelcomeName" class="text-emerald-400"></span></h2>
                        <p id="invHeroSubtitle" class="text-slate-400 text-sm mb-5 sm:mb-7"></p>
                        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4">
                            <div class="bg-white/5 border border-white/10 rounded-xl p-3 sm:p-4">
                                <p class="text-[11px] text-slate-400 uppercase tracking-wide mb-1.5">Invested</p>
                                <p id="invHeroAmount" class="text-base sm:text-xl font-bold text-white break-all leading-tight">—</p>
                            </div>
                            <div class="bg-emerald-500/10 border border-emerald-500/20 rounded-xl p-3 sm:p-4">
                                <p class="text-[11px] text-emerald-400 uppercase tracking-wide mb-1.5">Net ROI Earned</p>
                                <p id="invHeroReturn" class="text-base sm:text-xl font-bold text-emerald-300 break-all leading-tight">—</p>
                                <p id="invHeroReturnNote" class="text-[11px] text-emerald-500/70 mt-1 leading-snug hidden sm:block"></p>
                            </div>
                            <div class="bg-blue-500/10 border border-blue-500/20 rounded-xl p-3 sm:p-4">
                                <p class="text-[11px] text-blue-400 uppercase tracking-wide mb-1.5">Paid Out</p>
                                <p id="invHeroDistributed" class="text-base sm:text-xl font-bold text-blue-300 break-all leading-tight">—</p>
                            </div>
                            <div class="bg-amber-500/10 border border-amber-500/20 rounded-xl p-3 sm:p-4">
                                <p class="text-[11px] text-amber-400 uppercase tracking-wide mb-1.5">Site Progress</p>
                                <p id="invHeroProgress" class="text-base sm:text-xl font-bold text-amber-300 leading-tight">—</p>
                            </div>
                        </div>
                        <!-- Next Payout Banner -->
                        <div id="invNextPayoutBanner" class="hidden mt-4 flex items-center justify-between gap-3 px-4 py-3 bg-emerald-900/30 border border-emerald-700/30 rounded-xl">
                            <div class="flex items-center gap-3 min-w-0">
                                <div class="w-8 h-8 bg-emerald-900/60 border border-emerald-700/40 rounded-lg flex items-center justify-center flex-shrink-0">
                                    <i class="fas fa-calendar-check text-emerald-400 text-xs"></i>
                                </div>
                                <div class="min-w-0">
                                    <p class="text-[11px] text-emerald-400 uppercase tracking-wide">Next Scheduled Payout</p>
                                    <p id="invNextPayoutLabel" class="text-sm font-semibold text-white truncate"></p>
                                </div>
                            </div>
                            <div class="text-right flex-shrink-0">
                                <p id="invNextPayoutAmount" class="text-base font-bold text-emerald-300"></p>
                                <p id="invNextPayoutDate" class="text-xs text-gray-400"></p>
                            </div>
                        </div>
                    </div>
                </div>

                </div><!-- /SCREEN: OVERVIEW -->

                <!-- === SCREEN: PROJECT === -->
                <div class="inv-screen hidden" data-inv-screen="project">

                <!-- 2. CONSTRUCTION PROGRESS -->
                <div class="bg-gray-800 border border-gray-700/60 rounded-2xl p-5 sm:p-7">
                    <div class="flex items-center justify-between gap-3 mb-4 sm:mb-6">
                        <div>
                            <h3 class="font-bold text-white text-base sm:text-lg">Construction Progress</h3>
                            <p class="text-xs text-gray-500 mt-0.5">Live milestones posted by the project team</p>
                        </div>
                        <span id="invProgressHeadline" class="text-2xl sm:text-3xl font-bold text-emerald-400">0%</span>
                    </div>
                    <div class="relative h-3 sm:h-4 bg-gray-700 rounded-full overflow-hidden mb-2">
                        <div id="invMainProgressBar" class="h-full rounded-full bg-gradient-to-r from-emerald-600 to-teal-400 transition-all duration-1000" style="width:0%"></div>
                    </div>
                    <div class="flex justify-between text-xs text-gray-500 mb-5 sm:mb-7">
                        <span>0%</span><span>50%</span><span>100%</span>
                    </div>
                    <div id="invMilestones"></div>
                </div>

                </div><!-- /SCREEN: PROJECT -->

                <!-- === SCREEN: RETURNS === -->
                <div class="inv-screen hidden" data-inv-screen="returns">

                <!-- 3. RETURN SCHEDULE -->
                <div class="bg-gray-800 border border-gray-700/60 rounded-2xl p-5 sm:p-7">
                    <div class="flex items-start justify-between gap-3 mb-4 sm:mb-6">
                        <div>
                            <h3 class="font-bold text-white text-base sm:text-lg">Your Return Schedule</h3>
                            <p class="text-xs text-gray-500 mt-0.5">Projected annual distributions at maturity</p>
                        </div>
                        <span id="invRoiTag" class="text-xs font-semibold bg-emerald-900/60 text-emerald-300 border border-emerald-700/40 px-2.5 py-1.5 rounded-full flex-shrink-0"></span>
                    </div>
                    <div id="invReturnSchedule"></div>
                </div>
                </div><!-- /SCREEN: RETURNS -->

                <!-- === SCREEN: PROFILE === -->
                <div class="inv-screen hidden" data-inv-screen="profile">

                <!-- 4. DETAILS + DOCUMENTS -->
                <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 sm:gap-6">
                    <div class="bg-gray-800 border border-gray-700/60 rounded-2xl p-5 sm:p-7">
                        <h3 class="font-bold text-white text-base mb-4 sm:mb-5">Investment Details</h3>
                        <div id="invDetailsGrid"></div>
                    </div>
                    <div class="bg-gray-800 border border-gray-700/60 rounded-2xl p-5 sm:p-7">
                        <h3 class="font-bold text-white text-base mb-4 sm:mb-5">Your Documents</h3>
                        <div class="flex items-start gap-4 p-4 sm:p-5 bg-gray-700/50 border border-gray-600/50 rounded-xl mb-4">
                            <div class="w-10 h-10 bg-emerald-900/60 border border-emerald-700/40 rounded-lg flex items-center justify-center flex-shrink-0">
                                <i class="fas fa-file-contract text-emerald-400 text-sm"></i>
                            </div>
                            <div class="min-w-0 flex-1">
                                <p class="font-semibold text-white text-sm">Investment Agreement</p>
                                <p id="docStatus" class="text-xs text-gray-400 mt-1">Loading…</p>
                            </div>
                            <button id="viewAgreementBtn" onclick="viewMyContract()" class="hidden text-sm text-emerald-400 hover:text-emerald-300 border border-emerald-700/60 rounded-lg px-3 py-2.5 flex-shrink-0 transition-colors font-medium">View</button>
                        </div>
                        <p class="text-xs text-gray-500 leading-relaxed">Your signed agreement is legally binding and on record. The original is held securely by BrightWave Habitat Enterprise.</p>
                    </div>
                </div>

                <!-- 5. CONTACT CTA -->
                <div class="bg-gradient-to-r from-slate-800 to-gray-800 border border-slate-600/40 rounded-2xl p-5 sm:p-6 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
                    <div>
                        <p class="font-semibold text-white">Have a question about your investment?</p>
                        <p class="text-sm text-gray-400 mt-1">The CEO is directly available — reach out any time for updates, clarifications, or distribution requests.</p>
                    </div>
                    <div class="flex flex-col sm:flex-row gap-2 flex-shrink-0">
                        <a href="https://wa.me/2348037669462?text=Hi%2C%20I%20have%20a%20question%20about%20my%20BrightWave%20investment." target="_blank" rel="noopener noreferrer" class="flex items-center gap-2 bg-emerald-700 hover:bg-emerald-600 text-white text-sm font-medium py-3 px-5 rounded-xl transition-colors">
                            <i class="fab fa-whatsapp"></i> WhatsApp
                        </a>
                        <a href="mailto:brightwavehabitat@gmail.com" class="flex items-center gap-2 bg-slate-700 hover:bg-slate-600 text-white text-sm font-medium py-3 px-5 rounded-xl transition-colors">
                            <i class="fas fa-envelope text-xs"></i> Email CEO
                        </a>
                    </div>
                </div>

                <!-- 6. RESOURCES -->
                <div class="bg-gray-800 border border-gray-700/60 rounded-2xl p-5 sm:p-6">
                    <h3 class="font-bold text-white text-base mb-4">Investor Resources</h3>
                    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                        <a href="mailto:brightwavehabitat@gmail.com?subject=Investment%20Statement%20Request" class="flex items-center gap-3 p-3 bg-gray-700/50 border border-gray-600/50 rounded-xl hover:border-emerald-700/50 transition-colors group">
                            <div class="w-9 h-9 bg-emerald-900/50 border border-emerald-700/30 rounded-lg flex items-center justify-center flex-shrink-0">
                                <i class="fas fa-file-invoice text-emerald-400 text-sm"></i>
                            </div>
                            <div><p class="text-sm font-medium text-white group-hover:text-emerald-300 transition-colors">Request Statement</p><p class="text-xs text-gray-400">Formal investment summary</p></div>
                        </a>
                        <a href="/about" target="_blank" class="flex items-center gap-3 p-3 bg-gray-700/50 border border-gray-600/50 rounded-xl hover:border-blue-700/50 transition-colors group">
                            <div class="w-9 h-9 bg-blue-900/50 border border-blue-700/30 rounded-lg flex items-center justify-center flex-shrink-0">
                                <i class="fas fa-users text-blue-400 text-sm"></i>
                            </div>
                            <div><p class="text-sm font-medium text-white group-hover:text-blue-300 transition-colors">Meet the Team</p><p class="text-xs text-gray-400">Who manages your investment</p></div>
                        </a>
                        <a href="https://www.brightwavehabitat.com/" target="_blank" rel="noopener" class="flex items-center gap-3 p-3 bg-gray-700/50 border border-gray-600/50 rounded-xl hover:border-amber-700/50 transition-colors group">
                            <div class="w-9 h-9 bg-amber-900/50 border border-amber-700/30 rounded-lg flex items-center justify-center flex-shrink-0">
                                <i class="fas fa-globe text-amber-400 text-sm"></i>
                            </div>
                            <div><p class="text-sm font-medium text-white group-hover:text-amber-300 transition-colors">Public Website</p><p class="text-xs text-gray-400">brightwavehabitat.com</p></div>
                        </a>
                    </div>
                </div>

                </div><!-- /SCREEN: PROFILE -->

                <!-- BOTTOM NAV (mobile only) -->
                <nav class="md:hidden fixed bottom-0 left-0 right-0 bg-gray-900/95 backdrop-blur border-t border-gray-700/60 z-40 flex items-stretch">
                    <button data-inv-tab="overview" onclick="switchInvestorScreen('overview')" class="inv-tab-btn-mobile flex-1 flex flex-col items-center gap-0.5 py-2.5 text-emerald-400">
                        <i class="fas fa-home text-base"></i><span class="text-[10px] font-medium">Overview</span>
                    </button>
                    <button data-inv-tab="returns" onclick="switchInvestorScreen('returns')" class="inv-tab-btn-mobile flex-1 flex flex-col items-center gap-0.5 py-2.5 text-gray-500">
                        <i class="fas fa-chart-line text-base"></i><span class="text-[10px] font-medium">Returns</span>
                    </button>
                    <button data-inv-tab="project" onclick="switchInvestorScreen('project')" class="inv-tab-btn-mobile flex-1 flex flex-col items-center gap-0.5 py-2.5 text-gray-500">
                        <i class="fas fa-hard-hat text-base"></i><span class="text-[10px] font-medium">Project</span>
                    </button>
                    <button data-inv-tab="profile" onclick="switchInvestorScreen('profile')" class="inv-tab-btn-mobile flex-1 flex flex-col items-center gap-0.5 py-2.5 text-gray-500">
                        <i class="fas fa-user text-base"></i><span class="text-[10px] font-medium">Profile</span>
                    </button>
                </nav>

            </div>

            <!-- No profile state -->
            <div id="investorNoProfile" class="hidden">
                <div class="bg-gradient-to-br from-slate-900 via-slate-800 to-gray-900 border border-slate-600/50 rounded-2xl p-8 sm:p-14 text-center max-w-lg mx-auto relative overflow-hidden">
                    <div class="absolute -top-12 -right-12 w-48 h-48 bg-emerald-500/5 rounded-full pointer-events-none"></div>
                    <div class="absolute -bottom-10 -left-10 w-36 h-36 bg-blue-500/5 rounded-full pointer-events-none"></div>
                    <div class="relative">
                        <div class="w-20 h-20 bg-gradient-to-br from-emerald-900/60 to-slate-800 border border-emerald-700/30 rounded-2xl flex items-center justify-center mx-auto mb-6 shadow-lg">
                            <i class="fas fa-chart-line text-emerald-400 text-3xl"></i>
                        </div>
                        <h3 class="text-xl font-bold text-white mb-3">Your portfolio is being set up</h3>
                        <p class="text-gray-400 text-sm leading-relaxed mb-2">Your investment details are being configured by the BrightWave team. Once linked, you'll see your full return schedule, construction progress, and distribution timeline here.</p>
                        <p class="text-gray-500 text-xs mb-8">This usually takes less than 24 hours after your agreement is signed.</p>
                        <a href="mailto:brightwavehabitat@gmail.com" class="inline-flex items-center gap-2 bg-emerald-700 hover:bg-emerald-600 text-white text-sm font-medium px-5 py-2.5 rounded-xl transition-colors">
                            <i class="fas fa-envelope text-xs"></i> Contact BrightWave
                        </a>
                        <div class="flex items-center justify-center gap-2 text-xs text-gray-600 mt-8">
                            <img src="/assets/images/brightwave-logo.png" alt="" class="w-5 h-5 rounded-full opacity-50 object-cover">
                            BrightWave Habitat Enterprise · Kwara State, Nigeria
                        </div>
                    </div>
                </div>
            </div>

            {% elif r == 'MANAGER' %}
            <!-- MANAGER DASHBOARD -->
            <!-- Tab navigation -->
            <div class="overflow-x-auto -mx-3 sm:-mx-1 mb-5 border-b border-gray-700">
            <div class="flex gap-0.5 min-w-max px-3 sm:px-1" id="mgrTabBar">
                <button class="mgr-tab-btn px-3 sm:px-4 py-2.5 rounded-t-lg text-xs sm:text-sm font-medium transition-colors bg-slate-700 text-white border-b-2 border-slate-400 whitespace-nowrap" data-tab="mgrTabOverview" onclick="showMgrTab('mgrTabOverview')">Overview</button>
                <button class="mgr-tab-btn px-3 sm:px-4 py-2.5 rounded-t-lg text-xs sm:text-sm font-medium transition-colors text-gray-400 hover:text-white border-b-2 border-transparent whitespace-nowrap" data-tab="mgrTabUnits" onclick="showMgrTab('mgrTabUnits')">Units &amp; Tenants</button>
                <button class="mgr-tab-btn px-3 sm:px-4 py-2.5 rounded-t-lg text-xs sm:text-sm font-medium transition-colors text-gray-400 hover:text-white border-b-2 border-transparent whitespace-nowrap" data-tab="mgrTabInquiries" onclick="showMgrTab('mgrTabInquiries')">Inquiries <span id="mgrInquiriesBadge" class="hidden ml-1 bg-blue-600 text-white text-xs px-1.5 py-0.5 rounded-full leading-none align-middle"></span></button>
                <button class="mgr-tab-btn px-3 sm:px-4 py-2.5 rounded-t-lg text-xs sm:text-sm font-medium transition-colors text-gray-400 hover:text-white border-b-2 border-transparent whitespace-nowrap" data-tab="mgrTabConstruction" onclick="showMgrTab('mgrTabConstruction')">Construction</button>
                <button class="mgr-tab-btn px-3 sm:px-4 py-2.5 rounded-t-lg text-xs sm:text-sm font-medium transition-colors text-gray-400 hover:text-white border-b-2 border-transparent whitespace-nowrap" data-tab="mgrTabCapital" onclick="showMgrTab('mgrTabCapital')">Capital Calc</button>
                <button class="mgr-tab-btn px-3 sm:px-4 py-2.5 rounded-t-lg text-xs sm:text-sm font-medium transition-colors text-gray-400 hover:text-white border-b-2 border-transparent whitespace-nowrap" data-tab="mgrTabMaintenance" onclick="showMgrTab('mgrTabMaintenance')">Maintenance</button>
            </div>
            </div>

            <!-- OVERVIEW TAB -->
            <div id="mgrTabOverview">
                <div class="grid grid-cols-2 xl:grid-cols-4 gap-3 sm:gap-4 mb-4">
                    <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                        <div class="w-8 h-8 bg-slate-700 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-building text-slate-300 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Properties</p><p id="mgr_properties" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                    </div>
                    <div class="bg-emerald-900/70 border border-emerald-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                        <div class="w-8 h-8 bg-emerald-700/70 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-door-open text-emerald-300 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[11px] text-emerald-300 uppercase tracking-wide mb-0.5 truncate">Available Units</p><p id="mgr_available_units" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                    </div>
                    <div class="bg-blue-900/70 border border-blue-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                        <div class="w-8 h-8 bg-blue-700/70 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-inbox text-blue-300 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[11px] text-blue-300 uppercase tracking-wide mb-0.5 truncate">Open Inquiries</p><p id="mgr_inquiries" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                    </div>
                    <div class="bg-purple-900/70 border border-purple-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                        <div class="w-8 h-8 bg-purple-700/70 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-users text-purple-300 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[11px] text-purple-300 uppercase tracking-wide mb-0.5 truncate">Active Tenants</p><p id="mgr_active_tenants" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                    </div>
                </div>
                <!-- Financial snapshot row -->
                <div class="grid grid-cols-2 xl:grid-cols-4 gap-3 sm:gap-4 mb-4">
                    <div class="bg-teal-900/60 border border-teal-700/30 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                        <div class="w-8 h-8 bg-teal-700/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-naira-sign text-teal-300 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[11px] text-teal-300 uppercase tracking-wide mb-0.5 truncate">Revenue</p><p id="mgr_revenue" class="text-lg sm:text-xl font-bold text-white truncate">-</p><p class="text-[10px] text-teal-400 mt-0.5">All time collected</p></div>
                    </div>
                    <div class="bg-amber-900/60 border border-amber-700/30 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                        <div class="w-8 h-8 bg-amber-700/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-hammer text-amber-300 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[11px] text-amber-300 uppercase tracking-wide mb-0.5 truncate">Capital Spent</p><p id="mgr_capital" class="text-lg sm:text-xl font-bold text-white truncate">-</p><p class="text-[10px] text-amber-400 mt-0.5">Approved expenses</p></div>
                    </div>
                    <div class="bg-gray-800 border border-gray-700/50 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                        <div class="w-8 h-8 bg-gray-700 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-scale-balanced text-gray-300 text-xs"></i></div>
                        <div class="min-w-0"><p id="mgr_net_lbl" class="text-[11px] uppercase tracking-wide mb-0.5 truncate text-gray-400">Net P&amp;L</p><p id="mgr_net" class="text-lg sm:text-xl font-bold truncate">-</p></div>
                    </div>
                    <div class="bg-gray-800 border border-gray-700/50 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                        <div class="w-8 h-8 bg-gray-700 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-chart-pie text-gray-300 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Occupancy</p><p id="mgr_occupancy" class="text-lg sm:text-xl font-bold text-white truncate">-</p><p class="text-[10px] text-gray-500 mt-0.5" id="mgr_occ_sub">of all units</p></div>
                    </div>
                </div>
                <!-- Charts row -->
                <div class="grid grid-cols-1 lg:grid-cols-5 gap-4 mb-6">
                    <div class="lg:col-span-3 bg-gray-800/80 border border-gray-700/50 rounded-xl p-4">
                        <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
                            <h3 class="text-xs font-semibold text-slate-400 uppercase tracking-wide flex items-center gap-2"><i class="fas fa-chart-bar text-teal-400"></i> Revenue vs Spend</h3>
                            <div class="flex items-center gap-0.5 bg-slate-900/70 rounded-lg p-0.5">
                                <button onclick="mgrSetPeriod('3M')" id="mgrPeriod_3M" class="text-xs px-2 py-0.5 rounded-md font-medium transition-colors text-slate-400 hover:text-white">3M</button>
                                <button onclick="mgrSetPeriod('6M')" id="mgrPeriod_6M" class="text-xs px-2 py-0.5 rounded-md font-medium transition-colors bg-slate-600 text-white">6M</button>
                                <button onclick="mgrSetPeriod('12M')" id="mgrPeriod_12M" class="text-xs px-2 py-0.5 rounded-md font-medium transition-colors text-slate-400 hover:text-white">12M</button>
                                <button onclick="mgrSetPeriod('all')" id="mgrPeriod_all" class="text-xs px-2 py-0.5 rounded-md font-medium transition-colors text-slate-400 hover:text-white">All</button>
                            </div>
                        </div>
                        <div class="relative" style="height:180px"><canvas id="mgrRevenueChart"></canvas></div>
                    </div>
                    <div class="lg:col-span-2 bg-gray-800/80 border border-gray-700/50 rounded-xl p-4">
                        <h3 class="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-3 flex items-center gap-2"><i class="fas fa-circle-dot text-purple-400"></i> Tenant Status</h3>
                        <div class="relative flex items-center justify-center" style="height:180px"><canvas id="mgrTenantDonut"></canvas></div>
                    </div>
                </div>
                <div class="bg-gray-800 rounded-xl p-4 sm:p-6 mb-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Properties Overview</h3>
                    <div class="overflow-x-auto"><table class="w-full text-sm min-w-[420px]"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Location</th><th class="py-2 text-left text-gray-400">Status</th></tr></thead><tbody id="mgr_propertiesTable"></tbody></table></div>
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
                            <h3 class="font-semibold text-lg text-slate-300">All Units <span id="mgrUnitsCount" class="text-sm font-normal text-emerald-400 ml-1"></span></h3>
                            <span class="text-xs text-gray-500">Dropdown = status · Edit = rent & notes</span>
                        </div>
                        <div class="overflow-x-auto"><table class="w-full text-sm min-w-[540px]"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400 pr-2">Property</th><th class="py-2 text-left text-gray-400">Unit</th><th class="py-2 text-left text-gray-400">Status</th><th class="py-2 text-left text-gray-400">Yearly Rent</th><th class="py-2 text-left text-gray-400">Notes</th><th class="py-2 text-left text-gray-400">Action</th></tr></thead><tbody id="mgr_unitsTable"></tbody></table></div>
                    </div>
                    <div class="bg-gray-800 rounded-xl p-6">
                        <h3 class="font-semibold text-lg mb-4 text-slate-300">Add or Update Tenant</h3>
                        <form id="managerTenantForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <input id="mgrTenantEditId" type="hidden">
                            <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Full Name *</label><input id="mgrTenantName" type="text" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Email</label><input id="mgrTenantEmail" type="email" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Phone</label><input id="mgrTenantPhone" type="text" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Property</label><select id="mgrTenantProperty" onchange="populateManagerUnitSelect(window._mgrAllUnits||[], this.value); mgrTnUtRefreshForProperty();" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"><option value="">Select property</option></select></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit Type <span class="text-gray-500">(optional)</span></label><select id="mgrTenantUnitType" onchange="mgrTnOnUnitTypeChange()" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"><option value="">-- Select unit type --</option></select></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Unit</label><select id="mgrTenantUnit" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></select></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Yearly Rent</label><input id="mgrTenantRent" type="number" step="1000" placeholder="Auto-fills from unit type" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
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
                        <span id="mgr_tenantRentTotal" class="text-xs text-emerald-400 font-medium"></span>
                    </div>
                    <div id="mgr_tenantsList" class="space-y-3"></div>
                </div>
            </div>

            <!-- INQUIRIES TAB -->
            <div id="mgrTabInquiries" class="hidden">
                <div id="mgr_inqPipeline" class="flex flex-wrap gap-2 mb-4"></div>
                <!-- Quick Add Inquiry -->
                <div class="bg-gray-800 rounded-xl p-4 mb-4">
                    <div class="flex items-center justify-between gap-3 mb-3">
                        <h4 class="font-semibold text-slate-300">Log New Inquiry</h4>
                        <button type="button" id="mgrInqFormToggle" onclick="this.classList.toggle('hidden');document.getElementById('mgrInqFormBody').classList.toggle('hidden')" class="text-xs text-blue-400 border border-blue-700/50 px-3 py-1.5 rounded-lg hover:bg-blue-900/30">+ Add</button>
                    </div>
                    <div id="mgrInqFormBody" class="hidden">
                        <form id="mgrInqForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Full Name *</label><input id="mgrInqName" type="text" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Phone *</label><input id="mgrInqPhone" type="text" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Email</label><input id="mgrInqEmail" type="email" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Property</label><select id="mgrInqProperty" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"><option value="">General</option></select></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Type</label><select id="mgrInqType" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"><option value="general">General</option><option value="rent">Rent</option><option value="purchase">Purchase</option></select></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Status</label><select id="mgrInqStatus" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"><option value="new">New</option><option value="contacted">Contacted</option><option value="viewing_scheduled">Viewing Scheduled</option><option value="offer_made">Offer Made</option><option value="closed">Closed</option></select></div>
                            <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label><textarea id="mgrInqNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white resize-none"></textarea></div>
                            <div class="sm:col-span-2 flex items-center gap-3"><button type="submit" class="bg-blue-700 hover:bg-blue-600 text-white font-medium py-2 px-5 rounded-lg text-sm">Save Inquiry</button><span id="mgrInqMsg" class="text-sm"></span></div>
                        </form>
                    </div>
                </div>
                <div class="bg-gray-800 rounded-xl p-6">
                    <div class="flex items-center justify-between gap-3 mb-4">
                        <h3 class="font-semibold text-lg text-slate-300">All Inquiries</h3>
                        <span class="text-xs text-gray-500">Click a row to expand contact details</span>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-sm min-w-[480px]">
                            <thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Name</th><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400 min-w-[140px]">Status</th><th class="py-2 text-left text-gray-400">Date</th></tr></thead>
                            <tbody id="mgr_inquiriesTable"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- CONSTRUCTION TAB -->
            <div id="mgrTabConstruction" class="hidden">
                <div class="bg-gray-800 rounded-xl p-5 mb-4">
                    <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
                        <div>
                            <p id="mgrConstrFormLabel" class="font-semibold text-slate-300 text-sm">Post New Update</p>
                            <p class="text-xs text-gray-500 mt-0.5">Add or edit a milestone on the selected project</p>
                        </div>
                        <div class="flex items-center gap-2 flex-wrap">
                            <label class="text-xs text-gray-400">Project:</label>
                            <select id="mgrConstructionProperty" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm w-full sm:w-auto sm:min-w-[180px]"></select>
                        </div>
                    </div>
                    <form id="managerConstructionForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <input type="hidden" id="mgrConstructionEditId">
                        <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Milestone Title *</label><input id="mgrConstructionTitle" type="text" placeholder="e.g. Foundation complete, Finishing stage" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Progress % (0–100)</label><input id="mgrConstructionPercent" type="number" min="0" max="100" placeholder="e.g. 85" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Date</label><input id="mgrConstructionDate" type="date" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label><textarea id="mgrConstructionNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea></div>
                        <div class="sm:col-span-2 flex items-center gap-3 flex-wrap">
                            <button type="submit" id="mgrConstrSubmitBtn" class="bg-emerald-700 hover:bg-emerald-600 text-white font-medium py-2 px-5 rounded-lg text-sm">Post Update</button>
                            <button type="button" id="mgrConstrCancelBtn" class="hidden bg-gray-600 hover:bg-gray-500 text-white font-medium py-2 px-4 rounded-lg text-sm" onclick="cancelConstructionEdit('mgr')">Cancel Edit</button>
                            <span id="mgrConstructionMsg" class="text-sm"></span>
                        </div>
                    </form>
                </div>
                <div class="bg-gray-800 rounded-xl p-5">
                    <div class="flex items-center justify-between gap-3 mb-4">
                        <h3 class="font-semibold text-slate-300">Project Timeline</h3>
                        <span id="mgrConstructionProgress" class="text-sm text-emerald-400 font-medium">0%</span>
                    </div>
                    <div id="mgrConstructionList" class="space-y-3"></div>
                </div>
            </div>

            <!-- CAPITAL CALC TAB -->
            <div id="mgrTabCapital" class="hidden">
                <p class="text-sm text-gray-400 mb-4">Record all project spend below — entries go to the CEO and accountant for approval.</p>
                <div class="bg-gray-800 rounded-xl p-4 mb-4 flex flex-col sm:flex-row sm:items-center gap-3">
                    <label class="text-sm text-gray-400 flex-shrink-0">Project:</label>
                    <select id="mgrCapitalProperty" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm w-full sm:w-auto sm:min-w-[220px]"></select>
                </div>
                <div class="grid grid-cols-1 xl:grid-cols-[0.95fr_1.05fr] gap-4">
                    <div class="bg-gray-800 rounded-xl p-5">
                        <div class="flex items-center justify-between gap-3 mb-4">
                            <div>
                                <h3 class="font-semibold text-slate-300">Record Expense</h3>
                                <p class="text-xs text-gray-500 mt-0.5">Blocks, plumber wages, electrical fittings, diesel...</p>
                            </div>
                            <div class="text-right">
                                <p class="text-xs text-gray-500 uppercase tracking-wide">Total Recorded</p>
                                <p id="mgrExpenseTotal" class="text-lg font-bold text-amber-300">₦0</p>
                            </div>
                        </div>
                        <form id="mgrExpenseForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <input type="hidden" id="mgrExpenseEditId">
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Expense Date *</label><input type="date" id="mgrExpenseDate" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Category *</label><select id="mgrExpenseCategory" onchange="updateExpenseForm('mgr')" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="materials">Materials</option><option value="labour">Labour</option><option value="transport">Transport</option><option value="equipment">Equipment</option><option value="permits">Permits</option><option value="other">Other</option></select></div>
                            <div class="sm:col-span-2" id="mgrExpenseItemRow"><label id="mgrExpenseItemLabel" class="block text-xs font-medium mb-1 text-gray-400">Item / Purchase *</label><input type="text" id="mgrExpenseItem" placeholder="Blocks, electrical fittings..." class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div class="sm:col-span-2 hidden" id="mgrExpenseLabourHint"><p class="text-xs text-amber-300 bg-amber-900/30 border border-amber-700/40 rounded-lg px-3 py-2">Labour entry — fill in the worker name in <strong>Paid To</strong> and the total <strong>Amount</strong>. No item or quantity needed.</p></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400" id="mgrExpensePayeeLabel">Paid To / Supplier</label><input list="expensePayeeOptions" type="text" id="mgrExpensePayee" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div><label class="block text-xs font-medium mb-1 text-gray-400">Amount (₦) *</label><input type="number" id="mgrExpenseAmount" step="100" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div id="mgrExpenseQtyRow"><label class="block text-xs font-medium mb-1 text-gray-400">Quantity</label><input type="number" id="mgrExpenseQuantity" step="0.01" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div id="mgrExpenseUnitRow"><label class="block text-xs font-medium mb-1 text-gray-400">Unit Cost (₦)</label><input type="number" id="mgrExpenseUnitCost" step="0.01" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                            <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Receipt / Proof</label><input type="file" id="mgrExpenseReceipt" accept="image/*,.pdf" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm file:mr-3 file:rounded file:border-0 file:bg-gray-600 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-white hover:file:bg-gray-500"><p class="text-[11px] text-gray-500 mt-1">Optional. Upload invoice, receipt, transfer slip, or proof of payment.</p></div>
                            <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label><textarea id="mgrExpenseNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></textarea></div>
                            <div class="sm:col-span-2 flex items-center gap-3 flex-wrap"><button type="submit" id="mgrExpenseSubmitBtn" class="bg-amber-700 hover:bg-amber-600 text-white font-medium py-2 px-5 rounded-lg text-sm">Save Expense</button><button type="button" id="mgrExpenseCancelBtn" class="hidden bg-gray-600 hover:bg-gray-500 text-white font-medium py-2 px-4 rounded-lg text-sm" onclick="cancelExpenseEdit('mgr')">Cancel Edit</button><span id="mgrExpenseMsg" class="text-sm"></span></div>
                            <p class="sm:col-span-2 text-[11px] text-gray-500">Your entries stay pending until approved by the CEO or accountant.</p>
                        </form>
                    </div>
                    <div class="bg-gray-800 rounded-xl p-5">
                        <div class="flex items-center justify-between gap-3 mb-4">
                            <h3 class="font-semibold text-slate-300">Recorded Expenses</h3>
                            <div id="mgrExpenseBreakdown" class="text-xs text-gray-400 text-right"></div>
                        </div>
                        <div class="flex items-center gap-2 flex-wrap mb-4">
                            <select id="mgrExpenseStatusFilter" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-xs"><option value="">All statuses</option><option value="pending">Pending</option><option value="approved">Approved</option><option value="rejected">Rejected</option></select>
                            <select id="mgrExpenseCategoryFilter" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-xs"><option value="">All categories</option><option value="materials">Materials</option><option value="labour">Labour</option><option value="transport">Transport</option><option value="equipment">Equipment</option><option value="permits">Permits</option><option value="land">Land</option><option value="other">Other</option></select>
                            <label class="inline-flex items-center gap-2 text-xs text-gray-400 px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"><input type="checkbox" id="mgrExpenseReceiptOnly" class="rounded border-gray-500 bg-gray-800">Receipts only</label>
                        </div>
                        <div id="mgrExpenseList" class="space-y-3"></div>
                    </div>
                </div>
            </div>

            <!-- MANAGER MAINTENANCE TAB -->
            <div id="mgrTabMaintenance" class="hidden">
                <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
                    <h2 class="text-lg font-semibold">Maintenance Log</h2>
                    <button onclick="ceoToggleMaintenanceForm(false,'mgr')" class="bg-teal-700 hover:bg-teal-600 text-white text-sm font-medium px-4 py-2 rounded-lg flex items-center gap-2"><i class="fas fa-plus text-xs"></i> Record Maintenance</button>
                </div>
                <div class="flex flex-wrap gap-3 mb-4 items-center">
                    <select id="mgrMaintProperty" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="">All properties</option></select>
                    <select id="mgrMaintCategory" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-xs"><option value="">All categories</option><option value="general">General</option><option value="plumbing">Plumbing</option><option value="electrical">Electrical</option><option value="painting">Painting</option><option value="roofing">Roofing</option><option value="cleaning">Cleaning</option><option value="security">Security</option><option value="other">Other</option></select>
                    <button onclick="loadMaintenanceRecords('mgr')" class="px-3 py-2 bg-gray-700 hover:bg-gray-600 text-white text-xs rounded-lg">Filter</button>
                    <span id="mgrMaintSummary" class="text-xs text-gray-400 ml-auto"></span>
                </div>
                <div id="mgrMaintForm" class="hidden bg-gray-800 rounded-xl p-4 mb-4 border border-gray-700">
                    <h3 class="text-sm font-semibold text-gray-300 mb-3" id="mgrMaintFormTitle">New Maintenance Record</h3>
                    <input type="hidden" id="mgrMaintEditId">
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">Property *</label>
                            <select id="mgrMaintFormProperty" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="">Select property</option></select>
                        </div>
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">Date *</label>
                            <input type="date" id="mgrMaintDate" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white">
                        </div>
                        <div class="sm:col-span-2">
                            <label class="block text-xs text-gray-400 mb-1">Title *</label>
                            <input type="text" id="mgrMaintTitle" placeholder="e.g. Fixed roof leak in unit 3A" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-500">
                        </div>
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">Category</label>
                            <select id="mgrMaintFormCategory" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="general">General</option><option value="plumbing">Plumbing</option><option value="electrical">Electrical</option><option value="painting">Painting</option><option value="roofing">Roofing</option><option value="cleaning">Cleaning</option><option value="security">Security</option><option value="other">Other</option></select>
                        </div>
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">Status</label>
                            <select id="mgrMaintStatus" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="completed">Completed</option><option value="in_progress">In Progress</option><option value="scheduled">Scheduled</option></select>
                        </div>
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">Vendor / Contractor</label>
                            <input type="text" id="mgrMaintVendor" placeholder="Vendor or contractor name" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-500">
                        </div>
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">Cost (₦)</label>
                            <input type="number" id="mgrMaintCost" placeholder="0" min="0" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-500">
                        </div>
                        <div class="sm:col-span-2">
                            <label class="block text-xs text-gray-400 mb-1">Notes / Description</label>
                            <textarea id="mgrMaintDesc" rows="2" placeholder="Any additional details..." class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-500 resize-none"></textarea>
                        </div>
                    </div>
                    <div class="flex items-center gap-3">
                        <button onclick="ceoSubmitMaintenance('mgr')" class="bg-teal-700 hover:bg-teal-600 text-white text-sm font-medium px-4 py-2 rounded-lg">Save Record</button>
                        <button onclick="ceoToggleMaintenanceForm(true,'mgr')" class="bg-gray-700 hover:bg-gray-600 text-white text-sm font-medium px-4 py-2 rounded-lg">Cancel</button>
                        <span id="mgrMaintMsg" class="text-sm ml-2"></span>
                    </div>
                </div>
                <div id="mgrMaintList" class="space-y-3"><p class="text-gray-500 text-sm text-center py-8">Loading...</p></div>
            </div>

            {% elif r == 'ACCOUNTANT' %}
            <!-- ACCOUNTANT DASHBOARD -->
            <div class="grid grid-cols-2 md:grid-cols-3 gap-3 sm:gap-4 mb-6">
                <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                    <div class="w-8 h-8 bg-emerald-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-coins text-emerald-400 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Total Revenue</p><p id="acc_total_revenue" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                </div>
                <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                    <div class="w-8 h-8 bg-teal-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-calendar-day text-teal-400 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">This Month</p><p id="acc_monthly_revenue" class="text-xl sm:text-2xl font-bold truncate">-</p><p id="acc_rev_trend" class="text-[10px] text-gray-500 mt-0.5 truncate"></p></div>
                </div>
                <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden col-span-2 md:col-span-1 flex items-start gap-3">
                    <div class="w-8 h-8 bg-blue-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-users text-blue-400 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Active Tenants</p><p id="acc_tenants" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                </div>
                <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                    <div class="w-8 h-8 bg-amber-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-hard-hat text-amber-400 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Capital Spent</p><p id="acc_capital_spent" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                </div>
                <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                    <div class="w-8 h-8 bg-orange-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-receipt text-orange-400 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Capital This Month</p><p id="acc_monthly_capital" class="text-xl sm:text-2xl font-bold truncate">-</p><p id="acc_cap_trend" class="text-[10px] text-gray-500 mt-0.5 truncate"></p></div>
                </div>
                <div id="acc_budget_card" class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden col-span-2 md:col-span-1 flex items-start gap-3">
                    <div class="w-8 h-8 bg-cyan-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-piggy-bank text-cyan-400 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate" id="acc_budget_label">Budget Remaining</p><p id="acc_budget_remaining" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                </div>
            </div>
            <div class="grid grid-cols-1 xl:grid-cols-[0.9fr_1.1fr] gap-6 mb-6">
                <div class="bg-gray-800 rounded-xl p-4 sm:p-6">
                    <h3 class="font-semibold text-lg mb-4 text-slate-300">Record Payment</h3>
                    <form id="accountantPaymentForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <input type="hidden" id="accPaymentEditId">
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Tenant</label><select id="accPaymentTenant" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></select></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Fallback Tenant Name</label><input id="accPaymentTenantName" type="text" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Amount *</label><input id="accPaymentAmount" type="number" step="100" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Payment Date *</label><input id="accPaymentDate" type="date" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Payment Type</label><select id="accPaymentType" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"><option value="rent">Rent</option><option value="deposit">Deposit</option><option value="fee">Fee</option><option value="other">Other</option></select></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Description</label><input id="accPaymentDesc" type="text" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm"></div>
                        <div class="sm:col-span-2 flex items-center gap-3 flex-wrap"><button type="submit" id="accPaymentSubmit" class="bg-emerald-700 hover:bg-emerald-600 text-white font-medium py-2 px-4 rounded-lg text-sm">Record Payment</button><button type="button" id="accPaymentCancel" class="hidden bg-gray-600 hover:bg-gray-500 text-white font-medium py-2 px-4 rounded-lg text-sm">Cancel Edit</button><span id="accPaymentMsg" class="text-sm"></span></div>
                    </form>
                </div>
                <div class="bg-gray-800 rounded-xl p-4 sm:p-6">
                    <div class="flex items-center justify-between gap-3 mb-3">
                        <h3 class="font-semibold text-lg text-slate-300">Payments</h3>
                        <span id="accPaymentCount" class="text-xs text-gray-500"></span>
                    </div>
                    <div class="flex flex-wrap gap-2 mb-4">
                        <select id="accPayFilterType" onchange="renderAccountantPayments()" class="px-3 py-1.5 bg-gray-700 border border-gray-600 rounded-lg text-xs text-white">
                            <option value="">All types</option>
                            <option value="rent">Rent</option>
                            <option value="deposit">Deposit</option>
                            <option value="fee">Fee</option>
                            <option value="other">Other</option>
                        </select>
                        <input type="date" id="accPayFilterFrom" onchange="renderAccountantPayments()" class="px-3 py-1.5 bg-gray-700 border border-gray-600 rounded-lg text-xs text-white" title="From date">
                        <input type="date" id="accPayFilterTo" onchange="renderAccountantPayments()" class="px-3 py-1.5 bg-gray-700 border border-gray-600 rounded-lg text-xs text-white" title="To date">
                        <button type="button" onclick="document.getElementById('accPayFilterType').value='';document.getElementById('accPayFilterFrom').value='';document.getElementById('accPayFilterTo').value='';renderAccountantPayments()" class="px-3 py-1.5 bg-gray-600 hover:bg-gray-500 text-xs text-white rounded-lg">Clear</button>
                    </div>
                    <div id="acc_paymentsContainer" class="space-y-3"></div>
                </div>
            </div>
            <div class="bg-gray-800 rounded-xl p-4 sm:p-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Occupancy Snapshot</h3>
                <div id="accUnitsSummary" class="grid grid-cols-1 sm:grid-cols-3 gap-3"></div>
            </div>
            <!-- Cash Flow Summary -->
            <div class="bg-gray-800 rounded-xl p-4 sm:p-6 mt-6">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <h3 class="font-semibold text-lg text-slate-300">Cash Flow Summary</h3>
                    <span class="text-xs text-gray-500" id="cf_subtitle">All time · approved expenses</span>
                </div>
                <div class="grid grid-cols-3 gap-3 mb-5">
                    <div class="bg-emerald-900/40 border border-emerald-700/30 rounded-xl p-3 overflow-hidden flex items-start gap-2.5">
                        <div class="w-7 h-7 bg-emerald-800/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-arrow-circle-down text-emerald-400 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[10px] text-emerald-400 uppercase tracking-wide mb-0.5 truncate">Revenue In</p><p id="cf_revenue" class="text-sm font-bold text-white truncate">—</p></div>
                    </div>
                    <div class="bg-amber-900/40 border border-amber-700/30 rounded-xl p-3 overflow-hidden flex items-start gap-2.5">
                        <div class="w-7 h-7 bg-amber-800/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-arrow-circle-up text-amber-400 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[10px] text-amber-400 uppercase tracking-wide mb-0.5 truncate">Capital Out</p><p id="cf_expenses" class="text-sm font-bold text-white truncate">—</p></div>
                    </div>
                    <div id="cf_net_card" class="rounded-xl p-3 overflow-hidden flex items-start gap-2.5">
                        <div class="w-7 h-7 bg-gray-700 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-scale-balanced text-gray-300 text-xs"></i></div>
                        <div class="min-w-0"><p id="cf_net_label" class="text-[10px] uppercase tracking-wide mb-0.5 truncate">Net P&amp;L</p><p id="cf_net" class="text-sm font-bold truncate">—</p></div>
                    </div>
                </div>
                <div class="grid grid-cols-1 lg:grid-cols-5 gap-4 mt-2">
                    <div class="lg:col-span-3">
                        <div class="flex items-center justify-between mb-2 flex-wrap gap-2">
                            <h4 class="text-xs text-gray-500 uppercase tracking-wide">Revenue vs Spend</h4>
                            <div class="flex items-center gap-0.5 bg-slate-900/70 rounded-lg p-0.5">
                                <button onclick="accSetPeriod('3M')" id="accPeriod_3M" class="text-xs px-2 py-0.5 rounded-md font-medium transition-colors text-slate-400 hover:text-white">3M</button>
                                <button onclick="accSetPeriod('6M')" id="accPeriod_6M" class="text-xs px-2 py-0.5 rounded-md font-medium transition-colors bg-slate-600 text-white">6M</button>
                                <button onclick="accSetPeriod('12M')" id="accPeriod_12M" class="text-xs px-2 py-0.5 rounded-md font-medium transition-colors text-slate-400 hover:text-white">12M</button>
                                <button onclick="accSetPeriod('all')" id="accPeriod_all" class="text-xs px-2 py-0.5 rounded-md font-medium transition-colors text-slate-400 hover:text-white">All</button>
                            </div>
                        </div>
                        <div class="relative" style="height:155px"><canvas id="accRevenueChart"></canvas></div>
                    </div>
                    <div class="lg:col-span-2 flex flex-col">
                        <h4 class="text-xs text-gray-500 uppercase tracking-wide mb-2">Expense Categories</h4>
                        <div class="flex-1 relative" style="min-height:140px"><canvas id="accCategoryDonut"></canvas></div>
                    </div>
                </div>
            </div>
            <div class="bg-gray-800 rounded-xl p-4 sm:p-6 mt-6">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <h3 class="font-semibold text-lg text-slate-300">Recent Project Expenses</h3>
                    <div id="accExpenseBreakdown" class="text-xs text-gray-400 text-right"></div>
                </div>
                <div class="flex items-center gap-2 flex-wrap mb-4">
                    <select id="accExpensePropertyFilter" onchange="accFilterExpenses()" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-xs"><option value="">All projects</option></select>
                    <select id="accExpenseStatusFilter" onchange="accFilterExpenses()" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-xs"><option value="">All statuses</option><option value="pending">Pending</option><option value="approved">Approved</option><option value="rejected">Rejected</option></select>
                    <select id="accExpenseCategoryFilter" onchange="accFilterExpenses()" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-xs"><option value="">All categories</option><option value="materials">Materials</option><option value="labour">Labour</option><option value="transport">Transport</option><option value="equipment">Equipment</option><option value="permits">Permits</option><option value="land">Land</option><option value="other">Other</option></select>
                    <label class="inline-flex items-center gap-2 text-xs text-gray-400 px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg"><input type="checkbox" id="accExpenseReceiptOnly" onchange="accFilterExpenses()" class="rounded border-gray-500 bg-gray-800">Receipts only</label>
                </div>
                <div id="accExpenseList" class="space-y-3"></div>
            </div>
            <!-- Rent Roll -->
            <div class="bg-gray-800 rounded-xl p-4 sm:p-6 mt-6">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <h3 class="font-semibold text-lg text-slate-300">Rent Roll</h3>
                    <div id="accRentRollTotals" class="text-xs text-gray-400 text-right"></div>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm min-w-[520px]">
                        <thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400 pr-3">Tenant</th><th class="py-2 text-left text-gray-400 pr-3">Unit Type</th><th class="py-2 text-left text-gray-400 pr-3">Unit</th><th class="py-2 text-left text-gray-400 pr-3">Yearly Rent</th><th class="py-2 text-left text-gray-400 pr-3">Lease Start</th><th class="py-2 text-left text-gray-400">Lease End</th></tr></thead>
                        <tbody id="accRentRollBody"></tbody>
                    </table>
                </div>
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
            <div class="grid grid-cols-2 xl:grid-cols-4 gap-3 sm:gap-4 mb-4">
                <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                    <div class="w-8 h-8 bg-slate-700 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-building text-slate-300 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Active Properties</p><p id="rel_properties" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                </div>
                <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                    <div class="w-8 h-8 bg-emerald-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-door-open text-emerald-400 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Available Units</p><p id="rel_available_units" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                </div>
                <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                    <div class="w-8 h-8 bg-amber-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-user-clock text-amber-400 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Open Leads</p><p id="rel_inquiries" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                </div>
                <div class="bg-slate-800/80 border border-slate-700/40 rounded-xl p-3 sm:p-4 overflow-hidden flex items-start gap-3">
                    <div class="w-8 h-8 bg-green-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-handshake text-green-400 text-xs"></i></div>
                    <div class="min-w-0"><p class="text-[11px] text-gray-400 uppercase tracking-wide mb-0.5 truncate">Total Leads</p><p id="rel_new" class="text-xl sm:text-2xl font-bold truncate">-</p></div>
                </div>
            </div>
            <!-- Lead Pipeline Summary -->
            <div class="grid grid-cols-1 lg:grid-cols-5 gap-4 mb-6">
                <div class="lg:col-span-3 bg-gray-800 rounded-xl p-4">
                    <div class="flex items-center justify-between gap-3 mb-4">
                        <h4 class="text-sm font-semibold text-slate-300 flex items-center gap-2"><i class="fas fa-filter text-slate-400 text-xs"></i> Lead Pipeline</h4>
                        <span class="text-xs text-emerald-400 font-medium" id="rel_conversionRate"></span>
                    </div>
                    <div id="rel_pipelineBars" class="space-y-2.5"></div>
                </div>
                <div class="lg:col-span-2 bg-gray-800 rounded-xl p-4 flex flex-col">
                    <h4 class="text-sm font-semibold text-slate-300 mb-2 flex items-center gap-2"><i class="fas fa-chart-pie text-blue-400 text-xs"></i> Stage Mix</h4>
                    <div class="flex-1 relative" style="min-height:165px"><canvas id="relPipelineDonut"></canvas></div>
                </div>
            </div>
            <div class="bg-gray-800 rounded-xl p-4 sm:p-6 mb-6">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <h3 class="font-semibold text-lg text-slate-300">Units Ready To Lease</h3>
                    <span class="text-xs text-gray-500 hidden sm:block">Live availability for leasing conversations</span>
                </div>
                <div class="overflow-x-auto"><table class="w-full text-sm min-w-[420px]"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Unit</th><th class="py-2 text-left text-gray-400">Status</th><th class="py-2 text-left text-gray-400">Yearly Rent</th><th class="py-2 text-left text-gray-400">Notes</th></tr></thead><tbody id="rel_unitsTable"></tbody></table></div>
            </div>
            <div class="bg-gray-800 rounded-xl p-4 sm:p-6 mb-6">
                <h3 class="font-semibold text-lg mb-4 text-slate-300">Available Properties</h3>
                <div class="overflow-x-auto"><table class="w-full text-sm min-w-[420px]"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400">Location</th><th class="py-2 text-left text-gray-400">Price</th><th class="py-2 text-left text-gray-400">Status</th></tr></thead><tbody id="rel_propertiesTable"></tbody></table></div>
            </div>
            <!-- Commission Tracker -->
            <div class="bg-gray-800 rounded-xl p-4 sm:p-6 mb-6">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <h3 class="font-semibold text-lg text-slate-300">Commission Tracker</h3>
                    <span class="text-xs text-gray-500">10% sale · 10% first year rent</span>
                </div>
                <div class="grid grid-cols-3 gap-3 mb-4">
                    <div class="bg-emerald-900/40 border border-emerald-700/30 rounded-xl p-3 overflow-hidden">
                        <p class="text-[11px] text-emerald-400 uppercase tracking-wide mb-1 truncate">Rental Commission</p>
                        <p id="rel_rental_comm" class="text-base font-bold text-white truncate">—</p>
                        <p class="text-[11px] text-emerald-600/70 mt-0.5">10% × active leases</p>
                    </div>
                    <div class="bg-blue-900/40 border border-blue-700/30 rounded-xl p-3 overflow-hidden">
                        <p class="text-[11px] text-blue-400 uppercase tracking-wide mb-1 truncate">Sale Commission</p>
                        <p id="rel_sale_comm" class="text-base font-bold text-white truncate">—</p>
                        <p class="text-[11px] text-blue-600/70 mt-0.5">10% × closed deals</p>
                    </div>
                    <div class="bg-amber-900/40 border border-amber-700/30 rounded-xl p-3 overflow-hidden">
                        <p class="text-[11px] text-amber-400 uppercase tracking-wide mb-1 truncate">Total Estimated</p>
                        <p id="rel_total_comm" class="text-base font-bold text-amber-300 truncate">—</p>
                        <p class="text-[11px] text-amber-600/70 mt-0.5">Combined estimate</p>
                    </div>
                </div>
                <div id="rel_comm_detail" class="space-y-2"></div>
            </div>
            <!-- Add / Edit Lead Form -->
            <div class="bg-gray-800 rounded-xl p-4 sm:p-6 mb-6">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <h3 id="relLeadFormTitle" class="font-semibold text-lg text-slate-300">Add New Lead</h3>
                    <button type="button" id="relLeadFormToggle" onclick="relToggleLeadForm()" class="text-xs text-emerald-400 border border-emerald-700/50 px-3 py-1.5 rounded-lg hover:bg-emerald-900/30">+ Add Lead</button>
                </div>
                <div id="relLeadFormBody" class="hidden">
                    <form id="relLeadForm" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <input type="hidden" id="relLeadEditId">
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Full Name *</label><input id="relLeadName" type="text" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Phone *</label><input id="relLeadPhone" type="text" required class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white" placeholder="e.g. 08012345678"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Email</label><input id="relLeadEmail" type="email" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Property</label><select id="relLeadProperty" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"><option value="">General Inquiry</option></select></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Type</label><select id="relLeadType" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"><option value="general">General</option><option value="rent">Rent</option><option value="purchase">Purchase</option><option value="hostel">Hostel</option></select></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Status</label><select id="relLeadStatus" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"><option value="new">New</option><option value="contacted">Contacted</option><option value="viewing_scheduled">Viewing Scheduled</option><option value="offer_made">Offer Made</option><option value="closed">Closed</option><option value="rejected">Rejected</option></select></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Budget Range</label><input id="relLeadBudget" type="text" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white" placeholder="e.g. ₦500k – ₦1M"></div>
                        <div><label class="block text-xs font-medium mb-1 text-gray-400">Preferred Move Date</label><input id="relLeadMoveDate" type="date" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white"></div>
                        <div class="sm:col-span-2"><label class="block text-xs font-medium mb-1 text-gray-400">Notes</label><textarea id="relLeadNotes" rows="2" class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white resize-none" placeholder="Source of lead, additional details..."></textarea></div>
                        <div class="sm:col-span-2 flex items-center gap-3 flex-wrap">
                            <button type="submit" id="relLeadSubmitBtn" class="bg-emerald-700 hover:bg-emerald-600 text-white font-medium py-2 px-5 rounded-lg text-sm">Save Lead</button>
                            <button type="button" id="relLeadCancelBtn" class="hidden bg-gray-600 hover:bg-gray-500 text-white font-medium py-2 px-4 rounded-lg text-sm" onclick="relCancelLeadEdit()">Cancel Edit</button>
                            <span id="relLeadMsg" class="text-sm"></span>
                        </div>
                    </form>
                </div>
            </div>
            <!-- Leads Table -->
            <div class="bg-gray-800 rounded-xl p-4 sm:p-6">
                <div class="flex items-center justify-between gap-3 mb-4">
                    <h3 class="font-semibold text-lg text-slate-300">Leads / Inquiries</h3>
                    <span id="rel_leadsCount" class="text-xs text-gray-500"></span>
                </div>
                <div class="overflow-x-auto"><table class="w-full text-sm min-w-[480px]"><thead><tr class="border-b border-gray-700"><th class="py-2 text-left text-gray-400">Name</th><th class="py-2 text-left text-gray-400">Property</th><th class="py-2 text-left text-gray-400">Type</th><th class="py-2 text-left text-gray-400 min-w-[140px]">Status</th><th class="py-2 text-left text-gray-400">Date</th></tr></thead><tbody id="rel_inquiriesTable"></tbody></table></div>
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
        function fmtCompact(v) {
            const n = Number(v || 0);
            if (n >= 1e9) return '₦' + (n/1e9).toFixed(1).replace(/\.0$/,'') + 'B';
            if (n >= 1e6) return '₦' + (n/1e6).toFixed(1).replace(/\.0$/,'') + 'M';
            if (n >= 1e3) return '₦' + Math.round(n/1e3) + 'K';
            return formatNGN(v);
        }

        function countUp(elId, target, fmt) {
            const el = document.getElementById(elId);
            if (!el) return;
            const num = parseFloat(target) || 0;
            if (!num) { el.textContent = fmt ? fmt(0) : '0'; return; }
            const dur = 700, t0 = performance.now();
            const step = ts => {
                const p = Math.min((ts - t0) / dur, 1);
                const v = num * (1 - Math.pow(1 - p, 3));
                el.textContent = fmt ? fmt(v) : Math.round(v);
                if (p < 1) requestAnimationFrame(step); else el.textContent = fmt ? fmt(num) : num;
            };
            requestAnimationFrame(step);
        }

        function _trendSlice(monthly, yearly, period) {
            if (period === 'all') return { labels: yearly.map(y => y.year), revenue: yearly.map(y => y.revenue), capital: yearly.map(y => y.capital) };
            const n = period === '3M' ? 3 : period === '6M' ? 6 : 12;
            const s = monthly.slice(-n);
            return { labels: s.map(m => m.month), revenue: s.map(m => m.revenue), capital: s.map(m => m.capital) };
        }
        function _setPeriodBtns(prefix, active) {
            ['3M','6M','12M','all'].forEach(x => {
                const btn = document.getElementById(prefix + x);
                if (btn) btn.className = 'text-xs px-2.5 py-1 rounded-md font-medium transition-colors ' + (x === active ? 'bg-slate-600 text-white' : 'text-slate-400 hover:text-white');
            });
        }

        async function loadCapitalPropertyOptions() {
            try {
                const props = await fetchData('/admin/api/properties');
                const options = '<option value="">All projects</option>' + props.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                const sel = document.getElementById('mgrCapitalProperty');
                if (sel) {
                    const cur = sel.value;
                    sel.innerHTML = options;
                    sel.value = cur && props.some(p => String(p.id) === cur) ? cur : (props[0] ? String(props[0].id) : '');
                }
                await loadProjectExpenses('mgr');
            } catch (e) {}
        }

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

        async function loadRoleDashboard(role) {
            try {
                if (role === 'INVESTOR') await loadInvestorDashboard();
                else if (role === 'MANAGER') { await loadManagerDashboard(); loadRoleDocument('MANAGER'); }
                else if (role === 'ACCOUNTANT') { await loadAccountantDashboard(); loadRoleDocument('ACCOUNTANT'); }
                else if (role === 'REALTOR') { await loadRealtorDashboard(); loadRoleDocument('REALTOR'); }
            } finally {
                dismissLoader();
            }
        }

        let accountantPaymentsCache = [];

        function renderAccountantPayments() {
            const typeFilter = document.getElementById('accPayFilterType')?.value || '';
            const fromFilter = document.getElementById('accPayFilterFrom')?.value || '';
            const toFilter = document.getElementById('accPayFilterTo')?.value || '';
            const typeColors = { rent:'bg-blue-900/50 text-blue-300', deposit:'bg-purple-900/50 text-purple-300', fee:'bg-amber-900/50 text-amber-300', other:'bg-gray-700 text-gray-300' };
            let filtered = accountantPaymentsCache;
            if (typeFilter) filtered = filtered.filter(p => p.payment_type === typeFilter);
            if (fromFilter) filtered = filtered.filter(p => p.payment_date >= fromFilter);
            if (toFilter) filtered = filtered.filter(p => p.payment_date <= toFilter);
            const countEl = document.getElementById('accPaymentCount');
            if (countEl) countEl.textContent = filtered.length < accountantPaymentsCache.length ? `${filtered.length} of ${accountantPaymentsCache.length} shown` : `${filtered.length} total`;
            document.getElementById('acc_paymentsContainer').innerHTML = filtered.slice(0, 30).map(p =>
                `<div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4"><div class="flex items-start justify-between gap-3 mb-2"><div><p class="font-semibold text-white text-sm">${p.tenant_name || '—'}</p>${p.description ? `<p class="text-xs text-gray-400 mt-0.5">${p.description}</p>` : ''}</div><p class="text-emerald-400 font-bold text-base flex-shrink-0">${formatNGN(p.amount)}</p></div><div class="flex items-center justify-between gap-3 flex-wrap text-xs"><div class="flex items-center gap-3 flex-wrap"><span class="px-2.5 py-1 rounded-full ${typeColors[p.payment_type] || 'bg-gray-700 text-gray-300'}">${p.payment_type}</span><span class="text-gray-400">${p.payment_date}</span>${p.recorded_by ? `<span class="text-gray-500">by ${p.recorded_by}</span>` : ''}</div><div class="flex items-center gap-3"><button type="button" onclick="startAccountantPaymentEdit(${p.id})" class="text-blue-400 hover:text-blue-300 font-medium">Edit</button><button type="button" onclick="deleteAccountantPayment(${p.id})" class="text-red-400 hover:text-red-300 font-medium">Remove</button></div></div></div>`
            ).join('') || `<div class="text-center py-8"><svg class="w-10 h-10 text-gray-600 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z"/></svg>${accountantPaymentsCache.length === 0 ? '<p class="text-gray-300 text-sm font-medium">No payments recorded yet</p><p class="text-gray-500 text-xs mt-1">Use the form on the left to log the first payment</p>' : '<p class="text-gray-400 text-sm">No payments match this filter</p>'}</div>`;
        }

        function resetAccountantPaymentForm() {
            document.getElementById('accountantPaymentForm')?.reset();
            document.getElementById('accPaymentEditId').value = '';
            document.getElementById('accPaymentSubmit').textContent = 'Record Payment';
            document.getElementById('accPaymentCancel').classList.add('hidden');
        }

        function startAccountantPaymentEdit(paymentId) {
            const payment = accountantPaymentsCache.find(item => item.id === paymentId);
            if (!payment) return;
            document.getElementById('accPaymentEditId').value = payment.id;
            document.getElementById('accPaymentTenant').value = payment.tenant_id || '';
            document.getElementById('accPaymentTenantName').value = payment.tenant_name || '';
            document.getElementById('accPaymentAmount').value = payment.amount || '';
            document.getElementById('accPaymentDate').value = payment.payment_date || '';
            document.getElementById('accPaymentType').value = payment.payment_type || 'rent';
            document.getElementById('accPaymentDesc').value = payment.description || '';
            document.getElementById('accPaymentSubmit').textContent = 'Save Changes';
            document.getElementById('accPaymentCancel').classList.remove('hidden');
        }

        async function deleteAccountantPayment(paymentId) {
            if (!confirm('Remove this payment record?')) return;
            try {
                await fetchData('/admin/api/payments/' + paymentId, { method: 'DELETE' });
                resetAccountantPaymentForm();
                await loadAccountantDashboard();
            } catch (err) {
                const msgEl = document.getElementById('accPaymentMsg');
                msgEl.textContent = err.message || 'Error removing payment';
                msgEl.className = 'text-sm text-red-400';
            }
        }

        window.expenseCache = window.expenseCache || { ceo: [], mgr: [], acc: [] };
        window.expPage = window.expPage || { ceo: 1, mgr: 1, acc: 1 };
        const _PAGE_SIZE = 20;

        function renderExpensePage(prefix) {
            const listEl = document.getElementById(prefix + 'ExpenseList');
            if (!listEl) return;
            const all = (window.expenseCache[prefix]) || [];
            const total = all.length;
            const totalPages = Math.max(1, Math.ceil(total / _PAGE_SIZE));
            if (window.expPage[prefix] > totalPages) window.expPage[prefix] = totalPages;
            const start = (window.expPage[prefix] - 1) * _PAGE_SIZE;
            const page = all.slice(start, start + _PAGE_SIZE);
            if (!total) { listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-6">No expenses recorded for this project yet.</p>'; return; }
            const cards = page.map(exp => renderExpenseCard(prefix, exp)).join('');
            const prev = window.expPage[prefix] > 1 ? `<button onclick="window.expPage['${prefix}']--;renderExpensePage('${prefix}')" class="px-3 py-1 rounded bg-gray-700 hover:bg-gray-600 text-white text-xs">&#8592; Prev</button>` : '';
            const next = window.expPage[prefix] < totalPages ? `<button onclick="window.expPage['${prefix}']++;renderExpensePage('${prefix}')" class="px-3 py-1 rounded bg-gray-700 hover:bg-gray-600 text-white text-xs">Next &#8594;</button>` : '';
            const pager = totalPages > 1 ? `<div class="flex items-center justify-between mt-4 text-xs text-gray-400"><span>Showing ${start+1}&ndash;${Math.min(start+_PAGE_SIZE,total)} of ${total}</span><div class="flex items-center gap-3">${prev}<span>Page ${window.expPage[prefix]} / ${totalPages}</span>${next}</div></div>` : `<p class="text-xs text-gray-500 mt-3 text-right">${total} expense${total!==1?'s':''} total</p>`;
            listEl.innerHTML = cards + pager;
        }

        let vendorOptionsCache = [];

        async function loadVendorOptions() {
            try {
                vendorOptionsCache = await fetchData('/admin/api/vendors');
                let datalist = document.getElementById('expensePayeeOptions');
                if (!datalist) {
                    datalist = document.createElement('datalist');
                    datalist.id = 'expensePayeeOptions';
                    document.body.appendChild(datalist);
                }
                datalist.innerHTML = vendorOptionsCache.map(v => `<option value="${v.name}">${v.contact_type}</option>`).join('');
            } catch (e) {}
        }

        async function uploadExpenseReceipt(source) {
            const prefix = source === 'mgr' ? 'mgr' : 'ceo';
            const fileInput = document.getElementById(prefix + 'ExpenseReceipt');
            if (!fileInput || !fileInput.files || !fileInput.files.length) return '';
            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            const response = await fetch('/admin/api/upload-expense-receipt', {
                method: 'POST',
                credentials: 'include',
                headers: { 'X-CSRF-Token': adminCsrfToken },
                body: formData
            });
            const result = await response.json();
            if (!response.ok || !result.success) {
                throw new Error(result.message || 'Receipt upload failed');
            }
            return result.filename || '';
        }

        function canApproveExpenses() {
            return ALL_ROLES.includes('CEO');
        }

        function getExpenseStatusMeta(status) {
            const normalized = status || 'pending';
            if (normalized === 'approved') return { label: 'Approved', className: 'bg-emerald-900/60 text-emerald-300 border border-emerald-700/70' };
            if (normalized === 'rejected') return { label: 'Rejected', className: 'bg-rose-900/60 text-rose-300 border border-rose-700/70' };
            return { label: 'Pending Approval', className: 'bg-amber-900/60 text-amber-300 border border-amber-700/70' };
        }

        function buildExpenseQuery(propertyId, filters = {}) {
            const params = new URLSearchParams();
            if (propertyId) params.set('property_id', propertyId);
            if (filters.status) params.set('approval_status', filters.status);
            if (filters.receiptsOnly) params.set('has_receipt', 'true');
            if (filters.category) params.set('category', filters.category);
            const query = params.toString();
            return query ? ('?' + query) : '';
        }

        function getExpenseFilters(source) {
            const prefix = source === 'mgr' ? 'mgr' : (source === 'acc' ? 'acc' : 'ceo');
            return {
                status: document.getElementById(prefix + 'ExpenseStatusFilter')?.value || '',
                receiptsOnly: !!document.getElementById(prefix + 'ExpenseReceiptOnly')?.checked,
                category: document.getElementById(prefix + 'ExpenseCategoryFilter')?.value || '',
                propertyId: prefix === 'acc' ? (document.getElementById('accExpensePropertyFilter')?.value || '') : (document.getElementById(prefix + 'ConstructionProperty')?.value || ''),
            };
        }

        async function toggleExpensePaid(expId, current, prefix) {
            try {
                await fetchData('/admin/api/project-expenses/' + expId, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ is_paid: !current }) });
                const propertyId = document.getElementById(prefix + 'CapitalProperty')?.value || '';
                if (prefix === 'acc') { await loadAccountantDashboard(); } else { await loadProjectExpenses(prefix, propertyId, true); }
            } catch(e) { alert(e.message || 'Error updating payment status'); }
        }

        function renderExpenseCard(source, exp) {
            const prefix = source === 'mgr' ? 'mgr' : (source === 'acc' ? 'acc' : 'ceo');
            const statusMeta = getExpenseStatusMeta(exp.approval_status);
            const paidBadge = exp.is_paid
                ? `<span class="px-2 py-0.5 rounded-full text-[11px] bg-emerald-900/60 text-emerald-300 font-medium">Paid</span>`
                : `<span class="px-2 py-0.5 rounded-full text-[11px] bg-orange-900/60 text-orange-300 font-medium">Unpaid</span>`;
            const paidToggle = prefix !== 'acc'
                ? `<button type="button" onclick="toggleExpensePaid(${exp.id}, ${exp.is_paid}, '${prefix}')" class="${exp.is_paid ? 'text-orange-400 hover:text-orange-300' : 'text-emerald-400 hover:text-emerald-300'} font-medium">${exp.is_paid ? 'Mark Unpaid' : 'Mark Paid'}</button>`
                : '';
            const statusActions = canApproveExpenses()
                ? `<div class="flex items-center gap-3">${exp.approval_status !== 'approved' ? `<button type="button" onclick="setExpenseApprovalStatus(${exp.id}, 'approved', '${prefix}')" class="text-emerald-400 hover:text-emerald-300 font-medium">Approve</button>` : ''}${exp.approval_status !== 'rejected' ? `<button type="button" onclick="setExpenseApprovalStatus(${exp.id}, 'rejected', '${prefix}')" class="text-rose-400 hover:text-rose-300 font-medium">Reject</button>` : ''}${exp.approval_status !== 'pending' ? `<button type="button" onclick="setExpenseApprovalStatus(${exp.id}, 'pending', '${prefix}')" class="text-amber-400 hover:text-amber-300 font-medium">Mark Pending</button>` : ''}</div>`
                : '';
            const managementActions = prefix !== 'acc'
                ? `<button type="button" onclick="editProjectExpense('${prefix}', ${exp.id})" class="text-blue-400 hover:text-blue-300 font-medium">Edit</button><button type="button" onclick="deleteProjectExpense('${prefix}', ${exp.id})" class="text-red-400 hover:text-red-300 font-medium">Remove</button>`
                : '';
            return `<div class="rounded-xl border ${exp.is_paid ? 'border-emerald-800/50' : 'border-gray-700/70'} bg-gray-700/30 p-4"><div class="flex items-start justify-between gap-3"><div class="min-w-0"><div class="flex items-center gap-2 flex-wrap"><p class="font-semibold text-white text-sm">${exp.item_name}</p><span class="px-2.5 py-1 rounded-full text-[11px] ${statusMeta.className}">${statusMeta.label}</span>${paidBadge}</div><p class="text-xs text-gray-400 mt-1">${prefix === 'acc' ? (exp.property_title || 'Unassigned project') + ' · ' : ''}${exp.payee_name || 'No payee recorded'} · ${exp.category} · ${exp.expense_date || ''}</p>${exp.notes ? `<p class="text-xs text-gray-500 mt-2">${exp.notes}</p>` : ''}${exp.receipt_path ? `<p class="mt-2 flex items-center gap-3"><a href="/assets/${exp.receipt_path}" target="_blank" class="text-xs text-cyan-300 hover:text-cyan-200 underline">View receipt</a><a href="/assets/${exp.receipt_path}" download class="text-xs text-cyan-400 hover:text-cyan-300 underline">Download</a></p>` : ''}${exp.approved_by ? `<p class="text-[11px] text-gray-500 mt-2">Approved by ${exp.approved_by}${exp.approved_at ? ' · ' + exp.approved_at : ''}</p>` : ''}${exp.approval_note ? `<p class="text-[11px] text-rose-300 mt-1">Note: ${exp.approval_note}</p>` : ''}</div><div class="text-right flex-shrink-0"><p class="text-base font-bold text-amber-300">${formatNGN(exp.amount)}</p><p class="text-[11px] text-gray-500 mt-1">${exp.recorded_by || ''}</p></div></div><div class="flex items-center justify-between gap-3 mt-3 text-xs flex-wrap"><div class="text-gray-500">${exp.quantity ? 'Qty ' + exp.quantity : ''}${exp.quantity && exp.unit_cost ? ' · ' : ''}${exp.unit_cost ? 'Unit ' + formatNGN(exp.unit_cost) : ''}</div><div class="flex items-center gap-3 flex-wrap">${paidToggle}${managementActions}${statusActions}</div></div></div>`;
        }

        function updateExpenseForm(prefix) {
            const cat = document.getElementById(prefix + 'ExpenseCategory')?.value || 'materials';
            const isLabour = cat === 'labour';
            const showQty = cat === 'materials' || cat === 'equipment';
            document.getElementById(prefix + 'ExpenseItemRow')?.classList.toggle('hidden', isLabour);
            document.getElementById(prefix + 'ExpenseLabourHint')?.classList.toggle('hidden', !isLabour);
            document.getElementById(prefix + 'ExpenseQtyRow')?.classList.toggle('hidden', !showQty);
            document.getElementById(prefix + 'ExpenseUnitRow')?.classList.toggle('hidden', !showQty);
            const lbl = document.getElementById(prefix + 'ExpenseItemLabel');
            if (lbl) lbl.textContent = {materials:'Item / Purchase *', transport:'Route / Purpose', permits:'Permit / Fee Description', equipment:'Equipment / Item *', other:'Description'}[cat] || 'Item / Purchase *';
            const payeeLbl = document.getElementById(prefix + 'ExpensePayeeLabel');
            if (payeeLbl) payeeLbl.textContent = isLabour ? 'Worker / Paid To *' : 'Paid To / Supplier';
        }

        function cancelExpenseEdit(source) {
            const prefix = source === 'mgr' ? 'mgr' : 'ceo';
            document.getElementById(prefix + 'ExpenseEditId').value = '';
            document.getElementById(prefix + 'ExpenseForm')?.reset();
            document.getElementById(prefix + 'ExpenseSubmitBtn').textContent = 'Save Expense';
            document.getElementById(prefix + 'ExpenseCancelBtn').classList.add('hidden');
            updateExpenseForm(prefix);
        }

        function editProjectExpense(source, expenseId) {
            const prefix = source === 'mgr' ? 'mgr' : 'ceo';
            const expense = ((window.expenseCache||{})[prefix] || []).find(item => item.id === expenseId);
            if (!expense) return;
            document.getElementById(prefix + 'ExpenseEditId').value = expense.id;
            document.getElementById(prefix + 'ExpenseDate').value = expense.expense_date || '';
            document.getElementById(prefix + 'ExpenseCategory').value = expense.category || 'materials';
            updateExpenseForm(prefix);
            document.getElementById(prefix + 'ExpenseItem').value = expense.item_name || '';
            document.getElementById(prefix + 'ExpensePayee').value = expense.payee_name || '';
            document.getElementById(prefix + 'ExpenseAmount').value = expense.amount || '';
            document.getElementById(prefix + 'ExpenseQuantity').value = expense.quantity ?? '';
            document.getElementById(prefix + 'ExpenseUnitCost').value = expense.unit_cost ?? '';
            document.getElementById(prefix + 'ExpenseNotes').value = expense.notes || '';
            document.getElementById(prefix + 'ExpenseSubmitBtn').textContent = 'Save Changes';
            document.getElementById(prefix + 'ExpenseCancelBtn').classList.remove('hidden');
        }

        async function deleteProjectExpense(source, expenseId) {
            if (!confirm('Remove this expense record?')) return;
            const prefix = source === 'mgr' ? 'mgr' : 'ceo';
            try {
                await fetchData('/admin/api/project-expenses/' + expenseId, { method: 'DELETE' });
                cancelExpenseEdit(prefix);
                const propertyId = document.getElementById(prefix + 'ConstructionProperty')?.value || '';
                await loadProjectExpenses(prefix, propertyId, true);
                if (prefix === 'mgr') await loadManagerDashboard();
                else await loadStats();
            } catch (err) {
                const msgEl = document.getElementById(prefix + 'ExpenseMsg');
                msgEl.textContent = err.message || 'Error removing expense';
                msgEl.className = 'text-sm text-red-400';
            }
        }

        async function setExpenseApprovalStatus(expenseId, nextStatus, source) {
            const approvalNote = nextStatus === 'rejected' ? (prompt('Optional rejection note for this expense:', '') || '') : '';
            try {
                await fetchData('/admin/api/project-expenses/' + expenseId, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ approval_status: nextStatus, approval_note: approvalNote })
                });
                if (source === 'acc') {
                    await loadAccountantDashboard();
                    return;
                }
                const propertyId = document.getElementById((source === 'mgr' ? 'mgr' : 'ceo') + 'ConstructionProperty')?.value || '';
                await loadProjectExpenses(source, propertyId, true);
                if (source === 'mgr') await loadManagerDashboard();
                else await loadStats();
            } catch (err) {
                alert(err.message || 'Error updating approval status');
            }
        }

        async function loadProjectExpenses(source, propertyId, preservePage) {
            const prefix = source === 'mgr' ? 'mgr' : 'ceo';
            const listEl = document.getElementById(prefix + 'ExpenseList');
            const totalEl = document.getElementById(prefix + 'ExpenseTotal');
            const breakdownEl = document.getElementById(prefix + 'ExpenseBreakdown');
            if (!listEl || !totalEl || !breakdownEl) return;
            const selectedId = propertyId || document.getElementById(prefix + 'CapitalProperty')?.value || '';
            const filters = getExpenseFilters(prefix);
            try {
                const data = await fetchData('/admin/api/project-expenses' + buildExpenseQuery(selectedId, filters));
                window.expenseCache[prefix] = data.expenses || [];
                totalEl.textContent = formatNGN(data.total_amount || 0);
                const categoryBits = Object.entries(data.by_category || {}).map(([k, v]) => `${k}: ${formatNGN(v)}`);
                const budgetParts = [];
                if (data.budget_total !== null && data.budget_total !== undefined) {
                    budgetParts.push(`Budget ${formatNGN(data.budget_total)}`);
                }
                if (data.budget_remaining !== null && data.budget_remaining !== undefined) {
                    budgetParts.push(`${data.over_budget ? 'Over by' : 'Left'} ${formatNGN(Math.abs(data.budget_remaining))}`);
                }
                const approvalTotals = data.approval_totals || {};
                const approvalBits = [];
                if (approvalTotals.approved) approvalBits.push(`Approved ${formatNGN(approvalTotals.approved)}`);
                if (approvalTotals.pending) approvalBits.push(`Pending ${formatNGN(approvalTotals.pending)}`);
                if (approvalTotals.rejected) approvalBits.push(`Rejected ${formatNGN(approvalTotals.rejected)}`);
                const parts = [...budgetParts, ...approvalBits, ...categoryBits];
                breakdownEl.textContent = parts.length ? parts.join(' · ') : 'No expenses yet';
                if (prefix === 'ceo') {
                    const el = (id) => document.getElementById(id);
                    if (el('capApprovedTotal')) el('capApprovedTotal').textContent = fmtCompact(approvalTotals.approved || 0);
                    if (el('capPendingTotal')) el('capPendingTotal').textContent = fmtCompact(approvalTotals.pending || 0);
                    if (el('capRejectedTotal')) el('capRejectedTotal').textContent = fmtCompact(approvalTotals.rejected || 0);
                    if (el('capBudgetRemaining')) el('capBudgetRemaining').textContent = data.budget_remaining != null ? fmtCompact(Math.abs(data.budget_remaining)) + (data.over_budget ? ' over' : ' left') : '—';
                    if (el('capBudgetTotal')) el('capBudgetTotal').textContent = data.budget_total != null ? fmtCompact(data.budget_total) : '—';
                }
                breakdownEl.textContent = parts.length ? parts.join(' · ') : 'No expenses yet';
                if (!preservePage) window.expPage[prefix] = 1;
                renderExpensePage(prefix);
            } catch (err) {
                listEl.innerHTML = '<p class="text-red-400 text-sm text-center py-6">Error loading project expenses.</p>';
            }
        }

        async function submitProjectExpense(source) {
            const prefix = source === 'mgr' ? 'mgr' : 'ceo';
            const editId = document.getElementById(prefix + 'ExpenseEditId').value;
            const msgEl = document.getElementById(prefix + 'ExpenseMsg');
            const propertyId = document.getElementById(prefix + 'CapitalProperty')?.value || '';
            if (!propertyId) {
                msgEl.textContent = 'Select a project first.';
                msgEl.className = 'text-sm text-red-400';
                return;
            }
            const payload = {
                property_id: propertyId,
                expense_date: document.getElementById(prefix + 'ExpenseDate').value,
                category: document.getElementById(prefix + 'ExpenseCategory').value,
                item_name: document.getElementById(prefix + 'ExpenseItem').value,
                payee_name: document.getElementById(prefix + 'ExpensePayee').value,
                amount: document.getElementById(prefix + 'ExpenseAmount').value,
                quantity: document.getElementById(prefix + 'ExpenseQuantity').value,
                unit_cost: document.getElementById(prefix + 'ExpenseUnitCost').value,
                notes: document.getElementById(prefix + 'ExpenseNotes').value,
            };
            try {
                const receiptPath = await uploadExpenseReceipt(source);
                if (receiptPath) payload.receipt_path = receiptPath;
                const res = await fetchData(editId ? ('/admin/api/project-expenses/' + editId) : '/admin/api/project-expenses', {
                    method: editId ? 'PUT' : 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify(payload)
                });
                msgEl.textContent = res.message || 'Expense saved';
                msgEl.className = 'text-sm text-emerald-400';
                cancelExpenseEdit(prefix);
                await loadVendorOptions();
                await loadProjectExpenses(prefix, propertyId, !!editId);
                if (prefix === 'mgr') await loadManagerDashboard();
                else await loadStats();
            } catch (err) {
                msgEl.textContent = err.message || 'Error saving expense';
                msgEl.className = 'text-sm text-red-400';
            }
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

        document.addEventListener('DOMContentLoaded', () => {
            loadRoleDashboard(USER_ROLE);
        });

        function renderInvestorMilestones(updates) {
            const barEl = document.getElementById('invMainProgressBar');
            const headlineEl = document.getElementById('invProgressHeadline');
            const milestonesEl = document.getElementById('invMilestones');
            const latestPct = updates.length ? updates[updates.length - 1].progress_percentage : 0;
            if (headlineEl) headlineEl.textContent = latestPct + '%';
            if (barEl) setTimeout(() => { barEl.style.width = latestPct + '%'; }, 120);
            if (!milestonesEl) return;
            if (!updates.length) {
                milestonesEl.innerHTML = '<p class="text-sm text-gray-500 text-center py-6">Construction milestones will appear here once the project team posts them.</p>';
                return;
            }
            const sorted = updates.slice().reverse(); // newest first
            milestonesEl.innerHTML = '<div class="relative">' +
                '<div class="absolute left-3.5 top-0 bottom-0 w-px bg-gray-700/80"></div>' +
                sorted.map((item, i) => {
                    const pct = item.progress_percentage || 0;
                    const isLatest = i === 0;
                    const dotBg = pct >= 75 ? 'bg-emerald-500 ring-emerald-500/30' : pct >= 40 ? 'bg-amber-500 ring-amber-500/30' : 'bg-gray-500 ring-gray-500/30';
                    const labelColor = pct >= 75 ? 'text-emerald-400' : pct >= 40 ? 'text-amber-400' : 'text-gray-400';
                    return `<div class="relative flex gap-4 pb-5 last:pb-0">
                        <div class="flex-shrink-0 relative z-10 mt-0.5">
                            <span class="flex w-7 h-7 items-center justify-center rounded-full ring-4 ring-gray-800 ${dotBg} ${isLatest ? 'animate-pulse' : ''}">
                                <i class="fas ${pct >= 100 ? 'fa-check' : 'fa-hard-hat'} text-white text-[10px]"></i>
                            </span>
                        </div>
                        <div class="flex-1 min-w-0 pt-0.5">
                            <div class="flex items-start justify-between gap-2 mb-1">
                                <div>
                                    <p class="text-sm font-semibold text-white leading-tight">${item.title}${isLatest ? ' <span class="ml-1 text-[10px] font-bold bg-emerald-900/60 text-emerald-300 border border-emerald-700/40 px-1.5 py-0.5 rounded-full">Latest</span>' : ''}</p>
                                    <p class="text-xs text-gray-500 mt-0.5">${item.happened_on || 'Date pending'}</p>
                                </div>
                                <span class="text-xs font-bold flex-shrink-0 ${labelColor}">${pct}%</span>
                            </div>
                            <div class="w-full bg-gray-700 rounded-full h-1 mb-1.5">
                                <div class="h-1 rounded-full bg-gradient-to-r from-emerald-600 to-teal-400" style="width:${pct}%"></div>
                            </div>
                            ${item.notes ? `<p class="text-xs text-gray-400 leading-relaxed">${item.notes}</p>` : ''}
                        </div>
                    </div>`;
                }).join('') + '</div>';
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

        function populateManagerUnitSelect(units, filterPropertyId) {
            const select = document.getElementById('mgrTenantUnit');
            if (!select) return;
            const filtered = units.filter(unit => {
                const matchProp = !filterPropertyId || String(unit.property_id) === String(filterPropertyId);
                return matchProp && (unit.status === 'available' || unit.status === 'reserved');
            });
            select.innerHTML = '<option value="">Select unit</option>' + filtered.map(unit => `<option value="${unit.unit_code}">${unit.unit_code} · ${unit.property_title} · ${unit.status}</option>`).join('');
        }

        // Manager Unit Type cascade (mirrors CEO behaviour)
        async function mgrTnUtRefreshForProperty() {
            const propSel = document.getElementById('mgrTenantProperty');
            const utSel = document.getElementById('mgrTenantUnitType');
            if (!propSel || !utSel) return;
            const propId = propSel.value;
            if (!window._mgrUnitTypesCache) {
                try { window._mgrUnitTypesCache = await fetchData('/admin/api/unit-types'); }
                catch (e) { window._mgrUnitTypesCache = []; }
            }
            const filtered = propId ? window._mgrUnitTypesCache.filter(u => String(u.property_id) === String(propId)) : [];
            utSel.innerHTML = '<option value="">-- Select unit type --</option>' +
                filtered.map(u => `<option value="${u.id}" data-price="${u.annual_price || 0}">${u.name}${u.annual_price ? ' — ' + formatNGN(u.annual_price) + '/yr' : ''}</option>`).join('');
        }

        function mgrTnOnUnitTypeChange() {
            const sel = document.getElementById('mgrTenantUnitType');
            const price = sel?.selectedOptions[0]?.dataset.price;
            const rentEl = document.getElementById('mgrTenantRent');
            if (price && rentEl && (!rentEl.value || rentEl.value === '0')) rentEl.value = price;
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
            el.innerHTML = tenants.map(t => `<div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4 space-y-2"><div class="flex flex-wrap items-start justify-between gap-2"><div class="min-w-0"><p class="font-semibold text-white text-sm">${t.name}</p><p class="text-xs text-gray-400 mt-0.5 truncate">${t.property_name || 'Property pending'}${t.unit_number ? ' • ' + t.unit_number : ''}</p>${t.phone ? `<p class="text-xs text-gray-500 mt-0.5">${t.phone}</p>` : ''}</div><div class="flex gap-2 flex-shrink-0"><button onclick="editRoleTenant(${t.id})" class="text-xs text-blue-400 hover:text-blue-300 border border-blue-800 px-2.5 py-1 rounded-lg">Edit</button><button onclick="vacateRoleTenant(${t.id})" class="text-xs text-red-400 hover:text-red-300 border border-red-800 px-2.5 py-1 rounded-lg">Vacate</button></div></div><div class="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs pt-1 border-t border-gray-600/40"><div><p class="text-gray-500">Unit Type</p><p class="text-gray-300 truncate">${t.unit_type_name || '—'}</p></div><div><p class="text-gray-500">Yearly Rent</p><p class="text-emerald-400 font-semibold">${t.monthly_rent ? formatNGN(parseFloat(t.monthly_rent)) : '—'}</p></div><div><p class="text-gray-500">Lease</p><p class="text-gray-400">${t.lease_start || '—'}${t.lease_end ? ' → ' + t.lease_end : ''}</p></div></div></div>`).join('');
        }

        function editRoleTenant(id) {
            const tenant = roleTenantMap[String(id)];
            if (!tenant) return;
            // Find property_id by matching property_name
            const allUnits = window._mgrAllUnits || [];
            const matchedUnit = allUnits.find(u => u.unit_code === tenant.unit_number);
            const propId = matchedUnit ? matchedUnit.property_id : '';
            document.getElementById('mgrTenantEditId').value = tenant.id;
            document.getElementById('mgrTenantName').value = tenant.name || '';
            document.getElementById('mgrTenantEmail').value = tenant.email || '';
            document.getElementById('mgrTenantPhone').value = tenant.phone || '';
            const propSel = document.getElementById('mgrTenantProperty');
            if (propSel) { propSel.value = String(propId); populateManagerUnitSelect(allUnits, propId); }
            const unitSelect = document.getElementById('mgrTenantUnit');
            if (tenant.unit_number && unitSelect && !Array.from(unitSelect.options).some(opt => opt.value === tenant.unit_number)) {
                const option = document.createElement('option');
                option.value = tenant.unit_number;
                option.textContent = `${tenant.unit_number} • occupied`;
                unitSelect.appendChild(option);
            }
            if (unitSelect) unitSelect.value = tenant.unit_number || '';
            document.getElementById('mgrTenantRent').value = tenant.monthly_rent || '';
            // Populate unit type dropdown then set current value
            mgrTnUtRefreshForProperty().then(() => {
                const utSel = document.getElementById('mgrTenantUnitType');
                if (utSel && tenant.unit_type_id) utSel.value = tenant.unit_type_id;
            });
            document.getElementById('mgrTenantLeaseStart').value = tenant.lease_start || '';
            document.getElementById('mgrTenantLeaseEnd').value = tenant.lease_end || '';
            document.getElementById('mgrTenantNotes').value = tenant.notes || '';
            document.getElementById('mgrTenantSubmit').textContent = 'Update Tenant';
            document.getElementById('mgrTenantCancelEdit').classList.remove('hidden');
        }

        let _investorProfiles = [];
        let _activeProfileIdx = 0;

        async function loadInvestorDashboard() {
            try {
                const profiles = await fetchData('/admin/api/my-investment');
                document.getElementById('investorLoading').classList.add('hidden');
                document.getElementById('investorDashboard').classList.add('hidden');
                document.getElementById('investorNoProfile').classList.add('hidden');
                if (!profiles || !profiles.length) {
                    document.getElementById('investorNoProfile').classList.remove('hidden');
                    return;
                }
                _investorProfiles = profiles;
                renderInvestorProfile(_activeProfileIdx);
            } catch (e) {
                document.getElementById('investorLoading').classList.add('hidden');
                document.getElementById('investorNoProfile').classList.remove('hidden');
                console.error('Investor load error', e);
            }
        }

        function switchInvestorProject(idx) {
            _activeProfileIdx = idx;
            document.querySelectorAll('#invProjectTabs button').forEach((btn, i) => {
                btn.className = i === idx
                    ? 'px-4 py-2 rounded-xl text-sm font-semibold bg-emerald-700 text-white'
                    : 'px-4 py-2 rounded-xl text-sm font-medium bg-gray-700 text-gray-300 hover:bg-gray-600';
            });
            renderInvestorProfile(idx);
        }

        function switchInvestorScreen(name) {
            document.querySelectorAll('.inv-screen').forEach(el => {
                el.classList.toggle('hidden', el.getAttribute('data-inv-screen') !== name);
            });
            // Desktop tabs
            document.querySelectorAll('.inv-tab-btn').forEach(btn => {
                const active = btn.getAttribute('data-inv-tab') === name;
                btn.classList.toggle('bg-slate-700', active);
                btn.classList.toggle('text-white', active);
                btn.classList.toggle('text-gray-400', !active);
            });
            // Mobile bottom nav
            document.querySelectorAll('.inv-tab-btn-mobile').forEach(btn => {
                const active = btn.getAttribute('data-inv-tab') === name;
                btn.classList.toggle('text-emerald-400', active);
                btn.classList.toggle('text-gray-500', !active);
            });
            window.scrollTo({top: 0, behavior: 'smooth'});
        }

        function renderInvestorProfile(idx) {
            const profile = _investorProfiles[idx];
            if (!profile) return;
            document.getElementById('investorDashboard').classList.remove('hidden');

            // Property assignment pending banner
            const pendingEl = document.getElementById('invPendingAssignment');
            if (pendingEl) pendingEl.classList.toggle('hidden', !!profile.project_assigned);

            // Project selector
            const selectorEl = document.getElementById('invProjectSelector');
            const tabsEl = document.getElementById('invProjectTabs');
            if (_investorProfiles.length > 1) {
                selectorEl.classList.remove('hidden');
                tabsEl.innerHTML = _investorProfiles.map((p, i) =>
                    `<button onclick="switchInvestorProject(${i})" class="${i === idx ? 'px-4 py-2 rounded-xl text-sm font-semibold bg-emerald-700 text-white' : 'px-4 py-2 rounded-xl text-sm font-medium bg-gray-700 text-gray-300 hover:bg-gray-600'}">${p.project_property_title || 'Investment ' + (i+1)}</button>`
                ).join('');
            } else {
                selectorEl.classList.add('hidden');
            }

                const amount = profile.investment_amount || 0;
                const type = profile.investment_type || 'DEBT';
                const roi = parseFloat(profile.roi_rate) || 3.5;
                const equity = profile.equity_percentage || 0;
                const distributed = profile.total_distributed || 0;
                const termYears = profile.investment_term_years || 4;
                const annualPrincipal = profile.annual_principal_component || 0;
                const annualRoiAmount = profile.annual_roi_amount || 0;
                const payoutSchedule = profile.payout_schedule || [];
                const updates = (profile.construction_updates || []).sort((a, b) => (a.progress_percentage || 0) - (b.progress_percentage || 0));
                const latestProgress = updates.length ? updates[updates.length - 1].progress_percentage : 0;

                // --- Hero card ---
                const displayName = USER_NAME || 'Investor';
                document.getElementById('invWelcomeName').textContent = displayName;
                document.getElementById('invHeroSubtitle').textContent =
                    (profile.project_property_title || 'BrightWave Phase 1') +
                    (profile.investment_date ? ' · Since ' + profile.investment_date : '');

                const badgeText = type === 'DEBT' ? 'DEBT · ' + roi + '% p.a.' : 'EQUITY · ' + equity + '%';
                const badgeClasses = type === 'DEBT'
                    ? 'bg-blue-900/70 text-blue-300 border border-blue-700/50'
                    : 'bg-emerald-900/70 text-emerald-300 border border-emerald-700/50';
                const badgeEl = document.getElementById('invTypeBadge');
                badgeEl.textContent = badgeText;
                badgeEl.className = 'text-xs font-bold px-3 py-1.5 rounded-full flex-shrink-0 ' + badgeClasses;

                document.getElementById('invHeroAmount').textContent = formatNGN(amount);
                document.getElementById('invHeroDistributed').textContent = formatNGN(distributed);
                document.getElementById('invHeroProgress').textContent = latestProgress + '%';

                if (type === 'DEBT') {
                    const netRoiTotal = annualRoiAmount * termYears;
                    document.getElementById('invHeroReturn').textContent = formatNGN(netRoiTotal);
                    document.getElementById('invHeroReturnNote').textContent =
                        formatNGN(annualRoiAmount) + '/yr × ' + termYears + ' yrs · Total payout ' + formatNGN(profile.projected_total_payout || 0) + ' (incl. capital)';
                } else {
                    document.getElementById('invHeroReturn').textContent = equity + '% ownership';
                    document.getElementById('invHeroReturnNote').textContent = 'Proportional to project revenue';
                }

                // --- Next payout banner ---
                const now = new Date();
                const nextPayoutBanner = document.getElementById('invNextPayoutBanner');
                if (nextPayoutBanner && type === 'DEBT' && payoutSchedule.length) {
                    let paid = distributed;
                    const nextUnpaid = payoutSchedule.find(item => {
                        if (paid >= item.total_payout) { paid -= item.total_payout; return false; }
                        return true;
                    });
                    if (nextUnpaid) {
                        nextPayoutBanner.classList.remove('hidden');
                        document.getElementById('invNextPayoutLabel').textContent = 'Year ' + nextUnpaid.year + ' distribution';
                        document.getElementById('invNextPayoutAmount').textContent = formatNGN(nextUnpaid.total_payout);
                        const payDateObj = nextUnpaid.due_date ? new Date(nextUnpaid.due_date) : null;
                        document.getElementById('invNextPayoutDate').textContent = payDateObj
                            ? (now > payDateObj ? 'Overdue: ' : 'Due: ') + payDateObj.toLocaleDateString('en-GB', {day:'numeric', month:'short', year:'numeric'})
                            : 'Due date TBC';
                        if (payDateObj && now > payDateObj) {
                            nextPayoutBanner.className = nextPayoutBanner.className.replace(/emerald/g, 'red').replace(/bg-red-900\/30/,'bg-red-900/30').replace(/border-red-700\/30/,'border-red-700/30');
                        }
                    } else {
                        nextPayoutBanner.classList.add('hidden');
                    }
                } else if (nextPayoutBanner) {
                    nextPayoutBanner.classList.add('hidden');
                }

                // --- Construction progress ---
                renderInvestorMilestones(updates);

                // --- Return schedule ---
                const roiTagEl = document.getElementById('invRoiTag');
                roiTagEl.textContent = type === 'DEBT' ? roi + '% p.a.' : 'Equity · ' + equity + '%';

                const scheduleEl = document.getElementById('invReturnSchedule');
                if (type === 'DEBT' && payoutSchedule.length) {
                    // Track payment status per year using cumulative total_distributed
                    let remaining = distributed;
                    const annotated = payoutSchedule.map(item => {
                        if (remaining >= item.total_payout) {
                            remaining -= item.total_payout;
                            return { ...item, payStatus: 'paid' };
                        } else if (remaining > 0) {
                            const partial = remaining;
                            remaining = 0;
                            return { ...item, payStatus: 'partial', paid_amount: partial };
                        } else {
                            const payDate = item.due_date ? new Date(item.due_date) : null;
                            return { ...item, payStatus: payDate && now > payDate ? 'overdue' : 'scheduled' };
                        }
                    });
                    scheduleEl.innerHTML = annotated.map(item => {
                        const payDate = item.due_date ? new Date(item.due_date) : null;
                        const subline = `${formatNGN(item.principal_component)} principal + ${formatNGN(item.roi_component)} ROI`;
                        const statusMap = {
                            paid:      { label: '✓ Paid',      cls: 'text-emerald-400', bar: 'bg-emerald-500' },
                            partial:   { label: '~ Partial',   cls: 'text-amber-300',   bar: 'bg-amber-500' },
                            overdue:   { label: '! Overdue',   cls: 'text-red-400',     bar: 'bg-red-500' },
                            scheduled: { label: 'Scheduled',   cls: 'text-gray-400',    bar: 'bg-gray-600' },
                        };
                        const s = statusMap[item.payStatus] || statusMap.scheduled;
                        const paidBadge = item.payStatus === 'partial'
                            ? `<p class="text-[11px] text-amber-400/80 mt-0.5">${formatNGN(item.paid_amount)} of ${formatNGN(item.total_payout)} received</p>`
                            : '';
                        return `<div class="flex items-center justify-between gap-3 py-3 border-b border-gray-700/60 last:border-0">
                            <div class="min-w-0 flex-1">
                                <div class="flex items-center gap-2 mb-1">
                                    <span class="w-2 h-2 rounded-full flex-shrink-0 ${s.bar}"></span>
                                    <p class="text-sm text-white font-medium">Year ${item.year} — ${payDate ? payDate.toLocaleDateString('en-GB', {month:'short', year:'numeric'}) : 'TBC'}</p>
                                </div>
                                <p class="text-xs text-gray-400 pl-4">${subline}</p>
                                ${paidBadge ? `<p class="text-[11px] text-amber-400/80 pl-4">${paidBadge}</p>` : ''}
                            </div>
                            <div class="text-right flex-shrink-0">
                                <p class="font-bold text-white text-sm">${formatNGN(item.total_payout)}</p>
                                <span class="text-xs font-semibold ${s.cls}">${s.label}</span>
                            </div>
                        </div>`;
                    }).join('');
                    // Summary bar
                    const totalPayout = profile.projected_total_payout || 0;
                    const paidPct = totalPayout > 0 ? Math.min(100, Math.round(distributed / totalPayout * 100)) : 0;
                    scheduleEl.innerHTML += `<div class="mt-4 pt-3 border-t border-gray-700/60">
                        <div class="flex justify-between text-xs text-gray-400 mb-1.5"><span>Total paid out</span><span>${formatNGN(distributed)} of ${formatNGN(totalPayout)} (${paidPct}%)</span></div>
                        <div class="h-2 bg-gray-700 rounded-full overflow-hidden"><div class="h-full bg-emerald-500 rounded-full transition-all" style="width:${paidPct}%"></div></div>
                    </div>`;
                } else if (type === 'EQUITY') {
                    const rooms = profile.project_property_total_rooms || 0;
                    const pricePerRoom = profile.project_property_price || 0;
                    const constructionStatus = profile.project_property_construction_status || '';
                    const annualGrossRevenue = rooms * pricePerRoom;
                    const yourAnnualRevenue = annualGrossRevenue * (equity / 100);
                    const completionLabel = constructionStatus === 'completed' ? 'Project complete — distributions can begin' : constructionStatus === 'in_progress' ? 'Under construction' : 'Pre-construction';
                    const progressPct = latestProgress;
                    scheduleEl.innerHTML = `
                        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-5">
                            <div class="bg-emerald-900/30 border border-emerald-700/30 rounded-xl p-4 text-center">
                                <p class="text-xs text-emerald-400 uppercase tracking-wide mb-1">Your Equity Stake</p>
                                <p class="text-2xl font-bold text-white">${equity}%</p>
                                <p class="text-xs text-gray-400 mt-0.5">of project ownership</p>
                            </div>
                            <div class="bg-blue-900/30 border border-blue-700/30 rounded-xl p-4 text-center">
                                <p class="text-xs text-blue-400 uppercase tracking-wide mb-1">Est. Annual Revenue</p>
                                <p class="text-2xl font-bold text-white">${annualGrossRevenue > 0 ? formatNGN(yourAnnualRevenue) : '—'}</p>
                                <p class="text-xs text-gray-400 mt-0.5">${annualGrossRevenue > 0 ? 'at full occupancy' : 'Set room price to calculate'}</p>
                            </div>
                            <div class="bg-amber-900/30 border border-amber-700/30 rounded-xl p-4 text-center">
                                <p class="text-xs text-amber-400 uppercase tracking-wide mb-1">Total Received</p>
                                <p class="text-2xl font-bold text-white">${formatNGN(distributed)}</p>
                                <p class="text-xs text-gray-400 mt-0.5">distributed to date</p>
                            </div>
                        </div>
                        ${annualGrossRevenue > 0 ? `<div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-4 mb-4">
                            <p class="text-xs text-gray-400 mb-2 font-medium uppercase tracking-wide">Revenue Projection</p>
                            <div class="space-y-1.5 text-sm">
                                <div class="flex justify-between"><span class="text-gray-400">${rooms} units × ${formatNGN(pricePerRoom)}/yr</span><span class="text-white">${formatNGN(annualGrossRevenue)} gross</span></div>
                                <div class="flex justify-between"><span class="text-gray-400">Your ${equity}% stake</span><span class="text-emerald-400 font-semibold">${formatNGN(yourAnnualRevenue)}/yr</span></div>
                            </div>
                        </div>` : ''}
                        <div class="flex items-start gap-3 text-sm">
                            <div class="w-2 h-2 rounded-full mt-1.5 flex-shrink-0 ${constructionStatus === 'completed' ? 'bg-emerald-400' : 'bg-amber-400'}"></div>
                            <div>
                                <p class="text-white font-medium">${completionLabel}</p>
                                <p class="text-xs text-gray-400 mt-0.5">Construction progress: ${progressPct}% · Equity distributions are proportional to actual revenues and begin after project handover.</p>
                            </div>
                        </div>`;
                } else {
                    scheduleEl.innerHTML = '<p class="text-gray-400 text-sm leading-relaxed">Distribution schedule will be calculated once the CEO sets the expected project completion date.</p>';
                }

                // --- Investment details ---
                const detailsEl = document.getElementById('invDetailsGrid');
                const detailRows = [
                    ['Investment Type', type === 'DEBT' ? 'Debt (Fixed Return)' : 'Equity (Revenue Share)'],
                    ['Amount Invested', formatNGN(amount)],
                    type === 'DEBT' ? ['Annual Return Rate', roi + '% per annum'] : ['Equity Stake', equity + '% ownership'],
                    ['Investment Term', termYears + ' year' + (termYears !== 1 ? 's' : '')],
                    type === 'DEBT' ? ['Annual Principal Return', formatNGN(annualPrincipal)] : null,
                    type === 'DEBT' ? ['Annual ROI Cashflow', formatNGN(annualRoiAmount)] : null,
                    type === 'DEBT' ? ['Projected Total Payout', formatNGN(profile.projected_total_payout || 0)] : null,
                    ['Investment Date', profile.investment_date || 'Pending'],
                    ['Expected Completion', profile.expected_completion_date || 'TBC'],
                    ['Project', profile.project_property_title || 'BrightWave Phase 1 Apartment'],
                    ['Total Paid Out', formatNGN(distributed)],
                ].filter(Boolean);
                detailsEl.innerHTML = detailRows.map(([label, val]) => `
                    <div class="flex items-start justify-between gap-3 py-2.5 border-b border-gray-700/50 last:border-0">
                        <p class="text-xs text-gray-400 leading-tight">${label}</p>
                        <p class="text-sm text-white font-medium text-right leading-tight">${val}</p>
                    </div>`).join('');

                // --- Documents ---
                const docStatusMap = {
                    completed: 'Both parties signed — Agreement on file',
                    pending_ceo_signature: 'Awaiting CEO co-signature',
                    pending_user_signature: 'Awaiting your signature',
                };
                document.getElementById('docStatus').textContent = docStatusMap[CONTRACT_STATUS] || 'Status unknown';
                if (CONTRACT_STATUS === 'completed') document.getElementById('viewAgreementBtn')?.classList.remove('hidden');

        }

        window._mgrTrendPeriod = window._mgrTrendPeriod || '6M';
        function mgrSetPeriod(p) {
            window._mgrTrendPeriod = p;
            _setPeriodBtns('mgrPeriod_', p);
            const full = window._mgrTrendDataFull || { monthly: [], yearly: [] };
            const d = _trendSlice(full.monthly, full.yearly, p);
            if (window._mgrRevChart) { window._mgrRevChart.data.labels = d.labels; window._mgrRevChart.data.datasets[0].data = d.revenue; window._mgrRevChart.data.datasets[1].data = d.capital; window._mgrRevChart.update(); }
        }

        async function loadManagerDashboard() {
            try {
                const [stats, inquiries, props, units, tenants] = await Promise.all([
                    fetchData('/admin/api/stats').catch(() => ({})),
                    fetchData('/admin/api/inquiries').catch(() => []),
                    fetchData('/admin/api/properties').catch(() => []),
                    fetchData('/admin/api/units').catch(() => []),
                    fetchData('/admin/api/tenants').catch(() => [])
                ]);
                countUp('mgr_properties', stats.active_properties || 0, v => Math.round(v));
                countUp('mgr_available_units', stats.available_units || 0, v => Math.round(v));
                const openCount = stats.new_inquiries || 0;
                countUp('mgr_inquiries', openCount, v => Math.round(v));
                countUp('mgr_active_tenants', stats.active_tenants || 0, v => Math.round(v));
                // Financial snapshot
                const mgrRevEl = document.getElementById('mgr_revenue');
                const mgrCapEl = document.getElementById('mgr_capital');
                const mgrNetEl = document.getElementById('mgr_net');
                const mgrNetLbl = document.getElementById('mgr_net_lbl');
                const mgrOccEl = document.getElementById('mgr_occupancy');
                if (mgrRevEl) countUp('mgr_revenue', stats.total_revenue || 0, fmtCompact);
                if (mgrCapEl) countUp('mgr_capital', stats.approved_capital_spent || 0, fmtCompact);
                if (mgrNetEl && mgrNetLbl) {
                    const mgrNet = (stats.total_revenue || 0) - (stats.approved_capital_spent || 0);
                    const mgrNeg = mgrNet < 0;
                    const mgrNetAbs = Math.abs(mgrNet);
                    mgrNetEl.className = 'text-lg sm:text-xl font-bold truncate ' + (mgrNeg ? 'text-red-400' : 'text-emerald-400');
                    mgrNetLbl.textContent = mgrNeg ? 'Net Deficit' : 'Net Surplus';
                    mgrNetLbl.className = 'text-[11px] sm:text-xs uppercase tracking-wide mb-1 truncate ' + (mgrNeg ? 'text-red-400' : 'text-emerald-400');
                    countUp('mgr_net', mgrNetAbs, v => fmtCompact(v) + (mgrNeg ? ' deficit' : ' surplus'));
                }
                if (mgrOccEl) {
                    const totalU = (stats.total_units || 0);
                    const occU = (stats.occupied_units || 0);
                    const pct = totalU > 0 ? Math.round((occU / totalU) * 100) : 0;
                    if (totalU > 0) countUp('mgr_occupancy', pct, v => Math.round(v) + '%'); else mgrOccEl.textContent = '—';
                }
                // Inquiries badge on tab
                const badge = document.getElementById('mgrInquiriesBadge');
                if (badge) { badge.textContent = openCount; badge.classList.toggle('hidden', openCount === 0); }
                // Inquiries pipeline summary
                const mgrPipeline = document.getElementById('mgr_inqPipeline');
                if (mgrPipeline) {
                    const mgrStageCfg = [
                        {key:'new',label:'New',cls:'bg-blue-900/60 text-blue-300 border-blue-700/40'},
                        {key:'contacted',label:'Contacted',cls:'bg-teal-900/60 text-teal-300 border-teal-700/40'},
                        {key:'viewing_scheduled',label:'Viewing',cls:'bg-purple-900/60 text-purple-300 border-purple-700/40'},
                        {key:'offer_made',label:'Offer',cls:'bg-amber-900/60 text-amber-300 border-amber-700/40'},
                        {key:'closed',label:'Closed',cls:'bg-emerald-900/60 text-emerald-300 border-emerald-700/40'},
                        {key:'rejected',label:'Rejected',cls:'bg-red-900/60 text-red-300 border-red-700/40'},
                    ];
                    const mgrCounts = {};
                    (inquiries || []).forEach(i => { mgrCounts[i.status] = (mgrCounts[i.status] || 0) + 1; });
                    mgrPipeline.innerHTML = mgrStageCfg.map(s => {
                        const n = mgrCounts[s.key] || 0;
                        return `<div class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border ${s.cls} text-xs font-medium"><span>${s.label}</span><span class="font-bold">${n}</span></div>`;
                    }).join('');
                }
                // Inquiries table with expandable rows and WhatsApp
                const inqStatuses = ['new','contacted','viewing_scheduled','offer_made','closed','rejected'];
                const safeInquiries = Array.isArray(inquiries) ? inquiries : [];
                document.getElementById('mgr_inquiriesTable').innerHTML = safeInquiries.map(i => `
                    <tr class="border-b border-gray-700/60 cursor-pointer hover:bg-gray-700/20" onclick="mgrToggleInqDetail(${i.id})">
                        <td class="py-2.5 pr-3 font-medium text-sm">${i.full_name || '—'}</td>
                        <td class="py-2.5 pr-3 text-gray-400 text-xs max-w-[120px] truncate">${i.property_title || 'General'}</td>
                        <td class="py-2.5 pr-3 text-xs capitalize">${(i.inquiry_type || 'general').replace(/_/g,' ')}</td>
                        <td class="py-2.5 pr-2">
                            <select onclick="event.stopPropagation()" onchange="updateInquiry(${i.id}, this.value)" class="bg-gray-700 text-white text-xs px-2 py-1 rounded border border-gray-600">
                                ${inqStatuses.map(s => `<option value="${s}" ${i.status === s ? 'selected' : ''}>${s.replace(/_/g,' ')}</option>`).join('')}
                            </select>
                        </td>
                        <td class="py-2.5 text-xs text-gray-500 whitespace-nowrap">${new Date(i.created_at).toLocaleDateString()}</td>
                    </tr>
                    <tr id="mgrInqDetail_${i.id}" class="hidden bg-gray-800/60">
                        <td colspan="5" class="px-3 pb-4 pt-2">
                            <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-xs mb-3">
                                <div><span class="text-gray-500">Phone:</span> <span class="text-gray-300">${i.phone || '—'}</span></div>
                                <div><span class="text-gray-500">Email:</span> <a href="mailto:${i.email}" class="text-blue-400 hover:underline">${i.email || '—'}</a></div>
                                <div><span class="text-gray-500">Budget:</span> <span class="text-gray-300">${i.budget_range || '—'}</span></div>
                                <div><span class="text-gray-500">Move Date:</span> <span class="text-gray-300">${i.preferred_move_date || '—'}</span></div>
                                ${i.message ? `<div class="sm:col-span-2"><span class="text-gray-500">Message:</span> <span class="text-gray-300">${i.message}</span></div>` : ''}
                            </div>
                            ${i.phone ? `<a href="https://wa.me/${relFmtWA(i.phone)}" target="_blank" rel="noopener" class="inline-flex items-center gap-1.5 text-xs bg-green-700 hover:bg-green-600 text-white px-3 py-1.5 rounded-lg font-medium"><svg class="w-3 h-3" viewBox="0 0 24 24" fill="currentColor"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/></svg>WhatsApp</a>` : ''}
                        </td>
                    </tr>`).join('') || '<tr><td colspan="5" class="text-gray-400 py-3 text-center text-sm">No inquiries yet</td></tr>';
                document.getElementById('mgr_propertiesTable').innerHTML = props.map(p => `<tr class="border-b border-gray-700"><td class="py-2 pr-3 font-medium">${p.title}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.property_type}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.location}</td><td class="py-2"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${p.construction_status || p.status}</span></td></tr>`).join('');
                // Units table with status action column
                const phase1Units = Array.isArray(units) ? units : [];
                const mgrUnitsCountEl = document.getElementById('mgrUnitsCount');
                if (mgrUnitsCountEl) mgrUnitsCountEl.textContent = phase1Units.length ? `(${phase1Units.length})` : '';
                const statusClasses = { available: 'bg-emerald-900/50 text-emerald-300', occupied: 'bg-blue-900/50 text-blue-300', reserved: 'bg-amber-900/50 text-amber-300', maintenance: 'bg-red-900/50 text-red-300' };
                const unitTableEl = document.getElementById('mgr_unitsTable');
                if (unitTableEl) unitTableEl.innerHTML = phase1Units.length ? phase1Units.map(u => `
                    <tr class="border-b border-gray-700" id="unitRow_${u.id}">
                        <td class="py-2 pr-2 text-xs text-gray-400 max-w-[120px] truncate">${u.property_title || '—'}</td>
                        <td class="py-2 pr-3 font-medium text-white">${u.unit_code}</td>
                        <td class="py-2 pr-3"><span class="text-xs px-2 py-0.5 rounded-full ${statusClasses[u.status] || 'bg-gray-700 text-gray-300'}">${u.status}</span></td>
                        <td class="py-2 pr-3 text-xs text-gray-300">${u.monthly_rent ? formatNGN(u.monthly_rent) : '—'}</td>
                        <td class="py-2 pr-3 text-xs text-gray-400 max-w-[120px] truncate">${u.notes || '—'}</td>
                        <td class="py-2">
                            <div class="flex items-center gap-2 flex-wrap">
                                <select onchange="updateMgrUnitStatus(${u.id}, this.value)" class="bg-gray-700 text-white text-xs px-2 py-1 rounded border border-gray-600">
                                    ${['available','occupied','reserved','maintenance'].map(s => `<option value="${s}" ${u.status === s ? 'selected' : ''}>${s.charAt(0).toUpperCase()+s.slice(1)}</option>`).join('')}
                                </select>
                                <button type="button" onclick="openUnitEdit(${u.id}, ${u.monthly_rent || 0}, \`${(u.notes || '').replace(/`/g,'')}\`)" class="text-xs text-blue-400 hover:text-blue-300 font-medium">Edit</button>
                            </div>
                        </td>
                    </tr>
                    <tr id="unitEditRow_${u.id}" class="hidden bg-gray-700/30">
                        <td colspan="6" class="py-3 px-2">
                            <div class="flex items-end gap-3 flex-wrap">
                                <div><label class="block text-[11px] text-gray-400 mb-1">Yearly Rent (₦)</label><input type="number" id="unitRent_${u.id}" value="${u.monthly_rent || ''}" placeholder="0" class="w-32 px-2 py-1.5 bg-gray-700 border border-gray-600 rounded-lg text-xs text-white"></div>
                                <div class="flex-1 min-w-[140px]"><label class="block text-[11px] text-gray-400 mb-1">Notes</label><input type="text" id="unitNotes_${u.id}" value="${(u.notes || '').replace(/"/g,'&quot;')}" placeholder="Optional notes" class="w-full px-2 py-1.5 bg-gray-700 border border-gray-600 rounded-lg text-xs text-white"></div>
                                <div class="flex gap-2">
                                    <button type="button" onclick="saveMgrUnitDetails(${u.id})" class="text-xs bg-emerald-700 hover:bg-emerald-600 text-white px-3 py-1.5 rounded-lg font-medium">Save</button>
                                    <button type="button" onclick="closeUnitEdit(${u.id})" class="text-xs bg-gray-600 hover:bg-gray-500 text-white px-3 py-1.5 rounded-lg">Cancel</button>
                                </div>
                            </div>
                        </td>
                    </tr>`).join('') : '<tr><td colspan="6" class="text-gray-500 py-6 text-center text-sm">No units found — add properties and units first</td></tr>';
                window._mgrAllUnits = units;
                populateManagerUnitSelect(phase1Units);
                // Manager charts
                if (typeof Chart !== 'undefined') {
                    window._mgrTrendDataFull = { monthly: stats.monthly_trend || [], yearly: stats.yearly_trend || [] };
                    if (!window._mgrTrendPeriod) window._mgrTrendPeriod = '6M';
                    const _mgrD = _trendSlice(window._mgrTrendDataFull.monthly, window._mgrTrendDataFull.yearly, window._mgrTrendPeriod);
                    _setPeriodBtns('mgrPeriod_', window._mgrTrendPeriod);
                    const mgrRcCtx = document.getElementById('mgrRevenueChart');
                    if (mgrRcCtx) {
                        if (window._mgrRevChart) window._mgrRevChart.destroy();
                        window._mgrRevChart = new Chart(mgrRcCtx, {
                            type: 'bar',
                            data: {
                                labels: _mgrD.labels,
                                datasets: [
                                    { label: 'Revenue', data: _mgrD.revenue, backgroundColor: 'rgba(45,212,191,0.7)', borderColor: '#2dd4bf', borderWidth: 1, borderRadius: 4 },
                                    { label: 'Capital Spent', data: _mgrD.capital, backgroundColor: 'rgba(251,146,60,0.6)', borderColor: '#fb923c', borderWidth: 1, borderRadius: 4 }
                                ]
                            },
                            options: {
                                responsive: true, maintainAspectRatio: false,
                                plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
                                scales: {
                                    x: { ticks: { color: '#64748b', font: { size: 10 }, maxRotation: 45 }, grid: { color: 'rgba(255,255,255,0.04)' } },
                                    y: { ticks: { color: '#64748b', font: { size: 10 }, callback: v => v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? Math.round(v/1e3)+'K' : v }, grid: { color: 'rgba(255,255,255,0.05)' } }
                                }
                            }
                        });
                    }
                    const mgrTs = stats.tenant_status || {};
                    const mgrTdCtx = document.getElementById('mgrTenantDonut');
                    if (mgrTdCtx) {
                        if (window._mgrTenantChart) window._mgrTenantChart.destroy();
                        const mgrTdTotal = (mgrTs.active||0) + (mgrTs.reserved||0) + (mgrTs.vacated||0);
                        window._mgrTenantChart = new Chart(mgrTdCtx, {
                            type: 'doughnut',
                            data: {
                                labels: ['Active', 'Reserved', 'Vacated'],
                                datasets: [{ data: [mgrTs.active||0, mgrTs.reserved||0, mgrTs.vacated||0], backgroundColor: ['rgba(45,212,191,0.8)','rgba(250,204,21,0.8)','rgba(100,116,139,0.7)'], borderWidth: 0, hoverOffset: 4 }]
                            },
                            options: {
                                responsive: true, maintainAspectRatio: false, cutout: '65%',
                                plugins: {
                                    legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 11 }, padding: 10, boxWidth: 10 } },
                                    tooltip: { callbacks: { label: ctx => ctx.label + ': ' + ctx.raw + (mgrTdTotal ? ' (' + Math.round(ctx.raw/mgrTdTotal*100) + '%)' : '') } }
                                }
                            }
                        });
                    }
                }
                // Populate property dropdown in tenant form
                const mgrPropSel = document.getElementById('mgrTenantProperty');
                if (mgrPropSel) {
                    const curPropVal = mgrPropSel.value;
                    mgrPropSel.innerHTML = '<option value="">Select property</option>' + props.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                    if (curPropVal) mgrPropSel.value = curPropVal;
                }
                // Populate inquiry form property dropdown
                const mgrInqPropSel = document.getElementById('mgrInqProperty');
                if (mgrInqPropSel) mgrInqPropSel.innerHTML = '<option value="">General</option>' + props.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                const activeMgrTenants = tenants.filter(t => t.status === 'active');
                renderTenantCards('mgr_tenantsList', activeMgrTenants);
                const mgrRentTotal = activeMgrTenants.reduce((s, t) => s + (parseFloat(t.monthly_rent) || 0), 0);
                const mgrRentEl = document.getElementById('mgr_tenantRentTotal');
                if (mgrRentEl) mgrRentEl.textContent = activeMgrTenants.length > 0 ? formatNGN(mgrRentTotal) + '/yr expected' : '';
            } catch (e) { console.error('Manager dashboard error', e); }
        }

        async function accFilterExpenses() {
            try {
                const filters = getExpenseFilters('acc');
                const expensesData = await fetchData('/admin/api/project-expenses' + buildExpenseQuery(filters.propertyId, filters));
                window.expenseCache.acc = expensesData.expenses || [];
                window.expPage.acc = 1;
                renderExpensePage('acc');
                const approvalTotals = expensesData.approval_totals || {};
                const catEntries = Object.entries(expensesData.by_category || {}).sort((a,b) => b[1]-a[1]).slice(0,4);
                document.getElementById('accExpenseBreakdown').innerHTML = [`Approved: ${formatNGN(approvalTotals.approved || 0)}`, `Pending: ${formatNGN(approvalTotals.pending || 0)}`, `Rejected: ${formatNGN(approvalTotals.rejected || 0)}`].join('<br>') + (catEntries.length ? '<br>' + catEntries.map(([c,a]) => `${c}: ${formatNGN(a)}`).join('<br>') : '');
                // Recalculate cash flow — capital comes from filtered data when project selected
                const lastStats = window._accLastStats || {};
                const cfRevenue = lastStats.total_revenue || 0;
                const cfExpenses = filters.propertyId ? (approvalTotals.approved || 0) : (lastStats.approved_capital_spent || 0);
                const cfNet = cfRevenue - cfExpenses;
                const cfNeg = cfNet < 0;
                document.getElementById('cf_revenue').textContent = fmtCompact(cfRevenue);
                document.getElementById('cf_expenses').textContent = fmtCompact(cfExpenses);
                document.getElementById('cf_net').textContent = fmtCompact(Math.abs(cfNet));
                document.getElementById('cf_net').className = 'text-sm font-bold truncate ' + (cfNeg ? 'text-red-400' : 'text-emerald-400');
                document.getElementById('cf_net_label').textContent = cfNeg ? 'Net Deficit' : 'Net Surplus';
                document.getElementById('cf_net_label').className = 'text-[10px] uppercase tracking-wide mb-0.5 truncate ' + (cfNeg ? 'text-red-400' : 'text-emerald-400');
                document.getElementById('cf_net_card').className = (cfNeg ? 'bg-red-900/40 border border-red-700/30' : 'bg-emerald-900/40 border border-emerald-700/30') + ' rounded-xl p-3 overflow-hidden flex items-start gap-2.5';
                const subEl = document.getElementById('cf_subtitle');
                if (subEl) {
                    const propSel = document.getElementById('accExpensePropertyFilter');
                    const propName = propSel && propSel.value ? propSel.options[propSel.selectedIndex]?.text : null;
                    subEl.textContent = propName ? propName + ' · approved expenses' : 'All time · approved expenses';
                }
                // Update category donut live
                if (typeof Chart !== 'undefined' && window._accCatChart) {
                    const allCat = Object.entries(expensesData.by_category || {}).sort((a,b) => b[1]-a[1]).slice(0,6);
                    if (allCat.length) {
                        window._accCatChart.data.labels = allCat.map(([k]) => k.charAt(0).toUpperCase() + k.slice(1));
                        window._accCatChart.data.datasets[0].data = allCat.map(([,v]) => v);
                        window._accCatChart.update();
                    }
                }
            } catch(e) { console.error('accFilterExpenses error', e); }
        }

        window._accTrendPeriod = window._accTrendPeriod || '6M';
        function accSetPeriod(p) {
            window._accTrendPeriod = p;
            _setPeriodBtns('accPeriod_', p);
            const full = window._accTrendDataFull || { monthly: [], yearly: [] };
            const d = _trendSlice(full.monthly, full.yearly, p);
            if (window._accRevChart) { window._accRevChart.data.labels = d.labels; window._accRevChart.data.datasets[0].data = d.revenue; window._accRevChart.data.datasets[1].data = d.capital; window._accRevChart.update(); }
        }

        async function loadAccountantDashboard() {
            try {
                const expenseFilters = getExpenseFilters('acc');
                const [stats, payments, tenants, props, expensesData] = await Promise.all([fetchData('/admin/api/stats'), fetchData('/admin/api/payments'), fetchData('/admin/api/tenants?status=active'), fetchData('/admin/api/properties'), fetchData('/admin/api/project-expenses' + buildExpenseQuery(expenseFilters.propertyId, expenseFilters))]);
                countUp('acc_total_revenue', stats.total_revenue || 0, fmtCompact);
                countUp('acc_monthly_revenue', stats.monthly_revenue || 0, fmtCompact);
                countUp('acc_tenants', stats.active_tenants || 0, v => Math.round(v));
                countUp('acc_capital_spent', stats.approved_capital_spent || 0, fmtCompact);
                countUp('acc_monthly_capital', stats.monthly_capital_spent || 0, fmtCompact);
                const budgetVal = stats.capital_budget_remaining || 0;
                const isOverBudget = budgetVal < 0;
                const budgetAbs = Math.abs(budgetVal);
                document.getElementById('acc_budget_remaining').className = `text-xl sm:text-2xl font-bold truncate${isOverBudget ? ' text-red-400' : ''}`;
                document.getElementById('acc_budget_label').textContent = isOverBudget ? 'Over Budget' : 'Budget Remaining';
                document.getElementById('acc_budget_card').className = `${isOverBudget ? 'bg-red-900/30 border border-red-700/40' : 'bg-slate-800/80 border border-slate-700/40'} rounded-xl p-3 sm:p-4 overflow-hidden col-span-2 md:col-span-1 flex items-start gap-3`;
                countUp('acc_budget_remaining', budgetAbs, v => fmtCompact(v) + (isOverBudget ? ' over' : ' left'));
                // Trend arrows on monthly cards
                const accTrend = stats.monthly_trend || [];
                if (accTrend.length > 1) {
                    const curR = accTrend[accTrend.length - 1].revenue, prevR = accTrend[accTrend.length - 2].revenue;
                    const revTrendEl = document.getElementById('acc_rev_trend');
                    if (revTrendEl && prevR > 0) {
                        const rPct = ((curR - prevR) / prevR * 100).toFixed(1);
                        const rUp = curR >= prevR;
                        revTrendEl.textContent = (rUp ? '\u2191' : '\u2193') + ' ' + Math.abs(rPct) + '% vs last month';
                        revTrendEl.className = 'text-[10px] mt-0.5 truncate ' + (rUp ? 'text-emerald-400' : 'text-red-400');
                    }
                    const curC = accTrend[accTrend.length - 1].capital, prevC = accTrend[accTrend.length - 2].capital;
                    const capTrendEl = document.getElementById('acc_cap_trend');
                    if (capTrendEl && prevC > 0) {
                        const cPct = ((curC - prevC) / prevC * 100).toFixed(1);
                        const cDown = curC <= prevC;
                        capTrendEl.textContent = (curC >= prevC ? '\u2191' : '\u2193') + ' ' + Math.abs(cPct) + '% vs last month';
                        capTrendEl.className = 'text-[10px] mt-0.5 truncate ' + (cDown ? 'text-emerald-400' : 'text-amber-400');
                    }
                }
                accountantPaymentsCache = payments;
                renderAccountantPayments();
                // Cash flow summary
                const cfRevenue = stats.total_revenue || 0;
                const cfExpenses = stats.approved_capital_spent || 0;
                const cfNet = cfRevenue - cfExpenses;
                const cfNeg = cfNet < 0;
                document.getElementById('cf_revenue').textContent = fmtCompact(cfRevenue);
                document.getElementById('cf_expenses').textContent = fmtCompact(cfExpenses);
                window._accLastStats = stats;
                document.getElementById('cf_net').textContent = fmtCompact(Math.abs(cfNet));
                document.getElementById('cf_net').className = 'text-sm font-bold truncate ' + (cfNeg ? 'text-red-400' : 'text-emerald-400');
                document.getElementById('cf_net_label').textContent = cfNeg ? 'Net Deficit' : 'Net Surplus';
                document.getElementById('cf_net_label').className = 'text-[10px] uppercase tracking-wide mb-0.5 truncate ' + (cfNeg ? 'text-red-400' : 'text-emerald-400');
                document.getElementById('cf_net_card').className = (cfNeg ? 'bg-red-900/40 border border-red-700/30' : 'bg-emerald-900/40 border border-emerald-700/30') + ' rounded-xl p-3 overflow-hidden flex items-start gap-2.5';
                // Revenue vs spend chart (Chart.js)
                if (typeof Chart !== 'undefined') {
                    window._accTrendDataFull = { monthly: stats.monthly_trend || [], yearly: stats.yearly_trend || [] };
                    if (!window._accTrendPeriod) window._accTrendPeriod = '6M';
                    const _accD = _trendSlice(window._accTrendDataFull.monthly, window._accTrendDataFull.yearly, window._accTrendPeriod);
                    _setPeriodBtns('accPeriod_', window._accTrendPeriod);
                    const accRcCtx = document.getElementById('accRevenueChart');
                    if (accRcCtx) {
                        if (window._accRevChart) window._accRevChart.destroy();
                        window._accRevChart = new Chart(accRcCtx, {
                            type: 'bar',
                            data: {
                                labels: _accD.labels,
                                datasets: [
                                    { label: 'Revenue', data: _accD.revenue, backgroundColor: 'rgba(45,212,191,0.7)', borderColor: '#2dd4bf', borderWidth: 1, borderRadius: 4 },
                                    { label: 'Capital Spent', data: _accD.capital, backgroundColor: 'rgba(251,146,60,0.6)', borderColor: '#fb923c', borderWidth: 1, borderRadius: 4 }
                                ]
                            },
                            options: {
                                responsive: true, maintainAspectRatio: false,
                                plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
                                scales: {
                                    x: { ticks: { color: '#64748b', font: { size: 10 }, maxRotation: 45 }, grid: { color: 'rgba(255,255,255,0.04)' } },
                                    y: { ticks: { color: '#64748b', font: { size: 10 }, callback: v => v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? Math.round(v/1e3)+'K' : v }, grid: { color: 'rgba(255,255,255,0.05)' } }
                                }
                            }
                        });
                    }
                    // Expense category donut
                    const accCatCtx = document.getElementById('accCategoryDonut');
                    if (accCatCtx) {
                        if (window._accCatChart) window._accCatChart.destroy();
                        const catEntries = Object.entries(expensesData.by_category || {}).sort((a,b) => b[1]-a[1]).slice(0,6);
                        if (catEntries.length) {
                            const catColors = ['rgba(251,146,60,0.85)','rgba(45,212,191,0.85)','rgba(99,102,241,0.85)','rgba(250,204,21,0.85)','rgba(248,113,113,0.85)','rgba(156,163,175,0.75)'];
                            window._accCatChart = new Chart(accCatCtx, {
                                type: 'doughnut',
                                data: {
                                    labels: catEntries.map(([k]) => k.charAt(0).toUpperCase() + k.slice(1)),
                                    datasets: [{ data: catEntries.map(([,v]) => v), backgroundColor: catColors, borderWidth: 0, hoverOffset: 4 }]
                                },
                                options: {
                                    responsive: true, maintainAspectRatio: false, cutout: '60%',
                                    plugins: {
                                        legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 10 }, padding: 8, boxWidth: 10 } },
                                        tooltip: { callbacks: { label: ctx => ctx.label + ': ' + fmtCompact(ctx.raw) } }
                                    }
                                }
                            });
                        } else {
                            accCatCtx.parentElement.innerHTML = '<p class="text-center text-gray-500 text-xs pt-8">No expense data yet</p>';
                        }
                    }
                }
                const accTotalU = stats.total_units || 0, accOccU = stats.occupied_units || 0;
                const accOccPct = accTotalU > 0 ? Math.round(accOccU / accTotalU * 100) : 0;
                document.getElementById('accUnitsSummary').innerHTML = `
                    <div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-3 overflow-hidden flex items-start gap-2.5">
                        <div class="w-7 h-7 bg-emerald-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-door-open text-emerald-400 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[10px] text-gray-500 uppercase tracking-wide truncate">Available</p><p class="text-xl font-bold text-emerald-400">${stats.available_units || 0}</p></div>
                    </div>
                    <div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-3 overflow-hidden flex items-start gap-2.5">
                        <div class="w-7 h-7 bg-blue-900/60 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-key text-blue-400 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[10px] text-gray-500 uppercase tracking-wide truncate">Occupied</p><p class="text-xl font-bold text-blue-400">${accOccU}</p></div>
                    </div>
                    <div class="bg-gray-700/40 border border-gray-600/50 rounded-xl p-3 overflow-hidden flex items-start gap-2.5">
                        <div class="w-7 h-7 bg-gray-600 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5"><i class="fas fa-chart-pie text-gray-300 text-xs"></i></div>
                        <div class="min-w-0"><p class="text-[10px] text-gray-500 uppercase tracking-wide truncate">Occupancy</p><p class="text-xl font-bold text-white">${accOccPct}%</p><p class="text-[10px] text-gray-500 mt-0.5">${accOccU}/${accTotalU} units</p></div>
                    </div>`;
                const tenantSelect = document.getElementById('accPaymentTenant');
                const expensePropertySelect = document.getElementById('accExpensePropertyFilter');
                if (tenantSelect) tenantSelect.innerHTML = '<option value="">Select tenant</option>' + tenants.map(t => `<option value="${t.id}">${t.name}${t.unit_number ? ' • ' + t.unit_number : ''}</option>`).join('');
                if (expensePropertySelect) expensePropertySelect.innerHTML = '<option value="">All projects</option>' + props.map(p => `<option value="${p.id}" ${String(expenseFilters.propertyId || '') === String(p.id) ? 'selected' : ''}>${p.title}</option>`).join('');
                const expenseCategorySummary = Object.entries(expensesData.by_category || {})
                    .sort((a, b) => b[1] - a[1])
                    .slice(0, 4)
                    .map(([category, amount]) => `${category}: ${formatNGN(amount)}`)
                    .join('<br>');
                const approvalTotals = expensesData.approval_totals || {};
                const approvalSummary = [`Approved: ${formatNGN(approvalTotals.approved || 0)}`, `Pending: ${formatNGN(approvalTotals.pending || 0)}`, `Rejected: ${formatNGN(approvalTotals.rejected || 0)}`].join('<br>');
                document.getElementById('accExpenseBreakdown').innerHTML = `${approvalSummary}${expenseCategorySummary ? '<br>' + expenseCategorySummary : ''}`;
                window.expenseCache.acc = expensesData.expenses || [];
                window.expPage.acc = 1;
                renderExpensePage('acc');
                // Rent Roll
                const rrBody = document.getElementById('accRentRollBody');
                const rrTotals = document.getElementById('accRentRollTotals');
                const activeTenants = tenants.filter(t => t.status === 'active');
                if (rrBody) {
                    if (!activeTenants.length) {
                        rrBody.innerHTML = '<tr><td colspan="6" class="text-center text-gray-500 py-6 text-sm">No active tenants yet</td></tr>';
                    } else {
                        const totalExpectedAnnual = activeTenants.reduce((s, t) => s + (parseFloat(t.monthly_rent) || 0), 0);
                        rrBody.innerHTML = activeTenants.map(t => {
                            const leaseEndDate = t.lease_end ? new Date(t.lease_end) : null;
                            const isExpiringSoon = leaseEndDate && (leaseEndDate - new Date()) < 60 * 24 * 60 * 60 * 1000;
                            return `<tr class="border-b border-gray-700/60">
                                <td class="py-2.5 pr-3 font-medium text-white text-sm">${t.name}</td>
                                <td class="py-2.5 pr-3 text-xs text-gray-400">${t.unit_type_name || '—'}</td>
                                <td class="py-2.5 pr-3 text-xs text-gray-400">${t.unit_number || '—'}</td>
                                <td class="py-2.5 pr-3 text-sm text-emerald-300 font-medium">${t.monthly_rent ? formatNGN(parseFloat(t.monthly_rent)) : '—'}</td>
                                <td class="py-2.5 pr-3 text-xs text-gray-400">${t.lease_start || '—'}</td>
                                <td class="py-2.5 text-xs ${isExpiringSoon ? 'text-amber-400 font-medium' : 'text-gray-400'}">${t.lease_end || '—'}${isExpiringSoon ? ' ⚠' : ''}</td>
                            </tr>`;
                        }).join('');
                        if (rrTotals) rrTotals.innerHTML = `${activeTenants.length} active tenant${activeTenants.length !== 1 ? 's' : ''} · <span class="text-emerald-400 font-semibold">${formatNGN(totalExpectedAnnual)}/yr</span> expected`;
                    }
                }
            } catch (e) {
                document.getElementById('acc_paymentsContainer').innerHTML = '<p class="text-red-400 py-4 text-sm">Error loading financial data</p>';
                const expenseList = document.getElementById('accExpenseList');
                const expenseBreakdown = document.getElementById('accExpenseBreakdown');
                if (expenseList) expenseList.innerHTML = '<p class="text-red-400 py-4 text-sm">Error loading project expenses</p>';
                if (expenseBreakdown) expenseBreakdown.textContent = 'Unavailable';
            }
        }

        function relFmtWA(phone) {
            if (!phone) return '';
            const d = phone.replace(/\D/g, '');
            if (d.startsWith('234')) return d;
            if (d.startsWith('0')) return '234' + d.slice(1);
            return d;
        }

        async function loadRealtorDashboard() {
            try {
                const [stats, props, inquiries, units] = await Promise.all([fetchData('/admin/api/stats'), fetchData('/admin/api/properties'), fetchData('/admin/api/inquiries'), fetchData('/admin/api/units')]);
                countUp('rel_properties', stats.active_properties || 0, v => Math.round(v));
                countUp('rel_available_units', stats.available_units || 0, v => Math.round(v));
                countUp('rel_inquiries', stats.new_inquiries || 0, v => Math.round(v));
                countUp('rel_new', stats.total_inquiries || 0, v => Math.round(v));
                // Pipeline — horizontal bars + donut + conversion rate
                const stageCfg = [
                    {key:'new', label:'New', bar:'bg-blue-500', txt:'text-blue-300', col:'rgba(59,130,246,0.85)'},
                    {key:'contacted', label:'Contacted', bar:'bg-teal-500', txt:'text-teal-300', col:'rgba(45,212,191,0.85)'},
                    {key:'viewing_scheduled', label:'Viewing', bar:'bg-purple-500', txt:'text-purple-300', col:'rgba(168,85,247,0.85)'},
                    {key:'offer_made', label:'Offer', bar:'bg-amber-500', txt:'text-amber-300', col:'rgba(251,146,60,0.85)'},
                    {key:'closed', label:'Closed', bar:'bg-emerald-500', txt:'text-emerald-300', col:'rgba(52,211,153,0.85)'},
                    {key:'rejected', label:'Rejected', bar:'bg-red-500', txt:'text-red-400', col:'rgba(248,113,113,0.85)'},
                ];
                const counts = {};
                (inquiries || []).forEach(i => { counts[i.status] = (counts[i.status] || 0) + 1; });
                const relTotal = Object.values(counts).reduce((s, n) => s + n, 0);
                const convEl = document.getElementById('rel_conversionRate');
                if (convEl) convEl.textContent = relTotal > 0 ? Math.round((counts['closed']||0) / relTotal * 100) + '% conversion' : '';
                const barsEl = document.getElementById('rel_pipelineBars');
                if (barsEl) {
                    barsEl.innerHTML = relTotal ? stageCfg.map(s => {
                        const n = counts[s.key] || 0;
                        const pct = relTotal > 0 ? Math.round(n / relTotal * 100) : 0;
                        return `<div><div class="flex justify-between text-xs mb-1"><span class="${s.txt} font-medium">${s.label}</span><span class="text-gray-400">${n}</span></div><div class="h-1.5 bg-gray-700 rounded-full overflow-hidden"><div class="${s.bar} h-full rounded-full" style="width:${pct}%"></div></div></div>`;
                    }).join('') : '<p class="text-gray-500 text-xs py-4 text-center">No leads yet</p>';
                }
                if (typeof Chart !== 'undefined') {
                    const relDonutCtx = document.getElementById('relPipelineDonut');
                    if (relDonutCtx) {
                        if (window._relPipeChart) window._relPipeChart.destroy();
                        const donutData = stageCfg.map(s => counts[s.key] || 0);
                        if (donutData.some(v => v > 0)) {
                            window._relPipeChart = new Chart(relDonutCtx, {
                                type: 'doughnut',
                                data: { labels: stageCfg.map(s => s.label), datasets: [{ data: donutData, backgroundColor: stageCfg.map(s => s.col), borderWidth: 0, hoverOffset: 4 }] },
                                options: {
                                    responsive: true, maintainAspectRatio: false, cutout: '62%',
                                    plugins: {
                                        legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 10 }, padding: 8, boxWidth: 10 } },
                                        tooltip: { callbacks: { label: ctx => ctx.label + ': ' + ctx.raw + (relTotal ? ' (' + Math.round(ctx.raw/relTotal*100) + '%)' : '') } }
                                    }
                                }
                            });
                        } else {
                            relDonutCtx.parentElement.innerHTML = '<p class="text-center text-gray-500 text-xs pt-10">No lead data yet</p>';
                        }
                    }
                }
                // Commission tracker
                const priceMap = {};
                (props || []).forEach(p => { if (p.title) priceMap[p.title] = parseFloat(p.price) || 0; });
                const closedInquiries = (inquiries || []).filter(i => i.status === 'closed');
                const saleComm = closedInquiries.reduce((sum, i) => sum + (priceMap[i.property_title] || 0) * 0.10, 0);
                const rentedUnits = (units || []).filter(u => u.status === 'occupied' && u.monthly_rent);
                const rentalComm = rentedUnits.reduce((sum, u) => sum + (parseFloat(u.monthly_rent) || 0) * 0.10, 0);
                const totalComm = saleComm + rentalComm;
                const relRC = document.getElementById('rel_rental_comm');
                const relSC = document.getElementById('rel_sale_comm');
                const relTC = document.getElementById('rel_total_comm');
                const relCD = document.getElementById('rel_comm_detail');
                if (relRC) relRC.textContent = formatNGN(rentalComm);
                if (relSC) relSC.textContent = formatNGN(saleComm);
                if (relTC) relTC.textContent = formatNGN(totalComm);
                if (relCD) {
                    const rows = [
                        ...closedInquiries.filter(i => priceMap[i.property_title]).map(i => `<div class="flex items-center justify-between text-xs py-1.5 border-b border-gray-700/50"><span class="text-gray-300">${i.full_name} · <span class="text-gray-500">${i.property_title}</span></span><span class="text-blue-300 font-medium">${formatNGN(priceMap[i.property_title] * 0.10)} <span class="text-gray-500">sale</span></span></div>`),
                        ...rentedUnits.map(u => `<div class="flex items-center justify-between text-xs py-1.5 border-b border-gray-700/50"><span class="text-gray-300">${u.unit_code} · <span class="text-gray-500">${u.property_title || ''}</span></span><span class="text-emerald-300 font-medium">${formatNGN(parseFloat(u.monthly_rent) * 0.10)} <span class="text-gray-500">rent</span></span></div>`),
                    ];
                    relCD.innerHTML = rows.length ? rows.join('') : '<p class="text-xs text-gray-500 py-2">No closed deals or occupied units yet — commission will appear here once leases and sales are recorded.</p>';
                }
                _relLeadsCache = inquiries || [];
                // Populate property dropdown in Add Lead form
                const relPropSel = document.getElementById('relLeadProperty');
                if (relPropSel) relPropSel.innerHTML = '<option value="">General Inquiry</option>' + props.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                const relCountEl = document.getElementById('rel_leadsCount');
                if (relCountEl) relCountEl.textContent = inquiries.length + ' lead' + (inquiries.length !== 1 ? 's' : '');
                renderUnitsTable('rel_unitsTable', units.filter(unit => unit.status === 'available' || unit.status === 'reserved'), true);
                document.getElementById('rel_propertiesTable').innerHTML = props.map(p => `<tr class="border-b border-gray-700"><td class="py-2 pr-3 font-medium">${p.title}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.property_type === 'hostel' ? 'Apartment' : p.property_type}</td><td class="py-2 pr-3 text-xs text-gray-400">${p.location}</td><td class="py-2 pr-3 text-xs">${p.price ? formatNGN(p.price) : (p.price_type || 'Contact')}</td><td class="py-2"><span class="text-xs px-2 py-0.5 rounded bg-gray-700">${p.construction_status || p.status}</span></td></tr>`).join('');
                const statuses = ['new','contacted','viewing_scheduled','offer_made','closed','rejected'];
                const statusColors = {new:'bg-blue-900/50 text-blue-300',contacted:'bg-teal-900/50 text-teal-300',viewing_scheduled:'bg-purple-900/50 text-purple-300',offer_made:'bg-amber-900/50 text-amber-300',closed:'bg-emerald-900/50 text-emerald-300',rejected:'bg-red-900/50 text-red-300'};
                document.getElementById('rel_inquiriesTable').innerHTML = inquiries.length ? inquiries.slice(0, 60).map(i => `
                    <tr class="border-b border-gray-700/60 cursor-pointer hover:bg-gray-700/20" onclick="relToggleDetail(${i.id})">
                        <td class="py-2.5 pr-3 font-medium text-sm">${i.full_name || '—'}</td>
                        <td class="py-2.5 pr-3 text-gray-400 text-xs max-w-[120px] truncate">${i.property_title || 'General'}</td>
                        <td class="py-2.5 pr-3 text-xs capitalize">${(i.inquiry_type || 'general').replace(/_/g,' ')}</td>
                        <td class="py-2.5 pr-2">
                            <select onclick="event.stopPropagation()" onchange="relUpdateInquiryStatus(${i.id}, this.value)" class="text-xs bg-gray-700 border border-gray-600 rounded px-2 py-1 text-white">
                                ${statuses.map(s => `<option value="${s}"${s === i.status ? ' selected' : ''}>${s.replace(/_/g,' ')}</option>`).join('')}
                            </select>
                        </td>
                        <td class="py-2.5 text-xs text-gray-500">${new Date(i.created_at).toLocaleDateString()}</td>
                    </tr>
                    <tr id="relDetail_${i.id}" class="hidden bg-gray-800/60">
                        <td colspan="5" class="px-3 pb-4 pt-2">
                            <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-xs mb-3">
                                <div><span class="text-gray-500">Phone:</span> <span class="text-gray-300">${i.phone || '—'}</span></div>
                                <div><span class="text-gray-500">Email:</span> ${i.email && i.email !== 'manual@entry.local' ? `<a href="mailto:${i.email}" class="text-blue-400 hover:underline">${i.email}</a>` : '<span class="text-gray-500">—</span>'}</div>
                                <div><span class="text-gray-500">Budget:</span> <span class="text-gray-300">${i.budget_range || '—'}</span></div>
                                <div><span class="text-gray-500">Move Date:</span> <span class="text-gray-300">${i.preferred_move_date || '—'}</span></div>
                                ${i.message && i.message !== 'Manually added by staff' ? `<div class="sm:col-span-2"><span class="text-gray-500">Message:</span> <span class="text-gray-300">${i.message}</span></div>` : ''}
                            </div>
                            <div class="flex items-end gap-3">
                                <div class="flex-1"><label class="block text-[11px] text-gray-500 mb-1">Internal Notes</label><textarea id="relNote_${i.id}" rows="2" class="w-full px-2 py-1.5 bg-gray-700 border border-gray-600 rounded-lg text-xs text-white resize-none" placeholder="Add notes about this lead...">${i.inquiry_notes || ''}</textarea></div>
                                <div class="flex flex-col gap-1.5 flex-shrink-0">
                                    <button type="button" onclick="relSaveNotes(${i.id})" class="text-xs bg-emerald-700 hover:bg-emerald-600 text-white px-3 py-1.5 rounded-lg font-medium">Save Note</button>
                                    <button type="button" onclick="event.stopPropagation();relEditLead(_relLeadsCache.find(x=>x.id===${i.id}))" class="text-xs bg-blue-700 hover:bg-blue-600 text-white px-3 py-1.5 rounded-lg font-medium">Edit</button>
                                    <button type="button" onclick="event.stopPropagation();relDeleteLead(${i.id})" class="text-xs bg-red-800 hover:bg-red-700 text-white px-3 py-1.5 rounded-lg font-medium">Delete</button>
                                    ${i.phone ? `<a href="https://wa.me/${relFmtWA(i.phone)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="text-xs bg-green-700 hover:bg-green-600 text-white px-3 py-1.5 rounded-lg font-medium text-center flex items-center gap-1 justify-center"><svg class="w-3 h-3" viewBox="0 0 24 24" fill="currentColor"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/></svg>WhatsApp</a>` : ''}
                                </div>
                            </div>
                        </td>
                    </tr>`).join('') : '<tr><td colspan="5" class="py-10 text-center"><p class="text-gray-300 text-sm font-medium mb-1">No leads yet</p><p class="text-gray-500 text-xs">Click "+ Add Lead" above to log your first lead manually, or wait for website inquiries.</p></td></tr>';
            } catch (e) {
                console.error('Realtor dashboard error:', e);
                const tbl = document.getElementById('rel_inquiriesTable');
                if (tbl) tbl.innerHTML = '<tr><td colspan="5" class="text-red-400 py-3 text-center text-sm">Error loading data</td></tr>';
            }
        }

        async function relUpdateInquiryStatus(id, status) {
            try {
                await fetchData('/admin/api/inquiries/' + id, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({status}) });
                await loadRealtorDashboard();
            } catch (e) {
                alert('Failed to update status: ' + e.message);
            }
        }

        function relToggleDetail(id) {
            const row = document.getElementById('relDetail_' + id);
            if (!row) return;
            const isHidden = row.classList.contains('hidden');
            document.querySelectorAll('[id^="relDetail_"]').forEach(r => r.classList.add('hidden'));
            if (isHidden) row.classList.remove('hidden');
        }

        function mgrToggleInqDetail(id) {
            const row = document.getElementById('mgrInqDetail_' + id);
            if (!row) return;
            const isHidden = row.classList.contains('hidden');
            document.querySelectorAll('[id^="mgrInqDetail_"]').forEach(r => r.classList.add('hidden'));
            if (isHidden) row.classList.remove('hidden');
        }

        async function relSaveNotes(id) {
            const notes = document.getElementById('relNote_' + id)?.value || '';
            try {
                await fetchData('/admin/api/inquiries/' + id, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ inquiry_notes: notes }) });
                const btn = document.querySelector(`button[onclick="relSaveNotes(${id})"]`);
                if (btn) { btn.textContent = 'Saved ✓'; btn.classList.add('bg-emerald-600'); setTimeout(() => { btn.textContent = 'Save Note'; btn.classList.remove('bg-emerald-600'); }, 2000); }
            } catch (e) { alert('Failed to save note: ' + e.message); }
        }

        let _relLeadFormOpen = false;
        let _relLeadsCache = [];

        function relToggleLeadForm() {
            _relLeadFormOpen = !_relLeadFormOpen;
            const body = document.getElementById('relLeadFormBody');
            const btn = document.getElementById('relLeadFormToggle');
            if (body) body.classList.toggle('hidden', !_relLeadFormOpen);
            if (btn) btn.textContent = _relLeadFormOpen ? '✕ Close' : '+ Add Lead';
        }

        function relCancelLeadEdit() {
            document.getElementById('relLeadEditId').value = '';
            document.getElementById('relLeadForm').reset();
            document.getElementById('relLeadSubmitBtn').textContent = 'Save Lead';
            document.getElementById('relLeadCancelBtn').classList.add('hidden');
            document.getElementById('relLeadFormTitle').textContent = 'Add New Lead';
            document.getElementById('relLeadMsg').textContent = '';
        }

        function relEditLead(lead) {
            if (!_relLeadFormOpen) relToggleLeadForm();
            document.getElementById('relLeadEditId').value = lead.id;
            document.getElementById('relLeadName').value = lead.full_name || '';
            document.getElementById('relLeadPhone').value = lead.phone || '';
            document.getElementById('relLeadEmail').value = lead.email === 'manual@entry.local' ? '' : (lead.email || '');
            document.getElementById('relLeadProperty').value = lead.property_id || '';
            document.getElementById('relLeadType').value = lead.inquiry_type || 'general';
            document.getElementById('relLeadStatus').value = lead.status || 'new';
            document.getElementById('relLeadBudget').value = lead.budget_range || '';
            document.getElementById('relLeadMoveDate').value = lead.preferred_move_date || '';
            document.getElementById('relLeadNotes').value = lead.inquiry_notes || '';
            document.getElementById('relLeadSubmitBtn').textContent = 'Update Lead';
            document.getElementById('relLeadCancelBtn').classList.remove('hidden');
            document.getElementById('relLeadFormTitle').textContent = 'Edit Lead';
            document.getElementById('relLeadFormBody').classList.remove('hidden');
            _relLeadFormOpen = true;
            document.getElementById('relLeadFormTitle').scrollIntoView({behavior:'smooth', block:'start'});
        }

        async function relDeleteLead(id) {
            if (!confirm('Remove this lead permanently?')) return;
            try {
                await fetchData('/admin/api/inquiries/' + id, { method: 'DELETE' });
                await loadRealtorDashboard();
            } catch (e) { alert('Error removing lead: ' + e.message); }
        }

        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('relLeadForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                const editId = document.getElementById('relLeadEditId').value;
                const msgEl = document.getElementById('relLeadMsg');
                const submitBtn = document.getElementById('relLeadSubmitBtn');
                const payload = {
                    full_name: document.getElementById('relLeadName').value.trim(),
                    phone: document.getElementById('relLeadPhone').value.trim(),
                    email: document.getElementById('relLeadEmail').value.trim(),
                    property_id: document.getElementById('relLeadProperty').value || null,
                    inquiry_type: document.getElementById('relLeadType').value,
                    status: document.getElementById('relLeadStatus').value,
                    budget_range: document.getElementById('relLeadBudget').value.trim(),
                    preferred_move_date: document.getElementById('relLeadMoveDate').value || null,
                    inquiry_notes: document.getElementById('relLeadNotes').value.trim(),
                };
                submitBtn.disabled = true; submitBtn.textContent = 'Saving...';
                try {
                    if (editId) {
                        await fetchData('/admin/api/inquiries/' + editId, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                    } else {
                        await fetchData('/admin/api/inquiries', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                    }
                    msgEl.textContent = editId ? 'Lead updated!' : 'Lead added!';
                    msgEl.className = 'text-sm text-emerald-400';
                    relCancelLeadEdit();
                    if (!editId) { _relLeadFormOpen = false; document.getElementById('relLeadFormBody').classList.add('hidden'); document.getElementById('relLeadFormToggle').textContent = '+ Add Lead'; }
                    await loadRealtorDashboard();
                } catch (err) {
                    msgEl.textContent = err.message || 'Error saving lead';
                    msgEl.className = 'text-sm text-red-400';
                } finally { submitBtn.disabled = false; submitBtn.textContent = editId ? 'Update Lead' : 'Save Lead'; }
            });
        });

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
                const propSel = document.getElementById('mgrTenantProperty');
                const propName = propSel?.selectedOptions[0]?.text || '';
                const payload = {
                    name: document.getElementById('mgrTenantName').value,
                    email: document.getElementById('mgrTenantEmail').value,
                    phone: document.getElementById('mgrTenantPhone').value,
                    property_name: propName,
                    unit_type_id: document.getElementById('mgrTenantUnitType')?.value || null,
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
                document.getElementById('mgrTenantSubmit').textContent = 'Save Tenant';
                document.getElementById('mgrTenantCancelEdit').classList.add('hidden');
            });
            document.getElementById('mgrInqForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                const msgEl = document.getElementById('mgrInqMsg');
                const payload = {
                    full_name: document.getElementById('mgrInqName').value.trim(),
                    phone: document.getElementById('mgrInqPhone').value.trim(),
                    email: document.getElementById('mgrInqEmail').value.trim(),
                    property_id: document.getElementById('mgrInqProperty').value || null,
                    inquiry_type: document.getElementById('mgrInqType').value,
                    status: document.getElementById('mgrInqStatus').value,
                    inquiry_notes: document.getElementById('mgrInqNotes').value.trim(),
                };
                try {
                    await fetchData('/admin/api/inquiries', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                    msgEl.textContent = 'Inquiry saved!';
                    msgEl.className = 'text-sm text-emerald-400';
                    document.getElementById('mgrInqForm').reset();
                    document.getElementById('mgrInqFormBody').classList.add('hidden');
                    await loadManagerDashboard();
                } catch (err) { msgEl.textContent = err.message || 'Error'; msgEl.className = 'text-sm text-red-400'; }
            });
            document.getElementById('accountantPaymentForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                const msgEl = document.getElementById('accPaymentMsg');
                const editId = document.getElementById('accPaymentEditId').value;
                try {
                    const res = await fetchData(editId ? ('/admin/api/payments/' + editId) : '/admin/api/payments', { method: editId ? 'PUT' : 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ tenant_id: document.getElementById('accPaymentTenant').value || null, tenant_name: document.getElementById('accPaymentTenantName').value, amount: document.getElementById('accPaymentAmount').value, payment_date: document.getElementById('accPaymentDate').value, payment_type: document.getElementById('accPaymentType').value, description: document.getElementById('accPaymentDesc').value }) });
                    msgEl.textContent = res.message || 'Payment recorded';
                    msgEl.className = 'text-sm text-emerald-400';
                    resetAccountantPaymentForm();
                    await loadAccountantDashboard();
                } catch (err) {
                    msgEl.textContent = err.message || 'Error recording payment';
                    msgEl.className = 'text-sm text-red-400';
                }
            });
            document.getElementById('accPaymentCancel')?.addEventListener('click', () => {
                resetAccountantPaymentForm();
                document.getElementById('accPaymentMsg').textContent = '';
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
                listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-6">No updates yet. Post the first milestone above.</p>';
                return;
            }
            const isCeoList = listId === 'ceoConstructionList';
            const source = isCeoList ? 'ceo' : 'mgr';
            const sortedItems = items.slice().sort((a, b) => {
                const timeDiff = getConstructionSortValue(b) - getConstructionSortValue(a);
                if (timeDiff !== 0) return timeDiff;
                return (b.progress_percentage || 0) - (a.progress_percentage || 0);
            });
            listEl.innerHTML = `
                <div class="bg-gradient-to-br from-emerald-900/40 via-slate-800 to-gray-900 border border-emerald-700/40 rounded-2xl p-4 sm:p-5 mb-4">
                    <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                        <div class="min-w-0">
                            <p class="text-xs uppercase tracking-widest text-emerald-300/70 mb-1">Current Site Status</p>
                            <h4 class="text-lg sm:text-xl font-semibold text-white leading-snug">${latest?.title || 'Latest update'}</h4>
                            <p class="text-sm text-gray-400 mt-1">${latest?.property_title || ''}${latest?.happened_on ? ' · ' + latest.happened_on : ''}</p>
                        </div>
                        <div class="flex-shrink-0">
                            <p class="text-3xl font-bold text-emerald-400">${latest?.progress_percentage || 0}%</p>
                            <p class="text-xs text-gray-500 mt-0.5">Latest progress</p>
                        </div>
                    </div>
                    <div class="mt-4 h-2.5 rounded-full bg-gray-700 overflow-hidden">
                        <div class="h-2.5 rounded-full bg-gradient-to-r from-emerald-500 to-teal-400 transition-all duration-700" style="width:${latest?.progress_percentage || 0}%"></div>
                    </div>
                    ${latest?.notes ? `<p class="text-sm text-gray-300 leading-relaxed mt-3">${latest.notes}</p>` : ''}
                </div>
                <div class="space-y-2">
                    ${sortedItems.map((item, idx) => `
                        <div class="relative bg-gray-700/30 border ${idx === 0 ? 'border-emerald-600/50' : 'border-gray-600/30'} rounded-xl p-3 sm:p-4 pl-4 sm:pl-5">
                            <div class="absolute left-0 top-3 bottom-3 w-1 rounded-full ${idx === 0 ? 'bg-emerald-500' : 'bg-gray-600'}"></div>
                            <div class="flex items-start justify-between gap-2">
                                <div class="min-w-0 flex-1">
                                    <div class="flex items-center gap-2 flex-wrap">
                                        <p class="font-semibold text-white text-sm">${item.title}</p>
                                        ${idx === 0 ? '<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full bg-emerald-900/70 text-emerald-300 border border-emerald-700/40">Latest</span>' : ''}
                                    </div>
                                    <p class="text-xs text-gray-400 mt-0.5">${item.happened_on ? item.happened_on + ' · ' : ''}${item.progress_percentage}% complete</p>
                                    ${item.notes ? `<p class="text-xs text-gray-300 mt-1.5 leading-relaxed">${item.notes}</p>` : ''}
                                </div>
                                <div class="flex items-center gap-1.5 flex-shrink-0">
                                    <button onclick="editConstructionUpdate(${item.id},'${(item.title||'').replace(/'/g,"\\'")}',${item.progress_percentage},'${item.happened_on||''}','${(item.notes||'').replace(/'/g,"\\'").replace(/\\n/g,' ')}',${item.property_id},'${source}')" class="text-xs text-blue-400 hover:text-blue-300 border border-blue-800/50 rounded px-2 py-1 transition-colors">Edit</button>
                                    <button onclick="deleteConstructionUpdate(${item.id},'${source}')" class="text-xs text-red-400 hover:text-red-300 border border-red-800/50 rounded px-2 py-1 transition-colors">Delete</button>
                                </div>
                            </div>
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

        async function submitConstructionUpdate(source) {
            const ids = source === 'ceo'
                ? { property: 'ceoConstructionProperty', title: 'ceoConstructionTitle', percent: 'ceoConstructionPercent', date: 'ceoConstructionDate', notes: 'ceoConstructionNotes', msg: 'ceoConstructionMsg', form: 'ceoConstructionForm', editId: 'ceoConstructionEditId' }
                : { property: 'mgrConstructionProperty', title: 'mgrConstructionTitle', percent: 'mgrConstructionPercent', date: 'mgrConstructionDate', notes: 'mgrConstructionNotes', msg: 'mgrConstructionMsg', form: 'managerConstructionForm', editId: 'mgrConstructionEditId' };
            const msgEl = document.getElementById(ids.msg);
            const editId = document.getElementById(ids.editId)?.value || '';
            const propId = document.getElementById(ids.property)?.value;
            try {
                const payload = {
                    property_id: propId,
                    title: document.getElementById(ids.title).value,
                    progress_percentage: document.getElementById(ids.percent).value,
                    happened_on: document.getElementById(ids.date).value || null,
                    notes: document.getElementById(ids.notes).value,
                    is_public: true,
                };
                const url = editId ? `/admin/api/construction-updates/${editId}` : '/admin/api/construction-updates';
                const method = editId ? 'PUT' : 'POST';
                const res = await fetchData(url, { method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                if (msgEl) { msgEl.textContent = res.message || (editId ? 'Update saved' : 'Update posted'); msgEl.className = 'text-sm text-emerald-400'; }
                cancelConstructionEdit(source);
                loadConstructionPropertyOptions();
                loadConstructionUpdates(document.getElementById(ids.property)?.value || null);
            } catch (e) {
                if (msgEl) { msgEl.textContent = e.message || 'Error posting update'; msgEl.className = 'text-sm text-red-400'; }
            }
        }

        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('ceoConstructionProperty')?.addEventListener('change', (e) => loadConstructionUpdates(e.target.value));
            document.getElementById('mgrConstructionProperty')?.addEventListener('change', (e) => loadConstructionUpdates(e.target.value));
            document.getElementById('ceoExpenseForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                await submitProjectExpense('ceo');
            });
            document.getElementById('mgrExpenseForm')?.addEventListener('submit', async (e) => {
                e.preventDefault();
                await submitProjectExpense('mgr');
            });
            document.getElementById('mgrCapitalProperty')?.addEventListener('change', () => loadProjectExpenses('mgr'));
            document.getElementById('mgrExpenseStatusFilter')?.addEventListener('change', () => loadProjectExpenses('mgr'));
            document.getElementById('mgrExpenseReceiptOnly')?.addEventListener('change', () => loadProjectExpenses('mgr'));
            document.getElementById('mgrExpenseCategoryFilter')?.addEventListener('change', () => loadProjectExpenses('mgr'));
            document.getElementById('accExpensePropertyFilter')?.addEventListener('change', () => loadAccountantDashboard());
            document.getElementById('accExpenseStatusFilter')?.addEventListener('change', () => loadAccountantDashboard());
            document.getElementById('accExpenseReceiptOnly')?.addEventListener('change', () => loadAccountantDashboard());
            document.getElementById('accExpenseCategoryFilter')?.addEventListener('change', () => loadAccountantDashboard());
            document.getElementById('ceoExpenseCategoryFilter')?.addEventListener('change', () => ceoLoadExpenses());
        });

        async function deleteConstructionUpdate(id, source) {
            if (!confirm('Delete this construction update? This cannot be undone.')) return;
            try {
                await fetchData('/admin/api/construction-updates/' + id, { method: 'DELETE' });
                const prefix = source === 'mgr' ? 'mgr' : 'ceo';
                const sel = document.getElementById(prefix + 'ConstructionProperty') || document.getElementById('ceoConstructionProperty');
                await loadConstructionUpdates(sel?.value || '');
            } catch (e) {
                alert('Error deleting update. Please try again.');
            }
        }

        function editConstructionUpdate(id, title, pct, date, notes, propertyId, source) {
            const prefix = source === 'mgr' ? 'mgr' : 'ceo';
            const editIdEl = document.getElementById(prefix + 'ConstructionEditId');
            if (editIdEl) editIdEl.value = id;
            const titleEl = document.getElementById(prefix + 'ConstructionTitle');
            const pctEl = document.getElementById(prefix + 'ConstructionPercent');
            const dateEl = document.getElementById(prefix + 'ConstructionDate');
            const notesEl = document.getElementById(prefix + 'ConstructionNotes');
            const propEl = document.getElementById(prefix + 'ConstructionProperty');
            if (titleEl) titleEl.value = title;
            if (pctEl) pctEl.value = pct;
            if (dateEl) dateEl.value = date;
            if (notesEl) notesEl.value = notes;
            if (propEl && propertyId) propEl.value = propertyId;
            const submitBtn = document.getElementById(prefix + 'ConstrSubmitBtn');
            if (submitBtn) submitBtn.textContent = 'Save Changes';
            const labelEl = document.getElementById(prefix + 'ConstrFormLabel');
            if (labelEl) labelEl.textContent = 'Editing Update';
            const cancelBtn = document.getElementById(prefix + 'ConstrCancelBtn');
            if (cancelBtn) cancelBtn.classList.remove('hidden');
            const form = document.getElementById(prefix === 'mgr' ? 'managerConstructionForm' : 'ceoConstructionForm');
            form?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }

        function cancelConstructionEdit(source) {
            const prefix = source === 'mgr' ? 'mgr' : 'ceo';
            const editIdEl = document.getElementById(prefix + 'ConstructionEditId');
            if (editIdEl) editIdEl.value = '';
            const form = document.getElementById(prefix === 'mgr' ? 'managerConstructionForm' : 'ceoConstructionForm');
            if (form) form.reset();
            const submitBtn = document.getElementById(prefix + 'ConstrSubmitBtn');
            if (submitBtn) submitBtn.textContent = 'Post Update';
            const labelEl = document.getElementById(prefix + 'ConstrFormLabel');
            if (labelEl) labelEl.textContent = 'Post New Update';
            const cancelBtn = document.getElementById(prefix + 'ConstrCancelBtn');
            if (cancelBtn) cancelBtn.classList.add('hidden');
        }

        function showMgrTab(tabId) {
            const tabs = ['mgrTabOverview', 'mgrTabUnits', 'mgrTabInquiries', 'mgrTabConstruction', 'mgrTabCapital', 'mgrTabMaintenance'];
            tabs.forEach(t => {
                const el = document.getElementById(t);
                if (el) el.classList.toggle('hidden', t !== tabId);
            });
            document.querySelectorAll('.mgr-tab-btn').forEach(btn => {
                const active = btn.dataset.tab === tabId;
                btn.classList.toggle('bg-slate-700', active);
                btn.classList.toggle('text-white', active);
                btn.classList.toggle('border-slate-500', active);
                btn.classList.toggle('text-gray-400', !active);
                btn.classList.toggle('border-transparent', !active);
            });
            if (tabId === 'mgrTabConstruction') {
                loadConstructionPropertyOptions();
                const mgrSel = document.getElementById('mgrConstructionProperty');
                loadConstructionUpdates(mgrSel?.value || '');
            }
            if (tabId === 'mgrTabCapital') { loadCapitalPropertyOptions(); }
            if (tabId === 'mgrTabMaintenance') { loadMaintenancePropertyOptions(); loadMaintenanceRecords('mgr'); }
        }

        let _mgrMaintCache = [];

        async function loadMaintenancePropertyOptions() {
            try {
                const props = await fetchData('/admin/api/properties');
                const allOpt = '<option value="">All properties</option>';
                const opts = props.map(p => `<option value="${p.id}">${p.title}</option>`).join('');
                ['mgrMaintProperty', 'mgrMaintFormProperty'].forEach(id => {
                    const el = document.getElementById(id);
                    if (!el) return;
                    const cur = el.value;
                    el.innerHTML = (id.includes('Form') ? '' : allOpt) + opts;
                    if (cur && props.some(p => String(p.id) === cur)) el.value = cur;
                });
            } catch(e) {}
        }

        async function loadMaintenanceRecords(prefix) {
            prefix = prefix || 'mgr';
            const listEl = document.getElementById(prefix + 'MaintList');
            const summaryEl = document.getElementById(prefix + 'MaintSummary');
            if (!listEl) return;
            const propId = document.getElementById(prefix + 'MaintProperty')?.value || '';
            const cat = document.getElementById(prefix + 'MaintCategory')?.value || '';
            const params = new URLSearchParams();
            if (propId) params.set('property_id', propId);
            const qs = params.toString() ? '?' + params.toString() : '';
            try {
                listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-6">Loading...</p>';
                const records = await fetchData('/admin/api/maintenance' + qs);
                const filtered = cat ? records.filter(r => r.category === cat) : records;
                _mgrMaintCache = filtered;
                const totalCost = filtered.reduce((s, r) => s + (r.cost || 0), 0);
                if (summaryEl) summaryEl.textContent = filtered.length + ' record' + (filtered.length !== 1 ? 's' : '') + (totalCost ? ' · Total cost: ' + formatNGN(totalCost) : '');
                if (!filtered.length) { listEl.innerHTML = '<p class="text-gray-500 text-sm text-center py-8">No maintenance records found.</p>'; return; }
                const statusColors = { completed: 'bg-emerald-900/60 text-emerald-300', in_progress: 'bg-amber-900/60 text-amber-300', scheduled: 'bg-blue-900/60 text-blue-300' };
                const statusLabels = { completed: 'Completed', in_progress: 'In Progress', scheduled: 'Scheduled' };
                listEl.innerHTML = filtered.map(r => {
                    const sc = statusColors[r.status] || 'bg-gray-700 text-gray-300';
                    const sl = statusLabels[r.status] || r.status;
                    return `<div class="rounded-xl border border-gray-700/70 bg-gray-700/30 p-4"><div class="flex items-start justify-between gap-3"><div class="min-w-0"><div class="flex items-center gap-2 flex-wrap"><p class="font-semibold text-white text-sm">${r.title}</p><span class="px-2 py-0.5 rounded-full text-[11px] ${sc}">${sl}</span><span class="px-2 py-0.5 rounded-full text-[11px] bg-gray-700 text-gray-400">${r.category}</span></div><p class="text-xs text-gray-400 mt-1">${r.property_title || ''} · ${r.maintenance_date || ''}${r.vendor_name ? ' · ' + r.vendor_name : ''}</p>${r.description ? `<p class="text-xs text-gray-500 mt-1">${r.description}</p>` : ''}</div><div class="text-right flex-shrink-0">${r.cost ? '<p class="text-sm font-bold text-amber-300">' + formatNGN(r.cost) + '</p>' : ''}<p class="text-xs text-gray-500 mt-1">${r.recorded_by || ''}</p></div></div><div class="flex items-center gap-3 mt-3 text-xs"><button onclick="mgrEditMaint(${r.id})" class="text-blue-400 hover:text-blue-300">Edit</button><button onclick="mgrDeleteMaint(${r.id})" class="text-red-400 hover:text-red-300">Remove</button></div></div>`;
                }).join('');
            } catch(e) { listEl.innerHTML = '<p class="text-red-400 text-sm text-center py-6">Error loading records.</p>'; }
        }

        function ceoToggleMaintenanceForm(hide, prefix) {
            prefix = prefix || 'mgr';
            const form = document.getElementById(prefix + 'MaintForm');
            if (!form) return;
            if (hide) {
                form.classList.add('hidden');
                document.getElementById(prefix + 'MaintEditId').value = '';
                form.querySelectorAll('input,textarea,select').forEach(el => { if (el.type !== 'hidden') el.value = el.tagName === 'SELECT' ? el.options[0]?.value || '' : ''; });
                document.getElementById(prefix + 'MaintFormTitle').textContent = 'New Maintenance Record';
                const msg = document.getElementById(prefix + 'MaintMsg');
                if (msg) msg.textContent = '';
            } else {
                form.classList.remove('hidden');
                const d = new Date(); const pad = n => String(n).padStart(2,'0');
                const today = d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate());
                const dateEl = document.getElementById(prefix + 'MaintDate');
                if (dateEl && !dateEl.value) dateEl.value = today;
                form.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }

        async function ceoSubmitMaintenance(prefix) {
            prefix = prefix || 'mgr';
            const editId = document.getElementById(prefix + 'MaintEditId').value;
            const msgEl = document.getElementById(prefix + 'MaintMsg');
            const payload = {
                property_id: document.getElementById(prefix + 'MaintFormProperty')?.value || '',
                maintenance_date: document.getElementById(prefix + 'MaintDate')?.value || '',
                title: document.getElementById(prefix + 'MaintTitle')?.value?.trim() || '',
                category: document.getElementById(prefix + 'MaintFormCategory')?.value || 'general',
                status: document.getElementById(prefix + 'MaintStatus')?.value || 'completed',
                vendor_name: document.getElementById(prefix + 'MaintVendor')?.value?.trim() || '',
                cost: document.getElementById(prefix + 'MaintCost')?.value || '',
                description: document.getElementById(prefix + 'MaintDesc')?.value?.trim() || '',
            };
            if (!payload.title) { if (msgEl) { msgEl.textContent = 'Title is required'; msgEl.className = 'text-sm text-red-400'; } return; }
            if (!payload.property_id) { if (msgEl) { msgEl.textContent = 'Select a property'; msgEl.className = 'text-sm text-red-400'; } return; }
            try {
                if (msgEl) { msgEl.textContent = 'Saving...'; msgEl.className = 'text-sm text-gray-400'; }
                await fetchData(editId ? '/admin/api/maintenance/' + editId : '/admin/api/maintenance', {
                    method: editId ? 'PUT' : 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify(payload)
                });
                ceoToggleMaintenanceForm(true, prefix);
                await loadMaintenanceRecords(prefix);
            } catch(err) { if (msgEl) { msgEl.textContent = err.message || 'Error saving record'; msgEl.className = 'text-sm text-red-400'; } }
        }

        function mgrEditMaint(id) {
            const r = _mgrMaintCache.find(x => x.id === id);
            if (!r) return;
            document.getElementById('mgrMaintEditId').value = r.id;
            document.getElementById('mgrMaintFormProperty').value = r.property_id;
            document.getElementById('mgrMaintDate').value = r.maintenance_date || '';
            document.getElementById('mgrMaintTitle').value = r.title || '';
            document.getElementById('mgrMaintFormCategory').value = r.category || 'general';
            document.getElementById('mgrMaintStatus').value = r.status || 'completed';
            document.getElementById('mgrMaintVendor').value = r.vendor_name || '';
            document.getElementById('mgrMaintCost').value = r.cost || '';
            document.getElementById('mgrMaintDesc').value = r.description || '';
            document.getElementById('mgrMaintFormTitle').textContent = 'Edit Record';
            ceoToggleMaintenanceForm(false, 'mgr');
        }

        async function mgrDeleteMaint(id) {
            if (!confirm('Remove this maintenance record?')) return;
            try {
                await fetchData('/admin/api/maintenance/' + id, { method: 'DELETE' });
                await loadMaintenanceRecords('mgr');
            } catch(e) { alert(e.message || 'Error deleting record'); }
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

        function openUnitEdit(unitId, rent, notes) {
            document.querySelectorAll('[id^="unitEditRow_"]').forEach(r => r.classList.add('hidden'));
            document.getElementById('unitEditRow_' + unitId)?.classList.remove('hidden');
        }
        function closeUnitEdit(unitId) {
            document.getElementById('unitEditRow_' + unitId)?.classList.add('hidden');
        }
        async function saveMgrUnitDetails(unitId) {
            const rent = document.getElementById('unitRent_' + unitId)?.value;
            const notes = document.getElementById('unitNotes_' + unitId)?.value || '';
            try {
                await fetchData('/admin/api/units/' + unitId, {
                    method: 'PUT',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ monthly_rent: rent !== '' ? Number(rent) : null, notes })
                });
                await loadManagerDashboard();
            } catch (e) { alert('Error saving unit details.'); }
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

        function downloadContract() {
            const title = document.getElementById('cvModalTitle')?.textContent || 'Agreement';
            const body = document.getElementById('cvModalBody')?.textContent || '';
            const userSig = document.getElementById('cvUserSig')?.textContent || '';
            const userDate = document.getElementById('cvUserDate')?.textContent || '';
            const ceoSig = document.getElementById('cvCeoSig')?.textContent || '';
            const ceoDate = document.getElementById('cvCeoDate')?.textContent || '';
            const status = document.getElementById('cvStatus')?.textContent || '';
            const refNum = 'BWH-' + Date.now().toString(36).toUpperCase().slice(-8);
            const printDate = new Date().toLocaleDateString('en-GB', {day:'2-digit',month:'long',year:'numeric'});
            const safeBody = body.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            const html = `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>${title}</title><style>
@page{size:A4;margin:22mm 20mm 22mm 20mm}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Times New Roman',serif;color:#000;background:#fff;font-size:11pt;line-height:1.65}
.hdr{padding-bottom:12pt;margin-bottom:16pt;display:flex;align-items:flex-start;justify-content:space-between}
.co-name{font-size:22pt;font-weight:bold;letter-spacing:1px;line-height:1.1}
.co-sub{font-size:8pt;letter-spacing:2.5px;text-transform:uppercase;color:#333;margin-top:3pt}
.co-addr{font-size:8pt;color:#555;margin-top:4pt}
.doc-title{text-align:center;margin:16pt 0 14pt}
.doc-title h1{font-size:14pt;text-transform:uppercase;letter-spacing:2.5px;padding-bottom:5pt;display:inline-block}
.meta{display:flex;justify-content:space-between;font-size:8.5pt;color:#444;margin-bottom:16pt;padding:7pt 0;background:#fff}
.btext{white-space:pre-wrap;font-size:10.5pt;line-height:1.8;text-align:justify;margin-bottom:26pt}
.sig-hd{font-size:9.5pt;text-transform:uppercase;letter-spacing:1.8px;padding-bottom:4pt;margin-bottom:14pt}
.sig-grid{display:grid;grid-template-columns:1fr 1fr;gap:28pt;margin-bottom:28pt}
.sig-box{padding-top:9pt}
.sig-lbl{font-size:7.5pt;text-transform:uppercase;letter-spacing:1px;color:#555;margin-bottom:4pt}
.sig-name{font-size:14pt;font-style:italic;font-family:'Brush Script MT','Segoe Script',cursive;margin-bottom:3pt;min-height:22pt}
.sig-date{font-size:8.5pt;color:#333}
.status-badge{display:inline-block;border:0.8pt solid #888;padding:5pt 14pt;font-size:8.5pt;letter-spacing:1px;margin-top:10pt}
.stamp-area{margin-top:36pt}
.ftr{margin-top:28pt;padding-top:7pt;display:flex;justify-content:space-between;font-size:7.5pt;color:#888}
@media print{.no-print{display:none}}
</style></head><body>
<div class="hdr">
  <div>
    <div class="co-name">BrightWave</div>
    <div class="co-sub">Habitat Enterprise</div>
    <div class="co-addr">Kwara State, Nigeria &nbsp;&middot;&nbsp; brightwavehabitat@gmail.com</div>
  </div>
  <div style="text-align:right;font-size:8pt;color:#555;line-height:1.9">
    <div><strong>Ref:</strong> ${refNum}</div>
    <div><strong>Issued:</strong> ${printDate}</div>
  </div>
</div>
<div class="doc-title"><h1>${title}</h1></div>
<div class="meta">
  <span><strong>Document Reference:</strong> ${refNum}</span>
  <span><strong>Date Issued:</strong> ${printDate}</span>
  <span><strong>Jurisdiction:</strong> Kwara State, Nigeria</span>
</div>
<div class="btext">${safeBody}</div>
<div class="sig-hd">Signatures &amp; Execution</div>
<div class="sig-grid">
  <div class="sig-box">
    <div class="sig-lbl">Employee / Investor Signature</div>
    <div class="sig-name">${userSig || '&nbsp;'}</div>
    <div class="sig-date">${userDate || 'Date: ____________________________'}</div>
  </div>
  <div class="sig-box">
    <div class="sig-lbl">Chief Executive Officer &middot; BrightWave Habitat Enterprise</div>
    <div class="sig-name">${ceoSig || '&nbsp;'}</div>
    <div class="sig-date">${ceoDate || 'Date: ____________________________'}</div>
  </div>
</div>
<div class="stamp-area"></div>
<div style="text-align:center;margin-top:10pt"><span class="status-badge">${status || 'EXECUTED AGREEMENT'}</span></div>
<div class="ftr">
  <span>BrightWave Habitat Enterprise &nbsp;&middot;&nbsp; Kwara State, Nigeria</span>
  <span>Ref: ${refNum} &nbsp;&middot;&nbsp; Generated ${printDate}</span>
</div>
<script>window.onload=function(){window.print();}<\/script>
</body></html>`;
            const w = window.open('','_blank','width=820,height=1000');
            if (w) { w.document.write(html); w.document.close(); }
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
                <div class="flex justify-center gap-3 pt-2">
                    <button onclick="downloadContract()" class="bg-slate-700 hover:bg-slate-600 text-white text-xs font-medium py-2 px-4 rounded-lg flex items-center gap-2"><i class="fas fa-download"></i> Download PDF</button>
                    <button onclick="closeContractModal()" class="bg-gray-700 hover:bg-gray-600 text-white text-xs font-medium py-2 px-4 rounded-lg">Close</button>
                </div>
            </div>
        </div>
    </div>

    <!-- ====== FIRST-LOGIN ONBOARDING TOUR (Shepherd.js) ====== -->
    <script>
    (function() {
        const HAS_SEEN_TOUR = {{ has_seen_tour | tojson }};
        if (HAS_SEEN_TOUR) return;

        function loadCSS(href) {
            return new Promise((resolve) => {
                const l = document.createElement('link');
                l.rel = 'stylesheet'; l.href = href; l.onload = resolve; l.onerror = resolve;
                document.head.appendChild(l);
            });
        }
        function loadJS(src) {
            return new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = src; s.onload = resolve; s.onerror = reject;
                document.head.appendChild(s);
            });
        }
        async function markSeen() {
            try {
                await fetch('/admin/api/me/mark-tour-seen', {
                    method: 'POST', credentials: 'include',
                    headers: { 'X-CSRF-Token': adminCsrfToken || '' },
                });
            } catch (e) {}
        }

        function buildStepsForRole() {
            const role = USER_ROLE;
            const skip = { text: 'Skip tour', classes: 'shepherd-button-secondary', action() { return _tour.cancel(); } };
            const next = { text: 'Next', action() { return _tour.next(); } };
            const done = { text: 'Got it', action() { return _tour.complete(); } };

            if (role === 'INVESTOR') {
                return [
                    { id: 'welcome', title: 'Welcome to your portal', text: 'Quick 4-step tour so you know where everything lives. You can skip any time.', buttons: [skip, next] },
                    { id: 'overview', title: 'Overview', text: 'Headline numbers — what you invested, your net ROI, what\\'s been paid out, and site progress.', attachTo: { element: '[data-inv-tab="overview"]', on: 'bottom' }, buttons: [skip, next] },
                    { id: 'returns', title: 'Returns', text: 'Year-by-year payout schedule with paid/scheduled status. For equity investors, your stake and projected revenue.', attachTo: { element: '[data-inv-tab="returns"]', on: 'bottom' }, buttons: [skip, next] },
                    { id: 'project', title: 'Project', text: 'Live construction progress and milestones posted by the project team.', attachTo: { element: '[data-inv-tab="project"]', on: 'bottom' }, buttons: [skip, next] },
                    { id: 'profile', title: 'Profile', text: 'Your investment details, signed agreement, and direct line to the CEO.', attachTo: { element: '[data-inv-tab="profile"]', on: 'bottom' }, buttons: [skip, done] },
                ];
            }
            if (role === 'MANAGER' || role === 'REALTOR') {
                return [
                    { id: 'welcome', title: 'Welcome aboard', text: 'A quick 2-step tour. Skip any time.', buttons: [skip, next] },
                    { id: 'tenants', title: 'Tenants & Commission', text: 'Each tenant you sign gets recorded with you as "Serviced By". You earn 10% of every unit. Track it in real time.', buttons: [skip, done] },
                ];
            }
            return [
                { id: 'welcome', title: 'Welcome to BrightWave', text: 'Your dashboard is ready. Use the sidebar to navigate. Reach out to the CEO any time.', buttons: [done] },
            ];
        }

        let _tour;
        async function startTour() {
            await loadCSS('https://cdn.jsdelivr.net/npm/shepherd.js@11.2.0/dist/css/shepherd.css');
            await loadJS('https://cdn.jsdelivr.net/npm/shepherd.js@11.2.0/dist/js/shepherd.min.js');
            _tour = new Shepherd.Tour({
                useModalOverlay: true,
                defaultStepOptions: {
                    cancelIcon: { enabled: true },
                    classes: 'shadow-md',
                    scrollTo: { behavior: 'smooth', block: 'center' },
                },
            });
            const steps = buildStepsForRole();
            steps.forEach(s => _tour.addStep(s));
            _tour.on('complete', markSeen);
            _tour.on('cancel', markSeen);
            setTimeout(() => _tour.start(), 800);
        }

        document.addEventListener('DOMContentLoaded', startTour);
    })();
    </script>

</body>
</html>
"""

if __name__ == '__main__':
    with app.app_context():
        ensure_runtime_state()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
