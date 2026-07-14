from flask import Flask, request, jsonify, render_template
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from dotenv import load_dotenv
from datetime import datetime
from twilio.rest import Client as TwilioClient
import psycopg2, psycopg2.extras, os, requests

load_dotenv()

app = Flask(__name__)

app.config['MAIL_SERVER']         = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT']           = 587
app.config['MAIL_USE_TLS']        = True
app.config['MAIL_USERNAME']       = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD']       = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME')
app.config['SECRET_KEY']          = os.getenv('SECRET_KEY', 'sysconic-secret-2026')

mail = Mail(app)
s    = URLSafeTimedSerializer(app.config['SECRET_KEY'])

APPROVERS = {
    'rajesh':  {'name': 'Rajesh',  'email': os.getenv('RAJESH_EMAIL'),  'whatsapp': os.getenv('RAJESH_WHATSAPP')},
    'ajith':   {'name': 'Ajith',   'email': os.getenv('AJITH_EMAIL'),   'whatsapp': os.getenv('AJITH_WHATSAPP')},
    'nishant': {'name': 'Nishant', 'email': os.getenv('NISHANT_EMAIL'), 'whatsapp': os.getenv('NISHANT_WHATSAPP')},
}

APP_URL = os.getenv('APP_URL', 'http://localhost:5000')

# ── Database ─────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(os.getenv('DATABASE_URL'), cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS vouchers (
                    id              SERIAL PRIMARY KEY,
                    payee           TEXT NOT NULL,
                    amount          NUMERIC NOT NULL,
                    currency        TEXT DEFAULT 'AED',
                    payment_method  TEXT DEFAULT 'Bank Transfer',
                    category        TEXT NOT NULL,
                    project         TEXT NOT NULL,
                    invoice_no      TEXT,
                    due_date        TEXT,
                    remarks         TEXT,
                    submitted_by    TEXT,
                    submitted_at    TEXT,
                    status          TEXT DEFAULT 'pending',
                    rajesh_status   TEXT DEFAULT 'pending',
                    ajith_status    TEXT DEFAULT 'pending',
                    nishant_status  TEXT DEFAULT 'pending'
                )
            ''')
            for col, defn in [('currency', "TEXT DEFAULT 'AED'"), ('payment_method', "TEXT DEFAULT 'Bank Transfer'")]:
                try:
                    cur.execute(f'ALTER TABLE vouchers ADD COLUMN IF NOT EXISTS {col} {defn}')
                except:
                    pass
        conn.commit()

# ── Zoho Integration ─────────────────────────────────────────
def get_zoho_access_token():
    resp = requests.post('https://accounts.zoho.com/oauth/v2/token', data={
        'refresh_token': os.getenv('ZOHO_REFRESH_TOKEN'),
        'client_id':     os.getenv('ZOHO_CLIENT_ID'),
        'client_secret': os.getenv('ZOHO_CLIENT_SECRET'),
        'grant_type':    'refresh_token'
    })
    return resp.json().get('access_token')

def get_zoho_projects():
    try:
        token   = get_zoho_access_token()
        org_id  = os.getenv('ZOHO_ORG_ID')
        headers = {'Authorization': f'Zoho-oauthtoken {token}'}
        resp    = requests.get(
            f'https://www.zohoapis.com/books/v3/projects?organization_id={org_id}',
            headers=headers
        )
        data = resp.json()
        projects = data.get('projects', [])
        return [{'id': p.get('project_id'), 'name': p.get('project_name')} for p in projects]
    except Exception as e:
        print(f"Zoho projects error: {e}")
        return []

# ── Routes ───────────────────────────────────────────────────
@app.route('/')
def index():
    try:
        init_db()
    except Exception as e:
        print(f"DB init error: {e}")
    return render_template('index.html')

@app.route('/api/projects', methods=['GET'])
def get_projects():
    projects = get_zoho_projects()
    if not projects:
        # Fallback list if Zoho fails
        projects = [
            {'id': 'general', 'name': 'General / Admin'},
        ]
    return jsonify(projects)

@app.route('/api/vouchers', methods=['GET'])
def get_vouchers():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM vouchers ORDER BY id DESC')
                rows = cur.fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print(f"Get vouchers error: {e}")
        return jsonify([])

@app.route('/api/submit', methods=['POST'])
def submit_voucher():
    data = request.json
    now  = datetime.now().strftime('%Y-%m-%d %H:%M')
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO vouchers (payee, amount, currency, payment_method, category, project, invoice_no, due_date, remarks, submitted_by, submitted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                ''', (data['payee'], data['amount'],
                      data.get('currency', 'AED'),
                      data.get('payment_method', 'Bank Transfer'),
                      data['category'], data['project'],
                      data.get('invoice_no',''), data.get('due_date',''),
                      data.get('remarks',''), data.get('submitted_by','Team'), now))
                voucher_id = cur.fetchone()['id']
            conn.commit()

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM vouchers WHERE id=%s', (voucher_id,))
                voucher = dict(cur.fetchone())

        for key, approver in APPROVERS.items():
            token = s.dumps({'voucher_id': voucher_id, 'approver': key})
            approve_url = f"{APP_URL}/action/{token}/approve"
            reject_url  = f"{APP_URL}/action/{token}/reject"
            send_email(approver, voucher, approve_url, reject_url)

        return jsonify({'success': True, 'voucher_id': voucher_id})

    except Exception as e:
        print(f"Submit error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/action/<token>/<decision>')
def action(token, decision):
    try:
        data = s.loads(token, max_age=7 * 24 * 3600)
    except (BadSignature, SignatureExpired):
        return render_template('result.html', message='This link is invalid or has expired.')

    voucher_id = data['voucher_id']
    approver   = data['approver']

    if approver not in APPROVERS:
        return render_template('result.html', message='Unknown approver.')

    col = f"{approver}_status"

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f'SELECT {col} FROM vouchers WHERE id=%s', (voucher_id,))
                current = cur.fetchone()
                if current and current[col] != 'pending':
                    return render_template('result.html',
                        message='You have already responded to this voucher.')

                cur.execute(f'UPDATE vouchers SET {col}=%s WHERE id=%s', (decision, voucher_id))
                conn.commit()

                cur.execute('SELECT rajesh_status, ajith_status, nishant_status FROM vouchers WHERE id=%s', (voucher_id,))
                row = cur.fetchone()
                statuses = [row['rajesh_status'], row['ajith_status'], row['nishant_status']]

                if all(st == 'approve' for st in statuses):
                    cur.execute("UPDATE vouchers SET status='approved' WHERE id=%s", (voucher_id,))
                    conn.commit()
                    notify_submitter(voucher_id, 'approved')
                elif 'reject' in statuses:
                    cur.execute("UPDATE vouchers SET status='rejected' WHERE id=%s", (voucher_id,))
                    conn.commit()
                    notify_submitter(voucher_id, 'rejected')

    except Exception as e:
        print(f"Action error: {e}")
        return render_template('result.html', message='An error occurred. Please try again.')

    name = APPROVERS[approver]['name']
    return render_template('result.html',
        message=f"Thank you {name}! Your response ({decision}) has been recorded.")

