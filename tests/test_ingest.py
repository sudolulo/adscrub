import httpx
import pytest

from adscrub import db, ingest

FEED_URL = "https://feeds.example.com/show-a"


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    return conn


def feed_client(fixtures, name, status=200):
    content = (fixtures / name).read_bytes() if name else b""

    def handler(request):
        assert str(request.url) == FEED_URL
        return httpx.Response(status, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- parsing ---


def test_parse_feed(fixtures):
    parsed = ingest.parse_feed((fixtures / "feed_a.xml").read_bytes())
    assert parsed.title == "Example Show"
    assert parsed.description == "A show for tests."
    assert parsed.image_url == "https://example.com/art.png"
    assert len(parsed.episodes) == 2

    ep1, ep2 = parsed.episodes
    assert ep1.guid == "ep-001"
    assert ep1.duration_seconds == 3723
    assert ep1.audio_url == "https://example.com/audio/ep1.mp3"
    assert ep1.chapters_url == "https://example.com/ep1-chapters.json"

    assert ep2.duration_seconds == 2700
    assert ep2.chapters_url is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("01:02:03", 3723),
        ("45:00", 2700),
        ("90", 90),
        ("bogus", None),
        (None, None),
    ],
)
def test_parse_duration(value, expected):
    assert ingest.parse_duration(value) == expected


# --- add_feed / ingest ---


def test_add_feed_is_idempotent(conn):
    a = ingest.add_feed(conn, FEED_URL)
    b = ingest.add_feed(conn, FEED_URL)
    assert a["id"] == b["id"]
    assert conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 1


def test_ingest_all_inserts_then_updates(conn, fixtures):
    feed = ingest.add_feed(conn, FEED_URL)

    with feed_client(fixtures, "feed_a.xml") as client:
        [result] = ingest.ingest_all(conn, client)
    assert result.error is None
    assert result.inserted == 2
    assert result.updated == 0
    assert result.total == 2

    episodes = conn.execute(
        "SELECT * FROM episodes WHERE feed_id = ? ORDER BY guid", (feed["id"],)
    ).fetchall()
    assert [e["guid"] for e in episodes] == ["ep-001", "ep-002"]
    assert episodes[0]["chapters_url"] == "https://example.com/ep1-chapters.json"

    # re-ingesting the same feed content changes nothing
    with feed_client(fixtures, "feed_a.xml") as client:
        [result] = ingest.ingest_all(conn, client)
    assert (result.inserted, result.updated) == (0, 0)


def test_ingest_all_reports_http_errors(conn):
    ingest.add_feed(conn, FEED_URL)
    with feed_client(None, None, status=500) as client:
        [result] = ingest.ingest_all(conn, client)
    assert result.error is not None
