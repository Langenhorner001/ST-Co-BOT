# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   UI FORMATTER — ST-Checker Bot
#   Premium, consistent, mobile-friendly responses
#   + Auto-Format Engine (AF) — v2
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import functools
from telebot import types as _tbtypes

# ── Command metadata ──────────────────────────────
_CMD_META = {
    "dr":       {"icon": "🔥", "label": "DrGaM",    "full": "DrGaM GiveWP + Stripe"},
    "st":       {"icon": "⚡", "label": "Stripe",   "full": "Stripe Charge API"},
    "chk":      {"icon": "🔍", "label": "CHK",      "full": "WCS SetupIntent"},
    "vbv":      {"icon": "🛡️", "label": "VBV",      "full": "3DS Auth Gateway"},
    "p":        {"icon": "💙", "label": "PPCharge",  "full": "PayPal Charge API"},
    "sk":       {"icon": "🔑", "label": "SK Auth",  "full": "Stripe SK Auth"},
    "chkm":     {"icon": "🔍", "label": "CHK Mass", "full": "WCS SetupIntent Mass"},
    "vbvm":     {"icon": "🛡️", "label": "VBV Mass", "full": "3DS Auth Mass"},
    "pp":       {"icon": "💰", "label": "PayPal",   "full": "PayPal GiveWP + Stripe"},
    "skm":      {"icon": "🔑", "label": "SK Mass",  "full": "Stripe SK Auth Mass"},
    "skchk":    {"icon": "🔑", "label": "SK Check", "full": "Stripe Balance API"},
    "wcs":      {"icon": "🏪", "label": "WCS",      "full": "WooCommerce Stripe"},
    "b3":       {"icon": "🌀", "label": "B3DS",     "full": "Braintree 3DS"},
    "brt":      {"icon": "🌿", "label": "BRT",      "full": "Braintree Charge"},
    "pi":       {"icon": "💳", "label": "PI",       "full": "Stripe PaymentIntent"},
    "sa":       {"icon": "🔐", "label": "SA",       "full": "Stripe Auth $0"},
    "co":       {"icon": "🛒", "label": "CO",       "full": "Stripe Checkout"},
    "xco":      {"icon": "🔗", "label": "XCO",      "full": "XCO Checkout"},
    "ppm":      {"icon": "💳", "label": "PPM",      "full": "PayPal Mass Check"},
    "sp":       {"icon": "🛍️", "label": "Shopify",  "full": "Shopify v6"},
    "ah":       {"icon": "🔥", "label": "AutoHit",  "full": "Auto Hitter Multi-Gate"},
    "br":       {"icon": "🐾", "label": "BRV",      "full": "Bravehound $1 Charge"},
    "auth":     {"icon": "🔐", "label": "Assoc",    "full": "Assoc Stripe Auth $0"},
    "h":        {"icon": "🎯", "label": "Hitter",   "full": "Stripe Hitter"},
    "oc":       {"icon": "🌐", "label": "OC",       "full": "OC Checker"},
    "gc":       {"icon": "⚡", "label": "GC",       "full": "Gen + CHK via chkr.cc"},
    "m3ds":     {"icon": "🔒", "label": "M3DS",     "full": "Mohio 3DS Bypass"},
}

_SEP  = "─────────────────────────────"
_THIN = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
_SIG  = "⌤ <a href='https://t.me/yadistan'>@yadistan</a>"

_YADISTAN_URL = "https://t.me/yadistan"


def _meta(cmd):
    return _CMD_META.get(cmd.lower(), {"icon": "⚡", "label": cmd.upper(), "full": cmd.upper()})


def _progress_bar(checked, total, width=10):
    if total == 0:
        return "░" * width
    filled = int((checked / total) * width)
    return "█" * filled + "░" * (width - filled)


def _mask_card(card):
    parts = card.split("|")
    if not parts:
        return card
    num = parts[0]
    if len(num) >= 10:
        masked = num[:6] + "••••••" + num[-4:]
    else:
        masked = num
    if len(parts) > 1:
        return masked + "|" + "|".join(parts[1:])
    return masked


