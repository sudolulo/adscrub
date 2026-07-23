"""Acoustic-fingerprint ad detection: recognise ad audio this corpus has confirmed
before, straight from the episode's audio — no transcript, no model.

WHY THIS IS NOT THE FINGERPRINTING THIS PROJECT REJECTED
    CLAUDE.md and detect.py both say fingerprinting can't work because modern podcast
    ads are host-read and/or dynamically inserted per listener, so there's nothing
    stable to match. Read what that actually rules out, exactly as repeats.py's docstring
    does: a *global, crowdsourced* ad database (SponsorBlock for podcasts). Strangers'
    ads aren't our ads. That is still true.

    It is not true of our own corpus. We download each episode ONCE, server-side, from an
    ad server rotating a small pool of campaigns, so the same *recording* is stitched into
    many episodes fetched in the same period. repeats.py proved this on the TEXT of the
    transcript; this proves it on the AUDIO, which is the same evidence one layer earlier.

    Measured (2026-07-23), leave-one-out over 286 confirmed ad spans (Casefile, 82 eps),
    Chromaprint fingerprints, cross-EPISODE only: **90.5% of ad slices recovered** (98.3%
    by duration) with **0/82 non-ad control slices falsely matched**. Single-show ground
    truth, so cross-show generalisation is argued from the mechanism (a shared DAI pool
    repeats across shows; a produced sponsor spot repeats within one), not yet demonstrated
    — the same caveat repeats.py carries.

WHAT THIS BUYS OVER repeats.py
    repeats.py already recovers this recall — but from the transcript, so it still pays the
    Whisper cost first and only saves the model call. This tier matches the audio directly,
    so it runs BEFORE transcription: a campaign we have already confirmed once is cut with
    no ASR and no model. That is the cost lever repeats.py structurally cannot pull.

WHAT IT CANNOT DO
    - Recognise a campaign it has never confirmed. The FIRST airing of any ad is invisible
      here and must be caught by chapters/transcribe+LLM (or dai.py) to seed the library.
      This is a *recognition* tier, never a *discovery* one.
    - Catch a re-recorded host read. Fingerprints match the same RECORDING; a host reading
      the same script twice makes two different recordings. Those are repeats.py's job (it
      matches words) and the model's. Fingerprint recall is therefore a floor on, not a
      replacement for, text-level recall.

`fpmatch` spans are INFERENCE, not evidence — never let them seed the library
    (`repeats.GROUND_TRUTH_SOURCES` stays `("llm","chapter")`). A fingerprint match is a
    guess drawn from the library; feeding it back in makes the library bootstrap off its
    own output, the exact drift repeats.py was bitten by. Evidence in, inference out.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .audio import DEFAULT_DATA_DIR, download_audio, probe_duration
from .db import utcnow
from .detect import DetectedAdSpan, insert_spans
from .repeats import GROUND_TRUTH_SOURCES

# Chromaprint emits ~one 32-bit sub-fingerprint per 0.1238s of audio (~8.07 fps). We never
# hardcode that rate for time mapping — it's derived per file from frames/duration — but the
# match thresholds below are frame counts, so they are implicitly ~8 frames per second.
MATCH_FRAMES = 40          # >= ~5s of frames aligned on one offset = a real recording match.
                           # Validated: 0/82 non-ad controls reached this in the pilot.
BRIDGE_FRAMES = 16         # ~2s: an ad read has brief self-similar-to-nothing beats; bridging
                           # keeps one recording as one span instead of shattering it.
STOP_EPISODE_FRACTION = 0.30  # a fingerprint value present in >30% of source episodes is
                           # silence / a common music bed, not ad-identifying — drop it, or it
                           # manufactures diagonals between unrelated audio.
FP_LENGTH = 100_000        # fpcalc's -length cap (seconds); large enough to cover a whole
                           # episode. Its default is 120s, which would fingerprint only the
                           # pre-roll and miss every mid/post-roll ad.
FP_SAMPLE_RATE = 11025     # Chromaprint's own working rate; slicing to it keeps temp wavs tiny.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ad_fingerprints (
    ad_segment_id INTEGER PRIMARY KEY REFERENCES ad_segments(id) ON DELETE CASCADE,
    fingerprint   TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS episode_fingerprints (
    episode_id   INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
    fingerprint  TEXT NOT NULL,
    duration     REAL NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


def fpcalc_available() -> bool:
    return shutil.which("fpcalc") is not None


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the fingerprint cache table if absent.

    Owned entirely by this module and created lazily, so it works against hark's
    connection too without hark needing to know the table exists — hark never queries
    it. FK ON DELETE CASCADE ties each cached fingerprint to its ad_segment, so a
    deleted confirmed ad takes its stale fingerprint with it (foreign_keys is ON in
    both projects' connect()).
    """
    conn.executescript(_SCHEMA)
    conn.commit()


