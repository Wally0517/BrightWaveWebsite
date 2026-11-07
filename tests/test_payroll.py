import json
from datetime import date

import pytest
from sqlalchemy import text

from test_app import client, create_admin, login, admin_headers  # noqa: F401  (fixture re-export)
import app as app_module
from app import app as flask_app, db, Admin, Employee, PayrollPayment, SalaryHistory


def _login_as(client, username, role):
    with flask_app.app_context():
        create_admin(username, role=role)
    assert login(client, username).status_code == 200
    return admin_headers(client)


def test_manager_can_add_staff_without_account_and_backfill(client):
    headers = _login_as(client, 'mgr1', 'MANAGER')

    r = client.post('/admin/api/payroll/staff', json={
        'full_name': 'Musa Cleaner', 'job_title': 'Cleaner', 'monthly_salary': 40000,
        'start_date': '2026-09-01', 'phone': '0801',
    }, headers=headers)
    assert r.status_code == 200, r.data
    emp = json.loads(r.data)['employee']
    assert emp['has_account'] is False and emp['monthly_salary'] == 40000

    # Back-fill Sept..Nov 2026 salary
    r = client.post('/admin/api/payroll/backfill', json={
        'employee_id': emp['id'], 'from_year': 2026, 'from_month': 9, 'to_year': 2026, 'to_month': 11,
    }, headers=headers)
    body = json.loads(r.data)
    assert r.status_code == 200 and body['created'] == ['Sep 2026', 'Oct 2026', 'Nov 2026'], body

    # Running again skips months already covered
    r = client.post('/admin/api/payroll/backfill', json={
        'employee_id': emp['id'], 'from_year': 2026, 'from_month': 9, 'to_year': 2026, 'to_month': 11,
    }, headers=headers)
    assert json.loads(r.data)['created'] == []

    # Staff who started in Sept must not appear as owing for August
    r = client.get('/admin/api/payroll/summary?year=2026&month=8')
    assert all(row['employee_id'] != emp['id'] for row in json.loads(r.data)['rows'])

    r = client.get('/admin/api/payroll/summary?year=2026&month=10')
    row = next(x for x in json.loads(r.data)['rows'] if x['employee_id'] == emp['id'])
    assert row['salary'] == 40000 and row['already_paid'] == 40000 and row['outstanding'] == 0

    # Manager can edit but not delete
    pid = row['payment_history'][0]['id']
    r = client.put(f'/admin/api/payroll/pay/{pid}', json={'amount': 45000, 'notes': 'late top-up'}, headers=headers)
    assert r.status_code == 200 and json.loads(r.data)['payment']['amount'] == 45000
    r = client.delete(f'/admin/api/payroll/pay/{pid}', headers=headers)
    assert r.status_code == 403
    r = client.delete(f'/admin/api/payroll/staff/{emp["id"]}', headers=headers)
    assert r.status_code == 403


def test_ceo_sees_manager_entries_and_can_delete(client):
    mgr_headers = _login_as(client, 'mgr2', 'MANAGER')
    r = client.post('/admin/api/payroll/staff', json={'full_name': 'Ada Guard', 'monthly_salary': 30000, 'start_date': '2026-01-01'}, headers=mgr_headers)
    emp_id = json.loads(r.data)['employee']['id']
    r = client.post('/admin/api/payroll/pay', json={'employee_id': emp_id, 'amount': 30000, 'year': 2026, 'month': 9, 'kind': 'salary', 'paid_on': '2026-09-28'}, headers=mgr_headers)
    assert r.status_code == 200
    pid = json.loads(r.data)['id']

    client.get('/admin/logout')
    ceo_headers = _login_as(client, 'ceo1', 'CEO')
    r = client.get('/admin/api/payroll/ledger?year=2026&month=9')
    body = json.loads(r.data)
    entry = next(p for p in body['payments'] if p['id'] == pid)
    assert entry['paid_by'] == 'mgr2' and entry['paid_on'] == '2026-09-28' and body['can_delete'] is True
    assert client.delete(f'/admin/api/payroll/pay/{pid}', headers=ceo_headers).status_code == 200


def test_realtor_and_pa_cannot_access_payroll(client):
    _login_as(client, 'rl1', 'REALTOR')
    assert client.get('/admin/api/payroll/summary').status_code == 403
    client.get('/admin/logout')
    _login_as(client, 'pa1', 'PA')
    assert client.get('/admin/api/payroll/staff').status_code == 403


