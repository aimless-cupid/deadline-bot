"""
handler.py — shared bot logic for deadline-bot.

ONE code path, TWO transports:
  - bot.py  (local dev)   long-polls Telegram, calls handle_update() per update
  - web.py  (production)  receives Telegram webhooks,  calls handle_update() per update

Keeping the per-update pipeline here means long-poll and webhook have the SAME
behavior and SAME error handling — dev and prod can't drift apart.
"""

import os
import time
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from extractor import extract, resolve_when   # text -> dict ; when_text -> date
from db import save_deadline, get_or_create_board   # persist + per-chat board

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BASE = f"https://api.telegram.org/bot{TOKEN}"

# Anchor date resolution to the campus timezone (BOT_TZ in .env, e.g.
# Asia/Kolkata). Defaults to UTC. message["date"] is a Unix UTC timestamp and
# is present in BOTH long-poll AND webhook updates — identical message shape.
_TZ_NAME = os.environ.get("BOT_TZ")
BOT_TZ = ZoneInfo(_TZ_NAME) if _TZ_NAME else timezone.utc

# Public base for board links, e.g. https://<app>.onrender.com (no trailing
# slash). Required — board URLs are built from this, never hardcoded.
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")


def send_message(chat_id, text):
    resp = requests.post(
        f"{BASE}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    resp.raise_for_status()


def extract_with_retry(text):
    """Retry the model call on transient 429/503 only; let real errors surface."""
    attempts = 3
    backoff = 1
    for i in range(attempts):
        try:
            return extract(text)
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code
            if code in (429, 503) and i < attempts - 1:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            raise


def format_reply(d):
    """Turn the structured dict into a human-readable Telegram message."""
    if not d.get("has_deadline"):
        return "No deadline found in that message."
    lines = [f"📌 {d.get('title') or 'Deadline'}"]
    if d.get("deadline"):
        lines.append(f"🗓 {d['deadline']}")
    if d.get("course"):
        lines.append(f"📚 {d['course']}")
    if d.get("summary"):
        lines.append(d["summary"])
    return "\n".join(lines)


def handle_update(update):
    """Process ONE Telegram update end-to-end: extract -> resolve -> save -> reply.

    Shared by long-poll (bot.py) and webhook (web.py). Swallows per-message
    errors and sends a fallback reply — exactly as the long-poll loop did — so a
    single bad message never crashes the process and (under webhooks) never
    triggers an endless Telegram retry.
    """
    message = update.get("message")
    if not message:
        return                      # non-message update (edited_message, etc.) -> ignore

    text = message.get("text")
    if text is None:
        return                      # non-text message (photo/sticker) -> ignore

    chat_id = message["chat"]["id"]

    # Every chat owns a private board; ensure it exists and get its link.
    token = get_or_create_board(chat_id)
    board_url = f"{PUBLIC_BASE_URL}/b/{token}"

    # /start (incl. "/start@Bot" in groups): greet + hand over the board link.
    if text.strip().lower().startswith("/start"):
        send_message(
            chat_id,
            "Hi! Forward or paste a college announcement and I'll pull out the "
            "deadline and add it to your private board.\n\n"
            f"Your board: {board_url}",
        )
        return

    try:
        result = extract_with_retry(text)

        if result.get("has_deadline"):
            # Resolve the date phrase deterministically, anchored to when
            # Telegram says the message was SENT (Unix UTC) — never now().
            received_at = datetime.fromtimestamp(message["date"], tz=BOT_TZ)
            result["deadline"] = resolve_when(result.get("when_text"), received_at)

            status = save_deadline(result, text, chat_id)
            reply = format_reply(result)
            if status == "duplicate":
                reply += "\n\n(already saved earlier)"
        else:
            reply = "No deadline found in that message."

        reply += f"\n\nYour board: {board_url}"
        send_message(chat_id, reply)

    except Exception as e:
        print(f"Extraction failed: {e}")
        send_message(chat_id, "Couldn't process that one — try again.")