def _parse_fingerprint(raw: str) -> list[int]:
    return [int(x) & 0xFFFFFFFF for x in raw.split(",") if x]


def _fpcalc(path: str | Path, length: int = FP_LENGTH) -> list[int]:
    """Raw Chromaprint sub-fingerprints for a whole audio file (any format fpcalc reads)."""
    out = subprocess.run(
        ["fpcalc", "-raw", "-length", str(length), str(path)],
        capture_output=True, text=True,
    )
    for line in out.stdout.splitlines():
        if line.startswith("FINGERPRINT="):
            return _parse_fingerprint(line[len("FINGERPRINT="):])
    return []


def _fingerprint_region(audio_path: str | Path, start: float, end: float) -> list[int]:
    """Fingerprint just [start, end] of an audio file (used to build the library)."""
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(start), "-to", str(end),
             "-i", str(audio_path), "-ac", "1", "-ar", str(FP_SAMPLE_RATE), wav],
            check=True, capture_output=True,
        )
        return _fpcalc(wav)
    finally:
        os.unlink(wav)


def episode_fingerprint(
    conn: sqlite3.Connection, episode_id: int, audio_path: str | Path
) -> tuple[list[int], float]:
    """The whole-episode fingerprint + duration, computed once and cached.

    Fingerprinting a full episode's audio is the tier's ONLY real cost (fpcalc decodes the
    whole file). It never changes for a given episode, so it is cached here — that is what
    keeps re-scanning genuinely free as the library grows: a re-scan re-runs only the
    matching (cheap set-lookups), never the decode. An empty result (fpcalc failed) is not
    cached, so it retries next run.
    """
    ensure_schema(conn)
    row = conn.execute(
        "SELECT fingerprint, duration FROM episode_fingerprints WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    if row:
        return _parse_fingerprint(row[0]), row[1]
    fp = _fpcalc(audio_path)
    duration = probe_duration(Path(audio_path))
    if fp:
        conn.execute(
            "INSERT OR REPLACE INTO episode_fingerprints (episode_id, fingerprint, duration) "
            "VALUES (?, ?, ?)",
            (episode_id, ",".join(str(v) for v in fp), duration),
        )
        conn.commit()
    return fp, duration


@dataclass
class Library:
    """An inverted index over confirmed-ad fingerprints. `index` maps a fingerprint value
    to the (ad_segment_id, frame) positions carrying it; `ad_episode` maps an ad to its
    source episode so a query can be excluded from matching its own corpus."""

    index: dict[int, list[tuple[int, int]]]
    stop: set[int]
    ad_episode: dict[int, int]
    n_episodes: int = 0

    def __bool__(self) -> bool:
        return bool(self.index)


def build_library(
    conn: sqlite3.Connection,
    data_dir: Path = DEFAULT_DATA_DIR,
    exclude_episode_id: int | None = None,
    sources: tuple[str, ...] = GROUND_TRUTH_SOURCES,
    refresh: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> Library:
    """Fingerprint every CONFIRMED ad read (see GROUND_TRUTH_SOURCES) and index it.

    Fingerprints are cached in `ad_fingerprints` keyed by ad_segment id, because slicing
    and fingerprinting audio is far too slow to redo every run (unlike repeats.py, which
    can cheaply re-shingle text each time). `llm`/`chapter` rows are insert-once, so a
    fingerprint keyed by id never goes stale; pass `refresh=True` to recompute anyway.

    A missing/unreadable audio file costs recall, never correctness — it is skipped, same
    as repeats.py skips a missing transcript. `exclude_episode_id` supports leave-one-out
    evaluation; in production the episode being scanned has no confirmed ads yet, so it
    cannot contaminate its own library.
    """
    ensure_schema(conn)
    placeholders = ",".join("?" * len(sources))
    rows = conn.execute(
        f"""
        SELECT a.id, a.episode_id, a.start_second, a.end_second
        FROM ad_segments a JOIN episodes e ON e.id = a.episode_id
        WHERE a.source IN ({placeholders})
        ORDER BY a.id
        """,
        sources,
    ).fetchall()

    have = {r[0] for r in conn.execute("SELECT ad_segment_id FROM ad_fingerprints")}
    todo = [r for r in rows if refresh or r["id"] not in have]
    for n, r in enumerate(todo, 1):
        audio = Path(data_dir) / "audio" / f"{r['episode_id']}.mp3"
        if not audio.exists():
            continue
        fp = _fingerprint_region(audio, r["start_second"], r["end_second"])
        if fp:
            conn.execute(
                "INSERT OR REPLACE INTO ad_fingerprints (ad_segment_id, fingerprint) VALUES (?, ?)",
                (r["id"], ",".join(str(v) for v in fp)),
            )
        if on_progress:
            on_progress(n, len(todo))
    conn.commit()

    index: dict[int, list[tuple[int, int]]] = defaultdict(list)
    ad_episode: dict[int, int] = {}
    ep_of_value: dict[int, set[int]] = defaultdict(set)
    for ad_id, episode_id, raw in conn.execute(
        """
        SELECT f.ad_segment_id, a.episode_id, f.fingerprint
        FROM ad_fingerprints f JOIN ad_segments a ON a.id = f.ad_segment_id
        """
    ):
        if exclude_episode_id is not None and episode_id == exclude_episode_id:
            continue
        ad_episode[ad_id] = episode_id
        for k, v in enumerate(_parse_fingerprint(raw)):
            index[v].append((ad_id, k))
            ep_of_value[v].add(episode_id)

    n_eps = len(set(ad_episode.values()))
    stop = {v for v, eps in ep_of_value.items() if len(eps) > STOP_EPISODE_FRACTION * n_eps} if n_eps else set()
    return Library(index=dict(index), stop=stop, ad_episode=ad_episode, n_episodes=n_eps)


def _group_runs(frames: list[int], bridge: int, min_len: int) -> list[tuple[int, int]]:
    """Collapse sorted frame indices into (first, last) runs, bridging gaps <= `bridge`,
    keeping only runs spanning at least `min_len` frames."""
    if not frames:
        return []
    runs: list[list[int]] = [[frames[0], frames[0]]]
    for i in frames[1:]:
        if i - runs[-1][1] <= bridge + 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    return [(a, b) for a, b in runs if b - a + 1 >= min_len]


def match_regions(
    query_fp: list[int],
    library: Library,
    exclude_episode_id: int | None = None,
    match_frames: int = MATCH_FRAMES,
    bridge: int = BRIDGE_FRAMES,
) -> list[tuple[int, int]]:
    """Frame-index runs of `query_fp` that align to a confirmed ad recording.

    For each library hit, vote on the alignment diagonal (query_frame - library_frame) of
    that specific ad. A diagonal accumulating >= match_frames hits is a genuine recording
    match — coincidental value collisions scatter across offsets, they do not pile onto one
    (the 0% control false-match rate is this criterion working). The query frames on every
    surviving diagonal, grouped into runs, are the ad regions.
    """
    if not query_fp or not library:
        return []
    diagonals: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, v in enumerate(query_fp):
        if v in library.stop:
            continue
        for ad_id, j in library.index.get(v, ()):  # empty tuple = value not in library
            if exclude_episode_id is not None and library.ad_episode.get(ad_id) == exclude_episode_id:
                continue
            diagonals[(ad_id, i - j)].append(i)
    ad_frames: set[int] = set()
    for frames in diagonals.values():
        if len(frames) >= match_frames:
            ad_frames.update(frames)
    return _group_runs(sorted(ad_frames), bridge, min_len=match_frames)


@dataclass
class AudioFingerprintDetector:
    """Recognises confirmed-ad audio in a whole episode. No transcript, no network, no cost.

    Deliberately NOT an AdSpanDetector (that protocol takes a transcript): the entire point
    is to run before transcription. It is a sibling stage that takes an audio path, so it
    sits alongside chapters/transcribe/detect, not inside LayeredDetector.
    """

    library: Library
    match_frames: int = MATCH_FRAMES
    bridge: int = BRIDGE_FRAMES

    def match_fingerprint(
        self, fp: list[int], duration: float, exclude_episode_id: int | None = None
    ) -> list[DetectedAdSpan]:
        """Ad spans in an already-computed episode fingerprint. This is the cheap part —
        re-runnable freely against a grown library without touching the audio again."""
        if not fp:
            return []
        # Frame -> seconds via this file's own frame density; robust to fpcalc's edge
        # trimming and independent of the ~8.07 nominal fps.
        per_frame = duration / len(fp)
        runs = match_regions(fp, self.library, exclude_episode_id, self.match_frames, self.bridge)
        return [
            DetectedAdSpan(
                start_second=a * per_frame,
                end_second=(b + 1) * per_frame,
                reason="matches an ad recording confirmed in another episode",
                source="fpmatch",
            )
            for a, b in runs
        ]

    def detect_audio(
        self, audio_path: str | Path, exclude_episode_id: int | None = None
    ) -> list[DetectedAdSpan]:
        """Fingerprint an audio file from scratch and match it (uncached convenience path)."""
        fp = _fpcalc(audio_path)
        if not fp:
            return []
        return self.match_fingerprint(fp, probe_duration(Path(audio_path)), exclude_episode_id)


@dataclass
class FingerprintResult:
    episode_id: int
    title: str
    found: int = 0
    error: str | None = None


def fingerprint_episode(
    conn: sqlite3.Connection,
    episode: sqlite3.Row,
    detector: AudioFingerprintDetector,
    client,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> int:
    """Scan ONE episode's audio against the library and store what matches. Returns count.

    Downloads the audio if not already cached (same cache path as cut.py), so this can run
    on an episode that has never been transcribed. Idempotent: the episode's existing
    `fpmatch` rows are dropped and rewritten, so a re-scan against a grown library refreshes
    rather than duplicates. `llm`/`chapter`/`repeat` rows are never touched — this tier only
    ever speaks for itself. Mirrors repeats.repeat_episode / detect.detect_episode so a
    caller with its own episode selection (e.g. hark's per-show filter) can drive it.
    """
    audio_path = download_audio(
        client, episode["audio_url"], Path(data_dir) / "audio" / f"{episode['id']}.mp3"
    )
    fp, duration = episode_fingerprint(conn, episode["id"], audio_path)
    spans = detector.match_fingerprint(fp, duration, exclude_episode_id=episode["id"])
    conn.execute(
        "DELETE FROM ad_segments WHERE episode_id = ? AND source = 'fpmatch'",
        (episode["id"],),
    )
    insert_spans(conn, episode["id"], spans)
    conn.commit()
    return len(spans)


def apply_fingerprints(
    conn: sqlite3.Connection,
    client,
    data_dir: Path = DEFAULT_DATA_DIR,
    limit: int | None = None,
    on_result: Callable[[FingerprintResult], None] | None = None,
) -> list[FingerprintResult]:
    """Scan episodes' audio against the corpus's own confirmed ad recordings.

    Like apply_repeats, it has no `*_at` column and no pending queue: it re-scans, and
    re-scanning is the point — the library grows, so an episode scanned when ten ads were
    known deserves another look at a hundred. Scans episodes that HAVE an audio_url; the
    per-episode download is cached, so a re-run costs only the fingerprint compute.
    """
    if not fpcalc_available():
        raise RuntimeError("fpcalc (Chromaprint / libchromaprint-tools) is not installed")
    library = build_library(conn, data_dir)
    results: list[FingerprintResult] = []
    if not library:
        return results  # nothing confirmed anywhere yet — a discovery tier has to go first

    detector = AudioFingerprintDetector(library)
    query = "SELECT id, title, audio_url FROM episodes WHERE audio_url IS NOT NULL ORDER BY id"
    params: tuple = ()
    if limit:
        query += " LIMIT ?"
        params = (limit,)

    for row in conn.execute(query, params).fetchall():
        result = FingerprintResult(episode_id=row["id"], title=row["title"] or "")
        try:
            result.found = fingerprint_episode(conn, row, detector, client, data_dir)
        except Exception as exc:  # noqa: BLE001 — one bad episode must not stop the sweep
            conn.rollback()
            result.error = str(exc)
        results.append(result)
        if on_result:
            on_result(result)
    return results
