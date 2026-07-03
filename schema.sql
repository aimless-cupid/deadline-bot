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