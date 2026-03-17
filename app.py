import os, sqlite3, uuid, hashlib, json, datetime
import tornado.ioloop, tornado.web, tornado.escape
from jinja2 import Environment, FileSystemLoader, select_autoescape
import bcrypt, resend

# ââ Config ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.environ.get('DATA_DIR', BASE_DIR)
UPLOAD_DIR  = os.environ.get('UPLOAD_DIR', os.path.join(BASE_DIR, 'uploads'))
DB_PATH     = os.path.join(DATA_DIR, 'addrchange.db')
SECRET      = os.environ.get('COOKIE_SECRET', 'addrchange-secret-key-2026')
MAX_UPLOAD  = 10 * 1024 * 1024  # 10 MB

RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
FROM_EMAIL     = os.environ.get('FROM_EMAIL', 'noreply@updates.postaruba.app')
NOTIFY_EMAIL   = os.environ.get('NOTIFY_EMAIL', '')

PRICE_INDIVIDUAL = 25
PRICE_COMPANY    = 80

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ââ Email helper âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def send_email(to, subject, html):
    if not RESEND_API_KEY:
        print(f'[Email] No API key â skipping email to {to}')
        return
    try:
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({"from": FROM_EMAIL, "to": to, "subject": subject, "html": html})
        print(f'[Email] Sent "{subject}" to {to}')
    except Exception as e:
        print(f'[Email] Failed to send to {to}: {e}')

