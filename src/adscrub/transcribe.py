"""M2: local transcription for episodes with no usable chapter markers.

Backend: faster-whisper. Device is auto-detected via
`ctranslate2.get_cuda_device_count()` (no torch needed just for that check) —
CUDA float16 if a GPU is visible to the process, CPU int8 otherwise. `code`
physically has an RTX 2070 SUPER and Docker here has the `nvidia` runtime + a
CDI device registered, but an interactive dev shell doesn't get the device
nodes passed through, so this same code legitimately runs CPU in one context
and GPU in another — see CLAUDE.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import httpx

from .db import utcnow

DEFAULT_MODEL = os.environ.get("ADSCRUB_WHISPER_MODEL", "small")
DEFAULT_DATA_DIR = Path(os.environ.get("ADSCRUB_DATA_DIR", "data"))

_model = None
_model_size = None


def _pick_device() -> tuple[str, str]:
    import ctranslate2

    if ctranslate2.get_cuda_device_count() > 0:
        return "cuda", "float16"
    return "cpu", "int8"


def load_model(model_size: str = DEFAULT_MODEL):
    """Load (and cache) the Whisper model. Reloads only if model_size changes."""
    global _model, _model_size
    if _model is not None and _model_size == model_size:
        return _model
    from faster_whisper import WhisperModel

    device, compute_type = _pick_device()
    _model = WhisperModel(model_size, device=device, compute_type=compute_type)
    _model_size = model_size
    return _model


def download_audio(client: httpx.Client, audio_url: str, dest: Path) -> Path:
    """Fetch episode audio to dest if not already cached there."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", audio_url) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    tmp.rename(dest)
    return dest


def transcribe_episode(
    conn: sqlite3.Connection,
    episode: sqlite3.Row,
    client: httpx.Client,
    data_dir: Path = DEFAULT_DATA_DIR,
    model_size: str = DEFAULT_MODEL,
) -> Path:
    """Download (if needed), transcribe, store segment timestamps, update the episode row."""
    audio_path = download_audio(
        client, episode["audio_url"], data_dir / "audio" / f"{episode['id']}.mp3"
    )
    model = load_model(model_size)
    segments, _info = model.transcribe(str(audio_path), vad_filter=True)
    transcript = [
        {"start": seg.start, "end": seg.end, "text": seg.text.strip()} for seg in segments
    ]

    transcript_path = data_dir / "transcripts" / f"{episode['id']}.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps(transcript, indent=2))

    conn.execute(
        "UPDATE episodes SET transcript_path = ?, status = 'transcribed', updated_at = ? WHERE id = ?",
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
