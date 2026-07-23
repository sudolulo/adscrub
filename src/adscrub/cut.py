"""M4: cut ad_segments out of the downloaded audio with ffmpeg.

Ad spans can come from more than one source for the same episode (a chapter
marker and an LLM-flagged span might both cover roughly the same ad break, or
overlap partially). Rather than pick a "winning" source, merge overlapping
spans at cut time — this is the same idea PLAN.md flagged in M3 ("dedup is a
pipeline concern, not a schema one"), delivered here.

Approach: ffmpeg -ss/-to stream-copy extraction of each surviving (non-ad)
span, then the concat demuxer glues them back together — no re-encoding, so
no quality loss and no cost proportional to episode length.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .audio import DEFAULT_DATA_DIR, download_audio, probe_duration
from .db import utcnow


# Which sources are trusted to REMOVE AUDIO. The test is not "is this span evidence or
# inference" — `repeat` and `fpmatch` are inference and are exactly what the cheap tiers exist to
# cut. The test is whether the span's BOUNDARIES are grounded in something precise:
#   chapter  publisher-provided markers
#   llm      grounded in the transcript's own segment timestamps
#   repeat   same, via matched segments
#   fpmatch  grounded in audio alignment against a confirmed recording
# Excluded by default, because their edges are not:
#   dai      byte-derived; its END is only an upper bound (see dai.dai_episode), so cutting it
#            would eat into editorial either side of the real insert
#   recur    cold-start self-recurrence; roughly 1 flagged region in 10 is not an ad at all
# Both stay valuable for SEEDING and DISCOVERY, which is a different job from cutting. Opt in
# deliberately (`--sources`) if you want them cut anyway.
CUT_SOURCES = ("chapter", "llm", "repeat", "fpmatch")


# How far an ad edge may be moved to land on silence. Ad breaks are bounded by a beat of
# silence, so the true boundary is nearly always within a second or two of the detected one.
SNAP_WINDOW = 2.5
SILENCE_DB = -35        # ffmpeg silencedetect threshold
SILENCE_MIN = 0.30      # ignore pauses shorter than this; mid-sentence breaths are not boundaries


def detect_silences(
    audio_path: Path, noise_db: int = SILENCE_DB, min_duration: float = SILENCE_MIN
) -> list[tuple[float, float]]:
    """Silent intervals in the file, via one ffmpeg silencedetect pass."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(audio_path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in proc.stderr.splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:")[1].split()[0])
            except (IndexError, ValueError):
                start = None
        elif "silence_end:" in line and start is not None:
            try:
                silences.append((start, float(line.split("silence_end:")[1].split()[0])))
            except (IndexError, ValueError):
                pass
            start = None
    return silences


def snap_spans_to_silence(
    spans: list[tuple[float, float]],
    silences: list[tuple[float, float]],
    window: float = SNAP_WINDOW,
) -> list[tuple[float, float]]:
    """Pull each ad edge INWARD onto silence, so a cut never runs into speech.

    Why this exists, from a real cut: a fingerprint match ends where the ad RECORDING stops
    being recognisable, not where the break ends. On Casefile episode 1 that left the edge 2.3s
    inside the resumed narration, so the cut ate the opening of "It was 405 on the morning of
    Thursday, June 19, 2014" — invisible to every detection metric, obvious to a listener.

    Snapping to the NEAREST silence was tried first and measured worse (2.3s -> 2.88s clipped):
    the closest silence to that edge was a pause *within* the narration, and "nearest" has no
    idea which side of the edge is ad and which is content. Direction is the whole fix — starts
    only move later, ends only move earlier, so a span can shrink and never grow. That biases
    every error towards leaving a sliver of ad rather than deleting a sentence, which is the
    right way round: the leftover ad is audible and harmless, the deleted words are gone.

    An edge with no silence within `window` is left exactly where it was.
    """
    if not silences:
        return list(spans)
    edges = sorted(s for pair in silences for s in pair)

    out: list[tuple[float, float]] = []
    for start, end in spans:
        later = [x for x in edges if start <= x <= start + window]
        earlier = [x for x in edges if end - window <= x <= end]
        new_start = min(later) if later else start
        new_end = max(earlier) if earlier else end
        # shrinking must never invert or empty the span
        out.append((new_start, new_end) if new_end > new_start else (start, end))
    return out


