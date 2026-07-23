"""Tests for the acoustic-fingerprint tier.

The matching logic is tested on synthetic fingerprint value-lists (a Chromaprint frame is
just a 32-bit int, so the algorithm is exercised without decoding any audio). The DB plumbing
— caching, idempotency, ground-truth-only, tier isolation — is tested with `_fingerprint_region`
/ `_fpcalc` monkeypatched to deterministic fingerprints, the same way the transcribe/cut tests
mock their heavy audio calls. End-to-end confidence on real audio comes from the corpus eval
(the 90.5% leave-one-out pilot), not from synthesising audio here.
"""

from collections import defaultdict

import pytest

from adscrub import db, fingerprint, repeats

# A synthetic "ad recording": 60 distinct frame values (>= MATCH_FRAMES so one aligned run
# clears the threshold). Padding values (1, 2) never appear in any library.
AD = list(range(1000, 1060))
PAD = [1] * 20
# Values that occur in known non-ad audio (the show's beds, room tone, host chatter).
EDITORIAL = list(range(5000, 5100))


def _lib(index_pairs, ad_episode, stop=()):
    """Build a Library from {value: [(ad_id, frame), ...]} directly."""
    index = defaultdict(list)
    for value, entries in index_pairs.items():
        index[value].extend(entries)
    return fingerprint.Library(
        index=dict(index),
        stop=set(stop),
        ad_episode=dict(ad_episode),
        n_episodes=len(set(ad_episode.values())),
    )


def _one_ad_library(ad_id=1, episode_id=10):
    return _lib({v: [(ad_id, k)] for k, v in enumerate(AD)}, {ad_id: episode_id})


# --- _group_runs ---


def test_group_runs_bridges_small_gaps():
    assert fingerprint._group_runs([0, 1, 2, 5, 6, 7], bridge=2, min_len=1) == [(0, 7)]


def test_group_runs_splits_large_gaps():
    assert fingerprint._group_runs([0, 1, 2, 5, 6, 7], bridge=1, min_len=1) == [(0, 2), (5, 7)]


def test_group_runs_drops_runs_below_min_len():
    assert fingerprint._group_runs([0, 1], bridge=0, min_len=5) == []
    assert fingerprint._group_runs([], bridge=0, min_len=1) == []


# --- match_regions ---


def test_matches_an_aligned_recording():
    query = PAD + AD + PAD  # the ad sits in the middle, editorial-noise either side
    runs = fingerprint.match_regions(query, _one_ad_library(), min_region_frames=len(AD))
    assert runs == [(20, 79)]  # exactly the ad frames, padding untouched


def test_emit_floor_drops_short_fragments():
    """The tier's false positives on a second show were almost all short (~5-10s) music/filler
    fragments. The emit floor drops a run shorter than MIN_REGION_FRAMES even though it is a
    genuine alignment, which is the point — a real ad is >=15s, well clear of the floor."""
    query = PAD + AD + PAD  # AD is a 60-frame (~7.4s) run: a real alignment, but short
    assert fingerprint.match_regions(query, _one_ad_library(), min_region_frames=len(AD)) == [(20, 79)]
    assert fingerprint.match_regions(query, _one_ad_library(), min_region_frames=len(AD) + 1) == []
    # and the shipped default is above this fragment's length, so it is dropped by default
    assert fingerprint.MIN_REGION_FRAMES > len(AD)
    assert fingerprint.match_regions(query, _one_ad_library()) == []


def test_scattered_collisions_do_not_match():
    """A few coincidental value hits that do NOT line up on one diagonal must not add up."""
    lib = _one_ad_library()
    # only 5 of the ad's values appear, spread far apart -> no diagonal reaches MATCH_FRAMES
    query = [AD[0], 500, AD[10], 500, AD[20], 500, AD[30], 500, AD[40]]
    assert fingerprint.match_regions(query, lib) == []


def test_an_episode_is_excluded_from_its_own_corpus():
    lib = _one_ad_library(episode_id=10)
    query = PAD + AD + PAD
    # floor passed explicitly: without it the default emit floor alone would drop this run and
    # the assertion would pass whether or not exclusion actually works
    assert fingerprint.match_regions(query, lib, exclude_episode_id=10,
                                     min_region_frames=len(AD)) == []
    assert fingerprint.match_regions(query, lib, min_region_frames=len(AD)) == [(20, 79)]


