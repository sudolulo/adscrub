# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.12.0] - 2026-07-23

### Fixed

- **Speech corroboration removes the last residual false-positive class** (`drop_speechless_spans`,
  applied by `fingerprint_episode` when the episode has a transcript). A region that aligns to a
  confirmed recording but carries no words is a music bed, sting, or room tone that happened to
  recur — the one error an audio-only tier cannot see, because it never reads. This is what the
  14s no-speech region in the end-to-end cut was, and the class the cross-show music-bed match
  belonged to.
  - Measured on Casefile: removes **12 regions totalling 177s at 0.00% recall cost** (89.6%
    before and after). It costs nothing because ads talk — the only regions it takes are the
    ones with nothing to say.
  - Skipped when there is no transcript, so the tier stays usable *before* transcription, which
    is its entire reason to exist. Corroboration is free where it's available and never required.
  - Note this succeeds where the min-density guard failed: density was a proxy for "is this a
    real match" and had no knee, while speech is a direct test of "is this an ad".


## [0.11.0] - 2026-07-23

### Fixed

- **Campaign discovery consulted the filesystem before the cache**, so an already-indexed
  corpus still demanded the audio the index exists to replace — a feed streamed-and-discarded
  looked empty. `find_campaigns` and `discover_recurring` now go through the new
  `cached_fingerprint`, which reads `episode_fingerprints` first and only falls back to local
  audio when an episode has never been indexed. Verified end to end on 40 real episodes with
  **zero audio files on disk**.


## [0.10.0] - 2026-07-23

### Added

- **`stream_fingerprint` / `stream_episode_fingerprint` — index an episode without storing it.**
  fpcalc accepts the audio on stdin and produces a **byte-identical** fingerprint to the
  file-based path, so an episode can be fingerprinted straight off the network and thrown away.
  Measured: a fingerprint is **~184x smaller** than the audio it describes (6.3 GB of episodes
  -> 34 MB of fingerprints), which is the difference between a ~2 TB corpus and one that fits
  in well under a gigabyte.
  - **What a stream loses is DURATION** — fpcalc cannot seek, so it reports 0. Duration is
    derived from the frame count via `NOMINAL_SECONDS_PER_FRAME` (0.123882, the median over 122
    real episodes, 0.1% spread). Measured error on real episodes: **~0.02%** (1.1s on a 6,393s
    episode). Fine for the index this feeds — matching and campaign discovery ask *whether*
    audio recurs, not exactly where. Cutting still uses the real file and ffprobe, because it
    needs the audio anyway and 0.1% is ~4s on a two-hour episode.
  - Feed-declared `duration_seconds` is **not** a usable substitute: on this corpus episode 1
    declares 5,896s against 6,393s of actual audio. That 8-minute gap is DAI — every listener
    gets a differently-sized file — so the feed's number describes nobody's copy.
  - The same byte cap `download_audio` applies is enforced while streaming: a hostile or
    malformed feed must not stream unboundedly even when nothing is being kept.


## [0.9.0] - 2026-07-23

### Added

- **Campaign-level selection: `find_campaigns` / `select_seed_episodes`.** `discover_recurring`
  answers "where does this episode repeat itself?", per episode — the wrong unit for spending a
  model budget, because twelve episodes carrying one sponsor read look like twelve findings when
  reading any ONE teaches the library all twelve. Recurring regions are now linked across
  episodes by the alignment that matched them and merged with a union-find, so each connected
  component is one ad RECORDING. A component is `known` when any member overlaps a ground-truth
  span — a campaign read once is in the library wherever else it appears.
  - `select_seed_episodes` is greedy set cover over the UNKNOWN campaigns: the fewest episodes
    that confirm every recording the library doesn't have. An episode carrying three unread
    campaigns retires all three in one read, which is why it's cover and not a ranking. This
    supersedes `repeats.prioritize_pending` wherever audio exists — that ranks by how far an
    episode's ad COUNT sits from its show's median, and its own docstring concedes a matching
    count means matching quantity, not content.
  - **A cluster spanning more than `STOP_EPISODE_FRACTION` of the feed is dropped** as the
    show's own recurring content. Needed twice over: union-find chains regions transitively, so
    one shared music bed can fuse unrelated regions into a component spanning every episode.
    Measured on 40 Casual Criminalist episodes — without the ceiling the largest "campaign" had
    a reach of 40/40 and dragged silent and music-only regions into the selection.
  - Measured on those same 40 episodes: **11 campaigns, 7 episodes to read** (Pepsi, SimpliSafe,
    Flexcar, Quicksilver, Hershey's, and Shopify as three distinct recordings — that feed
    re-records it, so each take is its own campaign).
  - **Known blind spot:** a single recording running in more than `STOP_EPISODE_FRACTION` of a
    feed is invisible to self-recurrence. Real campaigns usually fragment into creative variants
    that each stay a minority, which is why this is survivable rather than fatal.


