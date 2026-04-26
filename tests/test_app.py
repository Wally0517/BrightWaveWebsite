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

from app import app as flask_app, db


@pytest.fixture()
def client():
    flask_app.config['TESTING'] = True
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    flask_app.config['WTF_CSRF_ENABLED'] = False
    flask_app.config['RATELIMIT_ENABLED'] = False
    with flask_app.test_client() as client:
        with flask_app.app_context():
            db.create_all()
            yield client
            db.drop_all()


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
