import subprocess
import sys
import os
import time
import datetime
import socket
import threading
import requests
from keep_alive import keep_alive

keep_alive()

# ── Replit pe sirf keep-alive chalao, EC2 pe actual bot ──
_IS_REPLIT = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEPLOYMENT"))
if _IS_REPLIT:
    print("[WATCHDOG] Replit environment detected — bot polling DISABLED to avoid conflict with EC2.")
    print("[WATCHDOG] Keep-alive Flask server is running. Deploy to EC2 to run the live bot.")
    while True:
        time.sleep(60)

BOT_TOKEN = os.environ.get('BOT_TOKEN', '').strip()
ADMIN_ID  = os.environ.get('ADMIN_ID', '').strip()

if not BOT_TOKEN:
    print("[WATCHDOG] ERROR: BOT_TOKEN is not set in environment. Exiting.")
    sys.exit(1)
if not ADMIN_ID:
    print("[WATCHDOG] ERROR: ADMIN_ID is not set in environment. Exiting.")
    sys.exit(1)

# ── Non-recoverable config errors → stop watchdog immediately ──
_CONFIG_ERRORS = [
    "BOT_TOKEN is not set",
    "ADMIN_ID is not set",
    "invalid literal for int",
    "SystemExit",
]

def tg_notify(text):
    if not BOT_TOKEN or not ADMIN_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"[WATCHDOG] Notify failed: {e}")

# ── Start local Stripe Hitter Node.js server (if not already running) ──
_hitter_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stripe_hitter")

def _hitter_already_up():
    try:
        s = socket.create_connection(("localhost", 3001), timeout=2)
        s.close()
        return True
    except Exception:
        return False

_hitter_proc = None
if os.path.isdir(_hitter_dir):
    if _hitter_already_up():
        print("[HITTER] Stripe hitter already running on port 3001 — skipping auto-start")
    else:
        _server_js = os.path.join(_hitter_dir, "server.js")
        if os.path.exists(_server_js):
            try:
                _hitter_proc = subprocess.Popen(
                    ["node", "server.js"],
                    cwd=_hitter_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"[HITTER] Stripe hitter started (PID {_hitter_proc.pid}) on port 3001")
                time.sleep(2)
            except Exception as _e:
                print(f"[HITTER] Failed to start stripe hitter: {_e}")
        else:
            print("[HITTER] server.js not found in stripe_hitter/ — skipping hitter start")

# ── Start Telethon Scraper (if credentials are set) ──────────────────────────
_scraper_proc = None

def _read_tg_secrets_from_file():
    """Read TG_ secrets from .tg_secrets file (fallback for env vars)."""
    _f = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tg_secrets")
    result = {}
    if os.path.exists(_f):
        try:
            with open(_f, "r", encoding="utf-8") as _fh:
                for _line in _fh:
                    _line = _line.strip()
                    if "=" in _line and not _line.startswith("#"):
                        _k, _v = _line.split("=", 1)
                        result[_k.strip()] = _v.strip()
        except Exception:
            pass
    return result

_tg_file = _read_tg_secrets_from_file()
_TG_API_ID  = os.environ.get("TG_API_ID",  _tg_file.get("TG_API_ID",  "")).strip()
_TG_API_HASH= os.environ.get("TG_API_HASH",_tg_file.get("TG_API_HASH","")).strip()
_TG_SESSION = os.environ.get("TG_SESSION", _tg_file.get("TG_SESSION", "")).strip()

