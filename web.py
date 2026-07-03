from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from db import get_upcoming, get_undated

app = FastAPI()
templates = Jinja2Templates(directory="templates")   # look for .html here


@app.get("/", response_class=HTMLResponse)            # now returns a page, not JSON
def dashboard(request: Request, q: str | None = None):
    upcoming = get_upcoming(q)                          # timeline rows
    undated = get_undated(q)                            # review block (empty for now)
    # render index.html, handing it the data it needs
    return templates.TemplateResponse(
        request,
        "index.html",
        {"upcoming": upcoming, "undated": undated, "q": q},
    )


@app.get("/api/deadlines")                             # keep the JSON endpoint
def api_deadlines(q: str | None = None):
    return get_upcoming(q)