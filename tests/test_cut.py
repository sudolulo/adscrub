import subprocess

import httpx
import pytest

from adscrub import cut, db

AUDIO_URL = "https://example.com/audio/ep1.mp3"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def seed_episode(conn, ad_spans=()):
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, audio_url) VALUES (1, 'ep-1', 'Ep 1', ?)",
        (AUDIO_URL,),
    )
    conn.commit()
    ep = conn.execute("SELECT * FROM episodes WHERE guid = 'ep-1'").fetchone()
    for start, end, source in ad_spans:
        conn.execute(
            "INSERT INTO ad_segments (episode_id, start_second, end_second, source)"
            " VALUES (?, ?, ?, ?)",
            (ep["id"], start, end, source),
        )
    conn.commit()
    return conn.execute("SELECT * FROM episodes WHERE guid = 'ep-1'").fetchone()


# --- compute_keep_spans ---


def test_compute_keep_spans_no_ads():
    assert cut.compute_keep_spans([], 100.0) == [(0.0, 100.0)]


def test_compute_keep_spans_ad_in_middle():
    assert cut.compute_keep_spans([(40.0, 60.0)], 100.0) == [(0.0, 40.0), (60.0, 100.0)]


def test_compute_keep_spans_ad_at_start_and_end():
    assert cut.compute_keep_spans([(0.0, 10.0), (90.0, 100.0)], 100.0) == [(10.0, 90.0)]


def test_compute_keep_spans_merges_overlapping_spans_from_different_sources():
    # a chapter-sourced span and an llm-sourced span covering roughly the same break
    spans = [(40.0, 65.0), (60.0, 70.0)]
    assert cut.compute_keep_spans(spans, 100.0) == [(0.0, 40.0), (70.0, 100.0)]


def test_compute_keep_spans_merges_adjacent_spans():
    assert cut.compute_keep_spans([(10.0, 20.0), (20.0, 30.0)], 100.0) == [
        (0.0, 10.0), (30.0, 100.0)
    ]


def test_compute_keep_spans_clamps_out_of_range_end():
    assert cut.compute_keep_spans([(90.0, 150.0)], 100.0) == [(0.0, 90.0)]


def test_compute_keep_spans_entire_episode_is_ads():
    assert cut.compute_keep_spans([(0.0, 100.0)], 100.0) == []


# --- cut_audio ---


def test_cut_audio_no_ads_copies_file_without_invoking_ffmpeg(tmp_path, monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("ffmpeg should not be invoked when there's nothing to cut")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    src = tmp_path / "in.mp3"
    src.write_bytes(b"original-audio-bytes")
    dest = tmp_path / "out" / "out.mp3"
    cut.cut_audio(src, [(0.0, 100.0)], dest)
    assert dest.read_bytes() == b"original-audio-bytes"


def test_cut_audio_invokes_ffmpeg_per_segment_then_concat(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # ffmpeg's real job is producing the output file at the end of argv
        with open(cmd[-1], "wb") as fh:
            fh.write(b"x")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    src = tmp_path / "in.mp3"
    src.write_bytes(b"original")
    dest = tmp_path / "out.mp3"
    cut.cut_audio(src, [(0.0, 40.0), (60.0, 100.0)], dest)

    assert dest.exists()
    # 2 segment extractions + 1 concat
    assert len(calls) == 3
    assert calls[0][:2] == ["ffmpeg", "-y"]
    assert "-ss" in calls[0] and "0.0" in calls[0]
    assert "-ss" in calls[1] and "60.0" in calls[1]
    assert calls[2][3] == "concat"


# --- pending_episodes ---


def test_pending_episodes_requires_ad_segments(conn):
    seed_episode(conn)  # no ad spans at all
    assert cut.pending_episodes(conn) == []


def test_pending_episodes_includes_episode_with_ad_spans(conn):
    ep = seed_episode(conn, ad_spans=[(10.0, 20.0, "chapter")])
    assert [e["id"] for e in cut.pending_episodes(conn)] == [ep["id"]]

    conn.execute("UPDATE episodes SET cut_path = '/x.mp3' WHERE id = ?", (ep["id"],))
    conn.commit()
    assert cut.pending_episodes(conn) == []


# --- cut_episode / cut_pending ---


def audio_client():
    def handler(request):
        return httpx.Response(200, content=b"fake-mp3-bytes")

    return httpx.Client(transport=httpx.MockTransport(handler))


def fake_cut_audio(audio_path, keep_spans, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"cut")


def test_cut_episode_updates_row_and_returns_ad_seconds(conn, tmp_path, monkeypatch):
    ep = seed_episode(conn, ad_spans=[(10.0, 20.0, "chapter")])
    monkeypatch.setattr(cut, "probe_duration", lambda path: 100.0)
    monkeypatch.setattr(cut, "cut_audio", fake_cut_audio)

    with audio_client() as client:
        path, ad_seconds = cut.cut_episode(conn, ep, client, data_dir=tmp_path)

    assert ad_seconds == 10.0
    assert path == tmp_path / "cut" / f"{ep['id']}.mp3"
    assert path.read_bytes() == b"cut"

    row = conn.execute("SELECT cut_path FROM episodes WHERE id = ?", (ep["id"],)).fetchone()
    assert row["cut_path"] == str(path)


def test_cut_pending_isolates_per_episode_failures(conn, tmp_path, monkeypatch):
    seed_episode(conn, ad_spans=[(10.0, 20.0, "chapter")])
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed2')")
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, audio_url) VALUES (2, 'ep-2', 'Ep 2', ?)",
        (AUDIO_URL,),
    )
    conn.commit()
    ep2 = conn.execute("SELECT * FROM episodes WHERE guid = 'ep-2'").fetchone()
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source) VALUES (?, 0, 5, 'chapter')",
        (ep2["id"],),
    )
    conn.commit()

    def fake_probe_duration(path):
        # Match the episode-2 audio file by its deterministic name
        # (data_dir/audio/<episode_id>.mp3), not a substring of the full
        # path — tmp_path itself is pytest's auto-numbered temp dir
        # ("pytest-26", "pytest-102", ...) and can coincidentally contain
        # "2", which made this test flaky depending on run order.
        if path.stem == str(ep2["id"]):
            raise RuntimeError("ffprobe failed")
        return 100.0

    monkeypatch.setattr(cut, "probe_duration", fake_probe_duration)
    monkeypatch.setattr(cut, "cut_audio", fake_cut_audio)

    with audio_client() as client:
        results = {r.title: r for r in cut.cut_pending(conn, client, data_dir=tmp_path)}

    assert results["Ep 1"].error is None
    assert results["Ep 2"].error is not None
    assert cut.pending_episodes(conn) == [
        conn.execute("SELECT * FROM episodes WHERE guid = 'ep-2'").fetchone()
    ]


