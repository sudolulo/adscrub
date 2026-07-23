"""Probe whether an episode's audio_url serves different content depending on
listener-targeting signals — dynamic ad insertion, detectable with zero
transcription or classification if two independently-targeted fetches diverge.

WHY THIS EXISTS
    repeats.py matches a transcript against ad reads already confirmed elsewhere
    in the corpus, but needs at least one confirmed read to match against, and
    needs the episode transcribed first. This tier needs neither: if the audio
    itself differs between two fetches of the SAME nominal episode, the
    differing region is provably server-inserted content, with no model, no
    transcript, no library. Confirmed live 2026-07-14 against an Acast-hosted
    show: two fetches with different User-Agents diverged at ~8.9s in, and
    were byte-identical before that point.

    Each fetch goes through its OWN freshly-constructed client, not a shared
    one — this matters. httpx.Client keeps a cookie jar by default, and it
    silently defeats the whole comparison: the first fetch's response sets a
    listener-tracking cookie, the second fetch (same client) auto-replays it,
    and the ad server sees the same "listener" both times regardless of the
    User-Agent difference. A live A/B run on this exact bug: one shared client
    (auto-persisted cookie) reported acast.com as "same" on an episode a raw
    two-`curl` test (no shared cookie jar) had already shown genuine
    divergence on. Two independent clients — no shared jar, no shared
    connection — is what actually makes each fetch look like a different
    session, the way two different real listeners would.

WHAT IT CANNOT DO
    Find where a diverged region reconverges if the ad is longer than the
    fetched window, or detect anything on a platform that doesn't vary by
    these particular signals (static/baked-in ads, or DAI keyed off something
    this probe doesn't vary — IP, precise geolocation, time-of-day). A clean
    "no divergence found" result is not proof there is no DAI on this
    platform, only that these signals didn't trigger a different render
    within the fetched window.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .audio import DEFAULT_DATA_DIR, probe_duration
from .detect import DetectedAdSpan, insert_spans

DEFAULT_BYTES = 6 * 1024 * 1024  # ~6MB: covers a typical pre-roll plus runway
ANCHOR_SIZE = 4096
ANCHOR_SKIP = 200_000  # bytes past divergence before trying a reconvergence anchor

# Real podcast-app signatures, not browsers: measured 2026-07-14 that browser
# UAs (Chrome/Safari desktop+mobile) under-trigger targeting relative to these
# — megaphone.fm reported no divergence with browser UAs, then diverged
# cleanly (with a real reconvergence point) once probed as Apple Podcasts vs.
# Spotify. A browser requesting an MP3 directly isn't traffic an ad server has
# any reason to personalize; a client claiming to BE a podcast app is exactly
# the traffic its targeting logic exists to look at. Deliberately just
# User-Agent, no cookies: we don't know a given platform's cookie scheme ahead
# of time, and sending none at all is itself a common "distinct listener"
# trigger on platforms that track one (see dai_probe's own client-isolation
# fix for why a shared cookie jar defeats this regardless of UA).
USER_AGENTS = (
    "AppleCoreMedia/1.0.0.21F90 (iPhone; U; CPU OS 17_5 like Mac OS X; en_us)",
    "Spotify/8.9.44 Android/34 (Pixel 8)",
    "Overcast/2024.1 (+http://overcastfm.com/; iOS podcast app)",
    "AntennaPod/3.5 (Linux; Android 14) (Google;Pixel 8)",
)


@dataclass
class DAIProbeResult:
    bytes_compared: int
    diverged: bool
    divergence_byte: int | None = None
    reconverged: bool = False
    reconvergence_byte: int | None = None


def _fetch(client: httpx.Client, url: str, user_agent: str, max_bytes: int) -> bytes:
    resp = client.get(
        url,
        headers={"User-Agent": user_agent, "Range": f"bytes=0-{max_bytes - 1}"},
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


def _find_divergence(a: bytes, b: bytes) -> int | None:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return None


def _find_reconvergence(
    a: bytes, b: bytes, after: int, anchor_skip: int = ANCHOR_SKIP, anchor_size: int = ANCHOR_SIZE
) -> int | None:
    """Search for a chunk of `a`, well past the divergence point, inside `b` —
    a content match rather than a positional one, since two differently-timed
    ad reads leave the post-ad audio at different absolute offsets in each
    stream even when its bytes are identical."""
    anchor_start = after + anchor_skip
    if anchor_start + anchor_size > len(a):
        return None
    anchor = a[anchor_start : anchor_start + anchor_size]
    pos = b.find(anchor)
    return pos if pos != -1 else None


def probe_variance(
    client_factory: Callable[[], httpx.Client],
    audio_url: str,
    max_bytes: int = DEFAULT_BYTES,
    user_agents: tuple[str, str] = USER_AGENTS[:2],
    anchor_skip: int = ANCHOR_SKIP,
    anchor_size: int = ANCHOR_SIZE,
) -> DAIProbeResult:
    """Fetch `audio_url` with two different User-Agents, each through its own
    freshly-built client (own cookie jar, own connection — see the module
    docstring for why a shared client silently breaks this), and compare."""
    with client_factory() as client_a:
        a = _fetch(client_a, audio_url, user_agents[0], max_bytes)
    with client_factory() as client_b:
        b = _fetch(client_b, audio_url, user_agents[1], max_bytes)
    n = min(len(a), len(b))
    divergence = _find_divergence(a, b)
    if divergence is None:
        return DAIProbeResult(bytes_compared=n, diverged=False)
    reconv = _find_reconvergence(a, b, divergence, anchor_skip, anchor_size)
    return DAIProbeResult(
        bytes_compared=n,
        diverged=True,
        divergence_byte=divergence,
        reconverged=reconv is not None,
        reconvergence_byte=reconv,
    )


# A probe result is bytes, not seconds, and the two are only related through the file's average
# byte rate — exact for CBR mp3, approximate for VBR. Trim this much off each end before storing.
DAI_EDGE_MARGIN = 2.0
# Refuse to store a span longer than one plausible ad break. Guards against a bogus
# reconvergence anchor turning half an episode into an "ad".
MAX_DAI_BREAK = 240.0
MIN_DAI_BREAK = 8.0


@dataclass
class DAIStoreResult:
    episode_id: int
    stored: int = 0
    reason: str = ""


def dai_episode(
    conn: sqlite3.Connection,
    episode: sqlite3.Row,
    client_factory: Callable[[], httpx.Client],
    data_dir: Path = DEFAULT_DATA_DIR,
    **probe_kwargs,
) -> DAIStoreResult:
    """Probe one episode and store what diverged as a `dai` ad span. No model, no transcript.

    WHAT THE STORED SPAN ACTUALLY MEANS — read before trusting it
        The probe compares two independently-targeted fetches. Where they first differ is
        provably server-inserted content, so the START is well-founded. The END is not: the
        reconvergence anchor is taken far past the divergence, so it locates where the two
        streams realign, which is an UPPER BOUND on where the inserted content ended, not the
        end itself (recovering the exact length would need the ad length in the *other* stream,
        which the probe never learns). The stored span is therefore a superset of the real ad,
        trimmed by DAI_EDGE_MARGIN and capped at MAX_DAI_BREAK.

        Two consequences, both deliberate:
        - It is good enough to SEED THE FINGERPRINT LIBRARY. Boundary slop there costs nothing:
          matching needs a long aligned run, and any editorial that bleeds into the span is
          neutralised by the editorial stop-list, which drops exactly that audio.
        - It is NOT good enough to seed the TEXT library (`repeats.GROUND_TRUTH_SOURCES` stays
          `llm`/`chapter`), where a wrong boundary would teach the matcher editorial wording.

    Also note the file we fingerprint is a THIRD fetch, so it may carry a different campaign
    than either probed variant — that is fine and even useful: whatever sits at the divergence
    point in our own copy is still a real inserted ad, and it is the copy we serve.

    Idempotent: existing `dai` rows for the episode are replaced.
    """
    result = DAIStoreResult(episode_id=episode["id"])
    probe = probe_variance(client_factory, episode["audio_url"], **probe_kwargs)
    if not probe.diverged:
        result.reason = "no divergence — static ads, or DAI not keyed on these signals"
        return result
    if not probe.reconverged:
        result.reason = "diverged but never realigned in the fetched window — no usable end"
        return result

    audio_path = Path(data_dir) / "audio" / f"{episode['id']}.mp3"
    if not audio_path.exists():
        result.reason = "local audio not downloaded — cannot convert bytes to seconds"
        return result
    duration = probe_duration(audio_path)
    size = audio_path.stat().st_size
    if duration <= 0 or size <= 0:
        result.reason = "unreadable audio"
        return result

    bytes_per_second = size / duration
    start = probe.divergence_byte / bytes_per_second + DAI_EDGE_MARGIN
    end = probe.reconvergence_byte / bytes_per_second - DAI_EDGE_MARGIN
    end = min(end, start + MAX_DAI_BREAK, duration)
    if end - start < MIN_DAI_BREAK:
        result.reason = f"span too short after trimming ({end - start:.1f}s)"
        return result

    conn.execute("DELETE FROM ad_segments WHERE episode_id = ? AND source = 'dai'", (episode["id"],))
    insert_spans(conn, episode["id"], [DetectedAdSpan(
        start_second=start, end_second=end,
        reason="two independently-targeted fetches diverged here (server-inserted)",
        source="dai")])
    # weaker than llm/chapter: the start is evidence, the end is an upper bound
    conn.execute("UPDATE ad_segments SET confidence = 0.5 WHERE episode_id = ? AND source = 'dai'",
                 (episode["id"],))
    conn.commit()
    result.stored = 1
    return result