## [0.8.0] - 2026-07-23

### Changed (breaking for AdSpanDetector implementors)

- **`AdSpanDetector.detect` takes a second argument**, `skip: frozenset[int] = frozenset()`.
  `LayeredDetector` passes it positionally, so any detector defined outside this package must
  accept it — a two-argument `detect(self, transcript)` now raises `TypeError` at run time.
  Caught by hark's own `_PrecomputedDetector`, which implements this protocol; downstream
  consumers pinned to adscrub main must update in the same step as the upgrade. Implementors
  with nothing to gain from it (anything that isn't billed per token) should accept and ignore it.

### Added

- **`fingerprint` tier — acoustic ad recognition** (`fingerprint.py`, `adscrub
  fingerprint`). Matches an episode's AUDIO against Chromaprint fingerprints of ad
  recordings already confirmed elsewhere in the corpus (`llm`/`chapter` spans), so a
  campaign confirmed once is cut with no transcript and no model — the cost lever
  `repeats` structurally can't pull, since it needs the Whisper transcript first. Runs
  before transcription: a sibling audio stage, not a transcript `AdSpanDetector`.
  Whole-episode and ad-region fingerprints are both cached (`episode_fingerprints`,
  `ad_fingerprints`), so re-scanning a grown library re-runs only cheap set-lookups, never
  the decode. `fpmatch` spans are inference and never seed the library
  (`GROUND_TRUTH_SOURCES` unchanged) — same discipline as `repeats`. Requires `fpcalc`
  (Chromaprint); a clear error, not a crash, if it's absent.
  - Measured leave-one-out on the live corpus (Casefile, 82 eps, 286 confirmed ads):
    **89.1% of confirmed ad DURATION recovered from audio alone**, 0 episodes fully
    missed (pilot slice-recovery 90.5% / 98.3% by duration, 0/82 non-ad control
    false-match). The ~30% of detected time outside the LLM's own spans is mostly ads the
    LLM silently missed, the same under-detection `repeats` was built to catch.
  - Cross-show (The Casual Criminalist, 40 eps, **no ad labels**): the mechanism
    generalises — 40/40 episodes carry recurring ad audio, verified as real ads
    (Pepsi/Nordstrom/Netflix/Flexcar), ~75%+ of flagged time explicitly ad-marked. 13/40
    episodes share the Flexcar DAI campaign with Casefile, caught cross-show from
    Casefile's library with zero CC labels — a library from one show recognises another
    show's ads when they share a programmatic campaign.
  - **Stop-list: frequency proposes, editorial vetoes.** A value common across episodes is
    only dropped if it ALSO appears in known non-ad audio, which is what distinguishes
    silence/a music bed from a sponsor that simply runs in every episode. Measured on
    Casefile: frequency alone 88.9% recall (but deletes any campaign in >30% of episodes —
    Flexcar runs in 27/40 Casual Criminalist episodes); editorial alone 76.8% (far too
    aggressive, 74,075 values stopped vs 1,173, because Chromaprint values collide between
    ad and ordinary speech); **the intersection 89.6%, 669 stopped** — better recall than
    either AND the ubiquitous sponsor survives.
  - Emit floor (`MIN_REGION_FRAMES`, ~10s) drops the short music/filler fragments that were
    the tier's main false positives. Swept against ground truth: costs 0.2pp of recall
    (89.8% -> 89.6%) for ~12% less false-positive time; past ~15s real short ads start dying.
  - `fpmatch` spans ARE cut (see `cut.CUT_SOURCES` below); hark wrappers are not built yet.
    Residual false positives are short fragments and the occasional shared music bed; the
    emit floor handles the former, the latter is unsolved.

- **The model is now only sent transcript it hasn't already had explained to it.**
  `detect_episode` computes `covered_segment_indices` (segments inside ANY tier's ad span) and
  `_chunks` omits them, so an episode already 60% covered costs ~40% of the tokens it used to.
  Omissions are replaced by an elision marker — without it the segments either side of a removed
  ad read as adjacent and the model sees a seam that isn't there. Indices stay GLOBAL, so spans
  still ground against the full transcript. `AdSpanDetector.detect` gained an optional `skip`
  argument; free tiers (repeats) deliberately ignore it, since re-examining costs nothing and can
  widen a span a cheaper tier only partly caught.

- **Cold start: `fingerprint.discover_recurring` / `adscrub discover`.** The seeded tier can only
  recognise campaigns something else already confirmed, which is useless on a new feed. This
  matches a feed against ITSELF: audio recurring across otherwise-unrelated episodes is the
  inserted material, not the content. Needs no library, no transcript and no model.
  - The frequency stop-list is used ALONE here and that is correct — its usual weakness (a
    sponsor in most episodes gets dropped) is the strength needed: the show's fixed intro/outro
    recurs everywhere and must be ignored, a rotating campaign is a minority and survives. No
    editorial veto is available, since deriving one needs the confirmed ads we don't have.
  - `RECUR_MIN_EPISODES = 8`, and the floor is arithmetic: a campaign needs 2+ episodes to recur
    but must stay under the ~30% frequency threshold, so discovery is impossible below ~7.
  - Verified on The Casual Criminalist (40 episodes, zero labels): recurring audio in 40/40
    episodes, 174 regions, ~211s/episode, reading as real ads (Pepsi, Mint Mobile, Mark Spain,
    Taco Bell). It recovered that feed's DAI inserts and NOT its host-read Shopify spot — which
    is the expected split, and matches the probe below.
  - Spans are `recur`: INFERENCE, absent from both `GROUND_TRUTH_SOURCES` and
    `FP_LIBRARY_SOURCES`, and NOT in `cut.CUT_SOURCES` — roughly 1 flagged region in 10 is not
    an ad, so they are not cut unless asked for explicitly (`adscrub cut --sources ...`).

- **Measured: being host-read is not what defeats fingerprinting — being re-*read* is.** A
  host-read spot recorded once and re-rolled across a flight is one recording and matches like
  any other insert. The Casual Criminalist re-records its Shopify read per episode: it appears in
  33 of 40 episodes and matched 0/39, while a DAI insert from the same episode matched 2/39 and a
  control editorial region matched 0/39. So the gap is per-show and worth measuring per feed
  rather than assuming; `repeats` covers it, since the WORDS recur even when the recording doesn't.

- **DAI probe results are now persisted** (`dai.dai_episode`, `adscrub dai`). The probe already
  proved which bytes were server-inserted, and then threw the finding away — nothing was ever
  written to `ad_segments`. Divergences are now stored as `dai` spans (byte offsets converted
  through the file's average byte rate, trimmed at both ends, capped at one plausible ad break,
  `confidence = 0.5`), giving ad discovery with **no transcript and no model**.
  - The START is evidence; the END is only an upper bound, because the reconvergence anchor
    locates where the two streams realign rather than where the insert stopped. So a `dai` span
    seeds the AUDIO library (`FP_LIBRARY_SOURCES`) — where boundary slop is harmless, since
    matching needs a long aligned run and any editorial bleed is what the stop-list removes —
    but never the TEXT library (`repeats.GROUND_TRUTH_SOURCES` stays `llm`/`chapter`), where a
    wrong boundary would teach the matcher editorial wording.
  - A span is stored only when the probe both diverged AND realigned; without an end, storing
    one would be a guess.

### Fixed

- **`fpcalc` is now installed in the image.** The Dockerfile installed `ffmpeg` but not
  `libchromaprint-tools`, so in any deploy the `fingerprint`/`discover` tiers were not broken but
  INERT: `fpcalc_available()` returned False, the command exited with a tidy message, and a
  healthy-looking image silently never matched an ad.

- **Cut edges are pulled inward onto silence (`snap_spans_to_silence`).** A cut edge is a guess
  about where a break ends, and the failure that matters is running into speech. On a real
  Casefile cut this tightened 3 of 5 edges by up to ~0.9s, and by construction it can only ever
  shrink what gets removed.
  - Direction is the point. Snapping to the NEAREST silence was tried first and measured worse:
    the closest silence to an edge is often a pause *inside* speech. Starts may only move later
    and ends only earlier, so every error leaves a sliver of ad rather than deleting a sentence.
  - **Correction to the previous entry for this change, which claimed the cut "ate the opening
    of 'It was 405 on the morning of Thursday, June 19, 2014'". That was a misdiagnosis.** Those
    frames match a confirmed ad recording from ANOTHER episode densely (gaps of 1-3 frames), and
    an episode's narration is unique to it and cannot match another episode's audio — so the
    region is the ad's outro bed, and Whisper had simply timestamped the segment early. Whisper
    segment starts are not evidence of speech onset. No sentence was being deleted.
  - **A match-density guard was measured and rejected.** Sparse, heavily-bridged regions looked
    like the signature of a false positive, but filtering on density has no knee: min-density
    0.35 costs 1.0pp recall to remove 220s of outside-LLM time, 0.55 costs 2.0pp, and 0.75
    collapses recall to 60.2%. "Outside the LLM's spans" is not a synonym for "false positive"
    either — much of it is ads the LLM missed — so shrinking it is not purely a win. Left out.

- **`cut` no longer removes audio on the strength of any span it can find.** It selected every
  `ad_segments` row regardless of source, so the new discovery tiers would have silently started
  deleting audio: `dai` spans whose END is only an upper bound (over-cutting into editorial
  either side of the real insert), and `recur` spans of which roughly 1 in 10 is not an ad.
  Cutting is now limited to `CUT_SOURCES` — `chapter`, `llm`, `repeat`, `fpmatch`.
  - The line is NOT evidence-vs-inference: `repeat` and `fpmatch` are inference and are exactly
    what the cheap tiers exist to cut. It is whether a tier pins the span's EDGES — those four
    ground their boundaries in publisher markers, transcript segment timestamps, or audio
    alignment. `dai` and `recur` find ads well without saying where they stop, which makes them
    good seeds and bad scissors.
  - `cut.pending_episodes` filters by the same list, so an episode carrying only discovery spans
    isn't treated as pending — that would rewrite the file unchanged and mark it cut, retiring it
    before any real span arrived.
  - Override deliberately with `adscrub cut --sources ...`.

- **`download_audio` now caps episode size** (default 1 GiB, override with
  `ADSCRUB_MAX_AUDIO_MB`). A hostile or malformed feed could previously stream
  an unbounded body and fill the data volume. An oversized declared
  `Content-Length` is rejected up front, and the running byte total aborts the
  stream mid-download; the partial `.part` file is cleaned up on abort.

## [0.7.2] - 2026-07-14

### Changed

- **`dai.USER_AGENTS` default swapped from browser UAs to real podcast-app
  signatures** (Apple Podcasts, Spotify, Overcast, AntennaPod). Measured on
  real data: browser UAs (Chrome/Safari desktop+mobile) reported no
  divergence on megaphone.fm; probing the same episode as Apple Podcasts vs.
  Spotify found a clean divergence-and-reconvergence pair. An ad server has
  no reason to personalize traffic that doesn't look like a real podcast
  client — a browser directly requesting an MP3 isn't that.

## [0.7.1] - 2026-07-14

### Fixed

- **`dai.probe_variance()` now uses an independent client per fetch, not a
  shared one.** `httpx.Client` keeps a cookie jar by default, and that
  silently defeated the whole comparison: the first fetch's response sets a
  listener-tracking cookie, the second fetch (same client) auto-replays it,
  and the ad server sees the same "listener" both times regardless of the
  User-Agent difference. Caught on real data: a shared-client run reported
  acast.com as "same" on an episode a raw two-`curl` test (no shared cookie
  jar) had already shown genuine divergence on. `probe_variance()` now takes
  a `client_factory` and builds a fresh client per fetch — no shared jar, no
  shared connection, so each fetch actually looks like a different session.

## [0.7.0] - 2026-07-14

### Added

- **`dai.probe_variance()`: a third, even cheaper ad-detection primitive.**
  Fetches an episode's `audio_url` twice with different User-Agents (no
  cookies — an absent one is itself a common "distinct listener" trigger) and
  byte-compares the results. If a platform's dynamic ad insertion actually
  varies by these signals, the divergence point is a provable ad-relevant
  boundary with zero transcription and zero classification. Confirmed live
  against an Acast-hosted show: two same-signature fetches came back cached
  and identical, but varying User-Agent produced a genuinely different
  stitched file, diverging at ~8.9s in — byte-identical before that point.
  Also finds where the streams reconverge, by searching for a content anchor
  (not a byte position) from well past the divergence point — necessary
  because two different-length ad reads leave the following editorial audio
  at different absolute offsets in each stream even when its bytes are
  identical. A clean "no divergence" result is not proof a platform has no
  DAI, only that these signals didn't trigger a different render within the
  fetched window.

## [0.6.1] - 2026-07-14

### Added

- **`repeats.prioritize_pending()`: order the LLM-detection queue by count mismatch,
  not by episode id.** For each pending episode, compares how many ad breaks the
  repeat tier finds against the show's typical count (median across its own
  `llm_detected_at`-confirmed episodes); episodes where the count doesn't match are
  ranked first. Explicitly **not a skip gate** — leave-one-out validation (Casefile
  True Crime, the only show with enough ground truth to test against; see the
  single-show caveat already on `repeats.py`) found exact-count matches still had real
  recall gaps as low as 66.7%, so a match is only trusted to mean "process later,"
  never "skip." Shows with fewer than `min_show_history` confirmed episodes have no
  reliable typical count and keep their original order, appended after every episode
  that does have a signal. `group_column` defaults to this project's own `feed_id`;
  a caller on a renamed schema (hark: `show_id`) passes the real name.