def test_stop_listed_values_cannot_match():
    lib = fingerprint.Library(
        index={v: [(1, k)] for k, v in enumerate(AD)},
        stop=set(AD),  # every ad value is stop-listed (as silence/a common bed would be)
        ad_episode={1: 10},
    )
    # floor passed explicitly so the stop-list is what makes this empty, not the emit floor
    assert fingerprint.match_regions(PAD + AD + PAD, lib, min_region_frames=len(AD)) == []


def test_empty_query_or_library_is_no_match():
    assert fingerprint.match_regions([], _one_ad_library()) == []
    assert fingerprint.match_regions(PAD + AD, _lib({}, {})) == []


def test_two_campaigns_in_one_episode_are_separate_spans():
    """Different ads align on different diagonals -> two regions, not one merged blob."""
    ad2 = list(range(2000, 2060))
    index = {v: [(1, k)] for k, v in enumerate(AD)}
    index.update({v: [(2, k)] for k, v in enumerate(ad2)})
    lib = _lib(index, {1: 10, 2: 11})
    query = AD + [1] * 200 + ad2  # 200-frame editorial gap, well past BRIDGE_FRAMES
    runs = fingerprint.match_regions(query, lib, min_region_frames=len(AD))
    assert len(runs) == 2
    assert runs[0][0] == 0 and runs[1][1] == len(query) - 1


# --- DB integration (fingerprints monkeypatched) ---


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "audio").mkdir(parents=True)
    return d


def _seed(conn, data_dir, guid, ad_spans=()):
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, audio_url) VALUES (1, ?, ?, ?)",
        (guid, guid, f"http://feed/{guid}.mp3"),
    )
    eid = conn.execute("SELECT id FROM episodes WHERE guid = ?", (guid,)).fetchone()["id"]
    (data_dir / "audio" / f"{eid}.mp3").write_bytes(b"stub")  # exists() -> no download
    for start, end in ad_spans:
        conn.execute(
            "INSERT INTO ad_segments (episode_id, start_second, end_second, source) "
            "VALUES (?, ?, ?, 'llm')",
            (eid, start, end),
        )
    conn.commit()
    return eid


@pytest.fixture
def corpus(conn, data_dir, monkeypatch):
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    a = _seed(conn, data_dir, "ep-a", ad_spans=[(2.0, 10.0)])  # ad confirmed here
    b = _seed(conn, data_dir, "ep-b")                          # same ad, unlabelled
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    monkeypatch.setattr(fingerprint, "_fingerprint_region", lambda p, s, e: AD)
    monkeypatch.setattr(fingerprint, "_fpcalc", lambda p, length=fingerprint.FP_LENGTH: PAD + AD + PAD)
    monkeypatch.setattr(fingerprint, "probe_duration", lambda p: len(PAD + AD + PAD) * 0.1238)
    # One source episode means every value is in 100% of episodes; the frequency stop-list
    # would nuke it. That guard is about corpus DIVERSITY (a real ad is in a MINORITY of a
    # rotating pool) and is exercised by test_stop_list_drops_ubiquitous_values below; here
    # we disable it to test the plumbing in isolation.
    monkeypatch.setattr(fingerprint, "STOP_EPISODE_FRACTION", 2.0)
    # Same isolation reason: the synthetic ad is a short fragment by the shipped emit floor's
    # standards. The floor is exercised by test_emit_floor_drops_short_fragments; here we want
    # the plumbing, so resolve it low. This works because the detector resolves the floor at
    # call time rather than baking it into __init__.
    monkeypatch.setattr(fingerprint, "MIN_REGION_FRAMES", fingerprint.MATCH_FRAMES)
    return conn, data_dir, a, b


def test_apply_fingerprints_finds_the_unlabelled_copy(corpus):
    conn, data_dir, a, b = corpus
    results = fingerprint.apply_fingerprints(conn, client=None, data_dir=data_dir)
    found = {r.episode_id: r.found for r in results}
    assert found[b] == 1        # ep-b's copy of the ad, recovered from audio alone
    assert found[a] == 0        # ep-a is excluded from its own corpus (leave-one-out)
    row = conn.execute(
        "SELECT source FROM ad_segments WHERE episode_id = ? AND source = 'fpmatch'", (b,)
    ).fetchone()
    assert row is not None


