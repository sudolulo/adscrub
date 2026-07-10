# adscrub

Self-hosted podcast ad-detection and removal proxy. Working title — renaming is cheap,
don't get attached.

## What this is

A homelab service (NOT a mobile app, NOT an AntennaPod fork) that sits between a real
podcast feed and the owner's player:

1. Fetch each new episode server-side.
2. Find ad spans — cheaply via existing chapter markers where a feed provides them,
   otherwise via local transcription + LLM classification.
3. Cut the ad spans out of the audio (`ffmpeg`).
4. Re-host a cleaned RSS feed pointing at the cut audio.

The owner's player (AntennaPod) subscribes to the proxy's feed URL instead of the
original. No player-side modification, ever — same principle as hark's output
integration.

Origin: a chat design discussion on 2026-07-10 about automatic podcast ad-skipping,
prompted by AntennaPod's own long-open, unresolved feature request
(github.com/AntennaPod/AntennaPod/issues/4159 — SponsorBlock-style skipping, still
`Needs: Decision` as of that date). Decided against waiting on that or building a
SponsorBlock-style crowdsourced timestamp database (coverage only exists for popular
shows, and dynamically-inserted/host-read ads are unique per download anyway — nothing
to crowdsource against). A per-owner proxy that actually transcribes/classifies each
episode works on any feed, at the cost of real per-episode compute.

## Architecture decisions (already made — don't relitigate)

- Standalone service, shaped like hark/tiltmeter: scheduled ingest → pipeline → SQLite
  → re-hosted feed. The owner's player stays AntennaPod.
- **Detection is layered, cheapest-first:** check for existing Podcasting 2.0
  `<podcast:chapters>` ad markers before ever transcribing. Only fall back to
  Whisper + LLM classification for feeds/episodes with no usable chapters.
- **Ad-span classification is LLM-over-transcript, not audio fingerprinting.**
  Fingerprinting (à la AdBlockRadio) only catches ads that repeat verbatim; modern
  podcast ads are frequently host-read and/or dynamically inserted per listener, so
  there's often nothing to fingerprint against. Transcribe, then classify the text.
- **No CUDA device on this host** (`code`, a LAN host — confirmed no `/dev/nvidia*`,
  no `nvidia-smi`, 2026-07-10) despite Tendril's GPU work happening elsewhere on the
  fleet. Whisper here means CPU-only inference (int8-quantized small/base model) until
  proven otherwise on a box with real passthrough. This is the same open question hark's
  own PLAN.md already flagged and deferred — now empirically confirmed, not just
  suspected.
- Integration output is a regenerated RSS feed, same pattern as hark: the player
  subscribes to it like any podcast, zero app-side changes.

## Relationship to hark

Explicitly a candidate for later merging into `flan/hark` as a module — both projects
fetch feeds, upsert episodes into SQLite, and re-host derived feeds for the same
AntennaPod-stays-unmodified loop. Kept as a separate repo for now because the two
pipelines (topic extraction from metadata vs. audio transcription/cutting) don't share
meaningful code yet. Don't build permanence into this separateness (e.g. don't invent
a distinct auth/web layer, deployment identity, or feed-registration UX that would
just get thrown away on merge) — keep it a thin CLI + SQLite + pipeline, matching
hark's own M0/M1 shape, so a later merge is a module import, not a rewrite.

## Conventions

- Python 3.12+, `uv` + `pyproject.toml`, src layout. SQLite for storage. Keep
  dependencies minimal; don't add faster-whisper/torch etc. until M2 actually needs them.
- CHANGELOG.md in Keep a Changelog format; SemVer.
- **No AI/Claude attribution in commit messages** (no Co-Authored-By). Disclose AI use
  in the README instead. Commit messages describe actual changes, concise; never
  reference prompts or instructions.
- Significant multi-commit features go on a feature branch; small increments can go on
  main while the project is pre-0.1.
- Remote: private Gitea repo `flan/adscrub` (origin, SSH). Do not create additional
  remotes or mirrors unprompted.
