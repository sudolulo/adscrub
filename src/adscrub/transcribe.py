"""M2: local transcription for episodes with no usable chapter markers.

Backend: faster-whisper. Device is auto-detected via
`ctranslate2.get_cuda_device_count()` (no torch needed just for that check) —
CUDA float16 if a GPU is visible to the process, CPU int8 otherwise. `code`
physically has an RTX 2070 SUPER and Docker here has the `nvidia` runtime + a
CDI device registered, but an interactive dev shell doesn't get the device
nodes passed through, so this same code legitimately runs CPU in one context
and GPU in another — see CLAUDE.md.

Device *visibility* and device *usability* aren't the same thing, though: a
container can have /dev/nvidia* nodes passed through (get_cuda_device_count()
> 0) while still lacking the actual CUDA runtime libraries (e.g. built
without the `gpu` extra) — hit in production 2026-07-14, every transcription
failing with "Library libcublas.so.12 is not found or cannot be loaded".
transcribe_episode() catches that lazily, at first real inference, and falls
back to CPU for the rest of the process rather than failing every episode.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import httpx

from .audio import DEFAULT_DATA_DIR, download_audio
from .db import utcnow

DEFAULT_MODEL = os.environ.get("ADSCRUB_WHISPER_MODEL", "small")

_model = None
_model_key = None
_cuda_broken = False  # flipped once a CUDA device that reports itself available
# turns out unusable at actual inference time (see transcribe_episode below)


def _pick_device() -> tuple[str, str]:
    import ctranslate2

    if not _cuda_broken and ctranslate2.get_cuda_device_count() > 0:
        return "cuda", "float16"
    return "cpu", "int8"


def load_model(model_size: str = DEFAULT_MODEL):
    """Load (and cache) the Whisper model. Reloads if model_size changes, or if
    the picked device changes (e.g. after a CUDA runtime failure flips
    _cuda_broken and _pick_device starts returning "cpu")."""
    global _model, _model_key
    device, compute_type = _pick_device()
    key = (model_size, device)
    if _model is not None and _model_key == key:
        return _model
    from faster_whisper import WhisperModel

    _model = WhisperModel(model_size, device=device, compute_type=compute_type)
    _model_key = key
    return _model


_CUDA_RUNTIME_ERROR_MARKERS = ("cublas", "cudnn")


def transcribe_episode(
    conn: sqlite3.Connection,
    episode: sqlite3.Row,
    client: httpx.Client,
    data_dir: Path = DEFAULT_DATA_DIR,
    model_size: str = DEFAULT_MODEL,
) -> Path:
    """Download (if needed), transcribe, store segment timestamps, update the episode row."""
    global _cuda_broken
    audio_path = download_audio(
        client, episode["audio_url"], data_dir / "audio" / f"{episode['id']}.mp3"
    )
    model = load_model(model_size)
    try:
        segments, _info = model.transcribe(str(audio_path), vad_filter=True)
    except RuntimeError as exc:
        # ctranslate2.get_cuda_device_count() can report a device present (driver +
        # device nodes visible) without its runtime libraries actually being
        # loadable — e.g. an image built without the gpu extra running on a host
        # that still exposes /dev/nvidia*. That surfaces here, lazily, on first
        # real inference rather than at model construction. Fall back to CPU for
        # the rest of this process instead of failing every episode forever.
        if _cuda_broken or not any(m in str(exc).lower() for m in _CUDA_RUNTIME_ERROR_MARKERS):
            raise
        _cuda_broken = True
        model = load_model(model_size)
        segments, _info = model.transcribe(str(audio_path), vad_filter=True)
    transcript = [
        {"start": seg.start, "end": seg.end, "text": seg.text.strip()} for seg in segments
    ]

    transcript_path = data_dir / "transcripts" / f"{episode['id']}.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps(transcript, indent=2))

    conn.execute(
        "UPDATE episodes SET transcript_path = ?, updated_at = ? WHERE id = ?",
        (str(transcript_path), utcnow(), episode["id"]),
    )
    conn.commit()
    return transcript_path


def pending_episodes(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Episodes with audio but no transcript yet, and no chapter-sourced ad spans
    already found (that's the M1 fast path — no point transcribing those)."""
    query = """
        SELECT * FROM episodes
        WHERE transcript_path IS NULL AND audio_url IS NOT NULL
          AND id NOT IN (SELECT episode_id FROM ad_segments WHERE source = 'chapter')
        ORDER BY id
    """
    if limit:
        query += " LIMIT ?"
        return conn.execute(query, (limit,)).fetchall()
    return conn.execute(query).fetchall()
