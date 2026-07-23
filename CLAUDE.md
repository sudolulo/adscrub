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
  - **NARROWED 2026-07-14 (0.6.0), on evidence — read this before "fixing" `repeats.py`.**
    The above rejects a *global, crowdsourced* ad database (SponsorBlock-for-podcasts), and
    it still does: strangers' ads are not our ads, so there is nothing to share.
    It was never an argument against matching our corpus against **itself**. We download
    each episode once, server-side, from an ad server rotating a small pool of campaigns —
    so the same reads recur near-verbatim across episodes fetched in the same period. The
    thing there was supposedly "nothing to fingerprint against" was sitting in our own
    `ad_segments` table.
    Measured leave-one-out on the live corpus: **93.5% of confirmed ad segments are
    recoverable from ad reads confirmed in other episodes** (5-word shingles, 0.4 overlap);
    51 of 80 episodes are ≥95% covered with no model call. And it is not merely cheaper —
    it is *more accurate*: the LLM was silently missing ads (62% of episodes had at least
    one provably-missed ad still in the audio), and the repeat tier finds them.
    So `repeats.py` is the cheap tier **in front of** the model, never a replacement — a
    novel campaign still needs the model to read the words; a campaign it has already read
    does not need it to read them twice. This is what "detection is layered, cheapest-first"
    always asked for.
  - **BUILT & MEASURED 2026-07-23 — audio fingerprinting is no longer "un-built and unneeded".**
    An earlier version of this note ended "audio fingerprinting remains un-built and unneeded";
    that is superseded. `fingerprint.py` does for AUDIO exactly what `repeats.py` did for TEXT:
    match our own corpus against itself, against a library of CONFIRMED ad RECORDINGS only
    (`GROUND_TRUTH_SOURCES`). Chromaprint (`fpcalc`), leave-one-out on Casefile: **89.1% of
    confirmed ad duration recovered from audio with no transcript and no model**; 0/82 non-ad
    control false-match in the pilot. It runs BEFORE transcription (a sibling audio stage, not a
    transcript `AdSpanDetector`), so unlike `repeats` it saves the Whisper cost too. Cross-show
    on The Casual Criminalist confirmed the mechanism generalises (recurring audio there is real
    ads — Pepsi/Nordstrom/Netflix) and that a library from one show catches another show's ads
    when they share a DAI campaign (Flexcar, 13/40 eps). `fpmatch` spans are inference — never
    seed the library, same rule as `repeat`. Precision follow-up before unsupervised cutting:
    residual false positives are short (~10s) fragments + occasional shared music beds.
  - **`repeat` spans are inference, not evidence — never feed them back into the library**
    (`repeats.GROUND_TRUTH_SOURCES`). Doing so makes the detector bootstrap off its own
    guesses: caught on real data, a second sweep went 958 → 993 spans as each pass's
    inferences became the next pass's evidence and the idea of "what an ad sounds like"
    drifted outward. Evidence in, inference out.
- **GPU is real, just not wired into this interactive shell.** `code` (a LAN host)
  physically has an RTX 2070 SUPER (`lspci`: NVIDIA TU104), and Docker on this host has
  the `nvidia` runtime registered plus a CDI spec for `/dev/nvidia0` (confirmed
  2026-07-10) — so a container run with GPU passthrough requested (`--gpus`/CDI device)
  gets real CUDA. The interactive dev LXC this Claude session runs in just doesn't have
  the device nodes passed to *it*, which is a different, narrower fact than "no GPU on
  this host" (an earlier version of this note wrongly conflated the two — don't repeat
  that mistake). Transcription code must not hard-require CUDA either way: detect it at
  runtime (`ctranslate2.get_cuda_device_count()`, no torch needed) and fall back to CPU
  int8 cleanly, since the CLI itself may still run outside GPU passthrough (e.g. ad hoc
  from a dev shell) even when the Docker deploy target has it.
- Integration output is a regenerated RSS feed, same pattern as hark: the player
  subscribes to it like any podcast, zero app-side changes.

## Relationship to hark

Resolved 2026-07-11 (PLAN.md's M5): stays a separate repo, separate CHANGELOG/SemVer,
separate test suite. `flan/hark` depends on this one as a library (`uv` path
dependency, editable) — hark's `episodes`/`shows`/`ad_segments` schema was shaped to
match this project's own, so this package's schema-coupled functions
(`pending_episodes`, `transcribe_episode`, `detect_pending`, `cut_pending`, ...) work
unchanged when called with a `conn` from hark's database. hark's CLI
(`chapters`/`transcribe`/`detect-ads`/`cut`) is a thin wrapper calling straight into
this package — no code here is duplicated into hark.

Don't build features assuming this will get absorbed into hark's own source later —
that already got tried once (a full copy-merge into `src/hark/`, wrong shape, reverted
same day) and isn't the direction. Keep this repo's own CLI/pipeline/tests
self-sufficient and runnable standalone, same as before.

## Conventions

- Python 3.12+, `uv` + `pyproject.toml`, src layout. SQLite for storage. Keep
  dependencies minimal — GPU runtime libs are an optional `gpu` extra rather than a
  base dependency (see M2), and there's no torch dependency anywhere (CUDA detection
  goes through `ctranslate2.get_cuda_device_count()` instead).
- CHANGELOG.md in Keep a Changelog format; SemVer.
- **No AI/Claude attribution in commit messages** (no Co-Authored-By). Disclose AI use
  in the README instead. Commit messages describe actual changes, concise; never
  reference prompts or instructions.
- Significant multi-commit features go on a feature branch; small increments are fine
  directly on main.
- Remote: **Gitea `flan/adscrub` is canonical** (origin, SSH) — always push there. The repo
  is public, and `claude-fleet`'s `jobs/repo-mirror.sh` mirrors it out to
  `github.com/sudolulo/adscrub` for visibility. GitHub is a read-only shop window: never
  push to it directly, and never treat it as a source of truth. Policy lives in
  `claude-fleet/config/repos.toml`. Do not add other remotes or mirrors unprompted.
- Public-facing docs must not link to `git.onetick.ninja` — outsiders cannot reach it.
  Cross-reference sibling projects by their GitHub URL.
