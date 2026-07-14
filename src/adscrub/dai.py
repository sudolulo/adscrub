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

from dataclasses import dataclass
from typing import Callable

import httpx

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