def compute_keep_spans(
    ad_spans: list[tuple[float, float]], duration: float
) -> list[tuple[float, float]]:
    """Merge overlapping/adjacent ad spans, then return the complementary spans to keep."""
    if not ad_spans:
        return [(0.0, duration)]
    merged: list[list[float]] = []
    for start, end in sorted(ad_spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    keep = []
    cursor = 0.0
    for start, end in merged:
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keep.append((cursor, duration))
    return keep


def cut_audio(audio_path: Path, keep_spans: list[tuple[float, float]], output_path: Path) -> None:
    """Write the audio restricted to keep_spans to output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if len(keep_spans) == 1 and keep_spans[0][0] == 0.0:
        shutil.copyfile(audio_path, output_path)  # nothing to cut
        return
    with tempfile.TemporaryDirectory() as tmp:
        segment_paths = []
        for i, (start, end) in enumerate(keep_spans):
            seg_path = Path(tmp) / f"seg_{i}{audio_path.suffix}"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(audio_path), "-ss", str(start), "-to", str(end),
                 "-c", "copy", str(seg_path)],
                capture_output=True, check=True,
            )
            segment_paths.append(seg_path)
        concat_list = Path(tmp) / "concat.txt"
        concat_list.write_text("".join(f"file '{p}'\n" for p in segment_paths))
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(output_path)],
            capture_output=True, check=True,
        )


@dataclass
class CutResult:
    episode_id: int
    title: str
    ad_seconds: float = 0.0
    error: str | None = None


def pending_episodes(
    conn: sqlite3.Connection,
    limit: int | None = None,
    sources: tuple[str, ...] = CUT_SOURCES,
) -> list[sqlite3.Row]:
    """Episodes with at least one CUTTABLE ad span, not yet cut.

    Filtered by source for the same reason cut_episode is: an episode whose only spans came from
    a discovery tier has nothing to remove, and treating it as pending would rewrite the file
    unchanged and mark it done — retiring it from cutting for good once real spans arrive.
    """
    placeholders = ",".join("?" * len(sources))
    query = f"""
        SELECT * FROM episodes
        WHERE cut_path IS NULL
          AND EXISTS (SELECT 1 FROM ad_segments
                      WHERE episode_id = episodes.id AND source IN ({placeholders}))
        ORDER BY id
    """
    params: tuple = tuple(sources)
    if limit:
        query += " LIMIT ?"
        params += (limit,)
    return conn.execute(query, params).fetchall()


def cut_episode(
    conn: sqlite3.Connection,
    episode: sqlite3.Row,
    client: httpx.Client,
    data_dir: Path = DEFAULT_DATA_DIR,
    sources: tuple[str, ...] = CUT_SOURCES,
) -> tuple[Path, float]:
    """Download (if needed), cut ad spans out, update the episode row.

    Only spans from `sources` are removed — see CUT_SOURCES for why a tier can be trusted to
    find an ad without being trusted to say where it stops.

    Returns (cut_path, ad_seconds_removed).
    """
    audio_path = download_audio(
        client, episode["audio_url"], data_dir / "audio" / f"{episode['id']}.mp3"
    )
    duration = probe_duration(audio_path)
    placeholders = ",".join("?" * len(sources))
    ad_spans = [
        (row["start_second"], row["end_second"])
        for row in conn.execute(
            f"SELECT start_second, end_second FROM ad_segments "
            f"WHERE episode_id = ? AND source IN ({placeholders})",
            (episode["id"], *sources),
        )
    ]
    # A detected edge is where the ad stopped being RECOGNISABLE, which is not quite where the
    # break ends; snapping to real silence keeps the cut off the first words of returning content.
    ad_spans = snap_spans_to_silence(ad_spans, detect_silences(audio_path))
    keep_spans = compute_keep_spans(ad_spans, duration)
    ad_seconds = duration - sum(end - start for start, end in keep_spans)

    output_path = data_dir / "cut" / f"{episode['id']}{audio_path.suffix}"
    cut_audio(audio_path, keep_spans, output_path)

    conn.execute(
        "UPDATE episodes SET cut_path = ?, updated_at = ? WHERE id = ?",
        (str(output_path), utcnow(), episode["id"]),
    )
    conn.commit()
    return output_path, ad_seconds


def cut_pending(
    conn: sqlite3.Connection,
    client: httpx.Client,
    data_dir: Path = DEFAULT_DATA_DIR,
    limit: int | None = None,
    on_result: Callable[[CutResult], None] | None = None,
    sources: tuple[str, ...] = CUT_SOURCES,
) -> list[CutResult]:
    results: list[CutResult] = []
    for row in pending_episodes(conn, limit, sources):
        result = CutResult(episode_id=row["id"], title=row["title"] or "")
        try:
            _path, ad_seconds = cut_episode(conn, row, client, data_dir, sources)
            result.ad_seconds = ad_seconds
        except Exception as exc:  # noqa: BLE001 — per-episode isolation
            result.error = str(exc)
        results.append(result)
        if on_result:
            on_result(result)
    return results
