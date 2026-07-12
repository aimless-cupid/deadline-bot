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


class DailyQuotaExceeded(Exception):
    """A per-DAY Gemini quota is exhausted — unrecoverable within this request."""


def _parse_quota_error(body):
    """Pull (quota_id, retry_delay_seconds) from a Gemini 429 JSON body.
    Defensive: returns (None, None) if the body isn't the expected shape."""
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
        pass                                       # unexpected shape -> fall back
    return quota_id, retry_delay


def extract_with_retry(text):
    """Call the model, retrying only on RECOVERABLE quota/transient errors.

    Quota-aware: a per-DAY 429 cannot recover inside this request, so raise
    DailyQuotaExceeded immediately instead of sleeping pointlessly — that silence
    is exactly what made a hard daily-quota wall look like 'a bit slow' for days.
    Per-minute / TPM 429s and 503s are retried, honoring the server's retryDelay
    when present. Logs ids/status/durations only — never text or keys."""
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
                    pass                           # non-JSON body -> fall back
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
    update_id = update.get("update_id")
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
            "Hi! Forward or paste any message with a deadline — an announcement, "
            "a bill, a renewal notice — and I'll pull out the deadline and add it "
            "to your private board.\n\n"
            f"Your board: {board_url}",
        )
        return

    t_save = 0.0
    try:
        t0 = time.perf_counter()
        result = extract_with_retry(text)
        t_extract = time.perf_counter() - t0

        if result.get("has_deadline"):
            # Resolve the date phrase deterministically, anchored to when
            # Telegram says the message was SENT (Unix UTC) — never now().
            received_at = datetime.fromtimestamp(message["date"], tz=BOT_TZ)
            result["deadline"] = resolve_when(result.get("when_text"), received_at)

            ts = time.perf_counter()
            status = save_deadline(result, text, chat_id)
            t_save = time.perf_counter() - ts
            reply = format_reply(result)
            if status == "duplicate":
                reply += "\n\n(already saved earlier)"
        else:
            reply = "No deadline found in that message."

        reply += f"\n\nYour board: {board_url}"
        ts = time.perf_counter()
        send_message(chat_id, reply)
        t_send = time.perf_counter() - ts

        # ids + durations only — never text, keys, or board tokens.
        print(f"timing update={update_id} extract={t_extract:.2f} save={t_save:.2f} "
              f"send={t_send:.2f} total={time.perf_counter() - t0:.2f}", flush=True)

    except DailyQuotaExceeded:
        # hard per-day wall: tell the user plainly, not a generic failure.
        send_message(chat_id, "Daily processing limit reached — try again tomorrow.")
    except Exception as e:
        print(f"extract_failed update={update_id} error={type(e).__name__}", flush=True)
        send_message(chat_id, "Couldn't process that one — try again.")