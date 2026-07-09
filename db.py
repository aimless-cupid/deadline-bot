import os
import hashlib
from datetime import date
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

def _connect(**kwargs):
    # managed Postgres (Neon) exposes a pooled endpoint that is PgBouncer in
    # transaction mode — it can't guarantee the same backend connection across
    # statements, so server-side prepared statements would break. disable them.
    return psycopg.connect(DATABASE_URL, prepare_threshold=None, **kwargs)


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
    with _connect() as conn:
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
    with _connect() as conn:
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


def _search_clause(q):
    """Return (sql_fragment, params) for an optional keyword filter.
    Empty/None q -> no filter. Keeps search identical across both queries."""
    if not q:
        return "", []
    # ILIKE = case-insensitive LIKE; %term% = substring match anywhere.
    # q is passed as a bound parameter (%s), never concatenated -> injection-safe.
    frag = "AND (title ILIKE %s OR summary ILIKE %s OR course ILIKE %s)"
    like = f"%{q}%"
    return frag, [like, like, like]


def get_upcoming(q=None):
    """Upcoming (today-or-future) deadlines, soonest first, optional keyword filter."""
    frag, params = _search_clause(q)
    sql = f"""
        SELECT id, title, type, deadline, course, summary, tags
        FROM deadlines
        WHERE deadline >= CURRENT_DATE
        {frag}
        ORDER BY deadline ASC
    """
    with _connect(row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def get_undated(q=None):
    """Real deadlines whose date didn't resolve (deadline IS NULL).
    The 'needs review' block — also surfaces pipeline parse failures."""
    frag, params = _search_clause(q)
    sql = f"""
        SELECT id, title, type, deadline, course, summary, tags
        FROM deadlines
        WHERE deadline IS NULL
        {frag}
        ORDER BY created_at DESC
    """
    with _connect(row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


if __name__ == "__main__":
    init_db()