## [0.6.0] - 2026-07-14

### Added

- **A repeat-ad tier (`adscrub repeats`, `repeats.py`) — the same ad read, recognised for free
  the second time.** Ads arrive in batches: the ad server rotates a small pool of campaigns, and
  we download each episode once, server-side, from that pool. So the same reads recur
  near-verbatim across the episodes we fetched in the same period. Measured leave-one-out over
  the live corpus (82 episodes, 286 confirmed spans): **93.5% of confirmed ad segments are
  recoverable from ad reads confirmed in *other* episodes**, and 51 of 80 episodes are ≥95%
  covered — with no model called at all.
  Matching is on 5-word shingles at a 0.4 overlap threshold, not whole-segment equality: Whisper
  segments the *same* ad read differently between episodes (different surrounding audio → different
  boundaries), and exact matching scored 70% where shingles score 93.5%.
  **This is not the fingerprinting CLAUDE.md rejects, and that ruling stands.** What was rejected
  was a *global, crowdsourced* ad-timestamp database, on the grounds that dynamically-inserted and
  host-read ads are unique per listener so there is nothing stable for strangers to match against.
  True then, true now. But it was never true of our *own* corpus — the thing there was "nothing to
  fingerprint against" has been sitting in our own `ad_segments` table the whole time. This is the
  cheap tier in front of the model, which is what "detection is layered, cheapest-first" already
  asked for.
