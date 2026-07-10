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

## M2 — transcription

- Local Whisper (faster-whisper, CPU-first) for episodes with no usable chapters URL,
  or where the chapters pass found nothing.
- **Open question:** no CUDA device on `code` (confirmed 2026-07-10 — no `/dev/nvidia*`).
  Decide whether M2 runs CPU-only here (slow but simple) or ships the transcription
  step to a different homelab box with real GPU passthrough (adds a network hop and a
  deployment target, but much faster). Don't guess — benchmark CPU-only first; only
  build the remote-dispatch option if CPU throughput is actually a problem given
  realistic episode volume.
- Store transcript + word/segment timestamps; `episodes.transcript_path`.

## M3 — LLM ad-span classification

- Send the timestamped transcript to a Claude model (reuse hark's
  `$ANTHROPIC_API_KEY`-from-rbw pattern, not a file). Ask it to flag spans matching
  host-read ad patterns: "brought to you by", promo codes, URL drops, tone/topic shift.
- Store spans in `ad_segments` with `source='llm'`; keep chapter-sourced spans too
  (dedup/precedence is a pipeline decision, not a schema one — don't drop lower-confidence
  sources, prefer/override at cut time).

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

- GPU/Whisper feasibility: CPU-only on `code`, or dispatch to a box with real
  passthrough? (M2 decision — see above.)
- Which Claude model for M3 classification (cost vs. accuracy on ad-span boundaries).
- Real-world validation of the M1 chapters-URL parsing against an actual subscribed
  feed, not just the synthetic test fixture.
