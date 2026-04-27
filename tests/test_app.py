"""
Tests for BrightWave Habitat Enterprise Flask app.
Run with: pytest tests/test_app.py -v
"""
import os
import json
import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-key-brightwave')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from werkzeug.security import generate_password_hash

import app as app_module
from app import app as flask_app, db, Admin


@pytest.fixture()
def client():
    flask_app.config['TESTING'] = True
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    flask_app.config['WTF_CSRF_ENABLED'] = False
    flask_app.config['RATELIMIT_ENABLED'] = False
    with flask_app.test_client() as client:
        with flask_app.app_context():
            app_module.runtime_state_initialized = False
            db.create_all()
            yield client
            db.drop_all()
            app_module.runtime_state_initialized = False


def create_admin(username, role='CEO', email=None, password='testpass123', secondary_roles=None):
    admin = Admin(
        username=username,
        email=email or f'{username}@example.com',
        password_hash=generate_password_hash(password),
        role=role,
        secondary_roles=secondary_roles or [],
        is_active=True,
    )
    db.session.add(admin)
    db.session.commit()
    return admin


def login(client, username, password='testpass123'):
    remote_addr = f'10.0.0.{abs(hash(username)) % 200 + 1}'
    return client.post(
        '/admin/login',
        json={'username': username, 'password': password},
        environ_overrides={'REMOTE_ADDR': remote_addr},
    )


def admin_headers(client):
    with client.session_transaction() as sess:
        token = sess.get('csrf_token', '')
    return {'X-CSRF-Token': token} if token else {}


# ── Page routes ──────────────────────────────────────────────────────────────

def test_homepage_returns_200(client):
    r = client.get('/')
    assert r.status_code == 200
    assert b'BrightWave' in r.data


def test_about_page_returns_200(client):
    r = client.get('/about')
    assert r.status_code == 200
    assert b'About' in r.data


def test_faq_page_returns_200(client):
    r = client.get('/faq')
    assert r.status_code == 200


def test_contact_page_returns_200(client):
    r = client.get('/contact')
    assert r.status_code == 200
    assert b'BrightWave' in r.data


def test_contact_page_has_form(client):
    r = client.get('/contact')
    assert b'contactForm' in r.data


def test_contact_page_no_redirect_stub(client):
    r = client.get('/contact')
    assert b'meta http-equiv' not in r.data.lower().replace(b' ', b'')


def test_health_returns_ok(client):
    r = client.get('/health')
    assert r.status_code == 200
    assert b'ok' in r.data


def test_phase1_detail_page_returns_200(client):
    r = client.get('/hostels/phase1')
    assert r.status_code == 200
    assert b'Phase 1' in r.data


def test_hostels_detail_redirects_to_phase1(client):
    r = client.get('/hostels/detail')
    assert r.status_code == 301
    assert '/hostels/phase1' in r.headers['Location']


def test_hostels_detail_redirect_follows_to_200(client):
    r = client.get('/hostels/detail', follow_redirects=True)
    assert r.status_code == 200
    assert b'Phase 1' in r.data


# ── API: properties ───────────────────────────────────────────────────────────

def test_properties_api_returns_list(client):
    r = client.get('/api/properties')
    assert r.status_code == 200
    data = json.loads(r.data)
    assert isinstance(data, list)


def test_properties_api_type_filter(client):
    r = client.get('/api/properties?type=hostel')
    assert r.status_code == 200
    data = json.loads(r.data)
    assert isinstance(data, list)
    for prop in data:
        assert prop.get('type') == 'hostel'


def test_properties_api_phase1_has_correct_url_slug(client):
    r = client.get('/api/properties')
    assert r.status_code == 200
    data = json.loads(r.data)
    phase1 = next((p for p in data if 'Phase 1' in p.get('title', '')), None)
    if phase1:
        assert phase1.get('construction_status') == 'completed'


# ── API: site content ─────────────────────────────────────────────────────────

