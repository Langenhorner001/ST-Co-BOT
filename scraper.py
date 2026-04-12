# -*- coding: utf-8 -*-
"""
ST-Checker Bot — Telethon Card Scraper Module
Command: /scr <BIN> <limit>
Only ADMIN_ID can use this command.
Requires: TG_API_ID, TG_API_HASH, TG_SESSION (StringSession)
"""

import re
import os
import asyncio
import logging
from datetime import datetime
from collections import defaultdict

from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format='[SCRAPER] %(asctime)s — %(levelname)s — %(message)s'
)
logger = logging.getLogger(__name__)

# ── Environment Variables ─────────────────────────────────────────────────────
def _load_tg_secrets():
    """Load TG_API_ID, TG_API_HASH, TG_SESSION from env or .tg_secrets file."""
    secrets = {
        "TG_API_ID":   os.environ.get("TG_API_ID", "").strip(),
        "TG_API_HASH": os.environ.get("TG_API_HASH", "").strip(),
        "TG_SESSION":  os.environ.get("TG_SESSION", "").strip(),
    }
    # Fallback: read from .tg_secrets file if env vars missing
    _secrets_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tg_secrets")
    if os.path.exists(_secrets_file):
        try:
            with open(_secrets_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k in secrets and not secrets[k]:
                            secrets[k] = v
        except Exception as _e:
            logging.warning(f"Could not read .tg_secrets: {_e}")
    return secrets

_tg = _load_tg_secrets()
_API_ID   = _tg["TG_API_ID"]
_API_HASH = _tg["TG_API_HASH"]
_SESSION  = _tg["TG_SESSION"]
_ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")

# Card pattern: NUMBER|MM|YY|CVV
CARD_RE = re.compile(r'\b(\d{15,16})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})\b')

# ── Helpers ───────────────────────────────────────────────────────────────────
def _chat_name(entity):
    try:
        if hasattr(entity, 'title') and entity.title:
            return entity.title
        if hasattr(entity, 'first_name') and entity.first_name:
            return entity.first_name
        if hasattr(entity, 'username') and entity.username:
            return f"@{entity.username}"
        return f"ID:{entity.id}"
    except Exception:
        return "Unknown"


async def _send_in_chunks(client, chat_id, text, chunk=4000):
    """Send long text split into Telegram-safe chunks."""
    for i in range(0, len(text), chunk):
        await client.send_message(chat_id, text[i:i+chunk])
        await asyncio.sleep(0.3)


# ── /scr Command Handler ──────────────────────────────────────────────────────
def register_handlers(client: TelegramClient):

    @client.on(events.NewMessage(pattern=r'^/scr'))
    async def scr_handler(event):
        sender = await event.get_sender()
        if sender.id != _ADMIN_ID:
            await event.reply("🚫 <b>Access Denied.</b> Admin only command.", parse_mode='html')
            return

        args = event.message.message.strip().split()
        if len(args) < 3:
            await event.reply(
                "⚠️ <b>Usage:</b> <code>/scr &lt;BIN&gt; &lt;messages_per_source&gt;</code>\n\n"
                "<b>Example:</b> <code>/scr 411110 500</code>\n\n"
                "📌 <b>BIN</b> = 6 digit bank prefix\n"
                "📌 <b>messages_per_source</b> = how many recent messages to scan per channel/group",
                parse_mode='html'
            )
            return

        bin_number      = args[1].strip()
        try:
            limit_per = int(args[2])
        except ValueError:
            await event.reply("❌ <b>Error:</b> <code>messages_per_source</code> must be a number.", parse_mode='html')
            return

        if not bin_number.isdigit() or len(bin_number) != 6:
            await event.reply("❌ <b>BIN must be exactly 6 digits.</b>", parse_mode='html')
            return

        if limit_per < 10 or limit_per > 10000:
            await event.reply("❌ <b>Limit must be between 10 and 10,000.</b>", parse_mode='html')
            return

        # ── Start scraping ────────────────────────────────────────────────────
        status = await event.reply(
            f"🔍 <b>Starting BIN Scraper</b>\n\n"
            f"💳 <b>BIN:</b> <code>{bin_number}</code>\n"
            f"📨 <b>Messages per source:</b> <code>{limit_per:,}</code>\n\n"
            f"⏳ Fetching all your channels & groups...",
            parse_mode='html'
        )

        try:
            dialogs = await client.get_dialogs()
        except Exception as e:
            await status.edit(f"❌ <b>Failed to fetch dialogs:</b> <code>{e}</code>", parse_mode='html')
            return

        sources = []
        for dlg in dialogs:
            try:
                if dlg.is_channel or dlg.is_group:
                    sources.append({'name': _chat_name(dlg.entity), 'entity': dlg.entity})
            except Exception:
                continue

        if not sources:
            await status.edit("❌ <b>No channels or groups found in your account.</b>", parse_mode='html')
            return

        await status.edit(
            f"✅ <b>Found {len(sources)} sources</b>\n\n"
            f"🔍 Scanning for cards with BIN <code>{bin_number}</code>...\n"
            f"⏳ This may take a while...",
            parse_mode='html'
        )

        found_cards    = []
        cards_by_src   = defaultdict(lambda: {'count': 0, 'samples': []})
        total_scanned  = 0
        active_sources = 0

        for idx, src in enumerate(sources):
            try:
                src_cards = []
                msg_count = 0

                async for msg in client.iter_messages(src['entity'], limit=limit_per):
                    msg_count += 1
                    if msg.text:
                        for m in CARD_RE.findall(msg.text):
                            card = f"{m[0]}|{m[1]}|{m[2]}|{m[3]}"
                            if card.startswith(bin_number):
                                if card not in found_cards:
                                    found_cards.append(card)
                                src_cards.append(card)

                total_scanned += msg_count

                if src_cards:
                    name = src['name']
                    cards_by_src[name]['count'] += len(src_cards)
                    if len(cards_by_src[name]['samples']) < 5:
                        cards_by_src[name]['samples'].extend(
                            c for c in src_cards
                            if c not in cards_by_src[name]['samples']
                        )
                    active_sources += 1

                # Progress update every 10 sources
                if (idx + 1) % 10 == 0:
                    await status.edit(
                        f"📊 <b>Progress:</b> {idx+1}/{len(sources)} sources scanned\n"
                        f"💳 <b>Unique cards found:</b> <code>{len(found_cards)}</code>\n"
                        f"📨 <b>Messages scanned:</b> <code>{total_scanned:,}</code>",
                        parse_mode='html'
                    )

            except Exception:
                continue

        # ── Build Report ──────────────────────────────────────────────────────
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"📊  BIN SCRAPER REPORT",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"💳 BIN          : {bin_number}",
            f"⏰ Time         : {now_str}",
            f"📁 Sources      : {len(sources)} total / {active_sources} with cards",
            f"📨 Msgs scanned : {total_scanned:,}",
            f"✅ Unique cards : {len(found_cards)}",
            "",
        ]

        if cards_by_src:
            lines.append("📌 Top Sources:")
            lines.append("─" * 30)
            sorted_src = sorted(cards_by_src.items(), key=lambda x: x[1]['count'], reverse=True)
            for sname, info in sorted_src[:20]:
                lines.append(f"\n🔹 {sname[:45]}")
                lines.append(f"   Cards: {info['count']}")
                if info['samples']:
                    preview = info['samples'][0]
                    # Mask middle digits
                    parts = preview.split('|')
                    masked = parts[0][:6] + 'xxxxxx' + parts[0][-4:] + '|' + '|'.join(parts[1:])
                    lines.append(f"   Sample: {masked}")
        else:
            lines.append(f"⚠️ No cards found with BIN {bin_number}")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("[⌤] ST-CHECKER-BOT · YADISTAN 🍀")

        report_text = "\n".join(lines)

        await status.delete()

        if found_cards:
            # Send report
            await _send_in_chunks(client, event.chat_id, report_text)

            # Save and send cards file
            filename = f"cards_BIN_{bin_number}_{datetime.now().strftime('%H%M%S')}.txt"
            with open(filename, 'w', encoding='utf-8') as f:
                f.write('\n'.join(found_cards))

            await client.send_file(
                event.chat_id,
                filename,
                caption=(
                    f"✅ <b>{len(found_cards)} unique cards</b> found\n"
                    f"💳 BIN: <code>{bin_number}</code>\n"
                    f"📨 {total_scanned:,} messages scanned\n\n"
                    f"[⌤] ST-CHECKER-BOT · YADISTAN 🍀"
                ),
                parse_mode='html'
            )

            # Clean up file
            try:
                os.remove(filename)
            except Exception:
                pass
        else:
            await client.send_message(
                event.chat_id,
                f"⚠️ <b>No cards found</b> with BIN <code>{bin_number}</code>\n\n"
                f"📨 Scanned <code>{total_scanned:,}</code> messages across <code>{len(sources)}</code> sources.\n\n"
                f"[⌤] ST-CHECKER-BOT · YADISTAN 🍀",
                parse_mode='html'
            )


# ── Entry Point ───────────────────────────────────────────────────────────────
async def run():
    if not _API_ID or not _API_HASH:
        logger.error("TG_API_ID or TG_API_HASH not set. Scraper will not start.")
        return

    if not _API_ID.isdigit():
        logger.error("TG_API_ID must be a number.")
        return

    if not _SESSION:
        logger.error(
            "TG_SESSION not set. Run gen_session.py once to generate your session string, "
            "then add it as the TG_SESSION secret."
        )
        return

    if not _ADMIN_ID:
        logger.error("ADMIN_ID not set. Scraper will not start.")
        return

    logger.info(f"Starting Telethon scraper (Admin: {_ADMIN_ID})")

    client = TelegramClient(
        StringSession(_SESSION),
        int(_API_ID),
        _API_HASH,
        system_version="4.16.30-vxCUSTOM"
    )

    register_handlers(client)

    await client.start()
    logger.info("✅ Telethon scraper is running. Listening for /scr commands...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(run())
