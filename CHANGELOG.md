# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
