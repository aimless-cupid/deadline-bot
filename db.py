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
    # managed Postgres (Neon) exposes a pooled endpoint that is PgBouncer in
    # transaction mode — it can't guarantee the same backend connection across
    # statements, so server-side prepared statements would break. disable them.
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
        -- uniqueness scoped to ownership: the same announcement forwarded by two
        -- users is two rows, not a silent drop for whoever sent it second.
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

    Dedupe is scoped to (chat_id, content_hash): re-delivery of the same message
    is still a per-user no-op, but two different users can each keep the same
    announcement — a global unique hash would silently drop the second user's."""
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
    """Upcoming (today-or-future) deadlines for ONE chat, soonest first.
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
    """ONE chat's real deadlines whose date didn't resolve (deadline IS NULL).
    The 'needs review' block — also surfaces pipeline parse failures."""
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
    """Return the stable board token for a chat, minting one on first contact.

    Idempotent: the INSERT is a no-op if the board already exists, and we always
    read back the row that's actually stored — so repeated calls (and the write
    on every incoming update) converge on a single token. INSERT + SELECT share
    one transaction, so the pooled (transaction-mode) endpoint keeps them on the
    same backend."""
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
