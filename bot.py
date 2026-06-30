"""
deadline-bot — Rung 3c
Long-polls Telegram, extracts a structured deadline via Gemini (extractor.py),
and replies with a formatted card. No storage yet — that's Rung 4.
"""

import os
import time
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from extractor import extract, resolve_when   # 3b: text->dict; when_text->date
from db import save_deadline                   # Rung 4: save extracted deadlines



load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BASE = f"https://api.telegram.org/bot{TOKEN}"

# Anchor date resolution to the campus timezone (set BOT_TZ in .env, e.g.
# Asia/Kolkata). Defaults to UTC. message["date"] is a Unix UTC timestamp.
_TZ_NAME = os.environ.get("BOT_TZ")
BOT_TZ = ZoneInfo(_TZ_NAME) if _TZ_NAME else timezone.utc

LONG_POLL_TIMEOUT = 30                  # how long Telegram holds the connection open
READ_TIMEOUT = LONG_POLL_TIMEOUT + 5    # our read timeout MUST exceed the poll window


# ---------- Telegram I/O (unchanged from Rung 2) ----------

def get_updates(offset):
    """Long-poll for new updates. Returns the list under `result`."""
    params = {"timeout": LONG_POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset

    resp = requests.get(
        f"{BASE}/getUpdates",
        params=params,
        timeout=(10, READ_TIMEOUT),     # (connect timeout, read timeout)
    )
    resp.raise_for_status()
    return resp.json()["result"]


def send_message(chat_id, text):
    resp = requests.post(
        f"{BASE}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    resp.raise_for_status()


# ---------- Extraction glue ----------

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


# ---------- Main loop ----------

def main():
    offset = None
    backoff = 1

    print("Deadline bot running. Ctrl+C to stop.")

    while True:
        try:
            updates = get_updates(offset)

            backoff = 1

            for update in updates:
                offset = update["update_id"] + 1

                message = update.get("message")
                if not message:
                    continue

                text = message.get("text")
                if text is None:
                    continue

                chat_id = message["chat"]["id"]

                try:
                    result = extract_with_retry(text)

                    if result.get("has_deadline"):
                        # Resolve the date phrase deterministically, anchored to
                        # when Telegram says the message was sent (Unix UTC).
                        received_at = datetime.fromtimestamp(
                            message["date"], tz=BOT_TZ
                        )
                        result["deadline"] = resolve_when(
                            result.get("when_text"), received_at
                        )

                        status = save_deadline(result, text)

                        reply = format_reply(result)

                        if status == "duplicate":
                            reply += "\n\n(already saved earlier)"
                    else:
                        reply = "No deadline found in that message."

                    send_message(chat_id, reply)

                except Exception as e:
                    print(f"Extraction failed: {e}")
                    send_message(
                        chat_id,
                        "Couldn't process that one — try again."
                    )

        except KeyboardInterrupt:
            print("\nShutting down cleanly.")
            break

        except requests.exceptions.RequestException as e:
            print(f"Transient error: {e}. Retrying in {backoff}s.")

            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()