# --- which sources are allowed to remove audio ---


def _ep_with_span(conn, source, start=10.0, end=20.0):
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://f')")
    conn.execute("INSERT INTO episodes (feed_id, guid, title, audio_url) "
                 "VALUES (1, ?, ?, 'http://f/a.mp3')", (source, source))
    eid = conn.execute("SELECT id FROM episodes WHERE guid=?", (source,)).fetchone()["id"]
    conn.execute("INSERT INTO ad_segments (episode_id, start_second, end_second, source) "
                 "VALUES (?, ?, ?, ?)", (eid, start, end, source))
    conn.commit()
    return eid


def test_discovery_only_episodes_are_not_pending(tmp_path):
    """`recur`/`dai` find ads but don't pin the edges. An episode with only those has nothing
    safe to remove — treating it as pending would rewrite the file unchanged and mark it done,
    retiring it from cutting before real spans ever arrive."""
    conn = db.connect(tmp_path / "t.db")
    _ep_with_span(conn, "recur")
    assert cut.pending_episodes(conn) == []
    assert len(cut.pending_episodes(conn, sources=("recur",))) == 1  # opt in explicitly


def test_trusted_sources_are_pending(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _ep_with_span(conn, "fpmatch")
    assert len(cut.pending_episodes(conn)) == 1


def test_untrusted_spans_are_not_removed_from_audio(tmp_path):
    """The real risk: a `recur` span sitting next to a trusted one must not widen the cut."""
    conn = db.connect(tmp_path / "t.db")
    eid = _ep_with_span(conn, "llm", 10.0, 20.0)
    conn.execute("INSERT INTO ad_segments (episode_id, start_second, end_second, source) "
                 "VALUES (?, 30.0, 40.0, 'recur')", (eid,))
    conn.commit()
    rows = conn.execute(
        "SELECT start_second, end_second FROM ad_segments WHERE episode_id=? AND source IN "
        "(?,?,?,?)", (eid, *cut.CUT_SOURCES)).fetchall()
    assert [(r["start_second"], r["end_second"]) for r in rows] == [(10.0, 20.0)]
    # 30-40 survives in the audio because nothing trusted vouches for those edges
    keep = cut.compute_keep_spans([(r["start_second"], r["end_second"]) for r in rows], 60.0)
    assert (20.0, 60.0) in keep


def test_cut_sources_excludes_the_edge_unsafe_tiers():
    assert "dai" not in cut.CUT_SOURCES and "recur" not in cut.CUT_SOURCES
    assert {"chapter", "llm", "repeat", "fpmatch"} == set(cut.CUT_SOURCES)