if _TG_API_ID and _TG_API_HASH and _TG_SESSION:
    try:
        _scraper_proc = subprocess.Popen(
            [sys.executable, "-u", "scraper.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        print(f"[SCRAPER] Telethon scraper started (PID {_scraper_proc.pid})")

        def _log_scraper(proc):
            for line in iter(proc.stdout.readline, ''):
                print(f"[SCRAPER] {line}", end='', flush=True)
            proc.stdout.close()

        import threading as _th
        _th.Thread(target=_log_scraper, args=(_scraper_proc,), daemon=True).start()
    except Exception as _se:
        print(f"[SCRAPER] Failed to start scraper: {_se}")
else:
    print("[SCRAPER] TG_API_ID / TG_API_HASH / TG_SESSION not set — scraper disabled.")
    print("[SCRAPER] Run gen_session.py locally and add secrets to enable scraping.")

print("Starting the bot watchdog...")
restart_count        = 0
consecutive_fast     = 0
MAX_CONSECUTIVE_FAST = 5

while True:
    t_start = time.time()
    stdout_buf = ""
    stderr_buf = ""
    try:
        # Capture stdout/stderr so we can detect config errors
        proc = subprocess.Popen(
            [sys.executable, "-u", "file1.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        # Stream output to console while also capturing it
        def _stream(pipe, store):
            for line in iter(pipe.readline, ''):
                print(line, end='', flush=True)
                store.append(line)
            pipe.close()

        _out_lines, _err_lines = [], []
        _t1 = threading.Thread(target=_stream, args=(proc.stdout, _out_lines), daemon=True)
        _t2 = threading.Thread(target=_stream, args=(proc.stderr, _err_lines), daemon=True)
        _t1.start(); _t2.start()

        proc.wait()
        _t1.join(timeout=3); _t2.join(timeout=3)

        stdout_buf = "".join(_out_lines)
        stderr_buf = "".join(_err_lines)
        output_combined = stdout_buf + stderr_buf

        exit_code = proc.returncode
        runtime   = time.time() - t_start

        # ── Detect non-recoverable config errors ──
        for cfg_err in _CONFIG_ERRORS:
            if cfg_err in output_combined:
                print(f"[WATCHDOG] Config error detected: {cfg_err}")
                print(f"[WATCHDOG] Fix your environment variables then restart. Stopping watchdog.")
                tg_notify(
                    f"<b>🛑 WATCHDOG STOPPED\n\n"
                    f"Config error: <code>{cfg_err}</code>\n"
                    f"Fix env vars and restart manually.</b>"
                )
                sys.exit(1)

        if exit_code == 0:
            consecutive_fast = 0
            print("[WATCHDOG] Bot stopped cleanly (exit 0). Restarting in 5s...")
        else:
            restart_count += 1
            _now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[WATCHDOG] Bot crashed (exit {exit_code}, runtime {runtime:.1f}s). Restart #{restart_count} in 5s...")

            if stderr_buf:
                print(f"[WATCHDOG] stderr: {stderr_buf[:300]}")

            # Fast-crash detection
            if runtime < 10:
                consecutive_fast += 1
                if consecutive_fast >= MAX_CONSECUTIVE_FAST:
                    msg = (stderr_buf or stdout_buf)[:300]
                    print(f"[WATCHDOG] {consecutive_fast} consecutive fast crashes (<10s). Stopping.")
                    tg_notify(
                        f"<b>🛑 WATCHDOG STOPPED\n\n"
                        f"Bot crashed {consecutive_fast}x in a row under 10s.\n"
                        f"Fix the error and restart manually.\n\n"
                        f"<code>{msg}</code></b>"
                    )
                    sys.exit(1)
            else:
                consecutive_fast = 0

            tg_notify(
                f"<b>╔══════════════════════╗\n"
                f"║  🔴  BOT CRASHED!     ║\n"
                f"╚══════════════════════╝\n\n"
                f"⚠️ Bot exited unexpectedly!\n\n"
                f"💥 Exit Code   : <code>{exit_code}</code>\n"
                f"⏱ Runtime     : <code>{runtime:.1f}s</code>\n"
                f"🔄 Restart No  : <code>#{restart_count}</code>\n"
                f"⏰ Time        : <code>{_now}</code>\n"
                f"📋 Error       : <code>{stderr_buf[:200] if stderr_buf else 'none'}</code>\n\n"
                f"♻️ <b>Auto-restarting in 5 seconds...</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"[⌤] YADISTAN - 🍀</b>"
            )

    except Exception as e:
        restart_count += 1
        _now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[WATCHDOG] Error running bot: {e}. Restarting in 5s...")
        tg_notify(
            f"<b>╔══════════════════════╗\n"
            f"║  ⚠️  BOT ERROR!       ║\n"
            f"╚══════════════════════╝\n\n"
            f"🛑 Watchdog caught an error:\n"
            f"<code>{str(e)[:200]}</code>\n\n"
            f"🔄 Restart No : <code>#{restart_count}</code>\n"
            f"⏰ Time       : <code>{_now}</code>\n\n"
            f"♻️ <b>Auto-restarting in 5 seconds...</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"[⌤] YADISTAN - 🍀</b>"
        )

    time.sleep(5)
