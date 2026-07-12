"""
handler.py — shared bot logic for deadline-bot.

Both transports run one per-update pipeline through handle_update():
  bot.py  (local dev)   long-polls Telegram
  web.py  (production)  receives webhooks

Keeping it in one place means long-poll and webhook behave the same, so dev and
prod don't drift.
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

# Resolve dates against the local timezone (BOT_TZ in .env, e.g. Asia/Kolkata),
# defaulting to UTC. message["date"] is a Unix UTC timestamp, and it's there in
# both long-poll and webhook updates, so the message shape is the same either way.
_TZ_NAME = os.environ.get("BOT_TZ")
BOT_TZ = ZoneInfo(_TZ_NAME) if _TZ_NAME else timezone.utc

# Base URL for board links, e.g. https://<app>.onrender.com (no trailing slash).
# Board URLs are built from this instead of being hardcoded.
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")


def send_message(chat_id, text):
    resp = requests.post(
        f"{BASE}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    resp.raise_for_status()


class DailyQuotaExceeded(Exception):
    """A daily Gemini quota is used up, so this request can't recover."""


def _parse_quota_error(body):
    """Read (quota_id, retry_delay_seconds) out of a Gemini 429 body.
    Returns (None, None) if the body isn't shaped the way we expect."""
    quota_id = retry_delay = None
    try:
        for d in body["error"]["details"]:
            t = d.get("@type", "")
            if t.endswith("QuotaFailure"):
                viols = d.get("violations") or []
                if viols:
                    quota_id = viols[0].get("quotaId")
            elif t.endswith("RetryInfo"):
                rd = d.get("retryDelay")            # e.g. "22s", "1.5s"
                if isinstance(rd, str) and rd.endswith("s"):
                    retry_delay = float(rd[:-1])
    except (KeyError, TypeError, ValueError, AttributeError):
        pass                                       # not the shape we expected; fall back
    return quota_id, retry_delay


def extract_with_retry(text):
    """Call the model, retrying only on errors that can actually recover.

    A daily 429 won't clear inside this request, so raise DailyQuotaExceeded right
    away rather than sleeping for nothing. (The old loop retried these silently,
    which is why a daily-quota wall looked like the bot just being slow.) Per-minute
    and TPM 429s and 503s do get retried, waiting the server's retryDelay if it
    gives one. Logs ids, status, and durations only, never text or keys."""
    attempts = 3
    backoff = 1
    for i in range(attempts):
        try:
            return extract(text)
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code
            if code not in (429, 503):
                raise
            quota_id = retry_delay = None
            if code == 429:
                try:
                    quota_id, retry_delay = _parse_quota_error(e.response.json())
                except ValueError:
                    pass                           # body wasn't JSON; fall back
                if quota_id and "PerDay" in quota_id:
                    print(f"gemini_quota_daily status=429 quota={quota_id}", flush=True)
                    raise DailyQuotaExceeded(quota_id) from e
            if i >= attempts - 1:
                raise
            s = max(backoff, retry_delay or 0)
            print(f"gemini_retry attempt={i+1} status={code} quota={quota_id} sleep={s:.1f}s", flush=True)
            time.sleep(s)
            backoff = min(backoff * 2, 30)


def format_reply(d):
    """Format the extracted dict into a Telegram reply."""
    if not d.get("has_deadline"):
        return "Didn't spot a deadline in that one."
    lines = [d.get("title") or "Deadline"]
    if d.get("deadline"):
        lines.append(f"Due {d['deadline']}")
    if d.get("course"):
        lines.append(d["course"])
    if d.get("summary"):
        lines.append(d["summary"])
    return "\n".join(lines)


def handle_update(update):
    """Run one Telegram update through the pipeline: extract, resolve, save, reply.

    Used by both bot.py and web.py. A bad message is caught and answered with a
    fallback reply instead of crashing the process, which also stops Telegram from
    retrying the webhook forever.
    """
    update_id = update.get("update_id")
    message = update.get("message")
    if not message:
        return                      # non-message update (edited_message, etc.) -> ignore

    text = message.get("text")
    if text is None:
        return                      # non-text message (photo/sticker) -> ignore

    chat_id = message["chat"]["id"]

    # Make sure this chat has a board, and grab its link.
    token = get_or_create_board(chat_id)
    board_url = f"{PUBLIC_BASE_URL}/b/{token}"

    # /start (or /start@Bot in groups): say hi and hand over the board link.
    if text.strip().lower().startswith("/start"):
        send_message(
            chat_id,
            "Hey! Send me anything with a deadline in it and I'll save the date "
            "to your private board. Bills, notices, assignments, whatever works.\n\n"
            f"Your board: {board_url}",
        )
        return

    t_save = 0.0
    try:
        t0 = time.perf_counter()
        result = extract_with_retry(text)
        t_extract = time.perf_counter() - t0

        if result.get("has_deadline"):
            # Resolve the date against when Telegram says the message was sent
            # (Unix UTC), not against the current time.
            received_at = datetime.fromtimestamp(message["date"], tz=BOT_TZ)
            result["deadline"] = resolve_when(result.get("when_text"), received_at)

            ts = time.perf_counter()
            status = save_deadline(result, text, chat_id)
            t_save = time.perf_counter() - ts
            reply = format_reply(result)
            if status == "duplicate":
                reply += "\n\n(you already saved this one)"
        else:
            reply = "Didn't spot a deadline in that one."

        reply += f"\n\nYour board: {board_url}"
        ts = time.perf_counter()
        send_message(chat_id, reply)
        t_send = time.perf_counter() - ts

        # ids and durations only, no text, keys, or tokens.
        print(f"timing update={update_id} extract={t_extract:.2f} save={t_save:.2f} "
              f"send={t_send:.2f} total={time.perf_counter() - t0:.2f}", flush=True)

    except DailyQuotaExceeded:
        # daily wall: tell the user plainly instead of the generic error.
        send_message(chat_id, "That's my limit for today. Try again tomorrow.")
    except Exception as e:
        print(f"extract_failed update={update_id} error={type(e).__name__}", flush=True)
        send_message(chat_id, "Something went wrong there. Give it another go.")