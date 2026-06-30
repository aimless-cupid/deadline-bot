import os
import hashlib
from datetime import date
import psycopg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS deadlines (
    id           SERIAL PRIMARY KEY,         -- auto-incrementing row id
    title        TEXT NOT NULL,
    type         TEXT,
    deadline     DATE,                        -- nullable: "dates TBA"
    course       TEXT,
    summary      TEXT,
    tags         TEXT[],                      -- Postgres array of text
    raw_text     TEXT NOT NULL,               -- original announcement
    content_hash TEXT UNIQUE NOT NULL,        -- dedupe fingerprint
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

def init_db():
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(SCHEMA)
    print("Schema ready.")


def _content_hash(raw_text):
    # normalize whitespace + case so trivial differences don't dodge dedupe
    normalized = " ".join(raw_text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()

def _parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value                           # already resolved by resolve_when()
    try:
        return date.fromisoformat(value)       # ISO string -> date (defensive)
    except (ValueError, TypeError):
        return None                            # malformed -> store NULL, don't crash

def save_deadline(d, raw_text):
    """Insert one deadline. Returns 'saved' or 'duplicate'."""
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            INSERT INTO deadlines
                (title, type, deadline, course, summary, tags, raw_text, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (content_hash) DO NOTHING
            RETURNING id
            """,
            (
                d.get("title") or "Untitled",
                d.get("type"),
                _parse_date(d.get("deadline")),
                d.get("course"),
                d.get("summary"),
                d.get("tags") or [],
                raw_text,
                _content_hash(raw_text),
            ),
        ).fetchone()
    return "saved" if row else "duplicate"


if __name__ == "__main__":
    init_db()