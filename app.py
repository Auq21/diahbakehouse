"""
Diah Bakehouse — Toko Ebook Brownies Lumer Cup
Flask + Midtrans Snap + Flask-Mail + WeasyPrint

Alur:
  1. Buyer isi nama + email di landing page (/)
  2. /beli  → simpan order ke DB, minta Snap token ke Midtrans
  3. Snap.js popup → buyer bayar (QRIS / GoPay / ShopeePay / VA / minimarket)
  4. Midtrans panggil webhook /notifikasi → verifikasi signature
  5. Status settlement → generate PDF ebook (WeasyPrint) → kirim ke email buyer
"""
import os
import re
import time
import hashlib
import sqlite3
import threading
from contextlib import closing

from dotenv import load_dotenv
load_dotenv()  # harus paling awal, sebelum WeasyPrint dipakai (WEASYPRINT_DLL_DIRECTORIES)

from flask import Flask, request, jsonify, render_template, abort, Response
from flask_mail import Mail, Message
from werkzeug.middleware.proxy_fix import ProxyFix
import midtransclient
# WeasyPrint di-import di dalam generate_pdf() supaya web tetap bisa jalan
# walaupun library Pango belum terpasang (misal saat develop di Windows).

app = Flask(__name__)
# Supaya request.host_url tetap https di belakang proxy (Heroku/Railway/Render)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ─────────────────────────────────────────
# KONFIGURASI — semua diambil dari environment (.env)
# ─────────────────────────────────────────
app.config.update(
    SECRET_KEY          = os.environ.get('SECRET_KEY', 'ganti-ini-dengan-random-string'),

    # Midtrans
    MIDTRANS_SERVER_KEY = os.environ.get('MIDTRANS_SERVER_KEY', '').strip().strip('"\'').strip(),
    MIDTRANS_CLIENT_KEY = os.environ.get('MIDTRANS_CLIENT_KEY', '').strip().strip('"\'').strip(),
    MIDTRANS_IS_PROD    = os.environ.get('MIDTRANS_IS_PROD', 'false').strip().strip('"\'').lower() == 'true',

    # Email (default Gmail + App Password)
    MAIL_SERVER         = os.environ.get('MAIL_SERVER', 'smtp.gmail.com'),
    MAIL_PORT           = int(os.environ.get('MAIL_PORT', 587)),
    MAIL_USE_TLS        = True,
    MAIL_USERNAME       = os.environ.get('MAIL_USERNAME', ''),
    MAIL_PASSWORD       = os.environ.get('MAIL_PASSWORD', ''),
    MAIL_DEFAULT_SENDER = ('Diah Bakehouse', os.environ.get('MAIL_USERNAME', '')),

    # Produk
    HARGA_EBOOK         = int(os.environ.get('HARGA_EBOOK', 29000)),
    NAMA_PRODUK         = 'Ebook Brownies Lumer Cup — @diah_bakehouse',

    # Lain-lain
    ADMIN_KEY           = os.environ.get('ADMIN_KEY', ''),
    DATABASE_PATH       = os.environ.get('DATABASE_PATH', 'orders.db'),
)

mail = Mail(app)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


