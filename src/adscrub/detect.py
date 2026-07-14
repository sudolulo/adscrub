"""M3: classify ad spans from a transcript via LLM call.

ClaudeAdDetector asks the model to point at *segment indices* rather than raw
timestamps — LLMs are unreliable at reproducing exact floating-point numbers
from memory but reliable at picking items from a numbered list. Indices are
mapped back to the transcript's own start/end times afterwards, so a stored
span is always grounded in what Whisper actually produced, never hallucinated.

This is the step fingerprinting/crowdsourcing can't replace: modern podcast
ads are frequently host-read and/or dynamically inserted per listener, so
there's often nothing stable to match against a known-ad database — the
model has to read the words.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable, Protocol

from pydantic import BaseModel

from .db import utcnow

DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM = """\
You find ad/sponsor reads in a podcast episode transcript. The transcript is a
numbered list of timestamped segments. Identify contiguous runs of segments
that are advertising, not editorial content: sponsor mentions ("brought to you
by", "this episode is sponsored by"), promo/discount codes, URLs to sponsor
sites, or a clear tonal pitch-switch mid-episode. Do not flag ordinary
editorial content, including the show's own self-promotion of its Patreon or
merch, unless it reads as a distinct inserted sponsor segment.

For each ad span, give the index of its first and last segment (inclusive,
0-indexed) and a short reason. If there are no ads, return an empty list.
"""


@dataclass
class DetectedAdSpan:
    start_second: float
    end_second: float
    reason: str
    # Which tier found it. Spans from different tiers are allowed to overlap and are
    # merged at cut time (see cut.py) — "dedup is a pipeline concern, not a schema one".
    source: str = "llm"


class AdSpanDetector(Protocol):
    def detect(self, transcript: list[dict]) -> list[DetectedAdSpan]: ...


class NullDetector:
    """Placeholder that detects nothing (used by tests and dry paths)."""

    def detect(self, transcript: list[dict]) -> list[DetectedAdSpan]:
        return []


@dataclass
class LayeredDetector:
    """Run several detectors over one transcript and take the union of what they find.

    This is "detection is layered, cheapest-first" made into an object instead of a
    convention. It satisfies AdSpanDetector itself, so it drops into detect_episode /
    detect_pending — and into hark's own per-show loop — with nothing else changing:
    a caller that wants the repeat tier in front of the model composes

        LayeredDetector([RepeatAdDetector(library), ClaudeAdDetector(client)])

    and a caller that wants only one passes only one. No flags, no branches, no tier
    knowing about any other tier.

    The union is deliberate: spans keep their own `source`, overlaps are allowed, and
    cut.py merges them at cut time. Nothing here has to decide which tier "wins" —
    dedup is a pipeline concern, not a schema one, and not a detector one either.
    """

    detectors: list[AdSpanDetector]

    def detect(self, transcript: list[dict]) -> list[DetectedAdSpan]:
        spans: list[DetectedAdSpan] = []
        for detector in self.detectors:
            spans += detector.detect(transcript)
        return spans


class _Span(BaseModel):
    start_segment: int
    end_segment: int
    reason: str


class _Detection(BaseModel):
    ad_spans: list[_Span]


def spans_from_segment_indices(
    transcript: list[dict], raw_spans: list[dict]
) -> list[DetectedAdSpan]:
    """Ground `{start_segment, end_segment, reason}` dicts in a transcript's own
    timestamps, discarding any span with a missing/out-of-range index.

    Factored out of ClaudeAdDetector.detect() so any other AdSpanDetector
    implementation — e.g. one fed pre-computed spans instead of calling an
    LLM live — gets the same index-validation/grounding for free rather than
    reimplementing it.
    """
    n = len(transcript)
    spans = []
    for raw in raw_spans:
        start, end = raw.get("start_segment"), raw.get("end_segment")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if not (0 <= start <= end < n):
            continue
        spans.append(
            DetectedAdSpan(
                start_second=transcript[start]["start"],
                end_second=transcript[end]["end"],
                reason=str(raw.get("reason", "")).strip(),
            )
        )
    return spans


# Bound the size of any ONE call, without bounding what the model gets to see.
#
# This used to be `body[:20000]` — a single call, hard-truncated, with the note "a bloated
# transcript shouldn't dominate the token bill". The cost instinct was right and is kept;
# the implementation silently threw away the episode. A rendered transcript runs ~88,000
# characters, so 20,000 of them is the first ~28% — segments 0-235 of 840. Every mid-roll
# and every end-tag ad sits past that cliff, unseen. The episode was then marked
# `llm_detected_at` and never looked at again, so the ads it "found none" of stayed in the
# audio permanently. A truncation that also marks the work complete is worse than no
# detection at all: it launders a 28% look as a finished one.
#
# Chunking keeps the per-call ceiling and covers the whole episode. Indices stay global,
# so spans are grounded exactly as before.
_CHUNK_CHARS = 20_000


def _chunks(transcript: list[dict]) -> list[str]:
    """Render the whole transcript as one or more calls' worth of numbered segments."""
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for i, seg in enumerate(transcript):
        line = f"[{i}] {seg['start']:.1f}-{seg['end']:.1f}: {seg['text']}"
        if buf and size + len(line) + 1 > _CHUNK_CHARS:
            out.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out


class ClaudeAdDetector:
    """Detect ad spans with a Claude model via structured outputs.

    `client` is an anthropic.Anthropic instance (or any object with a
    compatible messages.parse) — injected so tests never touch the network.
    """

    def __init__(self, client, model: str = DEFAULT_MODEL):
        self.client = client
        self.model = model

    def detect(self, transcript: list[dict]) -> list[DetectedAdSpan]:
        if not transcript:
            return []
        raw: list[dict] = []
        for chunk in _chunks(transcript):
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=2048,
                system=_SYSTEM,
                messages=[{"role": "user", "content": chunk}],
                output_format=_Detection,
            )
            parsed = response.parsed_output
            if parsed is None:  # refusal or malformed output — skip this chunk, keep the rest
                continue
            raw += [
                {"start_segment": s.start_segment, "end_segment": s.end_segment, "reason": s.reason}
                for s in parsed.ad_spans
            ]
        # Indices are global, so a span split across a chunk boundary comes back as two
        # adjacent spans. cut.py merges them; nothing here needs to.
        return spans_from_segment_indices(transcript, raw)


