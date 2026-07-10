from adscrub import cli, db


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


def test_not_built_yet_commands_report_milestone(tmp_path, capsys):
    for name, milestone in [
        ("transcribe", "M2"), ("detect", "M3"), ("cut", "M4"), ("serve", "M4/M5"),
    ]:
        rc = cli.main(["--db", str(tmp_path / "t.db"), name])
        assert rc == 1
        assert milestone in capsys.readouterr().err


def test_version(capsys):
    try:
        cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "adscrub" in out
