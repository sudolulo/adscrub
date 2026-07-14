# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.2] - 2026-07-14

### Changed

- **The CPU fallback is no longer silent.** 0.5.1 stopped CUDA-visible-but-unusable
  deploys from failing every episode, but it degraded quietly: the GPU sat at 0%
  utilization while faster-whisper's CPU int8 path saturated ~4 cores, roughly 10x
  slower, with nothing in the logs to say so. Found in production on the hark deploy,
  where it had been burning ~3.7 cores continuously against a 27k-episode backlog. The
  fallback now prints what happened, why it happened, and how to fix it (rebuild with
  the `gpu` extra) to stderr.
- `load_model()` announces the model, device, and compute type it loaded, so a run's
  logs state up front whether it is on GPU or CPU rather than leaving it to be inferred
  from CPU load.

## [0.5.1] - 2026-07-14

### Fixed

- **Transcription now falls back to CPU when CUDA is visible but not actually
  usable.** `_pick_device()` only checked `ctranslate2.get_cuda_device_count()`
  (driver/device nodes visible), not whether the runtime libraries were
  actually loadable — hit in production 2026-07-14 on a hark deploy: every
  single episode failed with `RuntimeError: Library libcublas.so.12 is not
  found or cannot be loaded`, since the failure only surfaces lazily at first
  real inference, not at model construction. `transcribe_episode()` now
  catches that specific failure, flips a `_cuda_broken` flag, and retries once
  on CPU int8 — for the rest of that process, `_pick_device()` skips CUDA
  entirely instead of failing every episode the same way forever.

## [0.5.0] - 2026-07-12

### Added

- `detect.detect_episode(conn, episode, detector)`: the per-episode step
  extracted out of `detect_pending`'s loop body, matching `transcribe_episode`/
  `cut_episode`'s existing shape (detect.py was the odd one out, only exposing
  the bulk `detect_pending` and a private `_store`). Lets a caller build its
  own pending-episode selection instead of going through `pending_episodes()`
  — needed by hark's per-show ad-stripping toggle. `detect_pending` now calls
  it internally; behavior unchanged.
- `detect.spans_from_segment_indices(transcript, raw_spans)`: the
  segment-index-to-timestamp grounding/validation `ClaudeAdDetector.detect()`
  did inline, now a public function. Lets a caller build a non-LLM
  `AdSpanDetector` (e.g. one fed pre-computed spans from an offline judgment
  pass) that still gets the same bounds-checking and timestamp-grounding
  `ClaudeAdDetector` gets, instead of reimplementing it. `ClaudeAdDetector`
  now calls it internally; behavior unchanged.

### Fixed

- `__version__` in `src/adscrub/__init__.py` was stuck at 0.1.0 across every
  milestone release since `pyproject.toml`'s version was bumped each time but
  this constant never was — the CLI's `--version` and every outbound
  User-Agent header have claimed to be `adscrub/0.1.0` this whole time.
- `test_cut_pending_isolates_per_episode_failures` matched a substring ("2")
  against the full audio file path to distinguish episode 2's failure from
  episode 1 — but pytest's auto-numbered `tmp_path` ("pytest-26", "pytest-102",
  ...) can itself contain that digit, making the test flaky depending on run
  order. Now matches the deterministic filename (episode id) instead.

## [0.4.0] - 2026-07-10

### Added

- Ad cutting (`adscrub cut`): merges overlapping ad spans from any source
  (chapter, LLM — no "which source wins" rule needed, overlap-merging handles it),
  then `ffmpeg`-extracts the surviving audio and concatenates with `-c copy` (no
  re-encode, no quality loss). Episode duration comes from `ffprobe` on the real
  file, not RSS metadata.
- Feed serving (`adscrub serve`): stdlib `http.server` (dependency-free, same
  approach as hark's web.py), regenerates a cleaned RSS feed live from the DB at
  `GET /feed/<id>`. Cut episodes are served locally at `/audio/<id>.<ext>`;
  everything else still points at its original `audio_url` — nothing gets a local
  copy unless it was actually cut. No login wall (machine-consumed feed on a
  trusted network, not a browsable dashboard).
- `--base-url` is required to make sense of generated audio links (embedded in
  every cut episode's enclosure); `serve` warns loudly if left at the
  unreachable `localhost` default instead of failing silently into a broken feed.
- Docker: default `CMD` now runs `adscrub serve` (port 8711, `restart:
  unless-stopped`), matching hark/tiltmeter's long-running-service shape;
  pipeline stages remain one-shot `docker compose run --rm` commands.
- Shared `audio.py` module: `download_audio`/`probe_duration`, split out of
  transcribe.py since cut.py needed the same downloaded-audio cache.

## [0.3.0] - 2026-07-10

### Added

- LLM ad-span classification (`adscrub detect`): sends each episode's transcript
  to a Claude model (structured outputs, `claude-opus-4-8` default) which flags
  ad spans by segment index rather than raw timestamps — indices are mapped back
  to the transcript's own timestamps, so stored spans are always grounded in what
  Whisper actually produced. Stores a `reason` alongside each `ad_segments` row
  for auditability.
- `episodes.chapters_scanned_at` / `episodes.llm_detected_at` timestamp columns,
  replacing the old free-text `status` column, to track per-stage completion.

### Fixed

- Episodes with zero ad spans found (chapters or LLM) were never marked done,
  so they'd be redundantly rescanned/re-sent-to-the-LLM on every run — caught by
  a test written for the zero-spans case. Now every processed episode is marked
  complete regardless of what it found.

## [0.2.0] - 2026-07-10

### Added

- Transcription pipeline (`adscrub transcribe`): downloads episode audio (cached,
  re-run safe), transcribes with faster-whisper, stores segment timestamps in
  `data/transcripts/<id>.json`. Skips episodes already covered by a chapter-sourced
  ad span (M1) or already transcribed.
- Device auto-detection via `ctranslate2.get_cuda_device_count()` — CUDA float16 if
  a GPU is visible to the process, CPU int8 otherwise. No config needed; works the
  same whether run from a plain dev shell or a GPU-enabled Docker deploy.
- `compose.gpu.yaml` override: requests the host's GPU via the `nvidia` Docker
  runtime and builds the image with the optional `gpu` extra (cuBLAS/cuDNN).

### Fixed

- CLAUDE.md/docs/PLAN.md previously claimed this host had no GPU at all; corrected
  — `code` has a physical RTX 2070 SUPER and Docker's `nvidia` runtime is
  registered. The earlier claim only reflected this interactive dev shell's LXC not
  having the device nodes passed through, which is a narrower fact.

## [0.1.0] - 2026-07-10

### Added

- Project scaffold: uv/pyproject, src layout, pytest.
- SQLite schema: feeds, episodes, ad_segments (transcript/cut path fields nullable —
  populated starting M2/M4).
- Feed ingest: register a source feed (`add-feed`), fetch + parse it, upsert episodes.
  Idempotent re-runs. Captures each episode's Podcasting 2.0 chapters URL if declared.
- Chapter-sourced ad detection: scan an episode's chapters JSON for ad/sponsor-keyword
  titles, store spans in `ad_segments` with no transcription needed.
- CLI: `adscrub add-feed`, `adscrub ingest`, `adscrub chapters`, `adscrub stats`;
  `transcribe`/`detect`/`cut`/`serve` registered as stubs reporting their milestone.
- Unit tests with feed fixtures (no network in tests).