# ── Helpers ──────────────────────────────────────────────────
def send_email(approver, voucher, approve_url, reject_url):
    try:
        currency = voucher.get('currency', 'AED')
        amount   = float(voucher['amount'])
        msg = Message(
            subject=f"[Action Required] Payment Voucher PV-{voucher['id']:04d} - {currency} {amount:,.2f}",
            recipients=[approver['email']]
        )
        msg.html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
          <div style="background:#0f1117;padding:20px;border-radius:8px 8px 0 0">
            <h2 style="color:#4f7eff;margin:0">Sysconic Payment Voucher</h2>
          </div>
          <div style="background:#f8f9fa;padding:24px;border:1px solid #e2e8f0">
            <p>Hi <strong>{approver['name']}</strong>, a payment voucher requires your approval.</p>
            <table style="width:100%;border-collapse:collapse;margin:16px 0">
              <tr><td style="padding:8px;color:#666;width:40%">Voucher #</td><td style="padding:8px;font-weight:bold">PV-{voucher['id']:04d}</td></tr>
              <tr style="background:#fff"><td style="padding:8px;color:#666">Payee</td><td style="padding:8px">{voucher['payee']}</td></tr>
              <tr><td style="padding:8px;color:#666">Amount</td><td style="padding:8px;font-weight:bold;font-size:1.1em">{currency} {amount:,.2f}</td></tr>
              <tr style="background:#fff"><td style="padding:8px;color:#666">Payment Method</td><td style="padding:8px">{voucher.get('payment_method','Bank Transfer')}</td></tr>
              <tr><td style="padding:8px;color:#666">Category</td><td style="padding:8px">{voucher['category']}</td></tr>
              <tr style="background:#fff"><td style="padding:8px;color:#666">Project</td><td style="padding:8px">{voucher['project']}</td></tr>
              <tr><td style="padding:8px;color:#666">Invoice #</td><td style="padding:8px">{voucher.get('invoice_no','-')}</td></tr>
              <tr style="background:#fff"><td style="padding:8px;color:#666">Due Date</td><td style="padding:8px">{voucher.get('due_date','-')}</td></tr>
              <tr><td style="padding:8px;color:#666">Submitted By</td><td style="padding:8px">{voucher.get('submitted_by','-')}</td></tr>
              <tr style="background:#fff"><td style="padding:8px;color:#666">Remarks</td><td style="padding:8px">{voucher.get('remarks','-')}</td></tr>
            </table>
            <div style="text-align:center;margin:24px 0">
              <a href="{approve_url}" style="background:#22c55e;color:#fff;padding:12px 32px;border-radius:6px;text-decoration:none;font-weight:bold;margin-right:12px">Approve</a>
              <a href="{reject_url}"  style="background:#ef4444;color:#fff;padding:12px 32px;border-radius:6px;text-decoration:none;font-weight:bold">Reject</a>
            </div>
            <p style="color:#999;font-size:0.8em;text-align:center">Links expire in 7 days. Sysconic Technologies, Abu Dhabi</p>
          </div>
        </div>
        """
        mail.send(msg)
    except Exception as e:
        print(f"Email error ({approver['name']}): {e}")

def notify_submitter(voucher_id, final_status):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM vouchers WHERE id=%s', (voucher_id,))
                voucher = dict(cur.fetchone())
        submitter_email = os.getenv('SUBMITTER_EMAIL')
        if not submitter_email:
            return
        color  = '#22c55e' if final_status == 'approved' else '#ef4444'
        label  = 'APPROVED' if final_status == 'approved' else 'REJECTED'
        currency = voucher.get('currency', 'AED')
        amount   = float(voucher['amount'])
        msg = Message(
            subject=f"Voucher PV-{voucher['id']:04d} has been {final_status.upper()}",
            recipients=[submitter_email]
        )
        msg.html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:24px">
          <h2 style="color:{color}">{label}</h2>
          <p>Voucher <strong>PV-{voucher['id']:04d}</strong> for <strong>{currency} {amount:,.2f}</strong>
             payable to <strong>{voucher['payee']}</strong> has been <strong>{final_status}</strong>.</p>
          <p style="color:#999;font-size:0.8em">Sysconic Technologies - sysconic.com</p>
        </div>
        """
        mail.send(msg)
    except Exception as e:
        print(f"Submitter notify error: {e}")

