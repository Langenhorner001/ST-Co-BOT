from flask import Flask, jsonify, request as flask_request
from threading import Thread
import requests
import urllib3
import time
import os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Configuration — set via Replit Secrets/Env Vars ─────────────────────────
# BASE_URL      → your Replit HTTPS link  (e.g. https://xxx.riker.replit.dev)
# BASE_URL_PORT → external port           (80 for HTTP, 443 for HTTPS)
BASE_URL      = os.environ.get('BASE_URL', '').strip().rstrip('/')
BASE_URL_PORT = int(os.environ.get('BASE_URL_PORT', '80'))
_INTERNAL_PORT = 8099
# ──────────────────────────────────────────────────────────────────────────────

app = Flask('')
start_time = time.time()

# ── Lazy-import Stripe core (avoids loading telebot at startup) ──────────────
_stripe_core = None
def _get_stripe_core():
    global _stripe_core
    if _stripe_core is None:
        import importlib
        _stripe_core = importlib.import_module('stripe_core')
    return _stripe_core

# ── API key for /api/stripe endpoint (set via env var API_SECRET_KEY) ────────
_API_KEY = os.environ.get('API_SECRET_KEY', '').strip()


@app.route('/')
def home():
    uptime_seconds = int(time.time() - start_time)
    hours   = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    return f'''
    <html>
    <head><title>Bot Status</title></head>
    <body style="font-family:Arial;text-align:center;margin-top:50px;background:#1a1a2e;color:#eee;">
        <h1>🤖 Bot is Running!</h1>
        <p>✅ Status: <b style="color:#00ff88">Online</b></p>
        <p>⏱️ Uptime: <b>{hours}h {minutes}m {seconds}s</b></p>
        <p>🔄 Auto-ping: Active</p>
        <p>🌐 Base URL: <b style="color:#88aaff">{BASE_URL or "auto-detect"}</b></p>
        <p>🔌 Port: <b>{BASE_URL_PORT}</b></p>
    </body>
    </html>
    '''


@app.route('/ping')
def ping():
    return jsonify({'status': 'alive', 'uptime': int(time.time() - start_time)})


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


# ─── Stripe Checker API ────────────────────────────────────────────────────────
# POST /api/stripe
# Body params: cc, mes, ano, cvc, link, [key]
# Response: plain-text (compatible with external checker scripts)
#
# Optional: set API_SECRET_KEY env var to require ?key= or body key= param.
# If API_SECRET_KEY is empty, the endpoint is open (no auth required).

@app.route('/api/stripe', methods=['POST', 'GET'])
def api_stripe():
    # ── Read params (POST body or GET query string) ──────────────────────────
    if flask_request.method == 'POST':
        data = flask_request.form
    else:
        data = flask_request.args

    # ── Optional API key check ───────────────────────────────────────────────
    if _API_KEY:
        given = data.get('key', '') or flask_request.args.get('key', '')
        if given != _API_KEY:
            return 'ERROR | Invalid API key', 403

    cc   = (data.get('cc') or '').strip()
    mes  = (data.get('mes') or '').strip()
    ano  = (data.get('ano') or '').strip()
    cvc  = (data.get('cvc') or '').strip()
    link = (data.get('link') or '').strip()

    if not all([cc, mes, ano, cvc, link]):
        return (
            'ERROR | Missing params. Required: cc, mes, ano, cvc, link\n'
            'Example: cc=4111111111111111&mes=12&ano=2026&cvc=123&link=https://checkout.stripe.com/...',
            400
        )

    try:
        core   = _get_stripe_core()
        result = core.stripe_check(link, cc, mes, ano, cvc)

        status   = result.get('status', 'error')
        message  = result.get('message', 'Unknown')
        merchant = result.get('merchant', 'N/A')
        amount   = result.get('amount', 'N/A')
        elapsed  = result.get('time', 'N/A')

        # ── Build plain-text response (matches common checker API format) ────
        if status == 'approved':
            label = 'APPROVED'
        elif status == 'insufficient_funds':
            label = 'CCN - Insufficient Funds'
        elif status == '3ds':
            label = '3DS - OTP Required'
        else:
            label = 'DECLINED'

        card_str = f"{cc}|{mes}|{ano}|{cvc}"
        response_text = (
            f"{label} | {message} | {merchant} | {amount} | "
            f"Card: {card_str} | Time: {elapsed}"
        )
        return response_text, 200

    except Exception as e:
        return f'ERROR | Internal server error: {str(e)}', 500