def test_apply_fingerprints_is_idempotent(corpus):
    conn, data_dir, a, b = corpus
    for _ in range(3):
        fingerprint.apply_fingerprints(conn, client=None, data_dir=data_dir)
    n = conn.execute(
        "SELECT COUNT(*) c FROM ad_segments WHERE episode_id = ? AND source = 'fpmatch'", (b,)
    ).fetchone()["c"]
    assert n == 1


def test_fpmatch_never_becomes_library_evidence(corpus):
    """A fpmatch span is inference; if it seeded the library the detector would bootstrap off
    its own guesses, the drift repeats.py was bitten by. GROUND_TRUTH_SOURCES excludes it."""
    conn, data_dir, a, b = corpus
    fingerprint.apply_fingerprints(conn, client=None, data_dir=data_dir)
    assert conn.execute("SELECT COUNT(*) c FROM ad_segments WHERE source='fpmatch'").fetchone()["c"] > 0
    # only the one ground-truth ad is ever fingerprinted into the cache
    assert conn.execute("SELECT COUNT(*) c FROM ad_fingerprints").fetchone()["c"] == 1
    assert fingerprint.build_library(conn, data_dir).n_episodes == 1


def test_apply_fingerprints_never_touches_other_sources_or_marks_detected(corpus):
    conn, data_dir, a, b = corpus
    fingerprint.apply_fingerprints(conn, client=None, data_dir=data_dir)
    assert conn.execute(
        "SELECT COUNT(*) c FROM ad_segments WHERE episode_id = ? AND source = 'llm'", (a,)
    ).fetchone()["c"] == 1
    # a free pass that never read the words must not retire the episode from the LLM
    assert conn.execute(
        "SELECT llm_detected_at FROM episodes WHERE id = ?", (b,)
    ).fetchone()["llm_detected_at"] is None


def test_library_is_cached_not_recomputed(corpus, monkeypatch):
    conn, data_dir, a, b = corpus
    fingerprint.build_library(conn, data_dir)  # first build fingerprints ep-a's ad
    # a second build must not re-fingerprint anything already cached
    def boom(p, s, e):
        raise AssertionError("re-fingerprinted a cached ad segment")
    monkeypatch.setattr(fingerprint, "_fingerprint_region", boom)
    lib = fingerprint.build_library(conn, data_dir)
    assert lib.n_episodes == 1


def test_episode_fingerprint_is_cached_across_rescans(corpus, monkeypatch):
    """The whole-episode fpcalc is the tier's only real cost; a re-scan (library grew) must
    re-run only the matching, never the decode."""
    conn, data_dir, a, b = corpus
    fingerprint.apply_fingerprints(conn, client=None, data_dir=data_dir)
    assert conn.execute("SELECT COUNT(*) c FROM episode_fingerprints").fetchone()["c"] == 2

    def boom(p, length=fingerprint.FP_LENGTH):
        raise AssertionError("re-fingerprinted a cached episode")

    monkeypatch.setattr(fingerprint, "_fpcalc", boom)
    results = fingerprint.apply_fingerprints(conn, client=None, data_dir=data_dir)
    assert {r.episode_id: r.found for r in results}[b] == 1  # still detected, no re-decode


def test_empty_library_returns_no_results(conn, data_dir, monkeypatch):
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    _seed(conn, data_dir, "ep-a")  # no confirmed ads anywhere
    assert fingerprint.apply_fingerprints(conn, client=None, data_dir=data_dir) == []


def test_missing_audio_costs_recall_not_correctness(conn, data_dir, monkeypatch):
    """A confirmed ad whose audio file is gone is skipped, not fatal."""
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    monkeypatch.setattr(fingerprint, "STOP_EPISODE_FRACTION", 2.0)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    a = _seed(conn, data_dir, "ep-a", ad_spans=[(2.0, 10.0)])
    (data_dir / "audio" / f"{a}.mp3").unlink()  # audio gone
    lib = fingerprint.build_library(conn, data_dir)
    assert lib.n_episodes == 0  # nothing fingerprinted, no crash
    assert conn.execute("SELECT COUNT(*) c FROM ad_fingerprints").fetchone()["c"] == 0


