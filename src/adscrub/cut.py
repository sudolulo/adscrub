"""M4: cut ad_segments out of the downloaded audio with ffmpeg.

Not implemented yet. Approach: ffmpeg -ss/-to segment extraction for the
surviving (non-ad) spans, concat-demuxer them back together, write the
result to episodes.cut_path. See docs/PLAN.md M4.
"""

from __future__ import annotations

import sqlite3


def cut_ads(conn: sqlite3.Connection, episode: sqlite3.Row, audio_path: str) -> str:
    raise NotImplementedError("M4: audio cutting not built yet — see docs/PLAN.md")
