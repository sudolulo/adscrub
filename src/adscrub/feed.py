"""M4/M5: re-host a cleaned feed (feedgen) pointing at cut_path episodes.

Not implemented yet. This is the only integration point AntennaPod (or any
podcast app) ever sees: subscribe to this feed's URL instead of the
original — same shape as hark's own "output integration" (custom RSS feeds
subscribed to like any podcast, no app-side changes). See docs/PLAN.md M4-M5.
"""

from __future__ import annotations

import sqlite3


def build_feed(conn: sqlite3.Connection, feed_id: int) -> bytes:
    raise NotImplementedError("M4/M5: proxy feed generation not built yet — see docs/PLAN.md")