def _yadistan_kb(extra_buttons=None):
    """Return InlineKeyboardMarkup with @yadistan button (+ optional extras)."""
    kb = _tbtypes.InlineKeyboardMarkup()
    if extra_buttons:
        for btn in extra_buttons:
            kb.add(btn)
    kb.add(_tbtypes.InlineKeyboardButton(text="@yadistan", url=_YADISTAN_URL))
    return kb


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   SINGLE CARD RESULT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fmt_processing(cmd, card=None, note=None):
    """'Processing...' placeholder message."""
    m = _meta(cmd)
    lines = [f"<b>{m['icon']} {m['label']} — Processing...</b>"]
    if card:
        lines.append(f"<code>{card}</code>")
    if note:
        lines.append(f"<i>{note}</i>")
    lines.append("⏳ Please wait...")
    return "\n".join(lines)


def fmt_single(
    cmd, card, status_emoji, result_text,
    gate_name=None, bin_info=None, bank=None,
    country=None, country_code=None,
    elapsed=None, amount=None, extra_fields=None
):
    """
    Premium single-card result block.
    extra_fields: list of (label, value) tuples for custom rows
    """
    m = _meta(cmd)
    gate = gate_name or m["full"]

    if status_emoji == "✅":
        banner = f"✅ <b>APPROVED</b>"
    elif status_emoji == "💰":
        banner = f"💰 <b>INSUFFICIENT / 3DS</b>"
    elif status_emoji == "⚠️":
        banner = f"⚠️ <b>CHALLENGE / OTP</b>"
    else:
        banner = f"❌ <b>DECLINED</b>"

    card_disp = f"<code>{card}</code>"

    amt_line = ""
    if amount:
        amt_line = f"\n│  💵  Amount    ›  <b>${amount}</b>"

    bin_block = ""
    if bin_info or bank or country:
        _b   = bin_info if bin_info and bin_info not in ("Unknown", "Unknown - ", "") else "—"
        _bk  = bank    if bank    and bank    not in ("Unknown", "")                  else "—"
        _cc  = country_code if country_code and country_code not in ("N/A","??","","Unknown") else ""
        _ct  = f"{country} ({_cc})" if _cc else (country or "Unknown")
        bin_block = (
            f"\n│\n│  🏦  BIN       ›  {_b}"
            f"\n│  🏛️  Bank      ›  {_bk}"
            f"\n│  🌍  Country   ›  {_ct}"
        )

    extra_block = ""
    if extra_fields:
        extra_block = "\n│"
        for lbl, val in extra_fields:
            extra_block += f"\n│  📌  {lbl:<9} ›  {val}"

    time_line = f"\n│  ⏱️  Time      ›  {elapsed:.2f}s" if elapsed is not None else ""

    text = (
        f"<b>{m['icon']} {m['label']} — {banner}\n"
        f"{_SEP}\n"
        f"│  💳  Card      ›  {card_disp}\n"
        f"│  📋  Status    ›  {result_text}\n"
        f"│  ⚡  Gate      ›  {gate}"
        f"{amt_line}"
        f"{bin_block}"
        f"{extra_block}"
        f"{time_line}\n"
        f"{_SEP}\n"
        f"       {_SIG}</b>"
    )
    return text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   MASS MODE — HEADER / PROGRESS / COMPLETE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fmt_mass_header(cmd, total, gate_name=None, amount=None):
    m = _meta(cmd)
    gate = gate_name or m["full"]
    amt_note = f" · ${amount}" if amount else ""
    return (
        f"<b>{m['icon']} {m['label']} — Mass Checker\n"
        f"{_SEP}\n"
        f"│  ⚡  Gate      ›  {gate}{amt_note}\n"
        f"│  📦  Cards     ›  {total} queued\n"
        f"{_SEP}\n"
        f"│  ⏳  Starting up...</b>"
    )


