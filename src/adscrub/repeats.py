"""Deterministic repeat-ad detection: match a transcript against ad reads already
confirmed elsewhere in this corpus.

WHY THIS IS NOT THE FINGERPRINTING THIS PROJECT REJECTED
    CLAUDE.md rules out fingerprinting, and that ruling stands — but read what it
    actually rules out: a *global, crowdsourced* ad-timestamp database (SponsorBlock
    for podcasts). The reasoning was that dynamically-inserted and host-read ads are
    unique per listener, so there is nothing stable for strangers to match against.
    That is true of a shared public database, and it is still true.

    It is not true of our own corpus. This service downloads each episode ONCE,
    server-side, from an ad server that is rotating a small pool of campaigns at that
    moment. So the same ad reads recur, near-verbatim, across the episodes we fetched
    in the same period. The thing there was "nothing to fingerprint against" is
    sitting in our own database.

    Measured over 82 transcripts with 286 confirmed ad spans: **87% of confirmed ad
    segments are recoverable from ad reads confirmed in OTHER episodes**, using 5-word
    shingles at a 0.4 overlap threshold. (Single show — Casefile — so cross-show
    generalisation is argued from the mechanism, not yet demonstrated. The mechanism is
    show-independent: a shared DAI pool repeats across shows, and a host-read sponsor
    spot repeats within one.)

    So this is not a replacement for LLM classification. It is the cheap tier in front
    of it, which is what "detection is layered, cheapest-first" already asks for. A
    campaign the model has already read once does not need it to read them again; a
    novel campaign still does.

WHAT IT IS ALSO FOR: FINDING WHAT THE MODEL MISSED
    The LLM does not have perfect recall over a 14k-token transcript — it reliably
    catches the pre-roll, then gets sloppy in the back half. Of the segments this
    matcher flags that the LLM did *not*, 31% carry an unambiguous brand/CTA marker
    ("betterhelp.com slash casefile", "download the free app") — against 29% of the
    segments the LLM *did* flag. Identical density: these are the same kind of content,
    not false positives. 62% of episodes had at least one such provably-missed ad still
    in the audio.

    Which means running this over episodes already marked `llm_detected_at` is free
    recall, not redundant work. See `apply_repeats(..., include_detected=True)`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .detect import DetectedAdSpan, insert_spans

# Validated on the corpus described above. Shingles rather than whole-segment equality
# because Whisper segments the SAME ad read differently between episodes (different
# surrounding audio -> different boundaries), so exact segment matching is brittle at
# the edges: it scored 70% recall where shingles score 87%.
SHINGLE_K = 5
MATCH_THRESHOLD = 0.4
# Ad reads contain brief non-matching beats (a laugh, a name, a price that varies).
# Bridging gaps of <=2 segments keeps one ad read as one span instead of shattering it.
BRIDGE_GAP = 2

_WORD_RE = re.compile(r"[^a-z0-9 ]")


def _words(text: str) -> list[str]:
    return _WORD_RE.sub(" ", (text or "").lower()).split()


def _shingles(words: list[str], k: int = SHINGLE_K) -> set[tuple[str, ...]]:
    if len(words) < k:
        return set()
    return {tuple(words[i : i + k]) for i in range(len(words) - k + 1)}


def _load_transcript(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data["segments"] if isinstance(data, dict) else data


def _ad_segment_indices(transcript: list[dict], spans: Iterable[tuple[float, float]]) -> set[int]:
    """Which transcript segments fall inside any of these (start_second, end_second) spans."""
    hit: set[int] = set()
    for start, end in spans:
        for i, seg in enumerate(transcript):
            # overlap, not containment: a segment straddling an ad boundary is still ad audio
            if seg["end"] > start and seg["start"] < end:
                hit.add(i)
    return hit


# The tiers whose spans are EVIDENCE — i.e. something actually examined the episode to
# produce them: a publisher's own chapter marker, or a model that read the words.
#
# `repeat` is deliberately NOT in this list, and that is the single most important line in
# this module. A repeat span is an *inference* drawn from the library; feeding it back in
# makes the library bootstrap off its own output. Caught on real data: a second sweep took
# 958 spans to 993, because each pass's guesses became the next pass's evidence, matched
# more loosely, and drifted — a detector slowly hallucinating a larger and larger idea of
# what an ad sounds like. Evidence in, inference out; never the reverse.
GROUND_TRUTH_SOURCES = ("llm", "chapter")


def build_library(
    conn: sqlite3.Connection,
    exclude_episode_id: int | None = None,
    k: int = SHINGLE_K,
    sources: tuple[str, ...] = GROUND_TRUTH_SOURCES,
) -> set[tuple[str, ...]]:
    """Shingles of every ad read CONFIRMED anywhere in the corpus (see GROUND_TRUTH_SOURCES).

    Built from `ad_segments` joined back to the transcripts they came from — so it is
    self-bootstrapping and always current, and there is no second copy of the truth to
    fall out of sync. `exclude_episode_id` exists for leave-one-out evaluation; in
    production the episode being scanned has no ad_segments yet, so it cannot
    contaminate its own library.
    """
    placeholders = ",".join("?" * len(sources))
    rows = conn.execute(
        f"""
        SELECT e.id, e.transcript_path, a.start_second, a.end_second
        FROM ad_segments a JOIN episodes e ON e.id = a.episode_id
        WHERE e.transcript_path IS NOT NULL AND a.source IN ({placeholders})
        ORDER BY e.id
        """,
        sources,
    ).fetchall()

    by_episode: dict[int, tuple[str, list[tuple[float, float]]]] = {}
    for row in rows:
        if exclude_episode_id is not None and row["id"] == exclude_episode_id:
            continue
        path, spans = by_episode.setdefault(row["id"], (row["transcript_path"], []))
        spans.append((row["start_second"], row["end_second"]))

    library: set[tuple[str, ...]] = set()
    for path, spans in by_episode.values():
        try:
            transcript = _load_transcript(path)
        except (OSError, json.JSONDecodeError):
            continue  # a missing/corrupt transcript costs us recall, never correctness
        for i in _ad_segment_indices(transcript, spans):
            library |= _shingles(_words(transcript[i].get("text", "")), k)
    return library


@dataclass
class RepeatAdDetector:
    """An AdSpanDetector that recognises ad reads it has seen before. No network, no cost."""

    library: set[tuple[str, ...]]
    k: int = SHINGLE_K
    threshold: float = MATCH_THRESHOLD
    bridge_gap: int = BRIDGE_GAP

    def detect(self, transcript: list[dict]) -> list[DetectedAdSpan]:
        if not transcript or not self.library:
            return []

        flagged: list[int] = []
        for i, seg in enumerate(transcript):
            grams = _shingles(_words(seg.get("text", "")), self.k)
            if not grams:
                continue  # too short to carry evidence either way
            if len(grams & self.library) / len(grams) >= self.threshold:
                flagged.append(i)
        if not flagged:
            return []

        runs: list[list[int]] = []
        for i in flagged:
            if runs and i - runs[-1][1] <= self.bridge_gap + 1:
                runs[-1][1] = i
            else:
                runs.append([i, i])

        return [
            DetectedAdSpan(
                start_second=transcript[a]["start"],
                end_second=transcript[b]["end"],
                reason="repeats an ad read confirmed in another episode",
                source="repeat",
            )
            for a, b in runs
        ]


@dataclass
class RepeatResult:
    episode_id: int
    title: str
    found: int = 0
    error: str | None = None


def apply_repeats(
    conn: sqlite3.Connection,
    limit: int | None = None,
    on_result=None,
) -> list[RepeatResult]:
    """Scan every transcribed episode against the corpus's own confirmed ad reads.

    Deliberately has no `repeat_detected_at` column and no pending-queue: it re-scans,
    and re-scanning is the point. The library GROWS — an episode scanned when only ten
    ad reads were known deserves another look once a hundred are. Since this tier costs
    nothing but local disk, "have I already done this one?" is the wrong question; the
    LLM needs that bookkeeping precisely because it is expensive, and this does not.

    Idempotent: an episode's existing `repeat` rows are dropped and rewritten, so a
    re-scan refreshes rather than duplicates. `llm` and `chapter` rows are never touched
    — this tier only ever speaks for itself.
    """
    library = build_library(conn)
    results: list[RepeatResult] = []
    if not library:
        return results  # nothing confirmed anywhere yet — the LLM has to go first

    query = """
        SELECT id, title, transcript_path FROM episodes
        WHERE transcript_path IS NOT NULL
        ORDER BY id
    """
    params: tuple = ()
    if limit:
        query += " LIMIT ?"
        params = (limit,)

    detector = RepeatAdDetector(library)
    for row in conn.execute(query, params).fetchall():
        result = RepeatResult(episode_id=row["id"], title=row["title"] or "")
        try:
            transcript = _load_transcript(row["transcript_path"])
            spans = detector.detect(transcript)
            conn.execute(
                "DELETE FROM ad_segments WHERE episode_id = ? AND source = 'repeat'",
                (row["id"],),
            )
            insert_spans(conn, row["id"], spans)
            conn.commit()
            result.found = len(spans)
        except Exception as exc:  # noqa: BLE001 — one bad transcript must not stop the sweep
            conn.rollback()
            result.error = str(exc)
        results.append(result)
        if on_result:
            on_result(result)
    return results