def test_site_content_api_returns_dict(client):
    r = client.get('/api/site-content')
    assert r.status_code == 200
    data = json.loads(r.data)
    assert isinstance(data, dict)
    assert 'home.hero_title' in data


def test_site_content_has_about_keys(client):
    r = client.get('/api/site-content')
    data = json.loads(r.data)
    assert 'about.hero_subtitle' in data
    assert 'about.intro_body' in data


# ── API: team members ─────────────────────────────────────────────────────────

def test_team_members_api_returns_list(client):
    r = client.get('/api/team-members')
    assert r.status_code == 200
    data = json.loads(r.data)
    assert isinstance(data, list)


# ── Contact form ──────────────────────────────────────────────────────────────

def test_contact_form_missing_fields_returns_400(client):
    r = client.post('/api/contact', json={})
    assert r.status_code == 400


def test_contact_form_invalid_email_returns_400(client):
    r = client.post('/api/contact', json={
        'fullName': 'Test User',
        'email': 'not-an-email',
        'phone': '+2348000000000',
        'message': 'Test message'
    })
    assert r.status_code == 400


def test_contact_form_valid_payload_accepted(client):
    r = client.post('/api/contact', json={
        'fullName': 'Test User',
        'email': 'test@example.com',
        'phone': '+2348000000000',
        'message': 'I am interested in Phase 1.',
        'interest': 'student-hostel'
    })
    assert r.status_code in (200, 202)
    data = json.loads(r.data)
    assert data.get('success') is True


# ── Property inquiry ──────────────────────────────────────────────────────────

def test_property_inquiry_missing_fields_returns_400(client):
    r = client.post('/api/property-inquiry', json={})
    assert r.status_code == 400


def test_property_inquiry_valid_payload_accepted(client):
    r = client.post('/api/property-inquiry', json={
        'fullName': 'Test Tenant',
        'email': 'tenant@example.com',
        'phone': '+2348000000000',
        'message': 'Availability query for Phase 1.',
        'inquiryType': 'booking'
    })
    assert r.status_code in (200, 202)
    data = json.loads(r.data)
    assert data.get('success') is True


# ── Admin login (unauthenticated) ─────────────────────────────────────────────

def test_admin_dashboard_redirects_without_auth(client):
    r = client.get('/admin/dashboard')
    assert r.status_code in (301, 302)


def test_admin_login_page_loads(client):
    r = client.get('/admin/login')
    assert r.status_code == 200


def test_admin_bad_credentials_rejected(client):
    r = client.post('/admin/login', json={
        'username': 'wrong',
        'password': 'wrong'
    })
    assert r.status_code == 401
    data = json.loads(r.data)
    assert data.get('success') is False


def test_apple_touch_icon_route_returns_png(client):
    r = client.get('/apple-touch-icon.png')
    assert r.status_code == 200
    assert r.mimetype == 'image/png'


def test_manager_can_create_tenant_and_units_seeded(client):
    with flask_app.app_context():
        create_admin('manager1', role='MANAGER')
    login_resp = login(client, 'manager1')
    assert login_resp.status_code == 200

    units_resp = client.get('/admin/api/units')
    assert units_resp.status_code == 200
    units = json.loads(units_resp.data)
    phase1_units = [u for u in units if u.get('property_title') == 'BrightWave Phase 1 Hostel']
    assert len(phase1_units) == 10

    tenant_resp = client.post('/admin/api/tenants', headers=admin_headers(client), json={
        'name': 'Tenant One',
        'property_name': 'BrightWave Phase 1 Hostel',
        'unit_number': '1A',
        'monthly_rent': 120000,
        'status': 'active'
    })
    assert tenant_resp.status_code == 200
    payload = json.loads(tenant_resp.data)
    assert payload.get('success') is True