- **`LayeredDetector`** — composes tiers (chapters / repeats / LLM) into one `AdSpanDetector`, so
  every existing call site takes the layering with nothing else changing. Spans keep their own
  `source`, overlaps are allowed, and `cut.py` merges them at cut time. No tier knows about any
  other tier; no caller branches. `adscrub detect` now puts the repeat tier in front of the model
  automatically — if the library is empty it degrades to exactly the old behaviour, with no special
  case.

### Fixed

- **`ClaudeAdDetector` was truncating every transcript to its first 20,000 characters — about 28%
  of an episode — and then marking the episode detected.** The line was `body[:20000]`, with the
  note "a bloated transcript shouldn't dominate the token bill". The cost instinct was right; the
  implementation threw the episode away. A rendered transcript runs ~88,000 characters, so the
  model saw segments 0–235 of 840: **every mid-roll and every end-tag ad sat past the cliff,
  unseen**, and `llm_detected_at` was then set, so the episode never came back and those ads stayed
  in the audio permanently. A truncation that also marks the work complete is worse than no
  detection at all — it launders a 28% look as a finished one. Now chunked: same per-call ceiling,
  whole episode covered, indices still global so spans are grounded exactly as before. Regression
  test asserts the last segment of a 1,500-segment transcript actually reaches the model.
- **Detection recall was worse than anyone could see.** Of the segments the repeat tier flags that
  the LLM did *not*, 31% carry an unambiguous brand/CTA marker ("betterhelp.com slash casefile",
  "download the free app") — against 29% of the segments the LLM *did* flag. Identical density:
  these are the same kind of content, not false positives. 62% of episodes had at least one
  provably-missed ad still in the audio. Running the repeat tier over episodes already marked
  `llm_detected_at` is therefore free recall, not redundant work.

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
