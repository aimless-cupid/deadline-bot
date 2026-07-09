import os
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from db import get_upcoming, get_undated
from handler import handle_update

load_dotenv()
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

app = FastAPI()
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, q: str | None = None):
    upcoming = get_upcoming(q)
    undated = get_undated(q)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"upcoming": upcoming, "undated": undated, "q": q},
    )


@app.get("/api/deadlines")
def api_deadlines(q: str | None = None):
    return get_upcoming(q)


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