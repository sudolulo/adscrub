"""M3: classify ad spans from a transcript via LLM call.

Not implemented yet. Approach: send the timestamped transcript to a Claude
model and ask it to flag spans matching host-read ad patterns ("brought to
you by", promo codes, URL drops, sudden tone/topic shift) — this is far more
robust than fingerprinting, since dynamically-inserted and host-read ads are
usually unique per download and can't be matched against a known-ad database.
See docs/PLAN.md M3.
"""

from __future__ import annotations

import sqlite3


def detect_ad_spans(conn: sqlite3.Connection, episode: sqlite3.Row, transcript_path: str) -> list[tuple[float, float]]:
    raise NotImplementedError("M3: LLM ad-span classification not built yet — see docs/PLAN.md")
