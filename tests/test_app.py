"""
Tests for BrightWave Habitat Enterprise Flask app.
Run with: pytest tests/test_app.py -v
"""
import os
import json
import io
import shutil
import tempfile
import pytest
from datetime import date
from unittest.mock import patch

os.environ.setdefault('SECRET_KEY', 'test-secret-key-brightwave')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from werkzeug.security import generate_password_hash

import app as app_module
from app import app as flask_app, db, Admin, InvestorProfile, PaymentRecord, ProjectExpense


@pytest.fixture()
def client():
    flask_app.config['TESTING'] = True
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    flask_app.config['WTF_CSRF_ENABLED'] = False
    flask_app.config['RATELIMIT_ENABLED'] = False
    os.makedirs(os.path.join(os.path.dirname(__file__), 'tmp'), exist_ok=True)
    receipt_dir = tempfile.mkdtemp(
        prefix='brightwave-receipts-',
        dir=os.path.join(os.path.dirname(__file__), 'tmp')
    )
    flask_app.config['EXPENSE_RECEIPT_FOLDER'] = receipt_dir
    with flask_app.test_client() as client:
        with flask_app.app_context():
            app_module.runtime_state_initialized = False
            db.create_all()
            yield client
            db.drop_all()
            app_module.runtime_state_initialized = False
    shutil.rmtree(receipt_dir, ignore_errors=True)


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
    assert r.status_code in (200, 301)
    if r.status_code == 301:
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


def test_accountant_can_update_and_delete_payment(client):
    with flask_app.app_context():
        create_admin('accountant1', role='ACCOUNTANT')
    login_resp = login(client, 'accountant1')
    assert login_resp.status_code == 200
    headers = admin_headers(client)

    create_resp = client.post('/admin/api/payments', json={
        'tenant_name': 'Test Tenant',
        'amount': 50000,
        'payment_date': '2026-04-01',
        'payment_type': 'rent',
        'description': 'Initial entry'
    }, headers=headers)
    assert create_resp.status_code == 200
    payment_id = json.loads(create_resp.data)['id']

    update_resp = client.put(f'/admin/api/payments/{payment_id}', json={
        'tenant_name': 'Test Tenant',
        'amount': 42000,
        'payment_date': '2026-04-02',
        'payment_type': 'fee',
        'description': 'Corrected entry'
    }, headers=headers)
    assert update_resp.status_code == 200
    update_data = json.loads(update_resp.data)
    assert update_data['success'] is True
    assert update_data['payment']['amount'] == 42000
    assert update_data['payment']['payment_type'] == 'fee'

    list_resp = client.get('/admin/api/payments')
    assert list_resp.status_code == 200
    payments = json.loads(list_resp.data)
    assert any(p['id'] == payment_id and p['amount'] == 42000 for p in payments)

    delete_resp = client.delete(f'/admin/api/payments/{payment_id}', headers=headers)
    assert delete_resp.status_code == 200
    delete_data = json.loads(delete_resp.data)
    assert delete_data['success'] is True

    with flask_app.app_context():
        assert PaymentRecord.query.get(payment_id) is None


def test_my_investment_returns_annual_principal_plus_roi_schedule(client):
    with flask_app.app_context():
        investor = create_admin('investor1', role='INVESTOR')
        db.session.add(InvestorProfile(
            user_id=investor.id,
            investment_type='DEBT',
            investment_amount=100000,
            investment_date=date(2026, 1, 1),
            roi_rate=3.5,
            expected_completion_date=date(2026, 12, 31),
            investment_term_years=5,
            total_distributed=0,
        ))
        db.session.commit()

    login_resp = login(client, 'investor1')
    assert login_resp.status_code == 200

    resp = client.get('/admin/api/my-investment')
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data['distribution_model'] == 'annual_principal_plus_roi'
    assert data['annual_principal_component'] == 20000
    assert data['annual_roi_amount'] == 3500
    assert len(data['payout_schedule']) == 5
    assert data['payout_schedule'][0]['total_payout'] == 23500
    assert data['payout_schedule'][-1]['remaining_principal'] == 0


