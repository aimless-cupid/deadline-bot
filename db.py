import os
import hashlib
import secrets
from datetime import date
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

def _connect(**kwargs):
    # Neon's pooled endpoint is PgBouncer in transaction mode, so two statements
    # aren't guaranteed to hit the same backend connection. That breaks
    # server-side prepared statements, so we turn them off.
    return psycopg.connect(DATABASE_URL, prepare_threshold=None, **kwargs)


# One statement per element: psycopg3's execute() uses the extended protocol,
# which rejects multiple ;-separated commands in a single call.
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS deadlines (
        id           SERIAL PRIMARY KEY,         -- auto-incrementing row id
        chat_id      BIGINT NOT NULL,            -- owner: the Telegram chat it came from
        title        TEXT NOT NULL,
        type         TEXT,
        deadline     DATE,                        -- nullable: "dates TBA"
        course       TEXT,
        summary      TEXT,
        tags         TEXT[],                      -- Postgres array of text
        raw_text     TEXT NOT NULL,               -- original announcement
        content_hash TEXT NOT NULL,               -- dedupe fingerprint (scoped below)
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        -- uniqueness is per owner: the same announcement forwarded by two users
        -- becomes two rows, instead of silently dropping the second one.
        UNIQUE (chat_id, content_hash)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS boards (
        chat_id    BIGINT PRIMARY KEY,           -- one board per Telegram chat
        token      TEXT UNIQUE NOT NULL,         -- unguessable slug for the board URL
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
)

def init_db():
    with _connect() as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
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

def save_deadline(d, raw_text, chat_id):
    """Insert one deadline owned by chat_id. Returns 'saved' or 'duplicate'.

    Dedupe is on (chat_id, content_hash), so re-sending the same message is a
    no-op for that user, but two different users can each keep the same
    announcement. A global hash would have dropped the second user's copy."""
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO deadlines
                (chat_id, title, type, deadline, course, summary, tags, raw_text, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, content_hash) DO NOTHING
            RETURNING id
            """,
            (
                chat_id,
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


def get_upcoming(chat_id, q=None):
    """Today-or-future deadlines for one chat, soonest first.
    chat_id is a bound parameter, never interpolated."""
    frag, params = _search_clause(q)
    sql = f"""
        SELECT id, title, type, deadline, course, summary, tags
        FROM deadlines
        WHERE chat_id = %s AND deadline >= CURRENT_DATE
        {frag}
        ORDER BY deadline ASC
    """
    with _connect(row_factory=dict_row) as conn:
        return conn.execute(sql, [chat_id, *params]).fetchall()


def get_undated(chat_id, q=None):
    """One chat's deadlines whose date didn't resolve (deadline IS NULL).
    This is the 'needs review' block, and it also surfaces parse failures."""
    frag, params = _search_clause(q)
    sql = f"""
        SELECT id, title, type, deadline, course, summary, tags
        FROM deadlines
        WHERE chat_id = %s AND deadline IS NULL
        {frag}
        ORDER BY created_at DESC
    """
    with _connect(row_factory=dict_row) as conn:
        return conn.execute(sql, [chat_id, *params]).fetchall()


def get_or_create_board(chat_id):
    """Return a chat's board token, creating one the first time we see the chat.

    The INSERT is a no-op if the board already exists, and we always read back
    the stored row, so repeated calls (it runs on every update) settle on one
    token. The INSERT and SELECT share a transaction, so the transaction-mode
    pooler keeps them on the same backend."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO boards (chat_id, token) VALUES (%s, %s) "
            "ON CONFLICT (chat_id) DO NOTHING",
            (chat_id, secrets.token_urlsafe(16)),
        )
        row = conn.execute(
            "SELECT token FROM boards WHERE chat_id = %s", (chat_id,)
        ).fetchone()
    return row[0]


def get_chat_id_for_token(token):
    """Resolve a board token to its chat_id, or None if the token is unknown."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT chat_id FROM boards WHERE token = %s", (token,)
        ).fetchone()
    return row[0] if row else None


if __name__ == "__main__":
    init_db()