def test_editorial_windows_sample_around_the_ads():
    # ads at 0-60 and 600-700 of a 1200s episode -> gaps are 60-600 and 700-1200
    w = fingerprint.editorial_windows([(0.0, 60.0), (600.0, 700.0)], 1200.0, want=120, window=60)
    assert w == [(60.0, 120.0), (700.0, 760.0)]          # spread across gaps, not one lump
    assert all(not (s < 700 and e > 600) for s, e in w)  # never overlaps an ad


def test_ubiquitous_sponsor_survives_the_stoplist(conn, data_dir, monkeypatch):
    """The measured Flexcar case: a campaign running in EVERY source episode must stay matchable.

    The frequency heuristic deleted exactly this (>30% of episodes -> assumed silence/bed). The
    editorial-derived stop-list keeps it, because the ad never appears in editorial audio."""
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    # the confirmed ad starts at 2.0s; the sampled editorial window starts at 90s
    monkeypatch.setattr(fingerprint, "_fingerprint_region",
                        lambda p, s, e: AD if s < 50 else EDITORIAL)
    monkeypatch.setattr(fingerprint, "probe_duration", lambda p: 1200.0)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    for i in range(4):  # the same ad confirmed in 100% of source episodes
        _seed(conn, data_dir, f"src-{i}", ad_spans=[(2.0, 90.0)])
    lib = fingerprint.build_library(conn, data_dir)

    # frequency alone would drop AD (it is in 100% of source episodes); the editorial veto
    # rescues it, because AD never appears in non-ad audio
    assert lib.stop == set(), "a ubiquitous ad must survive: frequency proposes, editorial vetoes"
    assert not (set(AD) & lib.stop), "a ubiquitous ad must NOT be stop-listed"
    assert fingerprint.match_regions(PAD + AD + PAD, lib, min_region_frames=len(AD)) == [(20, 79)]


def test_audio_appearing_in_editorial_is_stop_listed(conn, data_dir, monkeypatch):
    """Degenerate case: if the SAME audio shows up in both the confirmed ad and the editorial
    sample, it cannot identify an ad and must be dropped — whatever its frequency."""
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    monkeypatch.setattr(fingerprint, "probe_duration", lambda p: 1200.0)
    monkeypatch.setattr(fingerprint, "_fingerprint_region", lambda p, s, e: AD)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    # 4 source episodes ALL carrying the same ad -> every value is in 100% of episodes
    for i in range(4):
        _seed(conn, data_dir, f"src-{i}", ad_spans=[(2.0, 10.0)])
    lib = fingerprint.build_library(conn, data_dir)
    assert lib.n_episodes == 4
    assert lib.stop == set(AD)  # all values ubiquitous -> all stop-listed
    # floor passed explicitly so the stop-list is what makes this empty, not the emit floor
    assert fingerprint.match_regions(PAD + AD + PAD, lib, min_region_frames=len(AD)) == []


# --- cold start: discover_recurring (no confirmed ads anywhere) ---


@pytest.fixture
def coldstart(conn, data_dir, monkeypatch):
    """A feed with NO confirmed ads: 8 episodes, an ad in 2 of them, plus a shared intro.

    8 rather than 4 because a campaign in 2 of 4 episodes is 50% of the corpus and the frequency
    stop-list would delete it — see RECUR_MIN_EPISODES. In 2 of 8 it is a 25% minority and lives."""
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    eids = [_seed(conn, data_dir, f"cold-{i}") for i in range(8)]
    INTRO = list(range(7000, 7100))          # in EVERY episode -> must be stop-listed
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    monkeypatch.setattr(fingerprint, "probe_duration", lambda p: 100.0)
    monkeypatch.setattr(fingerprint, "MIN_REGION_FRAMES", fingerprint.MATCH_FRAMES)

    def fake_fpcalc(path, length=fingerprint.FP_LENGTH):
        eid = int(str(path).rsplit("/", 1)[-1].split(".")[0])
        # every episode: intro, unique body, then the shared ad in only 2 of the 4
        body = list(range(900_000 + eid * 1000, 900_000 + eid * 1000 + 100))
        return INTRO + body + (AD if eid in (eids[0], eids[1]) else [])

    monkeypatch.setattr(fingerprint, "_fpcalc", fake_fpcalc)
    return conn, data_dir, eids, INTRO