# ââ Database âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS requests (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                email       TEXT NOT NULL,
                phone       TEXT,
                old_address TEXT NOT NULL,
                new_address TEXT NOT NULL,
                req_type    TEXT NOT NULL DEFAULT "individual",
                kvk         TEXT,
                price       INTEGER NOT NULL,
                id_file     TEXT,
                kvk_file    TEXT,
                status      TEXT NOT NULL DEFAULT "pending",
                payment_status TEXT NOT NULL DEFAULT "unpaid",
                payment_date   TEXT,
                start_date     TEXT,
                expiry_date    TEXT,
                expiry_notif_sent INTEGER DEFAULT 0,
                client_notif_sent INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL,
                notes       TEXT
            );
            CREATE TABLE IF NOT EXISTS staff (
                id       TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role     TEXT NOT NULL DEFAULT "mpc"
            );
        ''')
        # Seed default admin if not exists
        cur = conn.execute("SELECT id FROM staff WHERE username='admin'")
        if not cur.fetchone():
            hashed = bcrypt.hashpw(b'admin123', bcrypt.gensalt()).decode()
            conn.execute("INSERT INTO staff (id,username,password,role) VALUES (?,?,?,?)",
                         (str(uuid.uuid4()), 'admin', hashed, 'admin'))
        cur2 = conn.execute("SELECT id FROM staff WHERE username='finance'")
        if not cur2.fetchone():
            hashed2 = bcrypt.hashpw(b'finance123', bcrypt.gensalt()).decode()
            conn.execute("INSERT INTO staff (id,username,password,role) VALUES (?,?,?,?)",
                         (str(uuid.uuid4()), 'finance', hashed2, 'finance'))
        cur3 = conn.execute("SELECT id FROM staff WHERE username='mpc'")
        if not cur3.fetchone():
            hashed3 = bcrypt.hashpw(b'mpc123', bcrypt.gensalt()).decode()
            conn.execute("INSERT INTO staff (id,username,password,role) VALUES (?,?,?,?)",
                         (str(uuid.uuid4()), 'mpc', hashed3, 'mpc'))

# ââ Jinja2 âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
jinja_env = Environment(
    loader=FileSystemLoader(os.path.join(BASE_DIR, 'templates')),
    autoescape=select_autoescape(['html'])
)

def render(template_name, **kwargs):
    t = jinja_env.get_template(template_name)
    return t.render(**kwargs)

# ââ Base handler âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        uid = self.get_secure_cookie('staff_id')
        if not uid:
            return None
        with get_db() as conn:
            row = conn.execute("SELECT * FROM staff WHERE id=?", (uid.decode(),)).fetchone()
        return dict(row) if row else None

    def render_template(self, name, **kwargs):
        user = self.get_current_user()
        self.write(render(name, current_user=user, **kwargs))

# ââ Public: request form ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
class RequestFormHandler(BaseHandler):
    def get(self):
        self.render_template('request_form.html', error=None, success=False)

    def post(self):
        name        = self.get_argument('name', '').strip()
        email       = self.get_argument('email', '').strip()
        phone       = self.get_argument('phone', '').strip()
        old_address = self.get_argument('old_address', '').strip()
        new_address = self.get_argument('new_address', '').strip()
        req_type    = self.get_argument('req_type', 'individual')
        kvk         = self.get_argument('kvk', '').strip()
        agree       = self.get_argument('agree', '')

        if not agree:
            self.render_template('request_form.html', error='You must agree to the Terms & Conditions.', success=False)
            return
        if not all([name, email, old_address, new_address]):
            self.render_template('request_form.html', error='Please fill in all required fields.', success=False)
            return
        if req_type == 'company' and not kvk:
            self.render_template('request_form.html', error='KVK number is required for companies.', success=False)
            return

        price = PRICE_COMPANY if req_type == 'company' else PRICE_INDIVIDUAL

        # Handle file uploads
        id_file  = None
        kvk_file = None

        if 'id_file' in self.request.files and self.request.files['id_file']:
            f = self.request.files['id_file'][0]
            if f['body'] and len(f['body']) <= MAX_UPLOAD:
                ext = os.path.splitext(f['filename'])[1].lower()
                fname = f'id_{uuid.uuid4().hex}{ext}'
                fpath = os.path.join(UPLOAD_DIR, fname)
                with open(fpath, 'wb') as fp:
                    fp.write(f['body'])
                id_file = fname

        if req_type == 'company' and 'kvk_file' in self.request.files and self.request.files['kvk_file']:
            f = self.request.files['kvk_file'][0]
            if f['body'] and len(f['body']) <= MAX_UPLOAD:
                ext = os.path.splitext(f['filename'])[1].lower()
                fname = f'kvk_{uuid.uuid4().hex}{ext}'
                fpath = os.path.join(UPLOAD_DIR, fname)
                with open(fpath, 'wb') as fp:
                    fp.write(f['body'])
                kvk_file = fname

        req_id     = str(uuid.uuid4())[:8].upper()
        created_at = datetime.datetime.utcnow().isoformat()

        with get_db() as conn:
            conn.execute('''
                INSERT INTO requests
                  (id, name, email, phone, old_address, new_address, req_type, kvk,
                   price, id_file, kvk_file, status, payment_status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (req_id, name, email, phone, old_address, new_address,
                  req_type, kvk, price, id_file, kvk_file, 'pending', 'unpaid', created_at))

        # Send confirmation to client
        send_email(
            to=email,
            subject='Post Aruba â Address Change Request Received',
            html=f'''
            <div style="font-family:Arial,sans-serif;max-width:600px;color:#333">
            <h2 style="color:#3a378b">Address Change Request Received</h2>
            <p>Dear {name},</p>
            <p>We have received your address change request. Our team will process it within <strong>5 business days</strong> after payment is confirmed.</p>
            <table style="border-collapse:collapse;width:100%;margin:12px 0">
              <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Request ID</td><td style="padding:8px;border:1px solid #e0e0e0">{req_id}</td></tr>
              <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Old Address</td><td style="padding:8px;border:1px solid #e0e0e0">{old_address}</td></tr>
              <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">New Address</td><td style="padding:8px;border:1px solid #e0e0e0">{new_address}</td></tr>
              <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Service Fee</td><td style="padding:8px;border:1px solid #e0e0e0">AWG {price},-</td></tr>
            </table>
            <p>Once payment is received, your address change will be active within 5 days and valid for <strong>3 months</strong>.</p>
            <p>Kind regards,<br><strong>Post Aruba</strong></p>
            </div>'''
        )

        # Notify team
        if NOTIFY_EMAIL:
            type_label = 'Company' if req_type == 'company' else 'Individual'
            send_email(
                to=NOTIFY_EMAIL,
                subject=f'New Address Change Request â {req_id} ({name})',
                html=f'''
                <div style="font-family:Arial,sans-serif;max-width:600px;color:#333">
                <h2 style="color:#3a378b">New Address Change Request</h2>
                <table style="border-collapse:collapse;width:100%;margin:12px 0">
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Request ID</td><td style="padding:8px;border:1px solid #e0e0e0">{req_id}</td></tr>
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Name</td><td style="padding:8px;border:1px solid #e0e0e0">{name}</td></tr>
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Email</td><td style="padding:8px;border:1px solid #e0e0e0">{email}</td></tr>
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Phone</td><td style="padding:8px;border:1px solid #e0e0e0">{phone or "â"}</td></tr>
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Type</td><td style="padding:8px;border:1px solid #e0e0e0">{type_label}</td></tr>
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Old Address</td><td style="padding:8px;border:1px solid #e0e0e0">{old_address}</td></tr>
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">New Address</td><td style="padding:8px;border:1px solid #e0e0e0">{new_address}</td></tr>
                  {f'<tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">KVK</td><td style="padding:8px;border:1px solid #e0e0e0">{kvk}</td></tr>' if kvk else ''}
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Fee</td><td style="padding:8px;border:1px solid #e0e0e0">AWG {price},-</td></tr>
                </table>
                <p><a href="https://addrchange.postaruba.app/staff/request/{req_id}" style="color:#3a378b;font-weight:bold">View request in dashboard â</a></p>
                </div>'''
            )

        self.redirect(f'/confirmation/{req_id}')


