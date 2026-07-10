"""feed.py tests: build_feed content, and real HTTP against a server on an
ephemeral port — same pattern as hark's test_web.py."""

import http.client
import threading

import pytest

from adscrub import db, feed


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO feeds (source_url, title, description, image_url)"
        " VALUES ('http://original/feed', 'Show A', 'A show', 'http://original/art.png')"
    )
    conn.commit()
    return conn


# --- build_feed ---


def test_build_feed_passthrough_for_uncut_episode(conn):
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, description, pubdate, audio_url)"
        " VALUES (1, 'ep-1', 'Ep 1', 'desc', '2026-01-01T00:00:00Z', 'http://original/ep1.mp3')"
    )
    conn.commit()
    feed_row = conn.execute("SELECT * FROM feeds WHERE id = 1").fetchone()

    xml = feed.build_feed(conn, feed_row, "http://myhost:8711").decode()
    assert "<title>Show A</title>" in xml
    assert 'url="http://original/ep1.mp3"' in xml
    assert "myhost" not in xml  # untouched episode keeps its original URL


def test_build_feed_points_cut_episodes_at_local_audio_route(conn, tmp_path):
    # cut.py names the file after the episode id (see cut_episode) — build_feed
    # reconstructs the same URL from ep['id'] + the stored cut_path's suffix,
    # so the fixture's filename here must match episode id 1 to be realistic.
    cut_path = tmp_path / "cut" / "1.mp3"
    cut_path.parent.mkdir(parents=True)
    cut_path.write_bytes(b"cut-audio-bytes")
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, audio_url, cut_path)"
        " VALUES (1, 'ep-1', 'Ep 1', 'http://original/ep1.mp3', ?)",
        (str(cut_path),),
    )
    conn.commit()
    feed_row = conn.execute("SELECT * FROM feeds WHERE id = 1").fetchone()

    xml = feed.build_feed(conn, feed_row, "http://myhost:8711").decode()
    assert 'url="http://myhost:8711/audio/1.mp3"' in xml
    assert f'length="{len(b"cut-audio-bytes")}"' in xml
    assert "original/ep1.mp3" not in xml


def test_build_feed_skips_episode_with_no_audio(conn):
    conn.execute("INSERT INTO episodes (feed_id, guid, title) VALUES (1, 'ep-1', 'No audio')")
    conn.commit()
    feed_row = conn.execute("SELECT * FROM feeds WHERE id = 1").fetchone()
    xml = feed.build_feed(conn, feed_row, "http://myhost:8711").decode()
    assert "No audio" not in xml


# --- HTTP server ---


@pytest.fixture
def server(tmp_path):
    conn = db.connect(tmp_path / "adscrub.db")
    conn.execute(
        "INSERT INTO feeds (source_url, title) VALUES ('http://original/feed', 'Show A')"
    )
    conn.execute(
        "INSERT INTO episodes (feed_id, guid, title, audio_url)"
        " VALUES (1, 'ep-1', 'Ep 1', 'http://original/ep1.mp3')"
    )
    conn.commit()
    conn.close()

    cut_dir = tmp_path / "data" / "cut"
    cut_dir.mkdir(parents=True)
    (cut_dir / "audio.mp3").write_bytes(b"cut-bytes")

    srv = feed.make_server(
        tmp_path / "adscrub.db", tmp_path / "data", "http://myhost:8711", bind="127.0.0.1:0"
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def request(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def test_healthz(server):
    resp, data = request(server, "/healthz")
    assert resp.status == 200
    assert data == b"ok"


def test_feed_route_serves_generated_rss(server):
    resp, data = request(server, "/feed/1")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/rss+xml; charset=utf-8"
    assert b"Show A" in data


def test_feed_route_unknown_id_404s(server):
    resp, _ = request(server, "/feed/999")
    assert resp.status == 404


def test_feed_route_non_numeric_id_404s(server):
    resp, _ = request(server, "/feed/not-a-number")
    assert resp.status == 404


def test_audio_route_serves_cut_file(server):
    resp, data = request(server, "/audio/audio.mp3")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "audio/mpeg"
    assert data == b"cut-bytes"


def test_audio_route_missing_file_404s(server):
    resp, _ = request(server, "/audio/nope.mp3")
    assert resp.status == 404


def test_audio_route_rejects_path_traversal(server):
    # Path(...).name strips any directory components regardless of how many
    # ".." segments precede the final part, so this can never escape cut_dir.
    resp, data = request(server, "/audio/../../../../etc/passwd")
    assert resp.status == 404
    assert b"root:" not in data


def test_audio_route_rejects_encoded_path_traversal(server):
    resp, data = request(server, "/audio/..%2F..%2F..%2Fetc%2Fpasswd")
    assert resp.status == 404
    assert b"root:" not in data


def test_unknown_route_404s(server):
    resp, _ = request(server, "/nonsense")
    assert resp.status == 404


# --- serve() base_url warning ---


class _FakeServer:
    def serve_forever(self):
        pass


def test_serve_warns_when_base_url_is_localhost(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(feed, "make_server", lambda *a, **k: _FakeServer())
    feed.serve(tmp_path / "db", tmp_path / "data", "http://localhost:8711", "127.0.0.1:0")
    assert "warning" in capsys.readouterr().out.lower()


def test_serve_no_warning_for_a_real_hostname(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(feed, "make_server", lambda *a, **k: _FakeServer())
    feed.serve(tmp_path / "db", tmp_path / "data", "http://truenas.local:8711", "127.0.0.1:0")
    assert "warning" not in capsys.readouterr().out.lower()
