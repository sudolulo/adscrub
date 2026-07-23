import json

import pytest

from adscrub import db, detect, repeats

# The same sponsor read, transcribed twice. Whisper does NOT segment it identically the
# second time (different surrounding audio -> different boundaries), which is exactly why
# the matcher works on shingles rather than whole-segment equality.
AD_A = [
    {"start": 0.0, "end": 4.0, "text": "Welcome back to the show, here is where we left off."},
    {"start": 4.0, "end": 9.0, "text": "This episode is brought to you by BetterHelp online therapy."},
    {"start": 9.0, "end": 14.0, "text": "Sign up and get ten percent off at betterhelp dot com slash casefile."},
    {"start": 14.0, "end": 30.0, "text": "The detective arrived at the scene just after midnight."},
]
AD_B = [
    {"start": 0.0, "end": 6.0, "text": "In nineteen eighty four the family moved to the coast."},
    {"start": 6.0, "end": 11.0, "text": "This episode is brought to you by BetterHelp"},
    {"start": 11.0, "end": 13.0, "text": "online therapy. Sign up and get ten percent"},
    {"start": 13.0, "end": 17.0, "text": "off at betterhelp dot com slash casefile."},
    {"start": 17.0, "end": 40.0, "text": "Nobody reported them missing for another three weeks."},
]


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def seed(conn, tmp_path, guid, transcript, ad_spans=()):
    p = tmp_path / f"{guid}.json"
    p.write_text(json.dumps(transcript))
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, transcript_path) VALUES (1, ?, ?, ?)",
        (guid, guid, str(p)),
    )
    eid = conn.execute("SELECT id FROM episodes WHERE guid = ?", (guid,)).fetchone()["id"]
    for start, end in ad_spans:
        conn.execute(
            "INSERT INTO ad_segments (episode_id, start_second, end_second, source) "
            "VALUES (?, ?, ?, 'llm')",
            (eid, start, end),
        )
    conn.commit()
    return eid


@pytest.fixture
def corpus(conn, tmp_path):
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    a = seed(conn, tmp_path, "ep-a", AD_A, ad_spans=[(4.0, 14.0)])  # ad confirmed here
    b = seed(conn, tmp_path, "ep-b", AD_B)                          # same ad, unlabelled
    return conn, a, b


# --- the matcher ---


def test_recognises_the_same_ad_read_in_another_episode(corpus):
    conn, a, b = corpus
    detector = repeats.RepeatAdDetector(repeats.build_library(conn))
    spans = detector.detect(AD_B)
    assert len(spans) == 1
    # segments 1-3 of AD_B are the ad; the editorial either side is untouched
    assert spans[0].start_second == 6.0
    assert spans[0].end_second == 17.0
    assert spans[0].source == "repeat"


def test_leaves_editorial_alone(corpus):
    conn, a, b = corpus
    detector = repeats.RepeatAdDetector(repeats.build_library(conn))
    editorial = [
        {"start": 0.0, "end": 5.0, "text": "The detective arrived at the scene just after midnight."},
        {"start": 5.0, "end": 9.0, "text": "Nobody reported them missing for another three weeks."},
    ]
    assert detector.detect(editorial) == []


def test_an_episode_cannot_be_its_own_library(corpus):
    conn, a, b = corpus
    lib = repeats.build_library(conn, exclude_episode_id=a)
    # ep-a's ad was the ONLY confirmed one, so excluding it leaves nothing to match against
    assert repeats.RepeatAdDetector(lib).detect(AD_A) == []


def test_empty_library_detects_nothing():
    assert repeats.RepeatAdDetector(set()).detect(AD_B) == []


# --- apply_repeats ---


def test_apply_repeats_finds_the_unlabelled_copy(corpus):
    conn, a, b = corpus
    results = repeats.apply_repeats(conn)
    found = {r.episode_id: r.found for r in results}
    assert found[b] == 1
    rows = conn.execute(
        "SELECT * FROM ad_segments WHERE episode_id = ? AND source = 'repeat'", (b,)
    ).fetchall()
    assert len(rows) == 1