class ConfirmationHandler(BaseHandler):
    def get(self, req_id):
        with get_db() as conn:
            req = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
        if not req:
            raise tornado.web.HTTPError(404)
        self.render_template('confirmation.html', req=dict(req))


# ââ Staff: login / logout âââââââââââââââââââââââââââââââââââââââââââââââââââââ
class StaffLoginHandler(BaseHandler):
    def get(self):
        if self.get_current_user():
            self.redirect('/staff/dashboard')
            return
        self.render_template('staff_login.html', error=None)

    def post(self):
        username = self.get_argument('username', '').strip()
        password = self.get_argument('password', '').encode()
        with get_db() as conn:
            row = conn.execute("SELECT * FROM staff WHERE username=?", (username,)).fetchone()
        if row and bcrypt.checkpw(password, row['password'].encode()):
            self.set_secure_cookie('staff_id', row['id'], expires_days=1)
            self.redirect('/staff/dashboard')
        else:
            self.render_template('staff_login.html', error='Invalid username or password.')


class StaffLogoutHandler(BaseHandler):
    def get(self):
        self.clear_cookie('staff_id')
        self.redirect('/staff/login')


# ââ Staff: dashboard ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
class StaffDashboardHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self):
        status_filter  = self.get_argument('status', '')
        payment_filter = self.get_argument('payment', '')
        search         = self.get_argument('q', '')

        query  = "SELECT * FROM requests WHERE 1=1"
        params = []
        if status_filter:
            query += " AND status=?"; params.append(status_filter)
        if payment_filter:
            query += " AND payment_status=?"; params.append(payment_filter)
        if search:
            query += " AND (name LIKE ? OR email LIKE ? OR id LIKE ?)"
            params += [f'%{search}%', f'%{search}%', f'%{search}%']
        query += " ORDER BY created_at DESC"

        with get_db() as conn:
            rows = conn.execute(query, params).fetchall()

        self.render_template('staff_dashboard.html',
                             requests=[dict(r) for r in rows],
                             status_filter=status_filter,
                             payment_filter=payment_filter,
                             search=search)

    def get_login_url(self):
        return '/staff/login'


