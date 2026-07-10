"""SQLite storage: schema and connection helper."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS feeds (
    id              INTEGER PRIMARY KEY,
    source_url      TEXT NOT NULL UNIQUE,
    title           TEXT,
    description     TEXT,
    image_url       TEXT,
    last_fetched_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
    id               INTEGER PRIMARY KEY,
    feed_id          INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid             TEXT NOT NULL,
    title            TEXT,
    description      TEXT,
    pubdate          TEXT,
    duration_seconds INTEGER,
    audio_url        TEXT,
    chapters_url     TEXT,
    status           TEXT NOT NULL DEFAULT 'new',
    local_path       TEXT,
    transcript_path  TEXT,
    cut_path         TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at       TEXT,
    UNIQUE (feed_id, guid)
);

-- Ad spans for an episode, however they were found. Multiple sources can
-- coexist (e.g. a chapter-sourced span later confirmed by transcript
-- classification) — dedup/precedence is a pipeline concern, not a schema one.
CREATE TABLE IF NOT EXISTS ad_segments (
    id           INTEGER PRIMARY KEY,
    episode_id   INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    start_second REAL NOT NULL,
    end_second   REAL NOT NULL,
    source       TEXT NOT NULL,
    confidence   REAL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_episodes_feed_pubdate ON episodes (feed_id, pubdate);
CREATE INDEX IF NOT EXISTS idx_ad_segments_episode ON ad_segments (episode_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
