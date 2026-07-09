"""
bot.py — LOCAL DEV transport (long-polling).

In production the bot runs as a Telegram *webhook* served by web.py. This
long-poll loop is kept only as a local dev tool: no public URL needed — just run
it and message the bot. Both transports call the SAME handle_update() in handler.py.
"""

import time
import requests

from handler import BASE, handle_update   # importing handler loads .env + config

LONG_POLL_TIMEOUT = 30                  # how long Telegram holds the connection open
READ_TIMEOUT = LONG_POLL_TIMEOUT + 5    # our read timeout MUST exceed the poll window


def get_updates(offset):
    """Long-poll for new updates. Returns the list under `result`."""
    params = {"timeout": LONG_POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(
        f"{BASE}/getUpdates",
        params=params,
        timeout=(10, READ_TIMEOUT),
    )
    resp.raise_for_status()
    return resp.json()["result"]


def main():
    offset = None
    backoff = 1
    print("Deadline bot (long-poll dev mode) running. Ctrl+C to stop.")
    while True:
        try:
            updates = get_updates(offset)
            backoff = 1
            for update in updates:
                offset = update["update_id"] + 1
                handle_update(update)
        except KeyboardInterrupt:
            print("\nShutting down cleanly.")
            break
        except requests.exceptions.RequestException as e:
            print(f"Transient error: {e}. Retrying in {backoff}s.")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()