def test_ceo_can_create_construction_update(client):
    with flask_app.app_context():
        create_admin('ceo1', role='CEO')
    login_resp = login(client, 'ceo1')
    assert login_resp.status_code == 200

    properties_resp = client.get('/admin/api/properties')
    properties = json.loads(properties_resp.data)
    project = next(p for p in properties if p.get('title') == 'BrightWave Hostel Phase 2')

    create_resp = client.post('/admin/api/construction-updates', headers=admin_headers(client), json={
        'property_id': project['id'],
        'title': 'Foundation complete',
        'progress_percentage': 35,
        'notes': 'Concrete works completed.',
        'is_public': True
    })
    assert create_resp.status_code == 200
    data = json.loads(create_resp.data)
    assert data.get('success') is True


def test_secondary_manager_role_can_create_tenant(client):
    with flask_app.app_context():
        create_admin('ops1', role='ACCOUNTANT', secondary_roles=['MANAGER'])
    login_resp = login(client, 'ops1')
    assert login_resp.status_code == 200

    tenant_resp = client.post('/admin/api/tenants', headers=admin_headers(client), json={
        'name': 'Tenant Two',
        'property_name': 'BrightWave Phase 1 Hostel',
        'unit_number': '2A',
        'monthly_rent': 130000,
        'status': 'active'
    })
    assert tenant_resp.status_code == 200
    payload = json.loads(tenant_resp.data)
    assert payload.get('success') is True


def test_investor_permissions_are_restricted(client):
    with flask_app.app_context():
        create_admin('investor1', role='INVESTOR')
    login_resp = login(client, 'investor1')
    assert login_resp.status_code == 200

    assert client.get('/admin/api/tenants').status_code == 403
    assert client.get('/admin/api/payments').status_code == 403
    assert client.get('/admin/api/inquiries').status_code == 403


def test_realtor_can_read_units_but_not_payments(client):
    with flask_app.app_context():
        create_admin('realtor1', role='REALTOR')
    login_resp = login(client, 'realtor1')
    assert login_resp.status_code == 200

    assert client.get('/admin/api/units').status_code == 200
    assert client.get('/admin/api/payments').status_code == 403


def test_secondary_role_contract_lookup_uses_requested_role(client):
    with flask_app.app_context():
        create_admin('hybrid1', role='MANAGER', secondary_roles=['REALTOR'])
    login_resp = login(client, 'hybrid1')
    assert login_resp.status_code == 200

    contract_resp = client.get('/admin/api/my-contract?role=REALTOR')
    assert contract_resp.status_code == 200
    payload = json.loads(contract_resp.data)
    assert payload.get('success') is True
    assert payload.get('role') == 'REALTOR'


# ── Static assets ─────────────────────────────────────────────────────────────

def test_logo_asset_served(client):
    r = client.get('/assets/images/brightwave-logo.png')
    assert r.status_code == 200


def test_stylesheet_served(client):
    r = client.get('/assets/style.css')
    assert r.status_code == 200


# ── Email draft — no page URL leaked ─────────────────────────────────────────
#
# These tests read the HTML source and verify that window.location.href is NOT
# used inside the email body array that feeds the mailto draft, ensuring the
# page URL is not included in outgoing email drafts.

def _email_body_array(html: str, form_id: str) -> str:
    """Extract the JS body array string from the HTML for a given form submit block."""
    import re
    # Find the block after the form's submit handler that builds `const body = [...]`
    pattern = r'const body = \[(.*?)\]\.join'
    matches = re.findall(pattern, html, re.DOTALL)
    return ' '.join(matches)


def test_contact_page_email_draft_no_page_url(client):
    r = client.get('/contact')
    body_array = _email_body_array(r.data.decode(), 'contactForm')
    assert 'window.location.href' not in body_array


def test_phase1_email_draft_no_page_url(client):
    r = client.get('/hostels/phase1')
    body_array = _email_body_array(r.data.decode(), 'hostelContactForm')
    assert 'window.location.href' not in body_array
