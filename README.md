# adscrub

Self-hosted podcast ad-detection and removal proxy. It sits between a real RSS feed
and your podcast player: fetch each new episode server-side, find the ad spans,
cut them out of the audio, and re-host a clean feed that the player subscribes to
instead of the original. No player-side changes — AntennaPod (or anything else)
just points at a different feed URL.

Why a proxy instead of patching a player: AntennaPod has no ad-skip hook to build
on, and a feed-level proxy works with any podcast app, not just one.

## Pipeline

1. **Ingest** (done) — register a source feed, fetch it, upsert episodes into SQLite.
2. **Chapters** (done) — many feeds already mark ad breaks via Podcasting 2.0
   `<podcast:chapters>`; scan those first since it's free (no transcription needed).
3. **Transcribe** (done) — for episodes with no usable chapter markers, download
   the audio and transcribe locally with Whisper (faster-whisper; CUDA if a GPU is
   visible to the process, CPU int8 otherwise — auto-detected, no config needed).
4. **Detect** (done) — classify ad spans from the transcript via a Claude model
   (host-read ad patterns: "brought to you by", promo codes, URL drops, tone
   shift) — this matters because modern ads are often host-read and unique per
   episode/listener, so a fingerprint/crowdsourced-timestamp database (SponsorBlock
   style) can't catch them. The model points at transcript segment indices, not
   raw timestamps, so stored spans are always grounded in Whisper's own output.
5. **Cut** (not built — M4) — `ffmpeg` out the ad spans, write the clean audio.
6. **Serve** (not built — M4/M5) — re-host a cleaned RSS feed pointing at the cut
   audio; this is the only thing the podcast player ever sees.

See [docs/PLAN.md](docs/PLAN.md) for the full milestone breakdown.

## Usage

```
uv sync
uv run adscrub add-feed https://feeds.example.com/show   # register a feed to proxy
uv run adscrub ingest                                     # fetch it, upsert episodes
uv run adscrub chapters                                   # scan chapter markers for ad spans
uv run adscrub transcribe                                 # Whisper the rest
uv run adscrub detect                                     # LLM ad-span classification
uv run adscrub stats                                      # counts
```

`detect` needs `$ANTHROPIC_API_KEY` set (get it from rbw, not a file — same
convention as hark).

`cut` / `serve` are registered subcommands that report "not built yet" until
their milestones land — see docs/PLAN.md.

Transcription runs CPU-only by default. `code` does have a real GPU (RTX 2070
SUPER) and Docker here has the `nvidia` runtime registered, but that's only wired
up for containers that request it — see `compose.gpu.yaml` and CLAUDE.md for the
GPU deploy path (`uv sync --extra gpu` pulls in the cuBLAS/cuDNN libs faster-whisper
needs for CUDA).

The database defaults to `./adscrub.db`; override with `--db` or `$ADSCRUB_DB`.

## Development

```
uv run pytest
```

Tests use local feed fixtures — no network.

## Relationship to hark

This may end up merged into [hark](https://git.onetick.ninja/flan/hark) as a
module rather than staying a permanently separate service — both projects fetch
feeds, upsert episodes into SQLite, and re-host derived RSS feeds for the same
AntennaPod-stays-unmodified loop. Kept separate for now since the two pipelines
(topic extraction vs. audio transcription/cutting) don't share much code yet;
don't over-invest in infrastructure that would need to be thrown away if they
merge later.

## AI use disclosure

This project is developed with substantial assistance from AI coding tools
(Anthropic Claude). Design decisions and review are human; much of the code is
AI-written.
