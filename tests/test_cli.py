import json

from adscrub import cli, cut, db, detect, transcribe


def test_add_feed_then_stats(tmp_path, capsys):
    path = tmp_path / "t.db"
    rc = cli.main(["--db", str(path), "add-feed", "http://a"])
    assert rc == 0
    assert "feed #1: http://a" in capsys.readouterr().out

    rc = cli.main(["--db", str(path), "stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "feeds:       1" in out
    assert "episodes:    0" in out


def test_stats_on_empty_db(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "feeds:       0" in out
    assert "episodes:    0" in out
    assert "ad_segments: 0" in out


def test_ingest_with_no_feeds_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "ingest"])
    assert rc == 1
    assert "adscrub add-feed" in capsys.readouterr().err


def test_chapters_with_nothing_to_scan_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "chapters"])
    assert rc == 1
    assert "no episodes" in capsys.readouterr().err




def test_transcribe_with_nothing_pending_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "transcribe"])
    assert rc == 1
    assert "no episodes pending" in capsys.readouterr().err


def test_transcribe_dry_run_reports_pending(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    conn.execute("INSERT INTO episodes (feed_id, guid, title, audio_url) VALUES (1, 'g1', 'ep', 'http://a/1.mp3')")
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "transcribe", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 1" in capsys.readouterr().out


def test_transcribe_success_path(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    conn.execute("INSERT INTO episodes (feed_id, guid, title, audio_url) VALUES (1, 'g1', 'Ep One', 'http://a/1.mp3')")
    conn.commit()
    conn.close()

    def fake_transcribe_episode(conn, ep, client, model_size=None):
        conn.execute(
            "UPDATE episodes SET transcript_path = 'x.json' WHERE id = ?", (ep["id"],)
        )
        conn.commit()
        return "x.json"

    monkeypatch.setattr(transcribe, "transcribe_episode", fake_transcribe_episode)

    rc = cli.main(["--db", str(path), "transcribe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok    Ep One -> x.json" in out
    assert "transcribed 1 episode(s) (0 failed, 0 still pending)" in out


def test_detect_with_nothing_pending_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "detect"])
    assert rc == 1
    assert "no episodes pending" in capsys.readouterr().err


def test_detect_dry_run_reports_pending(tmp_path, capsys):
    path = tmp_path / "t.db"
    transcript_path = tmp_path / "t.json"
    transcript_path.write_text(json.dumps([{"start": 0.0, "end": 1.0, "text": "hi"}]))
    conn = db.connect(path)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, transcript_path) VALUES (1, 'g1', 'ep', ?)",
        (str(transcript_path),),
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "detect", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 1" in capsys.readouterr().out


def test_detect_success_path(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    transcript_path = tmp_path / "t.json"
    transcript_path.write_text(json.dumps(
        [{"start": 0.0, "end": 5.0, "text": "a"}, {"start": 5.0, "end": 8.0, "text": "ad"}]
    ))
    conn = db.connect(path)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, transcript_path) VALUES (1, 'g1', 'Ep One', ?)",
        (str(transcript_path),),
    )
    conn.commit()
    conn.close()

    class FakeMessages:
        def parse(self, **kwargs):
            class Response:
                parsed_output = detect._Detection(
                    ad_spans=[detect._Span(start_segment=1, end_segment=1, reason="ad")]
                )
            return Response()

    class FakeAnthropic:
        def __init__(self):
            self.messages = FakeMessages()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)

    rc = cli.main(["--db", str(path), "detect"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok    Ep One: 1 ad span(s) from transcript" in out
    assert "detected across 1 episode(s) (0 failed, 0 still pending)" in out


def test_cut_with_nothing_pending_fails(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "t.db"), "cut"])
    assert rc == 1
    assert "no episodes pending cutting" in capsys.readouterr().err


def test_cut_dry_run_reports_pending(tmp_path, capsys):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, audio_url) VALUES (1, 'g1', 'ep', 'http://a/1.mp3')"
    )
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source) VALUES (1, 0, 5, 'chapter')"
    )
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(path), "cut", "--dry-run"])
    assert rc == 0
    assert "pending episodes: 1" in capsys.readouterr().out


def test_cut_success_path(tmp_path, capsys, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://feed')")
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, audio_url) VALUES (1, 'g1', 'Ep One', 'http://a/1.mp3')"
    )
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source) VALUES (1, 0, 5, 'chapter')"
    )
    conn.commit()
    conn.close()

    def fake_cut_episode(conn, ep, client, data_dir=None):
        conn.execute("UPDATE episodes SET cut_path = 'x.mp3' WHERE id = ?", (ep["id"],))
        conn.commit()
        return "x.mp3", 5.0

    monkeypatch.setattr(cut, "cut_episode", fake_cut_episode)

    rc = cli.main(["--db", str(path), "cut"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok    Ep One: removed 5.0s of ads" in out
    assert "cut 1 episode(s) (0 failed, 0 still pending)" in out


def test_version(capsys):
    try:
        cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "adscrub" in out