@app.route('/api/stripe/json', methods=['POST', 'GET'])
def api_stripe_json():
    """Same as /api/stripe but returns JSON instead of plain text."""
    if flask_request.method == 'POST':
        data = flask_request.form
    else:
        data = flask_request.args

    if _API_KEY:
        given = data.get('key', '') or flask_request.args.get('key', '')
        if given != _API_KEY:
            return jsonify({'error': 'Invalid API key'}), 403

    cc   = (data.get('cc') or '').strip()
    mes  = (data.get('mes') or '').strip()
    ano  = (data.get('ano') or '').strip()
    cvc  = (data.get('cvc') or '').strip()
    link = (data.get('link') or '').strip()

    if not all([cc, mes, ano, cvc, link]):
        return jsonify({'error': 'Missing params. Required: cc, mes, ano, cvc, link'}), 400

    try:
        core   = _get_stripe_core()
        result = core.stripe_check(link, cc, mes, ano, cvc)
        result['card'] = f"{cc}|{mes}|{ano}|{cvc}"
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/info', methods=['GET'])
def api_info():
    """Returns API info and usage."""
    auth_note = 'No auth required' if not _API_KEY else 'key= param required'
    return jsonify({
        'name':    'ST-CHECKER API',
        'version': '1.0',
        'auth':    auth_note,
        'endpoints': {
            'POST /api/stripe': {
                'params':   'cc, mes, ano, cvc, link, [key]',
                'response': 'plain-text',
                'example':  'APPROVED | Payment Approved | MerchantName | 1.00 USD | Card: 4111...|12|2026|123 | Time: 3.2s',
            },
            'POST /api/stripe/json': {
                'params':   'cc, mes, ano, cvc, link, [key]',
                'response': 'JSON',
            },
        },
        'uptime': int(time.time() - start_time),
    })


def _get_self_url():
    if BASE_URL:
        url = BASE_URL if BASE_URL.startswith('http') else f'http://{BASE_URL}'
        if BASE_URL_PORT not in (80, 443):
            url = f'{url}:{BASE_URL_PORT}'
        return url

    for var in ('REPLIT_DEV_DOMAIN', 'REPLIT_DOMAINS'):
        val = os.environ.get(var, '')
        if val:
            domain = val.split(',')[0].strip()
            return f'https://{domain}'

    slug  = os.environ.get('REPL_SLUG', '')
    owner = os.environ.get('REPL_OWNER', '')
    if slug and owner:
        return f'https://{slug}.{owner}.repl.co'

    return f'http://localhost:{_INTERNAL_PORT}'


def self_ping():
    url = _get_self_url()
    print(f"[KEEP-ALIVE] Ping target → {url}/ping  (port {BASE_URL_PORT})")
    while True:
        try:
            time.sleep(270)
            r = requests.get(f'{url}/ping', timeout=10, verify=False)
            print(f"[KEEP-ALIVE] Ping OK — {r.status_code}")
        except Exception as e:
            print(f"[KEEP-ALIVE] Ping failed: {e}")


def run():
    app.run(host='0.0.0.0', port=_INTERNAL_PORT, debug=False, use_reloader=False)


def keep_alive():
    server_thread = Thread(target=run, daemon=True)
    server_thread.start()

    ping_thread = Thread(target=self_ping, daemon=True)
    ping_thread.start()


if __name__ == '__main__':
    ping_thread = Thread(target=self_ping, daemon=True)
    ping_thread.start()
    run()