def test_apply_repeats_is_idempotent(corpus):
    """The library grows, so re-scanning is expected — it must refresh, not accumulate."""
    conn, a, b = corpus
    repeats.apply_repeats(conn)
    repeats.apply_repeats(conn)
    repeats.apply_repeats(conn)
    n = conn.execute(
        "SELECT COUNT(*) c FROM ad_segments WHERE episode_id = ? AND source = 'repeat'", (b,)
    ).fetchone()["c"]
    assert n == 1


def test_library_ignores_the_tier_s_own_output(corpus):
    """A repeat span is an inference, not evidence. If it feeds back into the library, the
    detector bootstraps off its own guesses and drifts — on the real corpus a second sweep
    went 958 -> 993 spans before this was fixed."""
    conn, a, b = corpus
    before = repeats.build_library(conn)
    repeats.apply_repeats(conn)  # writes source='repeat' rows
    assert conn.execute(
        "SELECT COUNT(*) c FROM ad_segments WHERE source='repeat'"
    ).fetchone()["c"] > 0
    after = repeats.build_library(conn)
    assert after == before, "repeat spans must never become library evidence"


def test_apply_repeats_never_touches_other_sources(corpus):
    conn, a, b = corpus
    repeats.apply_repeats(conn)
    llm = conn.execute(
        "SELECT COUNT(*) c FROM ad_segments WHERE episode_id = ? AND source = 'llm'", (a,)
    ).fetchone()["c"]
    assert llm == 1  # ep-a's confirmed span survives the sweep


def test_apply_repeats_does_not_mark_the_episode_llm_detected(corpus):
    """A free pass that never read the words must not retire the episode from the LLM."""
    conn, a, b = corpus
    repeats.apply_repeats(conn)
    row = conn.execute("SELECT llm_detected_at FROM episodes WHERE id = ?", (b,)).fetchone()
    assert row["llm_detected_at"] is None


# --- the truncation regression ---


def test_layered_detector_unions_its_tiers(corpus):
    """Composing tiers must need no branching — and each span keeps its own source."""
    conn, a, b = corpus

    class Stub:
        def detect(self, transcript, skip=frozenset()):
            return [detect.DetectedAdSpan(0.0, 1.0, "chapter marker", source="chapter")]

    layered = detect.LayeredDetector(
        [repeats.RepeatAdDetector(repeats.build_library(conn)), Stub()]
    )
    spans = layered.detect(AD_B)
    assert {s.source for s in spans} == {"repeat", "chapter"}


def test_chunks_cover_every_segment():
    """ClaudeAdDetector used to send `body[:20000]` — the first ~28% of an episode — and
    then mark it detected. Every segment must reach the model."""
    transcript = [
        {"start": float(i), "end": float(i + 1), "text": f"segment number {i} " + "filler " * 20}
        for i in range(2000)
    ]
    chunks = detect._chunks(transcript)
    assert len(chunks) > 1, "a 2000-segment transcript must not fit in one chunk"
    body = "\n".join(chunks)
    for i in (0, 235, 236, 999, 1999):  # 235/236 straddle the old cliff
        assert f"[{i}] " in body


def test_detector_scans_the_whole_transcript(monkeypatch):
    """The model is called for every chunk, not just the first."""
    transcript = [
        {"start": float(i), "end": float(i + 1), "text": f"segment {i} " + "filler " * 30}
        for i in range(1500)
    ]
    calls = []

    class Messages:
        def parse(self, **kw):
            calls.append(kw["messages"][0]["content"])

            class R:
                parsed_output = detect._Detection(ad_spans=[])

            return R()

    class Client:
        messages = Messages()

    detect.ClaudeAdDetector(Client(), model="m").detect(transcript)
    assert len(calls) > 1
    seen = "\n".join(calls)
    assert "[1499] " in seen, "the last segment never reached the model"


# --- prioritize_pending ---

NO_AD = [
    {"start": 0.0, "end": 10.0, "text": "A completely unrelated editorial segment about"
     " something else entirely, nothing here resembles any known sponsor read."},
]