def fmt_mass_progress(
    cmd, checked, total, live, dead, gate_name=None,
    secondary=0, secondary_emoji="💰", secondary_label="Insuf",
    results_lines=None, status="⏳", amount=None
):
    m = _meta(cmd)
    gate = gate_name or m["full"]
    amt_note = f" · ${amount}" if amount else ""

    bar = _progress_bar(checked, total)
    pct = int(checked / total * 100) if total else 0

    if status == "✅":
        status_line = "✅ <b>Completed!</b>"
    elif status == "🛑":
        status_line = "🛑 <b>Stopped by User</b>"
    else:
        status_line = f"⏳ <b>Checking...</b>"

    result_block = ""
    if results_lines:
        recent = results_lines[-12:]
        result_block = "\n│\n" + "\n".join(f"│  {r}" for r in recent)

    return (
        f"<b>{m['icon']} {m['label']} — Mass Checker\n"
        f"{_SEP}\n"
        f"│  ⚡  Gate      ›  {gate}{amt_note}\n"
        f"│  🔰  Status    ›  {status_line}\n"
        f"│  📊  Progress  ›  [{bar}] {pct}%\n"
        f"{_THIN}\n"
        f"│  🏁  Checked   ›  {checked} / {total}\n"
        f"│  ✅  Live      ›  {live}\n"
        f"│  {secondary_emoji}  {secondary_label:<9} ›  {secondary}\n"
        f"│  ❌  Dead      ›  {dead}"
        f"{result_block}\n"
        f"{_SEP}\n"
        f"       {_SIG}</b>"
    )