def test_account_salary_change_syncs_employee_and_history(client):
    ceo_headers = _login_as(client, 'ceo2', 'CEO')
    r = client.post('/admin/api/accounts', json={
        'username': 'acct1', 'email': 'acct1@example.com', 'password': 'password123',
        'role': 'ACCOUNTANT', 'display_name': 'Acct One', 'monthly_salary': 50000,
    }, headers=ceo_headers)
    assert r.status_code == 200, r.data
    uid = json.loads(r.data)['id']
    with flask_app.app_context():
        emp = Employee.query.filter_by(user_id=uid).first()
        assert emp is not None and emp.monthly_salary == 50000 and emp.full_name == 'Acct One'
        emp_id = emp.id
    r = client.put(f'/admin/api/accounts/{uid}', json={'monthly_salary': 60000}, headers=ceo_headers)
    assert r.status_code == 200
    with flask_app.app_context():
        emp = Employee.query.get(emp_id)
        assert emp.monthly_salary == 60000
        assert SalaryHistory.query.filter_by(employee_id=emp_id).count() == 2
    # Deleting the account keeps the roster row (unlinked), not the history
    r = client.post('/admin/api/payroll/pay', json={'employee_id': emp_id, 'amount': 1000, 'year': 2026, 'month': 10}, headers=ceo_headers)
    assert r.status_code == 200
    r = client.delete(f'/admin/api/accounts/{uid}', headers=ceo_headers)
    assert r.status_code == 200, r.data
    with flask_app.app_context():
        emp = Employee.query.get(emp_id)
        assert emp is not None and emp.user_id is None
        assert PayrollPayment.query.filter_by(employee_id=emp_id).count() == 1


def test_sqlite_migration_rebuilds_legacy_tables(client):
    """Simulate the production DB (user_id NOT NULL, no employee_id) and run the migration."""
    with flask_app.app_context():
        db.session.execute(text('DROP TABLE payroll_payment'))
        db.session.execute(text('DROP TABLE salary_history'))
        db.session.execute(text('DROP TABLE employee'))
        db.session.execute(text('''CREATE TABLE payroll_payment (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES admin(id),
            period_year INTEGER NOT NULL, period_month INTEGER NOT NULL, amount FLOAT NOT NULL,
            kind VARCHAR(20) NOT NULL, source_tenant_id INTEGER, notes TEXT, paid_at DATETIME, paid_by VARCHAR(80))'''))
        db.session.execute(text('''CREATE TABLE salary_history (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES admin(id), monthly_salary FLOAT NOT NULL,
            effective_from DATE NOT NULL, effective_to DATE, created_at DATETIME, created_by VARCHAR(80))'''))
        db.session.commit()
        a = create_admin('legacy_mgr', role='MANAGER')
        a.monthly_salary = 0
        a.display_name = 'Legacy Manager'
        db.session.commit()
        db.session.execute(text(f"INSERT INTO payroll_payment (user_id, period_year, period_month, amount, kind, paid_by) VALUES ({a.id}, 2026, 7, 12345, 'commission', 'CEO')"))
        db.session.execute(text(f"INSERT INTO salary_history (user_id, monthly_salary, effective_from, created_by) VALUES ({a.id}, 0, '2026-06-01', 'CEO')"))
        db.session.commit()
        db.create_all()
        app_module._migrate_payroll_to_employees('sqlite')

        emp = Employee.query.filter_by(user_id=a.id).first()
        assert emp is not None and emp.full_name == 'Legacy Manager'
        p = PayrollPayment.query.one()
        assert p.employee_id == emp.id and p.amount == 12345 and p.user_id == a.id
        assert SalaryHistory.query.one().employee_id == emp.id
        # second run is a no-op
        app_module._migrate_payroll_to_employees('sqlite')
        assert Employee.query.count() == 1
        # legacy table is gone and the rebuilt table accepts rows with no account
        tables = {r[0] for r in db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert 'payroll_payment_old' not in tables
        db.session.add(PayrollPayment(employee_id=emp.id, period_year=2026, period_month=9, amount=1, kind='salary'))
        db.session.commit()


def test_statement_and_register_pdfs(client):
    headers = _login_as(client, 'mgr_pdf', 'MANAGER')
    r = client.post('/admin/api/payroll/staff', json={'full_name': 'Bola Cleaner', 'job_title': 'Cleaner', 'monthly_salary': 35000, 'start_date': '2026-08-01'}, headers=headers)
    emp_id = json.loads(r.data)['employee']['id']
    r = client.post('/admin/api/payroll/backfill', json={'employee_id': emp_id, 'from_year': 2026, 'from_month': 8, 'to_year': 2026, 'to_month': 10}, headers=headers)
    assert r.status_code == 200
    r = client.get(f'/admin/payroll/statement/{emp_id}')
    assert r.status_code == 200 and r.data.startswith(b'%PDF') and 'Payment-Statement-Bola-Cleaner' in r.headers['Content-Disposition']
    r = client.get(f'/admin/payroll/statement/{emp_id}?from=2026-09&to=2026-09&inline=1')
    assert r.status_code == 200 and r.headers['Content-Disposition'].startswith('inline')
    r = client.get('/admin/payroll/register?year=2026&month=9')
    assert r.status_code == 200 and r.data.startswith(b'%PDF')
    assert client.get('/admin/payroll/statement/999999').status_code == 404
    client.get('/admin/logout')
    _login_as(client, 'pa_pdf', 'PA')
    assert client.get(f'/admin/payroll/statement/{emp_id}').status_code == 403
