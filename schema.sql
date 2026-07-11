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

CREATE TABLE IF NOT EXISTS boards (
    chat_id    BIGINT PRIMARY KEY,           -- one board per Telegram chat
    token      TEXT UNIQUE NOT NULL,         -- unguessable slug for the board URL
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