def test_discover_finds_the_shared_ad_without_any_labels(coldstart):
    conn, data_dir, eids, _ = coldstart
    results = fingerprint.discover_recurring(conn, data_dir)
    found = {r.episode_id: r.found for r in results}
    assert found[eids[0]] == 1 and found[eids[1]] == 1   # the two carrying the shared ad
    assert all(found[e] == 0 for e in eids[2:])          # nothing recurring in the rest
    assert conn.execute(
        "SELECT COUNT(*) c FROM ad_segments WHERE source='recur'").fetchone()["c"] == 2


def test_discover_ignores_the_shows_own_intro(coldstart):
    """The intro recurs in every episode — that's the frequency stop-list's whole job here."""
    conn, data_dir, eids, INTRO = coldstart
    fingerprint.discover_recurring(conn, data_dir)
    row = conn.execute("SELECT start_second, end_second FROM ad_segments "
                       "WHERE episode_id=? AND source='recur'", (eids[0],)).fetchone()
    per_frame = 100.0 / (100 + 100 + len(AD))
    assert row["start_second"] > len(INTRO) * per_frame  # match sits past the intro, not on it


def test_discover_output_never_seeds_a_library(coldstart):
    """`recur` is inference. If it seeded the library the detector would bootstrap off guesses."""
    conn, data_dir, _, _ = coldstart
    fingerprint.discover_recurring(conn, data_dir)
    assert "recur" not in fingerprint.FP_LIBRARY_SOURCES
    assert "recur" not in repeats.GROUND_TRUTH_SOURCES
    assert fingerprint.build_library(conn, data_dir).n_episodes == 0


def test_discover_needs_enough_episodes(conn, data_dir, monkeypatch):
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    _seed(conn, data_dir, "only-one")
    assert fingerprint.discover_recurring(conn, data_dir) == []


def test_discover_is_idempotent(coldstart):
    conn, data_dir, eids, _ = coldstart
    for _ in range(3):
        fingerprint.discover_recurring(conn, data_dir)
    assert conn.execute("SELECT COUNT(*) c FROM ad_segments WHERE episode_id=? AND source='recur'",
                        (eids[0],)).fetchone()["c"] == 1


# --- campaign clustering + seed selection ---


@pytest.fixture
def campaigns_corpus(conn, data_dir, monkeypatch):
    """12 episodes. Campaign A in eps 0-2, B in eps 2-4 (ep2 carries BOTH), C in eps 5-6.
    Plus a shared intro in every episode (must be stop-listed).

    Reach is kept a MINORITY on purpose: a single recording appearing in more than
    STOP_EPISODE_FRACTION of a feed is dropped as a common bed — see
    test_a_recording_in_most_episodes_is_invisible below."""
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    eids = [_seed(conn, data_dir, f"c-{i}") for i in range(12)]
    INTRO = list(range(7000, 7100))
    A = list(range(10_000, 10_120))
    B = list(range(20_000, 20_120))
    C = list(range(30_000, 30_120))
    pos = {e: i for i, e in enumerate(eids)}

    def fake_fpcalc(path, length=fingerprint.FP_LENGTH):
        eid = int(str(path).rsplit("/", 1)[-1].split(".")[0])
        i = pos[eid]
        # per-episode filler separates the two campaigns ep2 carries, so they are distinct
        # runs rather than one contiguous block (ads in a break are separated by a beat)
        gap = list(range(950_000 + i * 1000, 950_000 + i * 1000 + 60))
        out = INTRO + list(range(900_000 + i * 1000, 900_000 + i * 1000 + 150))
        if i in (0, 1, 2): out = out + A
        if i in (2, 3, 4): out = out + gap + B
        if i in (5, 6): out = out + C
        return out

    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    monkeypatch.setattr(fingerprint, "_fpcalc", fake_fpcalc)
    monkeypatch.setattr(fingerprint, "probe_duration", lambda p: 100.0)
    return conn, data_dir, eids


def test_finds_one_campaign_per_recording_not_per_episode(campaigns_corpus):
    """7 episodes carrying 3 recordings must yield 3 campaigns, not 7 findings."""
    conn, data_dir, eids = campaigns_corpus
    camps = fingerprint.find_campaigns(conn, data_dir)
    assert len(camps) == 3
    assert sorted(c.reach for c in camps) == [2, 3, 3]


