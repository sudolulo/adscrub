"""adscrub command line: add-feed, ingest, chapters, transcribe, detect, stats (cut/serve: M4+)."""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from . import __version__, chapters, db, detect, ingest, transcribe

DEFAULT_DB = os.environ.get("ADSCRUB_DB", "adscrub.db")
USER_AGENT = f"adscrub/{__version__} (homelab podcast ad-removal proxy)"

_NOT_BUILT_YET = {
    "cut": "M4",
    "serve": "M4/M5",
}


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


def cmd_add_feed(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    feed = ingest.add_feed(conn, args.url)
    print(f"  ok    feed #{feed['id']}: {feed['source_url']}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    with make_client() as client:
        results = ingest.ingest_all(conn, client)
    if not results:
        print("no feeds registered — run `adscrub add-feed <url>` first", file=sys.stderr)
        return 1
    errors = 0
    for r in results:
        if r.error:
            errors += 1
            print(f"  FAIL  {r.source_url}: {r.error}")
        else:
            print(f"  ok    {r.source_url}: +{r.inserted} new, {r.updated} updated ({r.total} in feed)")
    return 1 if errors else 0


def cmd_chapters(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    episodes = chapters.pending_episodes(conn)
    if not episodes:
        print("no episodes with an unscanned chapters_url", file=sys.stderr)
        return 1
    found = 0
    with make_client() as client:
        for ep in episodes:
            try:
                n = chapters.scan_episode(conn, client, ep)
            except httpx.HTTPError as exc:
                print(f"  FAIL  {ep['title']}: {exc}")
                continue
            found += n
            print(f"  ok    {ep['title']}: {n} ad span(s) from chapters")
    print(f"found {found} chapter-sourced ad span(s) across {len(episodes)} episode(s)")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    pending = transcribe.pending_episodes(conn, args.limit)
    if args.dry_run:
        total_pending = len(transcribe.pending_episodes(conn))
        print(f"pending episodes: {total_pending}"
              + (f" (would process {len(pending)} this run)" if args.limit else ""))
        return 0
    if not pending:
        print("no episodes pending transcription", file=sys.stderr)
        return 1

    errors = 0
    with make_client() as client:
        for ep in pending:
            try:
                path = transcribe.transcribe_episode(
                    conn, ep, client, model_size=args.model
                )
            except (httpx.HTTPError, OSError) as exc:
                errors += 1
                print(f"  FAIL  {ep['title']}: {exc}")
                continue
            print(f"  ok    {ep['title']} -> {path}")
    remaining = len(transcribe.pending_episodes(conn))
    print(f"transcribed {len(pending) - errors} episode(s) ({errors} failed, {remaining} still pending)")
    return 1 if errors else 0


def cmd_detect(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    pending = detect.pending_episodes(conn, args.limit)
    if args.dry_run:
        total_pending = len(detect.pending_episodes(conn))
        print(f"pending episodes: {total_pending}"
              + (f" (would process {len(pending)} this run)" if args.limit else ""))
        return 0
    if not pending:
        print("no episodes pending ad-span detection", file=sys.stderr)
        return 1

    import anthropic  # deferred: other commands must work without a key

    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        print(f"anthropic client: {exc}", file=sys.stderr)
        print("hint: export ANTHROPIC_API_KEY first (it lives in rbw, not in a file)",
              file=sys.stderr)
        return 1

    detector = detect.ClaudeAdDetector(client, model=args.model)

    def report(r: detect.DetectResult) -> None:
        if r.error:
            print(f"  FAIL  {r.title}: {r.error}")
        else:
            print(f"  ok    {r.title}: {r.found} ad span(s) from transcript")

    results = detect.detect_pending(conn, detector, limit=args.limit, on_result=report)
    errors = sum(1 for r in results if r.error)
    remaining = len(detect.pending_episodes(conn))
    print(f"detected across {len(results) - errors} episode(s) ({errors} failed, {remaining} still pending)")
    return 1 if errors else 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    feeds = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
    episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    segments = conn.execute("SELECT COUNT(*) FROM ad_segments").fetchone()[0]
    by_source = conn.execute(
        "SELECT source, COUNT(*) AS n FROM ad_segments GROUP BY source"
    ).fetchall()
    print(f"feeds:       {feeds}")
    print(f"episodes:    {episodes}")
    print(f"ad_segments: {segments}")
    for row in by_source:
        print(f"  {row['source']:<10} {row['n']}")
    return 0


def _cmd_not_built_yet(name: str):
    def handler(args: argparse.Namespace) -> int:
        milestone = _NOT_BUILT_YET[name]
        print(f"`adscrub {name}` is not built yet ({milestone}) — see docs/PLAN.md",
              file=sys.stderr)
        return 1
    return handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adscrub", description="Self-hosted podcast ad-detection and removal proxy."
    )
    parser.add_argument("--version", action="version", version=f"adscrub {__version__}")
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help=f"SQLite database path (default: $ADSCRUB_DB or {DEFAULT_DB})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-feed", help="register a source podcast feed to proxy")
    p.add_argument("url", help="source feed URL")
    p.set_defaults(func=cmd_add_feed)

    p = sub.add_parser("ingest", help="fetch registered feeds and upsert episodes")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("chapters", help="scan episodes' existing chapter markers for ad spans")
    p.set_defaults(func=cmd_chapters)

    p = sub.add_parser(
        "transcribe", help="transcribe episodes with no chapter-sourced ad spans"
    )
    p.add_argument("--limit", type=int, help="max episodes to process this run")
    p.add_argument("--model", default=transcribe.DEFAULT_MODEL,
                   help=f"faster-whisper model size (default: $ADSCRUB_WHISPER_MODEL or "
                        f"{transcribe.DEFAULT_MODEL})")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many episodes are pending")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("detect", help="classify ad spans from transcripts with a Claude model")
    p.add_argument("--limit", type=int, help="max episodes to process this run")
    p.add_argument("--model", default=detect.DEFAULT_MODEL,
                   help=f"Claude model id (default: {detect.DEFAULT_MODEL})")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many episodes are pending")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("stats", help="print database counts")
    p.set_defaults(func=cmd_stats)

    for name in _NOT_BUILT_YET:
        p = sub.add_parser(name, help=f"not built yet ({_NOT_BUILT_YET[name]})")
        p.set_defaults(func=_cmd_not_built_yet(name))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
