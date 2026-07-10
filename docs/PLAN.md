# adscrub — plan

Milestones. Each one ships something usable and gets a CHANGELOG version.

## M0 — scaffold + ingest (done, 0.1.0)

- Project scaffold: uv/pyproject, src layout, pytest.
- SQLite schema: feeds, episodes, ad_segments.
- Feed ingest: `add-feed`, `ingest`, idempotent upsert, captures chapters URL.
- CLI stubs for transcribe/detect/cut/serve so the eventual pipeline shape is visible
  from `adscrub --help` even before it's built.

## M1 — chapter-sourced ad detection (done, 0.1.0)

- Fetch a feed's `<podcast:chapters>` JSON, keyword-match ad/sponsor chapter titles,
  store spans in `ad_segments` with `source='chapter'`. No transcription, no LLM call —
  this is the free case and should cover any feed whose creator already tags ad breaks.
- Confirmed against feedparser 6.x: the chapters URL lands in
  `entry["podcast_chapters"]["url"]` for feeds using the conventional "podcast" prefix.
  Not yet tested against a real-world feed (only a synthetic fixture) — validate against
  an actual subscribed show before trusting this path in production.

## M2 — transcription (done, 0.2.0)

- Local Whisper (faster-whisper) for episodes with no usable chapters URL, or where
  the chapters pass found nothing (`ad_segments.source = 'chapter'` absent).
- Downloads episode audio to `data/audio/<id>.mp3` (cached, re-run safe), transcribes,
  writes segment-level timestamps to `data/transcripts/<id>.json`.
- **GPU:** `code` physically has an RTX 2070 SUPER and Docker here has the `nvidia`
  runtime + CDI device registered (confirmed 2026-07-10) — resolved, this is not CPU-only
  forever. Device selection is automatic at runtime via
  `ctranslate2.get_cuda_device_count()` (no torch dependency needed just to check): CUDA
  float16 if a device is visible, CPU int8 otherwise. The interactive dev shell itself
  doesn't have the device nodes passed through, so plain `uv run adscrub transcribe`
  here runs CPU — that's expected, not a bug. The Docker deploy path
  (`compose.gpu.yaml` override) requests the GPU explicitly.
- GPU runtime libs (cuBLAS/cuDNN) are an optional `gpu` extra
  (`uv sync --extra gpu`) rather than a base dependency, since they're large,
  CUDA-specific, and only needed on the deploy target.

## M3 — LLM ad-span classification (done, 0.3.0)

- Send the timestamped transcript to a Claude model (reuse hark's
  `$ANTHROPIC_API_KEY`-from-rbw pattern, not a file) via structured outputs
  (`messages.parse`, same idiom as hark's `ClaudeExtractor`). The model points at
  *segment indices*, not raw seconds — LLMs are unreliable at reproducing exact
  floating-point timestamps from memory but reliable at picking from a numbered
  list — and indices are mapped back to the transcript's own timestamps, so a
  stored span is always grounded in what Whisper actually produced.
- Store spans in `ad_segments` with `source='llm'` and a `reason` (why the model
  flagged it — auditability, same ethos as tiltmeter). Keep chapter-sourced spans
  too (dedup/precedence is a pipeline decision, not a schema one — don't drop
  lower-confidence sources, prefer/override at cut time).
- Completion is tracked via `episodes.llm_detected_at`, set even when zero spans
  are found — this fixed a real bug caught by the test suite: without it,
  ad-free episodes got silently re-sent to the LLM (and re-billed) on every run.
  Same fix applied to M1's `chapters_scanned_at` for the same reason (a free
  HTTP re-fetch, not a billing bug, but the same defect class).

## M4 — cut + re-hosted feed

- `ffmpeg` extraction of the surviving (non-ad) spans, concat back together, write to
  `episodes.cut_path`.
- `feedgen`-based feed regeneration: same episodes, `cut_path` audio instead of the
  original `audio_url`, served over HTTP. This is the only integration point any
  podcast player needs — subscribe to this feed's URL instead of the original.
- Docker deploy on TrueNAS, same shape as hark/tiltmeter (scheduled ingest → pipeline →
  serve), once the pipeline actually produces something worth deploying.

## M5 — hark module decision

- Once M4 is working end-to-end, decide whether to fold this into `flan/hark` as a
  module (shared feed-ingest code, one deployed service) or keep it standalone. Don't
  pre-build shared infrastructure for this before M4 proves the pipeline works —
  premature merging risks coupling two still-changing pipelines.

## Open questions (owner input needed, don't block on these)

- M3 currently defaults to `claude-opus-4-8`; revisit cost vs. accuracy on ad-span
  boundaries once it's run against real transcripts (a cheaper model may be plenty
  for a fairly mechanical "find the sponsor read" task).
- Real-world validation of the M1 chapters-URL parsing against an actual subscribed
  feed, not just the synthetic test fixture.
