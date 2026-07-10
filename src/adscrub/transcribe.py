"""M2: local transcription for episodes with no usable chapter markers.

Not implemented yet. Intended backend: faster-whisper. This host has no
CUDA device (`/dev/nvidia*` absent) so the first working version should
assume CPU-only inference (int8 quantized small/base model) and treat GPU
as an optional speed-up available only where real passthrough exists —
see docs/PLAN.md M2 and the open question inherited from hark's own
Whisper-feasibility note.
"""

from __future__ import annotations

import sqlite3


def transcribe_episode(conn: sqlite3.Connection, episode: sqlite3.Row, audio_path: str) -> str:
    raise NotImplementedError("M2: transcription pipeline not built yet — see docs/PLAN.md")
