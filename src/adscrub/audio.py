"""Shared audio-file helpers: download + duration probing.

Split out of transcribe.py since both transcribe (M2) and cut (M4) need the
same downloaded-episode-audio cache — neither owns it more than the other.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx

DEFAULT_DATA_DIR = Path(os.environ.get("ADSCRUB_DATA_DIR", "data"))

# Cap a single episode download so a hostile or malformed feed can't stream the
# data volume full. Podcast episodes are audio, not archives; 1 GiB is already far
# past any real episode. Override with ADSCRUB_MAX_AUDIO_MB.
MAX_AUDIO_BYTES = int(os.environ.get("ADSCRUB_MAX_AUDIO_MB", "1024")) * 1024 * 1024


def download_audio(
    client: httpx.Client,
    audio_url: str,
    dest: Path,
    max_bytes: int = MAX_AUDIO_BYTES,
) -> Path:
    """Fetch episode audio to dest if not already cached there.

    Aborts (and cleans up the partial file) if the body exceeds max_bytes, both
    from a declared Content-Length and from the actual stream, so an oversized or
    unbounded response can't fill the disk.
    """
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", audio_url) as resp:
        resp.raise_for_status()
        declared = resp.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError(
                f"{audio_url} declares {int(declared)} bytes, over the "
                f"{max_bytes}-byte cap"
            )
        written = 0
        try:
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(
                            f"{audio_url} exceeded the {max_bytes}-byte cap "
                            "mid-stream"
                        )
                    fh.write(chunk)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    tmp.rename(dest)
    return dest


def probe_duration(path: Path) -> float:
    """Audio duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())
