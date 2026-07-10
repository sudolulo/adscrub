# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