def test_campaign_records_every_episode_carrying_it(campaigns_corpus):
    conn, data_dir, eids = campaigns_corpus
    reaches = {tuple(c.episodes) for c in fingerprint.find_campaigns(conn, data_dir)}
    assert tuple(eids[0:3]) in reaches      # campaign A
    assert tuple(eids[2:5]) in reaches      # campaign B
    assert tuple(eids[5:7]) in reaches      # campaign C


def test_seed_selection_covers_every_campaign_with_fewest_episodes(campaigns_corpus):
    """ep2 carries two campaigns — a good selector reads it once and retires both, then needs
    only one more episode for C. Three campaigns, two model calls."""
    conn, data_dir, eids = campaigns_corpus
    picked = fingerprint.select_seed_episodes(conn, data_dir)
    assert len(picked) == 2, picked
    assert picked[0] == (eids[2], 2)        # highest-yield episode first
    assert picked[1][1] == 1


def test_a_recording_in_most_episodes_is_invisible(conn, data_dir, monkeypatch):
    """Honest limitation: self-recurrence cannot see a recording that runs in more than
    STOP_EPISODE_FRACTION of the feed — the frequency stop-list drops it as a bed. Real
    campaigns usually fragment into creative variants that each stay a minority, which is why
    this is survivable, but a single ad in every episode is a genuine blind spot."""
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    eids = [_seed(conn, data_dir, f"u-{i}") for i in range(10)]
    UBIQ = list(range(40_000, 40_120))
    pos = {e: i for i, e in enumerate(eids)}
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    monkeypatch.setattr(fingerprint, "probe_duration", lambda p: 100.0)
    monkeypatch.setattr(fingerprint, "_fpcalc", lambda path, length=fingerprint.FP_LENGTH: (
        list(range(900_000 + pos[int(str(path).rsplit("/", 1)[-1].split(".")[0])] * 1000,
                   900_000 + pos[int(str(path).rsplit("/", 1)[-1].split(".")[0])] * 1000 + 150))
        + UBIQ))
    assert fingerprint.find_campaigns(conn, data_dir) == []


def test_already_read_campaigns_are_not_selected_again(campaigns_corpus):
    """A campaign overlapping a ground-truth span is in the library already — reading another
    episode of it teaches nothing, so it must drop out of the selection."""
    conn, data_dir, eids = campaigns_corpus
    before = fingerprint.select_seed_episodes(conn, data_dir)
    assert len(before) == 2
    target = next(c for c in fingerprint.find_campaigns(conn, data_dir)
                  if tuple(c.episodes) == tuple(eids[5:7]))          # campaign C
    eid, start, end = target.representative
    conn.execute("INSERT INTO ad_segments (episode_id, start_second, end_second, source) "
                 "VALUES (?, ?, ?, 'llm')", (eid, start, end))
    conn.commit()
    assert any(c.known for c in fingerprint.find_campaigns(conn, data_dir))
    after = fingerprint.select_seed_episodes(conn, data_dir)
    assert len(after) == 1                                            # C no longer needs reading
    assert after[0][0] == eids[2]


def test_seed_selection_respects_limit(campaigns_corpus):
    conn, data_dir, eids = campaigns_corpus
    assert len(fingerprint.select_seed_episodes(conn, data_dir, limit=1)) == 1


def test_no_campaigns_below_the_episode_floor(conn, data_dir, monkeypatch):
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    monkeypatch.setattr(fingerprint, "probe_duration", lambda p: 100.0)
    monkeypatch.setattr(fingerprint, "_fpcalc", lambda p, length=fingerprint.FP_LENGTH: [1, 2, 3])
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    _seed(conn, data_dir, "lonely")
    assert fingerprint.find_campaigns(conn, data_dir) == []
    assert fingerprint.select_seed_episodes(conn, data_dir) == []


# --- streaming: fingerprint without storing the audio ---


class _Stream:
    """Minimal stand-in for an httpx client streaming bytes."""
    def __init__(self, payload, chunk=8): self.payload, self.chunk, self.read = payload, chunk, 0
    def stream(self, method, url):
        outer = self
        class Ctx:
            def __enter__(self):
                class R:
                    def raise_for_status(self): pass
                    def iter_bytes(self, n=None):
                        for i in range(0, len(outer.payload), outer.chunk):
                            outer.read += len(outer.payload[i:i + outer.chunk])
                            yield outer.payload[i:i + outer.chunk]
                return R()
            def __exit__(self, *a): return False
        return Ctx()