def _mark_ground_truth(conn, episode_id):
    conn.execute(
        "UPDATE episodes SET llm_detected_at = '2026-01-01T00:00:00Z' WHERE id = ?",
        (episode_id,),
    )


def test_prioritize_pending_ranks_count_mismatch_first(conn, tmp_path):
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    # 3 ground-truth episodes, each confirmed with exactly 1 ad segment -> typical count 1.
    for i in range(3):
        eid = seed(conn, tmp_path, f"gt-{i}", AD_A, ad_spans=[(4.0, 14.0)])
        _mark_ground_truth(conn, eid)
    conn.commit()

    # Repeats the known ad once -> repeat tier finds 1 -> matches typical (diff 0).
    match_id = seed(conn, tmp_path, "pending-match", AD_B)
    # No ad content at all -> repeat tier finds 0 -> diff 1, should rank first.
    mismatch_id = seed(conn, tmp_path, "pending-mismatch", NO_AD)

    pending = [
        conn.execute("SELECT * FROM episodes WHERE id = ?", (match_id,)).fetchone(),
        conn.execute("SELECT * FROM episodes WHERE id = ?", (mismatch_id,)).fetchone(),
    ]
    ranked = repeats.prioritize_pending(conn, pending, group_column="feed_id", min_show_history=3)
    assert [ep["id"] for ep in ranked] == [mismatch_id, match_id]


def test_prioritize_pending_leaves_low_history_shows_unranked(conn, tmp_path):
    """Fewer than min_show_history ground-truth episodes -> no typical count exists ->
    original relative order is kept, nothing reordered on a guess."""
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    for i in range(2):  # below the default min_show_history of 3
        eid = seed(conn, tmp_path, f"gt-{i}", AD_A, ad_spans=[(4.0, 14.0)])
        _mark_ground_truth(conn, eid)
    conn.commit()

    p1 = seed(conn, tmp_path, "pending-1", AD_B)
    p2 = seed(conn, tmp_path, "pending-2", NO_AD)

    pending = [
        conn.execute("SELECT * FROM episodes WHERE id = ?", (p1,)).fetchone(),
        conn.execute("SELECT * FROM episodes WHERE id = ?", (p2,)).fetchone(),
    ]
    ranked = repeats.prioritize_pending(conn, pending, group_column="feed_id")
    assert [ep["id"] for ep in ranked] == [p1, p2]


def test_prioritize_pending_keeps_unreadable_transcript_episodes(conn, tmp_path):
    """A transcript that can't be loaded must still be processed eventually — dropping
    it from the queue entirely would be worse than leaving it unranked."""
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    for i in range(3):
        eid = seed(conn, tmp_path, f"gt-{i}", AD_A, ad_spans=[(4.0, 14.0)])
        _mark_ground_truth(conn, eid)
    conn.commit()

    broken_id = seed(conn, tmp_path, "pending-broken", AD_B)
    conn.execute(
        "UPDATE episodes SET transcript_path = ? WHERE id = ?",
        (str(tmp_path / "missing.json"), broken_id),
    )
    conn.commit()

    pending = [conn.execute("SELECT * FROM episodes WHERE id = ?", (broken_id,)).fetchone()]
    ranked = repeats.prioritize_pending(conn, pending, group_column="feed_id", min_show_history=3)
    assert [ep["id"] for ep in ranked] == [broken_id]


def test_prioritize_pending_no_ground_truth_returns_unchanged(conn, tmp_path):
    """No llm_detected_at episodes anywhere yet -> nothing to compute a typical count
    from -> the input order passes through untouched."""
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    p1 = seed(conn, tmp_path, "pending-1", AD_B)
    p2 = seed(conn, tmp_path, "pending-2", NO_AD)
    pending = [
        conn.execute("SELECT * FROM episodes WHERE id = ?", (p1,)).fetchone(),
        conn.execute("SELECT * FROM episodes WHERE id = ?", (p2,)).fetchone(),
    ]
    ranked = repeats.prioritize_pending(conn, pending, group_column="feed_id")
    assert [ep["id"] for ep in ranked] == [p1, p2]