if __name__ == '__main__':
    init_db()
    app.run(debug=True)


# ── Admin Routes ─────────────────────────────────────────────
@app.route('/api/admin/verify', methods=['POST'])
def admin_verify():
    data = request.json
    if data.get('pin') == os.getenv('ADMIN_PIN'):
        return jsonify({'success': True})
    return jsonify({'success': False}), 401

@app.route('/api/admin/force', methods=['POST'])
def admin_force():
    data       = request.json
    pin        = data.get('pin')
    voucher_id = data.get('voucher_id')
    decision   = data.get('decision')  # 'approved' or 'rejected'

    if pin != os.getenv('ADMIN_PIN'):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    if decision not in ('approved', 'rejected'):
        return jsonify({'success': False, 'error': 'Invalid decision'}), 400

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                if decision == 'approved':
                    cur.execute("""
                        UPDATE vouchers
                        SET status='approved',
                            rajesh_status='approve',
                            ajith_status='approve',
                            nishant_status='approve'
                        WHERE id=%s
                    """, (voucher_id,))
                else:
                    cur.execute("""
                        UPDATE vouchers
                        SET status='rejected'
                        WHERE id=%s
                    """, (voucher_id,))
                conn.commit()
        notify_submitter(voucher_id, decision)
        return jsonify({'success': True})
    except Exception as e:
        print(f"Admin force error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Projects Tab Route ───────────────────────────────────────
@app.route('/api/zoho-projects', methods=['GET'])
def get_zoho_projects_full():
    try:
        token   = get_zoho_access_token()
        org_id  = os.getenv('ZOHO_ORG_ID')
        headers = {'Authorization': f'Zoho-oauthtoken {token}'}
        resp    = requests.get(
            f'https://www.zohoapis.com/books/v3/projects?organization_id={org_id}&per_page=200',
            headers=headers
        )
        data     = resp.json()
        projects = data.get('projects', [])

        # Get voucher spend per project from our DB
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT project,
                           COUNT(*) as voucher_count,
                           SUM(CASE WHEN status='approved' THEN amount ELSE 0 END) as approved_spend,
                           SUM(CASE WHEN status='pending'  THEN amount ELSE 0 END) as pending_spend
                    FROM vouchers
                    GROUP BY project
                """)
                voucher_rows = {row['project']: dict(row) for row in cur.fetchall()}

        result = []
        for p in projects:
            name  = p.get('project_name', '')
            vdata = voucher_rows.get(name, {})
            result.append({
                'project_id':     p.get('project_id'),
                'project_no':     p.get('cf_project_no', ''),
                'project_name':   name,
                'customer_name':  p.get('customer_name', ''),
                'status':         p.get('status', ''),
                'project_value':  float(p.get('rate', 0)),
                'voucher_count':  vdata.get('voucher_count', 0),
                'approved_spend': float(vdata.get('approved_spend', 0) or 0),
                'pending_spend':  float(vdata.get('pending_spend', 0) or 0),
            })

        return jsonify(result)
    except Exception as e:
        print(f"Zoho projects full error: {e}")
        return jsonify([])


# ── Quotation Routes ─────────────────────────────────────────
def init_quotes_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS quotes (
                    id              SERIAL PRIMARY KEY,
                    quote_no        TEXT,
                    customer_name   TEXT,
                    customer_email  TEXT,
                    project         TEXT,
                    currency        TEXT DEFAULT 'AED',
                    line_items      TEXT,
                    subtotal        NUMERIC DEFAULT 0,
                    line_discount   NUMERIC DEFAULT 0,
                    overall_discount NUMERIC DEFAULT 0,
                    vat_amount      NUMERIC DEFAULT 0,
                    grand_total     NUMERIC DEFAULT 0,
                    notes           TEXT,
                    status          TEXT DEFAULT 'draft',
                    created_at      TEXT,
                    updated_at      TEXT
                )
            ''')
        conn.commit()

@app.route('/api/quotes', methods=['GET'])
def get_quotes():
    try:
        init_quotes_db()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM quotes ORDER BY id DESC')
                rows = cur.fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print(f"Get quotes error: {e}")
        return jsonify([])

@app.route('/api/quotes', methods=['POST'])
def save_quote():
    try:
        init_quotes_db()
        data = request.json
        now  = datetime.now().strftime('%Y-%m-%d %H:%M')
        import json as json_lib

        with get_db() as conn:
            with conn.cursor() as cur:
                if data.get('id'):
                    cur.execute('''
                        UPDATE quotes SET
                            quote_no=%s, customer_name=%s, customer_email=%s, project=%s,
                            currency=%s, line_items=%s, subtotal=%s, line_discount=%s,
                            overall_discount=%s, vat_amount=%s, grand_total=%s,
                            notes=%s, status=%s, updated_at=%s
                        WHERE id=%s
                    ''', (data.get('quote_no'), data.get('customer_name'), data.get('customer_email'),
                          data.get('project'), data.get('currency','AED'),
                          json_lib.dumps(data.get('line_items',[])),
                          data.get('subtotal',0), data.get('line_discount',0),
                          data.get('overall_discount',0), data.get('vat_amount',0),
                          data.get('grand_total',0), data.get('notes',''),
                          data.get('status','draft'), now, data['id']))
                    quote_id = data['id']
                else:
                    cur.execute('''
                        INSERT INTO quotes (quote_no, customer_name, customer_email, project,
                            currency, line_items, subtotal, line_discount, overall_discount,
                            vat_amount, grand_total, notes, status, created_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    ''', (data.get('quote_no'), data.get('customer_name'), data.get('customer_email'),
                          data.get('project'), data.get('currency','AED'),
                          json_lib.dumps(data.get('line_items',[])),
                          data.get('subtotal',0), data.get('line_discount',0),
                          data.get('overall_discount',0), data.get('vat_amount',0),
                          data.get('grand_total',0), data.get('notes',''),
                          data.get('status','draft'), now, now))
                    quote_id = cur.fetchone()['id']
                conn.commit()
        return jsonify({'success': True, 'id': quote_id})
    except Exception as e:
        print(f"Save quote error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/quotes/<int:quote_id>', methods=['DELETE'])
def delete_quote(quote_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM quotes WHERE id=%s', (quote_id,))
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False}), 500