def fmt_mass_hits(cmd, hits, total, amount=None):
    m = _meta(cmd)
    amt_note = f" · ${amount}" if amount else ""
    cards_block = "\n".join(hits)
    return (
        f"<b>{m['icon']} {m['label']} — Live Hits{amt_note}\n"
        f"{_SEP}\n"
        f"│  ✅  Hits      ›  {len(hits)} / {total}\n"
        f"{_SEP}</b>\n"
        f"<code>{cards_block}</code>"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   ERROR / USAGE HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fmt_error(message_text, example=None):
    lines = [f"<b>❌ Error\n{_SEP}\n{message_text}"]
    if example:
        lines.append(f"\n📌 Example:\n<code>{example}</code>")
    lines.append(f"{_SEP}\n       {_SIG}</b>")
    return "\n".join(lines)


def fmt_vip_only():
    return "<b>❌ This command is for VIP users only.\n\nContact @yadistan to upgrade.</b>"


def fmt_rate_limit(wait):
    return f"<b>⏱️ Rate limited — wait <code>{wait}s</code> before next check.</b>"


def fmt_expired():
    return "<b>⛔ Your subscription has expired.\n\nContact @yadistan to renew.</b>"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   AUTO-FORMAT ENGINE  (AF)  — v2
#   ─────────────────────────────────────────────
#   New commands: return UI.R.single(...) or any
#   UI.R.* dict — UI.send() / @UI.auto_result
#   handle formatting + sending automatically.
#
#   Legacy commands (manual UI.fmt_* calls) keep
#   working unchanged — 100% backward compatible.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class R:
    """
    Result dict builders.  Every method returns a plain dict with
    a "_type" key that auto_format() uses to pick the right template.

    Usage:
        return UI.R.single("dr", card, "✅", "Approved", gate="CrisisAid", elapsed=1.2)
        return UI.R.error("Invalid card", example="/dr 4111|12|25|123")
        return UI.R.vip_only()
        return UI.R.rate_limit(5)
    """

    @staticmethod
    def single(
        cmd, card, emoji, result_text, *,
        gate=None, bin_info=None, bank=None,
        country=None, country_code=None,
        elapsed=None, amount=None, extra_fields=None
    ):
        """Single card result dict."""
        return {
            "_type":        "single",
            "cmd":          cmd,
            "card":         card,
            "emoji":        emoji,
            "result":       result_text,
            "gate":         gate,
            "bin_info":     bin_info,
            "bank":         bank,
            "country":      country,
            "country_code": country_code,
            "elapsed":      elapsed,
            "amount":       amount,
            "extra_fields": extra_fields,
        }

    @staticmethod
    def error(message_text, example=None):
        """Error / invalid usage dict."""
        return {"_type": "error", "msg": message_text, "example": example}

    @staticmethod
    def vip_only():
        """VIP gate dict."""
        return {"_type": "vip_only"}

    @staticmethod
    def rate_limit(wait):
        """Rate-limit dict."""
        return {"_type": "rate_limit", "wait": wait}

    @staticmethod
    def expired():
        """Subscription expired dict."""
        return {"_type": "expired"}

    @staticmethod
    def text(content):
        """Raw pre-formatted text (no auto-template)."""
        return {"_type": "raw", "text": content}

    @staticmethod
    def mass_header(cmd, total, gate_name=None, amount=None):
        """Mass check header dict."""
        return {
            "_type": "mass_header",
            "cmd": cmd, "total": total,
            "gate_name": gate_name, "amount": amount,
        }

    @staticmethod
    def mass_progress(
        cmd, checked, total, live, dead, gate_name=None,
        secondary=0, secondary_emoji="💰", secondary_label="Insuf",
        results_lines=None, status="⏳", amount=None
    ):
        """Mass check progress dict."""
        return {
            "_type": "mass_progress",
            "cmd": cmd, "checked": checked, "total": total,
            "live": live, "dead": dead,
            "gate_name": gate_name, "amount": amount,
            "secondary": secondary, "secondary_emoji": secondary_emoji,
            "secondary_label": secondary_label,
            "results_lines": results_lines, "status": status,
        }

    @staticmethod
    def mass_hits(cmd, hits, total, amount=None):
        """Mass hits summary dict."""
        return {
            "_type": "mass_hits",
            "cmd": cmd, "hits": hits, "total": total, "amount": amount,
        }


def auto_format(result):
    """
    Convert any R.* result dict → formatted HTML string.
    Falls back to str(result) if type unknown.
    """
    if not isinstance(result, dict):
        return str(result)

    t = result.get("_type", "single")

    if t == "single":
        return fmt_single(
            result["cmd"], result["card"],
            result["emoji"], result["result"],
            gate_name=result.get("gate"),
            bin_info=result.get("bin_info"),
            bank=result.get("bank"),
            country=result.get("country"),
            country_code=result.get("country_code"),
            elapsed=result.get("elapsed"),
            amount=result.get("amount"),
            extra_fields=result.get("extra_fields"),
        )
    elif t == "error":
        return fmt_error(result["msg"], result.get("example"))
    elif t == "vip_only":
        return fmt_vip_only()
    elif t == "rate_limit":
        return fmt_rate_limit(result["wait"])
    elif t == "expired":
        return fmt_expired()
    elif t == "raw":
        return result["text"]
    elif t == "mass_header":
        return fmt_mass_header(
            result["cmd"], result["total"],
            result.get("gate_name"), result.get("amount")
        )
    elif t == "mass_progress":
        return fmt_mass_progress(
            result["cmd"], result["checked"], result["total"],
            result["live"], result["dead"],
            gate_name=result.get("gate_name"),
            secondary=result.get("secondary", 0),
            secondary_emoji=result.get("secondary_emoji", "💰"),
            secondary_label=result.get("secondary_label", "Insuf"),
            results_lines=result.get("results_lines"),
            status=result.get("status", "⏳"),
            amount=result.get("amount"),
        )
    elif t == "mass_hits":
        return fmt_mass_hits(
            result["cmd"], result["hits"],
            result["total"], result.get("amount")
        )
    else:
        return str(result)


def send(bot, message, result, edit_msg=None, extra_buttons=None, stop_button=False):
    """
    Auto-format a result dict and send (or edit) it to the user.

    Args:
        bot          — telebot.TeleBot instance
        message      — original Message object
        result       — R.* dict  OR  plain HTML string
        edit_msg     — if provided, edit this message instead of sending new
        extra_buttons— list of InlineKeyboardButton to add above @yadistan
        stop_button  — if True, add 🛑 Stop button (for mass checks)

    Returns the sent/edited Message object.
    """
    text = auto_format(result) if isinstance(result, dict) else result

    btns = list(extra_buttons or [])
    if stop_button:
        btns.insert(0, _tbtypes.InlineKeyboardButton("🛑 𝗦𝘁𝗼𝗽", callback_data="stop"))

    kb = _yadistan_kb(btns) if btns else _yadistan_kb()

    if edit_msg is not None:
        return bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=edit_msg.message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    else:
        return bot.reply_to(
            message, text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )


def auto_result(bot_instance):
    """
    Decorator factory.  Wrap a command handler so that if it returns
    an R.* dict, UI.send() is called automatically.

    If the handler returns None (already sent manually), nothing extra happens.

    Usage:
        @bot.message_handler(commands=["mygate"])
        @UI.auto_result(bot)
        def mygate_command(message):
            # ... work ...
            return UI.R.single("dr", card, "✅", "Approved", gate="CrisisAid", elapsed=1.2)
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(message):
            result = fn(message)
            if isinstance(result, dict):
                send(bot_instance, message, result)
        return wrapper
    return decorator