def test_manager_can_create_update_and_delete_project_expense(client):
    with flask_app.app_context():
        create_admin('manager_exp', role='MANAGER')
        create_admin('accountant_exp', role='ACCOUNTANT')
    login_resp = login(client, 'manager_exp')
    assert login_resp.status_code == 200
    headers = admin_headers(client)

    properties_resp = client.get('/admin/api/properties')
    assert properties_resp.status_code == 200
    properties = json.loads(properties_resp.data)
    property_with_budget = next((prop for prop in properties if prop.get('capital_budget') is not None), properties[0])
    property_id = property_with_budget['id']

    create_resp = client.post('/admin/api/project-expenses', json={
        'property_id': property_id,
        'expense_date': '2026-04-10',
        'category': 'materials',
        'item_name': 'Cement bags',
        'payee_name': 'Main Supplier',
        'quantity': 20,
        'unit_cost': 9500,
        'amount': 190000,
        'notes': 'Foundation batch'
    }, headers=headers)
    assert create_resp.status_code == 200
    expense_id = json.loads(create_resp.data)['expense']['id']

    list_resp = client.get(f'/admin/api/project-expenses?property_id={property_id}')
    assert list_resp.status_code == 200
    list_data = json.loads(list_resp.data)
    assert list_data['total_amount'] == 190000
    assert list_data['budget_total'] is not None
    assert list_data['budget_remaining'] == list_data['budget_total']
    assert list_data['approval_totals']['pending'] == 190000
    created_expense = next(exp for exp in list_data['expenses'] if exp['id'] == expense_id)
    assert created_expense['approval_status'] == 'pending'

    update_resp = client.put(f'/admin/api/project-expenses/{expense_id}', json={
        'category': 'labour',
        'item_name': 'Bricklayer wages',
        'amount': 210000,
        'notes': 'Corrected amount'
    }, headers=headers)
    assert update_resp.status_code == 200
    update_data = json.loads(update_resp.data)
    assert update_data['expense']['category'] == 'labour'
    assert update_data['expense']['amount'] == 210000
    assert update_data['expense']['approval_status'] == 'pending'

    blocked_approve_resp = client.put(f'/admin/api/project-expenses/{expense_id}', json={
        'approval_status': 'approved'
    }, headers=headers)
    assert blocked_approve_resp.status_code == 403

    accountant_login = login(client, 'accountant_exp')
    assert accountant_login.status_code == 200
    approve_resp = client.put(f'/admin/api/project-expenses/{expense_id}', json={
        'approval_status': 'approved'
    }, headers=admin_headers(client))
    assert approve_resp.status_code == 200
    approve_data = json.loads(approve_resp.data)
    assert approve_data['expense']['approval_status'] == 'approved'
    assert approve_data['expense']['approved_by']

    approved_list_resp = client.get(f'/admin/api/project-expenses?property_id={property_id}&approval_status=approved')
    assert approved_list_resp.status_code == 200
    approved_list_data = json.loads(approved_list_resp.data)
    assert approved_list_data['budget_remaining'] == approved_list_data['budget_total'] - 210000
    assert approved_list_data['approval_totals']['approved'] == 210000
    assert len(approved_list_data['expenses']) == 1

    stats_resp = client.get('/admin/api/stats')
    assert stats_resp.status_code == 200
    stats = json.loads(stats_resp.data)
    assert stats['total_capital_spent'] == 210000
    assert stats['total_capital_budget'] >= stats['total_capital_spent']

    delete_resp = client.delete(f'/admin/api/project-expenses/{expense_id}', headers=admin_headers(client))
    assert delete_resp.status_code == 200
    with flask_app.app_context():
        assert ProjectExpense.query.get(expense_id) is None


def test_manager_can_upload_expense_receipt_and_vendor_is_captured(client):
    with flask_app.app_context():
        create_admin('manager_receipts', role='MANAGER')
    login_resp = login(client, 'manager_receipts')
    assert login_resp.status_code == 200
    headers = admin_headers(client)

    with patch('werkzeug.datastructures.FileStorage.save', autospec=True) as mocked_save:
        upload_resp = client.post(
            '/admin/api/upload-expense-receipt',
            headers=headers,
            data={'file': (io.BytesIO(b'receipt-bytes'), 'cement-invoice.pdf')},
            content_type='multipart/form-data'
        )
    assert upload_resp.status_code == 200
    upload_data = json.loads(upload_resp.data)
    assert upload_data['success'] is True
    assert upload_data['filename'].startswith('uploads/expense-receipts/')
    mocked_save.assert_called_once()

    properties_resp = client.get('/admin/api/properties')
    properties = json.loads(properties_resp.data)
    property_id = properties[0]['id']

    create_resp = client.post('/admin/api/project-expenses', json={
        'property_id': property_id,
        'expense_date': '2026-04-11',
        'category': 'materials',
        'item_name': 'Roofing sheets',
        'payee_name': 'Open Market Supplier',
        'amount': 750000,
        'receipt_path': upload_data['filename']
    }, headers=headers)
    assert create_resp.status_code == 200
    expense_data = json.loads(create_resp.data)['expense']
    assert expense_data['receipt_path'] == upload_data['filename']

    vendors_resp = client.get('/admin/api/vendors')
    assert vendors_resp.status_code == 200
    vendors = json.loads(vendors_resp.data)
    assert any(vendor['name'] == 'Open Market Supplier' for vendor in vendors)


def test_accountant_can_create_vendor_record(client):
    with flask_app.app_context():
        create_admin('accountant_vendor', role='ACCOUNTANT')
    login_resp = login(client, 'accountant_vendor')
    assert login_resp.status_code == 200

    create_resp = client.post('/admin/api/vendors', json={
        'name': 'Anonymous Labour Team',
        'contact_type': 'worker',
        'notes': 'Use for crews without a fixed registered business name'
    }, headers=admin_headers(client))
    assert create_resp.status_code == 200
    create_data = json.loads(create_resp.data)
    assert create_data['success'] is True
    assert create_data['vendor']['contact_type'] == 'worker'


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