def test_streaming_stops_at_the_byte_cap(monkeypatch):
    """A hostile or malformed feed must not stream unboundedly even when nothing is kept."""
    seen = {}
    class P:
        def __init__(self, *a, **k):
            self.stdin, self.stdout = _Sink(seen), _Out(b"FINGERPRINT=1,2,3\n")
        def wait(self): pass
    monkeypatch.setattr(fingerprint.subprocess, "Popen", lambda *a, **k: P())
    src = _Stream(b"x" * 10_000, chunk=100)
    fingerprint.stream_fingerprint(src, "http://feed/ep.mp3", max_bytes=500)
    assert src.read <= 700, f"kept reading past the cap: {src.read}"


class _Sink:
    def __init__(self, seen): self.seen = seen; self.buf = bytearray()
    def write(self, b): self.buf += b
    def close(self): self.seen["written"] = len(self.buf)


class _Out:
    def __init__(self, data): self.data = data
    def read(self): return self.data
    def close(self): pass


def test_streaming_parses_the_fingerprint(monkeypatch):
    class P:
        def __init__(self, *a, **k):
            self.stdin, self.stdout = _Sink({}), _Out(b"DURATION=0\nFINGERPRINT=7,8,9\n")
        def wait(self): pass
    monkeypatch.setattr(fingerprint.subprocess, "Popen", lambda *a, **k: P())
    assert fingerprint.stream_fingerprint(_Stream(b"audio"), "http://x") == [7, 8, 9]


def test_stream_episode_fingerprint_caches_and_derives_duration(conn, data_dir, monkeypatch):
    """Duration comes from the frame count because a piped fpcalc reports DURATION=0 — it
    cannot seek. Measured error on real episodes is ~0.02%."""
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://f')")
    eid = _seed(conn, data_dir, "streamed")
    monkeypatch.setattr(fingerprint, "stream_fingerprint", lambda c, u, **k: list(range(100)))
    fp, dur = fingerprint.stream_episode_fingerprint(conn, eid, "http://f/a.mp3", client=None)
    assert len(fp) == 100
    assert dur == pytest.approx(100 * fingerprint.NOMINAL_SECONDS_PER_FRAME)
    assert conn.execute("SELECT COUNT(*) c FROM episode_fingerprints").fetchone()["c"] == 1

    def boom(*a, **k):
        raise AssertionError("re-streamed an episode that was already fingerprinted")
    monkeypatch.setattr(fingerprint, "stream_fingerprint", boom)
    again, _ = fingerprint.stream_episode_fingerprint(conn, eid, "http://f/a.mp3", client=None)
    assert again == fp


def test_cached_fingerprint_needs_no_audio(conn, data_dir, monkeypatch):
    """The whole point of the cache: an episode indexed once — from a file or straight off the
    network — never needs its audio again for matching or discovery. Checking the filesystem
    first would quietly require keeping the thing the index exists to replace."""
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://f')")
    eid = _seed(conn, data_dir, "indexed")
    fingerprint.ensure_schema(conn)          # cache tables are created lazily
    conn.execute("INSERT INTO episode_fingerprints (episode_id, fingerprint, duration) "
                 "VALUES (?, ?, ?)", (eid, ",".join(str(v) for v in AD), 42.0))
    conn.commit()
    (data_dir / "audio" / f"{eid}.mp3").unlink()          # audio thrown away

    def boom(*a, **k):
        raise AssertionError("touched the audio for an already-indexed episode")
    monkeypatch.setattr(fingerprint, "_fpcalc", boom)
    monkeypatch.setattr(fingerprint, "probe_duration", boom)

    got = fingerprint.cached_fingerprint(conn, eid, data_dir)
    assert got is not None and got[0] == AD and got[1] == 42.0


def test_cached_fingerprint_is_none_without_cache_or_audio(conn, data_dir):
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://f')")
    eid = _seed(conn, data_dir, "gone")
    (data_dir / "audio" / f"{eid}.mp3").unlink()
    assert fingerprint.cached_fingerprint(conn, eid, data_dir) is None
