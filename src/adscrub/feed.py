"""M4/M5: re-host a cleaned feed (feedgen) pointing at cut_path episodes.

This is the only integration point any podcast player needs: subscribe to
`/feed/<feed_id>` instead of the original feed URL. Episodes with a cut_path
are served locally at `/audio/<id>.<ext>`; everything else still points at its
original audio_url unchanged — an episode nobody has cut (no ads found, or
not processed yet) doesn't need a local copy at all.

Dependency-free HTTP by design, same as hark's web.py: stdlib http.server, no
framework. Unlike hark's browsable dashboard this has no login wall — it's a
machine-consumed feed for one owner on a homelab network, not a searchable UI
with a filesystem of someone's listening habits behind it. Revisit if this
ever needs to be reachable from outside a trusted network (see docs/PLAN.md).
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from feedgen.feed import FeedGenerator

from . import __version__, db


def _parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_feed(conn: sqlite3.Connection, feed: sqlite3.Row, base_url: str) -> bytes:
    fg = FeedGenerator()
    fg.title(feed["title"] or feed["source_url"])
    fg.link(href=feed["source_url"], rel="self")
    fg.description(feed["description"] or feed["title"] or feed["source_url"])
    if feed["image_url"]:
        fg.image(feed["image_url"])

    episodes = conn.execute(
        "SELECT * FROM episodes WHERE feed_id = ? ORDER BY pubdate DESC", (feed["id"],)
    ).fetchall()
    for ep in episodes:
        length = 0
        if ep["cut_path"]:
            cut_path = Path(ep["cut_path"])
            audio_url = f"{base_url}/audio/{ep['id']}{cut_path.suffix}"
            if cut_path.is_file():
                length = cut_path.stat().st_size
        else:
            audio_url = ep["audio_url"]
        if not audio_url:
            continue  # nothing playable to link — skip rather than emit a dead enclosure
        fe = fg.add_entry()
        fe.id(ep["guid"])
        fe.title(ep["title"] or "(untitled)")
        fe.description(ep["description"] or "")
        pubdate = _parse_pubdate(ep["pubdate"])
        if pubdate:
            fe.pubDate(pubdate)
        fe.enclosure(audio_url, length, "audio/mpeg")

    return fg.rss_str(pretty=True)


class Handler(BaseHTTPRequestHandler):
    db_path: str
    data_dir: Path
    base_url: str
    server_version = f"adscrub/{__version__}"

    def log_message(self, fmt, *args):  # quiet access log
        pass

    def respond(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def not_found(self) -> None:
        self.respond(404, b"not found", "text/plain; charset=utf-8")

    def do_GET(self):
        route = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"

        if route == "/healthz":
            return self.respond(200, b"ok", "text/plain; charset=utf-8")

        if route.startswith("/feed/"):
            try:
                feed_id = int(route.rsplit("/", 1)[1])
            except ValueError:
                return self.not_found()
            conn = db.connect(self.db_path)
            try:
                feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
                if feed is None:
                    return self.not_found()
                body = build_feed(conn, feed, self.base_url)
            finally:
                conn.close()
            return self.respond(200, body, "application/rss+xml; charset=utf-8")

        if route.startswith("/audio/"):
            # strip any path components the client sent — only ever look inside
            # data_dir/cut, never let the URL choose an arbitrary filesystem path
            name = Path(route[len("/audio/"):]).name
            cut_dir = (self.data_dir / "cut").resolve()
            candidate = (cut_dir / name).resolve()
            if cut_dir not in candidate.parents or not candidate.is_file():
                return self.not_found()
            return self.respond(200, candidate.read_bytes(), "audio/mpeg")

        return self.not_found()


def make_server(
    db_path: str | Path, data_dir: str | Path, base_url: str, bind: str = "0.0.0.0:8711"
) -> ThreadingHTTPServer:
    host, _, port = bind.rpartition(":")
    try:
        port_num = int(port)
    except ValueError:
        raise SystemExit(f"invalid --bind {bind!r}: expected host:port or :port")
    handler = type("BoundHandler", (Handler,), {
        "db_path": str(db_path), "data_dir": Path(data_dir), "base_url": base_url.rstrip("/"),
    })
    return ThreadingHTTPServer((host or "0.0.0.0", port_num), handler)


def serve(db_path: str | Path, data_dir: str | Path, base_url: str, bind: str) -> None:
    if "localhost" in base_url or "127.0.0.1" in base_url:
        print(f"warning: --base-url is {base_url!r} — a podcast player running "
              f"anywhere but this exact machine won't be able to reach cut audio "
              f"links embedded in the generated feed. Set --base-url/$ADSCRUB_BASE_URL "
              f"to this host's actual reachable address.")
    server = make_server(db_path, data_dir, base_url, bind)
    print(f"adscrub serving on {bind} (feeds at {base_url.rstrip('/')}/feed/<id>)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