@dataclass
class DetectResult:
    episode_id: int
    title: str
    found: int = 0
    error: str | None = None


def pending_episodes(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Episodes with a transcript that haven't been run through LLM detection yet."""
    query = """
        SELECT * FROM episodes
        WHERE transcript_path IS NOT NULL AND llm_detected_at IS NULL
        ORDER BY id
    """
    if limit:
        query += " LIMIT ?"
        return conn.execute(query, (limit,)).fetchall()
    return conn.execute(query).fetchall()


def insert_spans(conn: sqlite3.Connection, episode_id: int, spans: list[DetectedAdSpan]) -> None:
    """Insert spans under each span's own `source`, without marking anything processed.

    Split out of _store so a tier that costs nothing to re-run (repeats.py) can record
    what it finds WITHOUT setting llm_detected_at — which would retire the episode from
    LLM detection forever on the strength of a free pass that never read it.
    """
    for span in spans:
        conn.execute(
            """
            INSERT INTO ad_segments (episode_id, start_second, end_second, source, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (episode_id, span.start_second, span.end_second, span.source, span.reason),
        )


def _store(conn: sqlite3.Connection, episode_id: int, spans: list[DetectedAdSpan]) -> None:
    """Store any detected spans and mark the episode processed.

    Marks llm_detected_at even when zero spans are found — otherwise an
    episode with no ads gets re-sent to the LLM (and re-billed) every run.
    """
    insert_spans(conn, episode_id, spans)
    now = utcnow()
    conn.execute(
        "UPDATE episodes SET llm_detected_at = ?, updated_at = ? WHERE id = ?",
        (now, now, episode_id),
    )


def detect_episode(conn: sqlite3.Connection, episode: sqlite3.Row, detector: AdSpanDetector) -> int:
    """Detect ad spans in one episode's transcript and store them. Returns count found.

    Public so callers can build their own pending-episode selection (e.g. hark
    filtering by its own per-show config) instead of going through
    detect_pending's built-in pending_episodes() query.
    """
    with open(episode["transcript_path"], encoding="utf-8") as fh:
        transcript = json.load(fh)
    spans = detector.detect(transcript)
    _store(conn, episode["id"], spans)
    conn.commit()
    return len(spans)


def detect_pending(
    conn: sqlite3.Connection,
    detector: AdSpanDetector,
    limit: int | None = None,
    on_result: Callable[[DetectResult], None] | None = None,
    max_consecutive_errors: int = 5,
) -> list[DetectResult]:
    """Run detection over pending episodes; stops early on repeated failures.

    Failed episodes are left unmarked (no 'llm' ad_segments row) and are
    retried on the next run.
    """
    results: list[DetectResult] = []
    consecutive_errors = 0
    for row in pending_episodes(conn, limit):
        result = DetectResult(episode_id=row["id"], title=row["title"] or "")
        try:
            result.found = detect_episode(conn, row, detector)
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001 — per-episode isolation, abort on streaks
            conn.rollback()
            result.error = str(exc)
            consecutive_errors += 1
        results.append(result)
        if on_result:
            on_result(result)
        if consecutive_errors >= max_consecutive_errors:
            break
    return results