# ─────────────────────────────────────────
# DATABASE (SQLite) — menyimpan nama/email per order.
# Penting: notifikasi Midtrans TIDAK membawa customer_details,
# jadi nama & email harus kita simpan sendiri saat /beli.
# ─────────────────────────────────────────
def db():
    conn = sqlite3.connect(app.config['DATABASE_PATH'], timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id    TEXT PRIMARY KEY,
                nama        TEXT NOT NULL,
                email       TEXT NOT NULL,
                harga       INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                terkirim    INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            )
        """)


init_db()


def get_order(order_id):
    with closing(db()) as conn:
        return conn.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,)).fetchone()


def update_status(order_id, status):
    with closing(db()) as conn, conn:
        conn.execute('UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?',
                     (status, int(time.time()), order_id))


def klaim_pengiriman(order_id):
    """
    Tandai order 'sedang/sudah dikirim' secara atomik.
    Return True kalau kita yang berhak mengirim (mencegah email dobel
    karena Midtrans bisa mengirim notifikasi settlement lebih dari sekali).
    """
    with closing(db()) as conn, conn:
        cur = conn.execute('UPDATE orders SET terkirim = 1, updated_at = ? '
                           'WHERE order_id = ? AND terkirim = 0',
                           (int(time.time()), order_id))
        return cur.rowcount == 1


def batal_klaim(order_id):
    with closing(db()) as conn, conn:
        conn.execute('UPDATE orders SET terkirim = 0 WHERE order_id = ?', (order_id,))


# ─────────────────────────────────────────
# MIDTRANS CLIENT
# ─────────────────────────────────────────
def get_snap():
    return midtransclient.Snap(
        is_production = app.config['MIDTRANS_IS_PROD'],
        server_key    = app.config['MIDTRANS_SERVER_KEY'],
        client_key    = app.config['MIDTRANS_CLIENT_KEY'],
    )


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.route('/')
def index():
    """Landing page penjualan ebook."""
    return render_template(
        'index.html',
        harga      = app.config['HARGA_EBOOK'],
        produk     = app.config['NAMA_PRODUK'],
        client_key = app.config['MIDTRANS_CLIENT_KEY'],
        is_prod    = app.config['MIDTRANS_IS_PROD'],
    )


@app.route('/beli', methods=['POST'])
def beli():
    """Buyer submit form → simpan order → minta Snap token ke Midtrans."""
    data  = request.get_json(silent=True) or {}
    nama  = (data.get('nama') or '').strip()[:80]
    email = (data.get('email') or '').strip().lower()[:120]

    if not nama or not email:
        return jsonify({'error': 'Nama dan email wajib diisi.'}), 400
    if not EMAIL_RE.match(email):
        return jsonify({'error': 'Format email tidak valid.'}), 400

    order_id = f"DBH-{int(time.time())}-{hashlib.md5((email + str(time.time_ns())).encode()).hexdigest()[:6].upper()}"
    harga    = app.config['HARGA_EBOOK']

    param = {
        "transaction_details": {"order_id": order_id, "gross_amount": harga},
        "customer_details":    {"first_name": nama, "email": email},
        "item_details": [{
            "id": "ebook-brownies-lumer", "price": harga, "quantity": 1,
            "name": "Ebook Brownies Lumer Cup",  # maks 50 karakter
        }],
        # Metode pembayaran yang ditampilkan. OVO, DANA, LinkAja, dll.
        # bisa bayar lewat QRIS (other_qris). Metode harus AKTIF di dashboard
        # Midtrans. Kalau ragu, hapus key ini → Snap tampilkan semua yang aktif.
        "enabled_payments": [
            "other_qris", "gopay", "shopeepay",
            "bca_va", "bni_va", "bri_va", "permata_va", "echannel", "other_va",
            "indomaret", "alfamart",
        ],
        "callbacks": {"finish": request.host_url.rstrip('/') + '/sukses'},
        "expiry": {"unit": "hours", "duration": 24},
    }

    try:
        transaction = get_snap().create_transaction(param)
    except Exception as e:
        app.logger.exception('Gagal membuat transaksi Midtrans')
        return jsonify({'error': 'Gagal membuat transaksi. Coba lagi sebentar ya.'}), 502

    now = int(time.time())
    with closing(db()) as conn, conn:
        conn.execute('INSERT INTO orders (order_id, nama, email, harga, status, created_at, updated_at) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (order_id, nama, email, harga, 'pending', now, now))

    return jsonify({'snap_token': transaction['token'], 'order_id': order_id})


@app.route('/notifikasi', methods=['POST'])
def notifikasi():
    """
    Webhook dari Midtrans (set di Dashboard → Settings → Payment → Notification URL:
    https://domainkamu.com/notifikasi). Verifikasi signature → kirim ebook.
    """
    notif = request.get_json(silent=True) or {}
    order_id     = notif.get('order_id', '')
    status_code  = notif.get('status_code', '')
    gross_amount = notif.get('gross_amount', '')
    status       = notif.get('transaction_status')
    fraud        = notif.get('fraud_status')

    # 1) Verifikasi signature (SHA512 order_id + status_code + gross_amount + server_key)
    raw_sig = order_id + status_code + gross_amount + app.config['MIDTRANS_SERVER_KEY']
    if notif.get('signature_key') != hashlib.sha512(raw_sig.encode()).hexdigest():
        return jsonify({'status': 'invalid signature'}), 403

    order = get_order(order_id)
    if order is None:
        # Bisa jadi notifikasi test dari dashboard Midtrans — balas 200 supaya tidak di-retry
        return jsonify({'status': 'order tidak dikenal'}), 200

    # 2) Cek nominal sesuai harga di DB
    try:
        if int(float(gross_amount)) != order['harga']:
            app.logger.warning('Nominal tidak cocok untuk %s: %s', order_id, gross_amount)
            return jsonify({'status': 'nominal tidak cocok'}), 200
    except ValueError:
        return jsonify({'status': 'nominal invalid'}), 200

    update_status(order_id, status or 'unknown')

    sukses = (status == 'settlement') or (status == 'capture' and fraud == 'accept')
    if sukses and klaim_pengiriman(order_id):
        # Kirim di background supaya webhook cepat membalas (Midtrans ada timeout)
        threading.Thread(
            target=kirim_ebook_bg,
            args=(order['email'], order['nama'], order_id),
            daemon=True,
        ).start()

    return jsonify({'status': 'ok'})


@app.route('/sukses')
def sukses():
    """Halaman setelah bayar (redirect dari Snap: ?order_id=...&transaction_status=...)."""
    order_id = request.args.get('order_id', '')
    order    = get_order(order_id) if order_id else None
    status   = (order['status'] if order else None) or request.args.get('transaction_status', '')
    return render_template('sukses.html', order=order, order_id=order_id, status=status)


@app.route('/status/<order_id>')
def cek_status(order_id):
    """Dipanggil halaman sukses untuk polling status (tanpa data pribadi)."""
    order = get_order(order_id)
    if not order:
        return jsonify({'status': 'not_found'}), 404
    return jsonify({'status': order['status'], 'terkirim': bool(order['terkirim'])})


# ─────────────────────────────────────────
# ADMIN (butuh ?key=ADMIN_KEY)
# ─────────────────────────────────────────
def cek_admin():
    key = app.config['ADMIN_KEY']
    if not key or request.args.get('key') != key:
        abort(404)


@app.route('/preview-ebook')
def preview_ebook():
    """Preview ebook di browser. Tambah &pdf=1 untuk lihat hasil PDF."""
    cek_admin()
    if request.args.get('pdf'):
        return Response(generate_pdf('Preview Admin', 'DBH-PREVIEW'), mimetype='application/pdf')
    return render_template('ebook.html', nama='Preview Admin', order_id='DBH-PREVIEW')


@app.route('/admin/kirim-ulang/<order_id>')
def kirim_ulang(order_id):
    """Kirim ulang ebook untuk order yang sudah lunas (misal email buyer salah ketik → update dulu di DB)."""
    cek_admin()
    order = get_order(order_id)
    if not order:
        abort(404)
    if order['status'] not in ('settlement', 'capture'):
        return jsonify({'error': f"Order belum lunas (status: {order['status']})"}), 400
    ok = kirim_ebook(order['email'], order['nama'], order_id)
    return jsonify({'terkirim': ok, 'email': order['email']})


@app.route('/admin/cek-midtrans')
def cek_midtrans():
    """Diagnosa koneksi Midtrans: key apa yang terbaca server & apakah diterima Midtrans."""
    cek_admin()
    import base64, requests
    sk, ck = app.config['MIDTRANS_SERVER_KEY'], app.config['MIDTRANS_CLIENT_KEY']
    prod = app.config['MIDTRANS_IS_PROD']
    raw = os.environ.get('MIDTRANS_SERVER_KEY', '')
    samar = lambda k: (k[:12] + '...' + k[-4:]) if len(k) > 16 else ('(kosong)' if not k else k[:4] + '...')
    base = 'https://api.midtrans.com' if prod else 'https://api.sandbox.midtrans.com'
    try:
        auth = base64.b64encode((sk + ':').encode()).decode()
        r = requests.get(f'{base}/v2/cek-koneksi-dbh/status',
                         headers={'Authorization': 'Basic ' + auth, 'Accept': 'application/json'}, timeout=15)
        body = r.json() if 'json' in r.headers.get('content-type', '') else r.text[:300]
        kode = str(body.get('status_code', r.status_code)) if isinstance(body, dict) else str(r.status_code)
        hasil = ('KEY DITERIMA ✅ (404 = order tes memang tidak ada, itu normal)' if kode == '404'
                 else 'KEY DITOLAK ❌ — Server Key salah / beda environment' if kode == '401'
                 else f'Status {kode}')
    except Exception as e:
        body, hasil = str(e), 'Gagal menghubungi Midtrans'
    return jsonify({
        'environment_dipakai': 'PRODUCTION' if prod else 'SANDBOX',
        'endpoint_dites': base,
        'server_key_terbaca': samar(sk),
        'server_key_panjang': len(sk),
        'server_key_ada_kata_server': 'server' in sk.lower(),
        'server_key_awalan_SB': sk.startswith('SB-'),
        'server_key_asli_ada_spasi_atau_kutip': raw != raw.strip().strip('"\'').strip(),
        'client_key_terbaca': samar(ck),
        'client_key_ada_kata_client': 'client' in ck.lower(),
        'hasil_tes': hasil,
        'respon_midtrans': body,
    })


# ─────────────────────────────────────────
# FUNGSI BANTU
# ─────────────────────────────────────────
def generate_pdf(nama=None, order_id=None):
    """Render template ebook → bytes PDF (dipersonalisasi nama buyer)."""
    from weasyprint import HTML  # import lambat, lihat catatan di atas
    html_string = render_template('ebook.html', nama=nama, order_id=order_id)
    return HTML(string=html_string, base_url=app.root_path).write_pdf()


def kirim_ebook(email, nama, order_id):
    """Generate PDF dan kirim ke email buyer. Return True kalau berhasil."""
    try:
        pdf_bytes = generate_pdf(nama, order_id)
        msg = Message(
            subject    = '🍫 Ebook Brownies Lumer Cup kamu sudah siap!',
            recipients = [email],
        )
        msg.html = render_template('email_sukses.html', nama=nama, order_id=order_id)
        msg.body = (f"Halo {nama},\n\nTerima kasih sudah membeli Ebook Brownies Lumer Cup!\n"
                    f"Ebook terlampir dalam format PDF.\n\nOrder ID: {order_id}\n\n"
                    "Salam manis,\nDiah — @diah_bakehouse")
        msg.attach(
            filename     = 'Brownies-Lumer-Cup-DiahBakehouse.pdf',
            content_type = 'application/pdf',
            data         = pdf_bytes,
        )
        mail.send(msg)
        app.logger.info('[OK] Ebook terkirim ke %s — order %s', email, order_id)
        return True
    except Exception:
        app.logger.exception('[ERROR] Gagal kirim ebook ke %s — order %s', email, order_id)
        return False


def kirim_ebook_bg(email, nama, order_id):
    """Versi background thread: butuh app context untuk render_template & mail."""
    with app.app_context():
        if not kirim_ebook(email, nama, order_id):
            batal_klaim(order_id)  # supaya bisa dicoba lagi (notifikasi ulang / kirim-ulang)


# ─────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG') == '1')
