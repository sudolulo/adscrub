"""M1: detect ad spans from existing chapter markers (Podcasting 2.0 chapters JSON).

Many feeds already mark ad breaks as chapters (e.g. "Sponsor Break", "Advertisement").
Where present this is free — no transcription or LLM call needed — so it runs before
the transcribe/detect pipeline (M2/M3) and its results simply seed the same
ad_segments table with source="chapter".
"""

from __future__ import annotations

import re
import sqlite3

import httpx

_AD_KEYWORDS = re.compile(
    r"\b(ad|ads|advert|advertisement|sponsor|sponsors|sponsored|promo)\b", re.IGNORECASE
)


def fetch_chapters(client: httpx.Client, chapters_url: str) -> list[dict]:
    """Fetch and return the raw chapter list from a Podcasting 2.0 chapters JSON URL.

    Format: {"chapters": [{"startTime": 0, "title": "...", "endTime": 30}, ...]}
    endTime is optional in the spec — callers should treat a missing one as
    "runs until the next chapter's startTime".
    """
    resp = client.get(chapters_url)
    resp.raise_for_status()
    return resp.json().get("chapters", [])


def ad_spans_from_chapters(chapters: list[dict], episode_duration: float | None = None) -> list[tuple[float, float]]:
    """Return (start, end) spans for chapters whose title looks like an ad break."""
    spans = []
    for i, chapter in enumerate(chapters):
        title = chapter.get("title") or ""
        if not _AD_KEYWORDS.search(title):
            continue
        start = chapter.get("startTime")
        if start is None:
            continue
        end = chapter.get("endTime")
        if end is None:
            end = (
                chapters[i + 1]["startTime"]
                if i + 1 < len(chapters) and "startTime" in chapters[i + 1]
                else episode_duration
            )
        if end is None:
            continue
        spans.append((float(start), float(end)))
    return spans


def scan_episode(conn: sqlite3.Connection, client: httpx.Client, episode: sqlite3.Row) -> int:
    """Fetch chapters for one episode and store any ad spans found. Returns count stored."""
    if not episode["chapters_url"]:
        return 0
    chapters = fetch_chapters(client, episode["chapters_url"])
    spans = ad_spans_from_chapters(chapters, episode["duration_seconds"])
    for start, end in spans:
        conn.execute(
            """
            INSERT INTO ad_segments (episode_id, start_second, end_second, source)
            VALUES (?, ?, ?, 'chapter')
            """,
            (episode["id"], start, end),
        )
    if spans:
        conn.execute(
            "UPDATE episodes SET status = 'chapters_scanned' WHERE id = ?", (episode["id"],)
        )
    conn.commit()
    return len(spans)
