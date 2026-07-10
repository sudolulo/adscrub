import sqlite3

import pytest

from adscrub import db


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_connect_creates_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"feeds", "episodes", "ad_segments"} <= tables


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    db.connect(path).close()
    db.connect(path).close()


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episodes (feed_id, guid) VALUES (999, 'x')")


def test_episode_guid_unique_per_feed(conn):
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://a')")
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://b')")
    conn.execute("INSERT INTO episodes (feed_id, guid) VALUES (1, 'ep-1')")
    # same guid on another feed is fine
    conn.execute("INSERT INTO episodes (feed_id, guid) VALUES (2, 'ep-1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episodes (feed_id, guid) VALUES (1, 'ep-1')")


def test_ad_segments_cascade_on_episode_delete(conn):
    conn.execute("INSERT INTO feeds (source_url) VALUES ('http://a')")
    conn.execute("INSERT INTO episodes (feed_id, guid) VALUES (1, 'ep-1')")
    conn.execute(
        "INSERT INTO ad_segments (episode_id, start_second, end_second, source)"
        " VALUES (1, 0, 30, 'chapter')"
    )
    conn.execute("DELETE FROM episodes WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM ad_segments").fetchone()[0] == 0


def test_utcnow_format():
    value = db.utcnow()
    assert len(value) == 20 and value.endswith("Z") and value[10] == "T"
