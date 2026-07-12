import os
import re
import json
import requests
import dateparser
from datetime import date
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["GEMINI_API_KEY"]
# quotas are PER-MODEL: on this project's free tier 2.5-flash is capped at 20 RPD
# (unusable), while 3.1-flash-lite gives 500 RPD / 15 RPM. it's also the RIGHT
# model for this task — schema-constrained classification (has_deadline) + verbatim
# span extraction (when_text) at temp 0, no reasoning. id confirmed via ListModels.
MODEL = "gemini-3.1-flash-lite"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
HEADERS = {"x-goog-api-key": KEY, "Content-Type": "application/json"}

# The schema GUARANTEES the response shape. The prompt guarantees the SEMANTICS
# (what counts as a deadline, what to copy into when_text, the has_deadline gate).
SCHEMA = {
    "type": "object",
    "properties": {
        "has_deadline": {"type": "boolean"},
        "type":     {"type": "string", "nullable": True},   # assignment/exam/fee/...
        "title":    {"type": "string", "nullable": True},
        "when_text": {"type": "string", "nullable": True},   # literal phrase; Python resolves it
        "course":   {"type": "string", "nullable": True},
        "summary":  {"type": "string", "nullable": True},
        "tags":     {"type": "array", "items": {"type": "string"}},
    },
    "required": ["has_deadline"],
    "propertyOrdering": ["has_deadline", "type", "title", "when_text",
                         "course", "summary", "tags"],
}

PROMPT = """You extract deadline info from a student/college announcement.

Rules:
- If the message states an actionable deadline (assignment, exam, fee, registration,
  submission, application), set has_deadline=true and fill the fields.
- If it is a social event or info that merely mentions a date but has NO actionable
  deadline (e.g. "biryani night this Friday"), set has_deadline=false and leave the
  other fields null/empty.
- "when_text": copy the date phrase EXACTLY as written in the announcement
  ("tomorrow", "by the 25th", "next Friday", "this Sunday"). Do NOT compute or
  convert it into a calendar date — extract the words verbatim. Python resolves it.
- If a deadline clearly exists but the message names no date phrase, keep
  has_deadline=true and set when_text=null.
- tags: 2-4 short lowercase keywords.

ANNOUNCEMENT:
{announcement}"""


def extract(text):
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(announcement=text)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0,                     # determinism: same input → same output
        },
    }
    resp = requests.post(URL, headers=HEADERS, json=body, timeout=30)
    resp.raise_for_status()
    # Even in JSON mode, the model's answer is a JSON *string* inside the response.
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw)


# ---------- Date resolution (Python, not the LLM) ----------
# The model returns when_text as a verbatim phrase; we turn it into a real date
# deterministically. dateparser does the parsing, but v1.4.1 is wrong in two
# spots we patch here: it can't parse "next/this <weekday>" (returns None), and
# it misreads lone ordinals ("1st" -> a month, "31st" -> a year). Everything
# else ("tomorrow", "in 3 days", "5 July", ISO dates) is dateparser's job.

_LEADING = re.compile(
    r"^(?:by|on|due|before|the|this|next|coming|upcoming)\b\s*", re.IGNORECASE)
_BARE_DAY = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$", re.IGNORECASE)


def _strip_leading(phrase):
    """Drop leading determiners/prepositions dateparser stumbles on."""
    prev, s = None, phrase.strip()
    while s != prev:
        prev = s
        s = _LEADING.sub("", s).strip()
    return s


def _next_day_of_month(day, anchor):
    """First date whose day-of-month is `day`, on or after `anchor`."""
    year, month = anchor.year, anchor.month
    for _ in range(48):                       # cap the search; never loop forever
        try:
            candidate = date(year, month, day)
        except ValueError:                    # e.g. 31st in a 30-day month -> skip
            candidate = None
        if candidate and candidate >= anchor:
            return candidate
        month, year = (1, year + 1) if month == 12 else (month + 1, year)
    return None


def resolve_when(when_text, received_at):
    """Resolve a verbatim date phrase into a real date, deterministically.

    Anchored to `received_at` (the message's send time) so the result never
    depends on when we happen to process the message.

    dateparser settings:
      RELATIVE_BASE=received_at  -> "now" for relative phrases ("tomorrow",
                                    "next week") is the send time. Reproducible.
      PREFER_DATES_FROM="future" -> ambiguous dates ("Friday", "5 July") resolve
                                    forward — the sense a deadline carries.

    Returns a datetime.date, or None if the phrase is empty/unparseable.
    """
    if not when_text:
        return None
    anchor = received_at.date()
    bare = _strip_leading(when_text)

    # Bare day-of-month ("the 25th"): place it ourselves and roll to the next
    # future month — dateparser is unreliable on lone ordinals.
    match = _BARE_DAY.match(bare)
    if match:
        day = int(match.group(1))
        return _next_day_of_month(day, anchor) if 1 <= day <= 31 else None

    settings = {"RELATIVE_BASE": received_at, "PREFER_DATES_FROM": "future"}
    dt = dateparser.parse(when_text, settings=settings)
    if dt is None and bare and bare != when_text:   # "next Friday" -> "Friday"
        dt = dateparser.parse(bare, settings=settings)
    return dt.date() if dt else None


if __name__ == "__main__":
    samples = [
        "Reminder: DBMS assignment 3 is due this Sunday on the portal.",
        "Last date to pay the semester exam fee is the 25th. No extensions.",
        "Come for biryani night this Friday at the hostel mess!",
        "Project submissions open soon, dates to be announced.",
    ]
    for s in samples:
        print("IN :", s)
        print("OUT:", extract(s), "\n")