# ââ Staff: view/edit request ââââââââââââââââââââââââââââââââââââââââââââââââââ
class StaffRequestHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self, req_id):
        with get_db() as conn:
            req = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
        if not req:
            raise tornado.web.HTTPError(404)
        self.render_template('staff_request.html', req=dict(req), saved=False, error=None)

    @tornado.web.authenticated
    def post(self, req_id):
        user = self.get_current_user()
        role = user['role']

        with get_db() as conn:
            req = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
            if not req:
                raise tornado.web.HTTPError(404)
            req = dict(req)

            if role == 'mpc':
                self.render_template('staff_request.html', req=req,
                                     saved=False, error='You do not have permission to edit.')
                return

            if role == 'finance':
                # Finance can only edit payment status and payment date
                payment_status = self.get_argument('payment_status', req['payment_status'])
                payment_date   = self.get_argument('payment_date', req['payment_date'] or '')

                # If payment just confirmed, compute start & expiry dates
                start_date  = req['start_date']
                expiry_date = req['expiry_date']
                if payment_status == 'paid' and req['payment_status'] != 'paid' and payment_date:
                    pd = datetime.date.fromisoformat(payment_date)
                    sd = pd + datetime.timedelta(days=5)
                    ed = sd + datetime.timedelta(days=91)  # ~3 months
                    start_date  = sd.isoformat()
                    expiry_date = ed.isoformat()

                conn.execute('''UPDATE requests SET payment_status=?, payment_date=?,
                                start_date=?, expiry_date=? WHERE id=?''',
                             (payment_status, payment_date or None, start_date, expiry_date, req_id))

            elif role == 'admin':
                # Admin can edit everything
                name           = self.get_argument('name', req['name'])
                email          = self.get_argument('email', req['email'])
                phone          = self.get_argument('phone', req['phone'] or '')
                old_address    = self.get_argument('old_address', req['old_address'])
                new_address    = self.get_argument('new_address', req['new_address'])
                status         = self.get_argument('status', req['status'])
                payment_status = self.get_argument('payment_status', req['payment_status'])
                payment_date   = self.get_argument('payment_date', req['payment_date'] or '')
                start_date     = self.get_argument('start_date', req['start_date'] or '')
                expiry_date    = self.get_argument('expiry_date', req['expiry_date'] or '')
                notes          = self.get_argument('notes', req['notes'] or '')

                # Auto-compute dates if payment just confirmed
                if payment_status == 'paid' and req['payment_status'] != 'paid' and payment_date and not start_date:
                    pd = datetime.date.fromisoformat(payment_date)
                    sd = pd + datetime.timedelta(days=5)
                    ed = sd + datetime.timedelta(days=91)
                    start_date  = sd.isoformat()
                    expiry_date = ed.isoformat()

                conn.execute('''UPDATE requests SET
                    name=?, email=?, phone=?, old_address=?, new_address=?,
                    status=?, payment_status=?, payment_date=?,
                    start_date=?, expiry_date=?, notes=? WHERE id=?''',
                    (name, email, phone or None, old_address, new_address,
                     status, payment_status, payment_date or None,
                     start_date or None, expiry_date or None, notes or None, req_id))

        with get_db() as conn:
            req = dict(conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone())

        self.render_template('staff_request.html', req=req, saved=True, error=None)

    def get_login_url(self):
        return '/staff/login'


# ââ File download âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
class FileDownloadHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self, filename):
        path = os.path.join(UPLOAD_DIR, filename)
        if not os.path.exists(path) or '..' in filename:
            raise tornado.web.HTTPError(404)
        self.set_header('Content-Disposition', f'attachment; filename="{filename}"')
        with open(path, 'rb') as f:
            self.write(f.read())

    def get_login_url(self):
        return '/staff/login'


