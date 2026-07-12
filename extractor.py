import os
import re
import json
import requests
import dateparser
from datetime import date
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["GEMINI_API_KEY"]
# Quotas are per-model. On this project's free tier 2.5-flash caps out at 20
# requests a day, while 3.1-flash-lite gives 500 a day / 15 a minute. It also
# fits the task better: classify has_deadline and copy when_text at temp 0, with
# no reasoning involved. The id came from ListModels.
MODEL = "gemini-3.1-flash-lite"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
HEADERS = {"x-goog-api-key": KEY, "Content-Type": "application/json"}

# The schema pins down the response shape; the prompt handles the meaning (what
# counts as a deadline, what goes in when_text, the has_deadline gate).
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

PROMPT = """Pull the deadline out of a message or announcement.

Rules:
- If the reader has to do something by a date (an assignment, exam, fee, bill,
  renewal, registration, application, submission, booking, or appointment), set
  has_deadline to true and fill in the fields.
- If the message only mentions a date but doesn't ask the reader to do anything by
  then, like a party or social event ("biryani night this Friday", "Ram's birthday
  party is on Saturday"), set has_deadline to false and leave the other fields empty.
- For when_text, copy the date phrase exactly as it's written ("tomorrow", "by the
  25th", "next Friday", "5 August"). Don't turn it into a calendar date yourself;
  Python handles that.
- If there's clearly a deadline but no date is given, keep has_deadline true and set
  when_text to null.
- tags: 2 to 4 short lowercase keywords.

MESSAGE:
{announcement}"""


def extract(text):
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(announcement=text)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0,                     # same input gives the same output
        },
    }
    resp = requests.post(URL, headers=HEADERS, json=body, timeout=30)
    resp.raise_for_status()
    # In JSON mode the answer still comes back as a JSON string inside the response.
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw)


# ---------- Date resolution (in Python, not the model) ----------
# The model gives us when_text as a phrase; we turn it into a real date here.
# dateparser does most of the work, but v1.4.1 gets two things wrong that we
# patch below: it can't parse "next/this <weekday>" (returns None), and it
# misreads lone ordinals ("1st" as a month, "31st" as a year). Everything else
# ("tomorrow", "in 3 days", "5 July", ISO dates) is left to dateparser.

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
    """Turn a date phrase into a real date, anchored to when the message was sent.

    Anchoring to `received_at` means the result doesn't depend on when we happen
    to process the message. Two dateparser settings do the work:
      RELATIVE_BASE=received_at   makes "now" for phrases like "tomorrow" or
                                  "next week" the send time, so it's reproducible.
      PREFER_DATES_FROM="future"  makes ambiguous dates like "Friday" or "5 July"
                                  resolve forward, which is what a deadline means.

    Returns a datetime.date, or None if the phrase is empty or unparseable.
    """
    if not when_text:
        return None
    anchor = received_at.date()
    bare = _strip_leading(when_text)

    # Bare day-of-month like "the 25th": place it ourselves and roll to the next
    # month that has it, since dateparser is unreliable on lone ordinals.
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