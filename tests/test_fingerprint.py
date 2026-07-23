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

from adscrub import db, fingerprint

# A synthetic "ad recording": 60 distinct frame values (>= MATCH_FRAMES so one aligned run
# clears the threshold). Padding values (1, 2) never appear in any library.
AD = list(range(1000, 1060))
PAD = [1] * 20


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
    runs = fingerprint.match_regions(query, _one_ad_library())
    assert runs == [(20, 79)]  # exactly the ad frames, padding untouched


def test_scattered_collisions_do_not_match():
    """A few coincidental value hits that do NOT line up on one diagonal must not add up."""
    lib = _one_ad_library()
    # only 5 of the ad's values appear, spread far apart -> no diagonal reaches MATCH_FRAMES
    query = [AD[0], 500, AD[10], 500, AD[20], 500, AD[30], 500, AD[40]]
    assert fingerprint.match_regions(query, lib) == []


def test_an_episode_is_excluded_from_its_own_corpus():
    lib = _one_ad_library(episode_id=10)
    query = PAD + AD + PAD
    assert fingerprint.match_regions(query, lib, exclude_episode_id=10) == []


def test_stop_listed_values_cannot_match():
    lib = fingerprint.Library(
        index={v: [(1, k)] for k, v in enumerate(AD)},
        stop=set(AD),  # every ad value is stop-listed (as silence/a common bed would be)
        ad_episode={1: 10},
    )
    assert fingerprint.match_regions(PAD + AD + PAD, lib) == []


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
    runs = fingerprint.match_regions(query, lib)
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


def test_stop_list_drops_ubiquitous_values(conn, data_dir, monkeypatch):
    """A value present in more than STOP_EPISODE_FRACTION of source episodes is treated as
    silence/a common bed and dropped. This is why a diverse rotating ad pool works (each ad
    is in a MINORITY of episodes) and a single-source library cannot match."""
    monkeypatch.setattr(fingerprint, "fpcalc_available", lambda: True)
    monkeypatch.setattr(fingerprint, "_fingerprint_region", lambda p, s, e: AD)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    # 4 source episodes ALL carrying the same ad -> every value is in 100% of episodes
    for i in range(4):
        _seed(conn, data_dir, f"src-{i}", ad_spans=[(2.0, 10.0)])
    lib = fingerprint.build_library(conn, data_dir)
    assert lib.n_episodes == 4
    assert lib.stop == set(AD)  # all values ubiquitous -> all stop-listed
    assert fingerprint.match_regions(PAD + AD + PAD, lib) == []
