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
    """A board is addressed only by its unguessable token. Unknown token -> 404;
    we never reveal whether a token 'could' exist. Tokens are never logged."""
    chat_id = get_chat_id_for_token(token)
    if chat_id is None:
        raise HTTPException(status_code=404, detail="board not found")
    return chat_id


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    # There is no global board anymore — each chat has its own private one, so
    # the root exposes nobody's deadlines; it just points at the bot.
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
    """Telegram POSTs one Update here. `def` (not async) so FastAPI runs the
    blocking pipeline (Gemini/DB/requests) in a threadpool — no event-loop stall."""
    # The URL is guessable; only Telegram knows the secret we set via setWebhook.
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad secret token")

    handle_update(update)

    # 200 = "delivered", so Telegram won't retry. handle_update already swallows
    # per-message errors, so we always ack — no retry storms.
    return {"ok": True}
