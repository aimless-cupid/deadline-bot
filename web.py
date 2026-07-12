import os
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from db import get_upcoming, get_undated, get_chat_id_for_token
from handler import handle_update

load_dotenv()
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

app = FastAPI()
templates = Jinja2Templates(directory="templates")


def _chat_id_or_404(token):
    """Look up the chat behind a board token, or 404 if there isn't one.
    An unknown token just 404s, and tokens are never logged."""
    chat_id = get_chat_id_for_token(token)
    if chat_id is None:
        raise HTTPException(status_code=404, detail="board not found")
    return chat_id


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    # There's no shared board; each chat has its own. The root shows nobody's
    # deadlines, it just points people at the bot.
    return templates.TemplateResponse(request, "landing.html", {})


@app.get("/b/{token}", response_class=HTMLResponse)
def board(request: Request, token: str, q: str | None = None):
    chat_id = _chat_id_or_404(token)
    upcoming = get_upcoming(chat_id, q)
    undated = get_undated(chat_id, q)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"upcoming": upcoming, "undated": undated, "q": q, "token": token},
    )


@app.get("/api/b/{token}")
def api_board(token: str, q: str | None = None):
    chat_id = _chat_id_or_404(token)
    return get_upcoming(chat_id, q)


@app.post("/telegram/webhook")
def telegram_webhook(
    update: dict,                                   # FastAPI parses the JSON body
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Telegram POSTs one update here. It's a sync def, so FastAPI runs the
    blocking work (Gemini, DB, requests) in a threadpool and the event loop
    keeps moving."""
    # The URL is guessable; only Telegram knows the secret we set via setWebhook.
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad secret token")

    handle_update(update)

    # A 200 tells Telegram we've got it, so it won't retry. handle_update already
    # handles per-message errors, so we always ack and avoid retry storms.
    return {"ok": True}
