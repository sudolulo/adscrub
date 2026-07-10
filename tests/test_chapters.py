import httpx
import pytest

from adscrub import chapters, db, ingest

CHAPTERS_URL = "https://example.com/ep1-chapters.json"
FEED_URL = "https://feeds.example.com/show-a"


@pytest.fixture
def conn(tmp_path, fixtures):
    conn = db.connect(tmp_path / "test.db")
    feed = ingest.add_feed(conn, FEED_URL)
    with (fixtures / "feed_a.xml").open("rb") as fh:
        parsed = ingest.parse_feed(fh.read())
    ingest.upsert_episodes(conn, feed["id"], parsed.episodes)
    conn.commit()
    return conn


def chapters_client(fixtures, status=200):
    content = (fixtures / "ep1_chapters.json").read_bytes()

    def handler(request):
        assert str(request.url) == CHAPTERS_URL
        return httpx.Response(status, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ad_spans_from_chapters_matches_keyword_titles(fixtures):
    data = (fixtures / "ep1_chapters.json").read_text()
    import json

    parsed = json.loads(data)["chapters"]
    spans = chapters.ad_spans_from_chapters(parsed)
    assert spans == [(45.0, 105.0), (3600.0, 3660.0)]


def test_ad_spans_ignores_non_ad_chapters():
    spans = chapters.ad_spans_from_chapters(
        [{"startTime": 0, "title": "Intro"}, {"startTime": 60, "title": "Main"}]
    )
    assert spans == []


def test_scan_episode_stores_ad_segments(conn, fixtures):
    ep = conn.execute("SELECT * FROM episodes WHERE guid = 'ep-001'").fetchone()
    with chapters_client(fixtures) as client:
        n = chapters.scan_episode(conn, client, ep)
    assert n == 2

    rows = conn.execute(
        "SELECT * FROM ad_segments WHERE episode_id = ? ORDER BY start_second", (ep["id"],)
    ).fetchall()
    assert [(r["start_second"], r["end_second"], r["source"]) for r in rows] == [
        (45.0, 105.0, "chapter"),
        (3600.0, 3660.0, "chapter"),
    ]

    scanned_at = conn.execute(
        "SELECT chapters_scanned_at FROM episodes WHERE id = ?", (ep["id"],)
    ).fetchone()[0]
    assert scanned_at is not None


def test_scan_episode_skips_when_no_chapters_url(conn):
    ep = conn.execute("SELECT * FROM episodes WHERE guid = 'ep-002'").fetchone()
    n = chapters.scan_episode(conn, httpx.Client(), ep)
    assert n == 0


def test_scan_episode_marks_scanned_even_with_zero_ad_chapters(conn):
    """Regression: an episode with chapters but no ad-keyword ones must still be
    marked scanned, or its chapters JSON gets re-fetched forever."""
    ep = conn.execute("SELECT * FROM episodes WHERE guid = 'ep-001'").fetchone()

    def handler(request):
        return httpx.Response(200, json={"chapters": [{"startTime": 0, "title": "Intro"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        n = chapters.scan_episode(conn, client, ep)
    assert n == 0
    scanned_at = conn.execute(
        "SELECT chapters_scanned_at FROM episodes WHERE id = ?", (ep["id"],)
    ).fetchone()[0]
    assert scanned_at is not None
    assert chapters.pending_episodes(conn) == []
