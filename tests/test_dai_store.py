"""Persisting DAI probe results as ad spans (dai.dai_episode).

The probe speaks in BYTES; a stored span is in seconds, converted through the file's average
byte rate. These tests pin the conversion, the conservative trimming, and — most importantly —
that a `dai` span may seed the AUDIO library but never the TEXT one, since its end is only an
upper bound.
"""
import pytest

from adscrub import dai, db, fingerprint, repeats


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


@pytest.fixture
def episode(conn, tmp_path, monkeypatch):
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    conn.execute("INSERT INTO episodes (feed_id, guid, title, audio_url) "
                 "VALUES (1, 'g', 'Ep', 'http://feed/ep.mp3')")
    conn.commit()
    row = conn.execute("SELECT * FROM episodes").fetchone()
    audio = tmp_path / "data" / "audio" / f"{row['id']}.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"x" * 1_000_000)                       # 1,000,000 bytes
    monkeypatch.setattr(dai, "probe_duration", lambda p: 1000.0)  # -> exactly 1000 bytes/sec
    return row, tmp_path / "data"


def _probe(monkeypatch, **kw):
    monkeypatch.setattr(dai, "probe_variance", lambda *a, **k: dai.DAIProbeResult(**kw))


def test_stores_a_trimmed_span_from_byte_offsets(conn, episode, monkeypatch):
    row, data_dir = episode
    # 1000 bytes/sec: divergence at 20s, realignment at 80s; trimmed by DAI_EDGE_MARGIN each end
    _probe(monkeypatch, bytes_compared=10, diverged=True, divergence_byte=20_000,
           reconverged=True, reconvergence_byte=80_000)
    r = dai.dai_episode(conn, row, lambda: None, data_dir=data_dir)
    assert r.stored == 1
    span = conn.execute("SELECT * FROM ad_segments WHERE source='dai'").fetchone()
    assert span["start_second"] == pytest.approx(20.0 + dai.DAI_EDGE_MARGIN)
    assert span["end_second"] == pytest.approx(80.0 - dai.DAI_EDGE_MARGIN)
    assert span["confidence"] == 0.5  # weaker than llm/chapter: the end is an upper bound


def test_no_divergence_stores_nothing(conn, episode, monkeypatch):
    row, data_dir = episode
    _probe(monkeypatch, bytes_compared=10, diverged=False)
    assert dai.dai_episode(conn, row, lambda: None, data_dir=data_dir).stored == 0
    assert conn.execute("SELECT COUNT(*) c FROM ad_segments").fetchone()["c"] == 0


def test_divergence_without_realignment_stores_nothing(conn, episode, monkeypatch):
    """Without a reconvergence point there is no end at all — storing one would be a guess."""
    row, data_dir = episode
    _probe(monkeypatch, bytes_compared=10, diverged=True, divergence_byte=20_000, reconverged=False)
    r = dai.dai_episode(conn, row, lambda: None, data_dir=data_dir)
    assert r.stored == 0 and "no usable end" in r.reason


def test_absurd_span_is_capped(conn, episode, monkeypatch):
    """A bogus anchor must not turn half the episode into an 'ad'."""
    row, data_dir = episode
    _probe(monkeypatch, bytes_compared=10, diverged=True, divergence_byte=10_000,
           reconverged=True, reconvergence_byte=900_000)  # would be a 890s "ad"
    dai.dai_episode(conn, row, lambda: None, data_dir=data_dir)
    span = conn.execute("SELECT * FROM ad_segments WHERE source='dai'").fetchone()
    assert span["end_second"] - span["start_second"] <= dai.MAX_DAI_BREAK


def test_is_idempotent(conn, episode, monkeypatch):
    row, data_dir = episode
    _probe(monkeypatch, bytes_compared=10, diverged=True, divergence_byte=20_000,
           reconverged=True, reconvergence_byte=80_000)
    for _ in range(3):
        dai.dai_episode(conn, row, lambda: None, data_dir=data_dir)
    assert conn.execute("SELECT COUNT(*) c FROM ad_segments WHERE source='dai'").fetchone()["c"] == 1


def test_missing_local_audio_is_skipped_not_fatal(conn, episode, monkeypatch):
    row, data_dir = episode
    (data_dir / "audio" / f"{row['id']}.mp3").unlink()
    _probe(monkeypatch, bytes_compared=10, diverged=True, divergence_byte=20_000,
           reconverged=True, reconvergence_byte=80_000)
    r = dai.dai_episode(conn, row, lambda: None, data_dir=data_dir)
    assert r.stored == 0 and "bytes to seconds" in r.reason


def test_dai_seeds_the_audio_library_but_never_the_text_library():
    """The whole reason `dai` is split across two source lists: byte-derived boundaries are fine
    for fingerprint matching (which needs a long aligned run) and wrong for text shingles."""
    assert "dai" in fingerprint.FP_LIBRARY_SOURCES
    assert "dai" not in repeats.GROUND_TRUTH_SOURCES