# ââ Background job: expiry reminders âââââââââââââââââââââââââââââââââââââââââ
def check_expiry_reminders():
    today = datetime.date.today()
    week_from_now = (today + datetime.timedelta(days=7)).isoformat()
    today_str = today.isoformat()

    with get_db() as conn:
        # Requests expiring within 7 days where client hasn't been notified
        expiring_soon = conn.execute('''
            SELECT * FROM requests
            WHERE expiry_date IS NOT NULL
              AND expiry_date <= ?
              AND expiry_date > ?
              AND client_notif_sent = 0
              AND payment_status = "paid"
        ''', (week_from_now, today_str)).fetchall()

        for r in expiring_soon:
            r = dict(r)
            # Email client
            send_email(
                to=r['email'],
                subject='Post Aruba â Your Address Change Service Expires Soon',
                html=f'''
                <div style="font-family:Arial,sans-serif;max-width:600px;color:#333">
                <h2 style="color:#3a378b">Your Address Change Service Expires Soon</h2>
                <p>Dear {r["name"]},</p>
                <p>Your 3-month address change service is expiring on <strong>{r["expiry_date"]}</strong>.</p>
                <p>If you wish to continue the service, please submit a new request before the expiry date.</p>
                <table style="border-collapse:collapse;width:100%;margin:12px 0">
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Request ID</td><td style="padding:8px;border:1px solid #e0e0e0">{r["id"]}</td></tr>
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Old Address</td><td style="padding:8px;border:1px solid #e0e0e0">{r["old_address"]}</td></tr>
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">New Address</td><td style="padding:8px;border:1px solid #e0e0e0">{r["new_address"]}</td></tr>
                  <tr><td style="padding:8px;border:1px solid #e0e0e0;background:#f7f7f7;font-weight:bold">Expiry Date</td><td style="padding:8px;border:1px solid #e0e0e0">{r["expiry_date"]}</td></tr>
                </table>
                <p>Kind regards,<br><strong>Post Aruba</strong></p>
                </div>'''
            )
            # Notify team
            if NOTIFY_EMAIL:
                send_email(
                    to=NOTIFY_EMAIL,
                    subject=f'Address Change Expiring Soon â {r["id"]} ({r["name"]})',
                    html=f'''
                    <div style="font-family:Arial,sans-serif;max-width:600px;color:#333">
                    <h2 style="color:#3a378b">Address Change Expiring Soon</h2>
                    <p>Request <strong>{r["id"]}</strong> for <strong>{r["name"]}</strong> ({r["email"]}) expires on <strong>{r["expiry_date"]}</strong>.</p>
                    <p><a href="https://addrchange.postaruba.app/staff/request/{r["id"]}" style="color:#3a378b;font-weight:bold">View request â</a></p>
                    </div>'''
                )
            conn.execute("UPDATE requests SET client_notif_sent=1 WHERE id=?", (r['id'],))
            print(f'[Reminder] Sent expiry reminder for {r["id"]}')

        # Mark expired requests
        conn.execute('''
            UPDATE requests SET status="expired"
            WHERE expiry_date IS NOT NULL
              AND expiry_date < ?
              AND status NOT IN ("expired","cancelled")
        ''', (today_str,))


# ââ App setup ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def make_app():
    return tornado.web.Application([
        (r'/',                         RequestFormHandler),
        (r'/confirmation/([A-Z0-9]+)', ConfirmationHandler),
        (r'/staff/login',              StaffLoginHandler),
        (r'/staff/logout',             StaffLogoutHandler),
        (r'/staff/dashboard',          StaffDashboardHandler),
        (r'/staff/request/([A-Z0-9]+)',StaffRequestHandler),
        (r'/staff/files/(.+)',         FileDownloadHandler),
        (r'/static/(.*)',              tornado.web.StaticFileHandler,
                                       {'path': os.path.join(BASE_DIR, 'static')}),
    ],
    cookie_secret=SECRET,
    login_url='/staff/login',
    debug=os.environ.get('DEBUG', '0') == '1',
    xsrf_cookies=False,
    )

if __name__ == '__main__':
    init_db()
    app = make_app()
    port = int(os.environ.get('PORT', 8080))
    app.listen(port)
    print(f'[addrchange] Listening on :{port}')

    # Check expiry reminders every 6 hours
    pc = tornado.ioloop.PeriodicCallback(check_expiry_reminders, 6 * 60 * 60 * 1000)
    pc.start()

    tornado.ioloop.IOLoop.current().start()
