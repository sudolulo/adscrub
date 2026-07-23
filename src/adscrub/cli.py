"""adscrub command line: add-feed, ingest, chapters, transcribe, repeats, fingerprint, detect, cut, serve, stats."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from . import __version__, chapters, cut, db, detect, feed, fingerprint, ingest, repeats, transcribe

DEFAULT_DB = os.environ.get("ADSCRUB_DB", "adscrub.db")
USER_AGENT = f"adscrub/{__version__} (homelab podcast ad-removal proxy)"


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


def cmd_repeats(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    library = repeats.build_library(conn)
    if not library:
        print("no confirmed ad spans anywhere yet — the LLM (or chapters) has to go first",
              file=sys.stderr)
        return 1

    def report(r: repeats.RepeatResult) -> None:
        if r.error:
            print(f"  FAIL  {r.title}: {r.error}", file=sys.stderr)
        elif r.found:
            print(f"  ok    {r.title}: {r.found} repeated ad span(s)")

    results = repeats.apply_repeats(conn, limit=args.limit, on_result=report)
    errors = sum(1 for r in results if r.error)
    found = sum(r.found for r in results)
    hit = sum(1 for r in results if r.found)
    print(f"matched {found} repeated ad span(s) across {hit} of {len(results)} episode(s) "
          f"({errors} failed) — {len(library):,} known ad shingles, no model called")
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    if not fingerprint.fpcalc_available():
        print("fpcalc (Chromaprint / libchromaprint-tools) is not installed", file=sys.stderr)
        return 1
    data_dir = Path(args.data_dir)

    def progress(n: int, total: int) -> None:
        if n == 1 or n % 25 == 0 or n == total:
            print(f"  ..    building library: fingerprinted {n}/{total} confirmed ad read(s)",
                  file=sys.stderr)

    library = fingerprint.build_library(conn, data_dir, on_progress=progress)
    if not library:
        print("no confirmed ad recordings yet — chapters/transcribe+LLM (or dai) has to go first",
              file=sys.stderr)
        return 1

    def report(r: fingerprint.FingerprintResult) -> None:
        if r.error:
            print(f"  FAIL  {r.title}: {r.error}", file=sys.stderr)
        elif r.found:
            print(f"  ok    {r.title}: {r.found} fingerprinted ad span(s)")

    with make_client() as client:
        results = fingerprint.apply_fingerprints(
            conn, client, data_dir=data_dir, limit=args.limit, on_result=report
        )
    errors = sum(1 for r in results if r.error)
    found = sum(r.found for r in results)
    hit = sum(1 for r in results if r.found)
    print(f"matched {found} ad span(s) across {hit} of {len(results)} episode(s) "
          f"({errors} failed) — {library.n_episodes} source episode(s) in the library, "
          f"no transcript or model")
    return 0


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

    # Cheapest tier first, always — the model should never be asked to re-read an ad read
    # the corpus already knows. Composing rather than branching: if the library is empty
    # (nothing confirmed yet) this is just the Claude detector, with no special case.
    library = repeats.build_library(conn)
    tiers: list[detect.AdSpanDetector] = []
    if library:
        tiers.append(repeats.RepeatAdDetector(library))
        print(f"  ..    {len(library):,} known ad shingles in front of the model", file=sys.stderr)
    tiers.append(detect.ClaudeAdDetector(client, model=args.model))
    detector = detect.LayeredDetector(tiers)

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


def cmd_cut(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    pending = cut.pending_episodes(conn, args.limit)
    if args.dry_run:
        total_pending = len(cut.pending_episodes(conn))
        print(f"pending episodes: {total_pending}"
              + (f" (would process {len(pending)} this run)" if args.limit else ""))
        return 0
    if not pending:
        print("no episodes pending cutting", file=sys.stderr)
        return 1

    def report(r: cut.CutResult) -> None:
        if r.error:
            print(f"  FAIL  {r.title}: {r.error}")
        else:
            print(f"  ok    {r.title}: removed {r.ad_seconds:.1f}s of ads")

    with make_client() as client:
        results = cut.cut_pending(conn, client, limit=args.limit, on_result=report)
    errors = sum(1 for r in results if r.error)
    remaining = len(cut.pending_episodes(conn))
    print(f"cut {len(results) - errors} episode(s) ({errors} failed, {remaining} still pending)")
    return 1 if errors else 0


def cmd_serve(args: argparse.Namespace) -> int:
    feed.serve(args.db, args.data_dir, args.base_url, args.bind)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    feeds = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
    episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    segments = conn.execute("SELECT COUNT(*) FROM ad_segments").fetchone()[0]
    cut_count = conn.execute("SELECT COUNT(*) FROM episodes WHERE cut_path IS NOT NULL").fetchone()[0]
    by_source = conn.execute(
        "SELECT source, COUNT(*) AS n FROM ad_segments GROUP BY source"
    ).fetchall()
    print(f"feeds:       {feeds}")
    print(f"episodes:    {episodes}")
    print(f"ad_segments: {segments}")
    for row in by_source:
        print(f"  {row['source']:<10} {row['n']}")
    print(f"cut:         {cut_count}")
    return 0


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

    p = sub.add_parser(
        "repeats",
        help="match transcripts against ad reads already confirmed elsewhere (free, no model)")
    p.add_argument("--limit", type=int,
                   help="scan only the first N episodes by id — ad-hoc/testing only. There is no pending-queue (re-scanning is free and the library grows), so the pipeline runs this UNBOUNDED; a limit in a loop would rescan the same head forever and never reach the tail.")
    p.set_defaults(func=cmd_repeats)

    p = sub.add_parser(
        "fingerprint",
        help="match episode AUDIO against confirmed ad recordings (free, no transcript or model)")
    p.add_argument("--limit", type=int,
                   help="scan only the first N episodes by id — ad-hoc/testing only. Like "
                        "repeats there is no pending-queue (re-scanning is free, the library "
                        "grows), so the pipeline runs this UNBOUNDED.")
    p.add_argument("--data-dir", default=os.environ.get("ADSCRUB_DATA_DIR", "data"),
                   help="directory holding audio/ (default: $ADSCRUB_DATA_DIR or data)")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser("detect", help="classify ad spans from transcripts with a Claude model")
    p.add_argument("--limit", type=int, help="max episodes to process this run")
    p.add_argument("--model", default=detect.DEFAULT_MODEL,
                   help=f"Claude model id (default: {detect.DEFAULT_MODEL})")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many episodes are pending")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("cut", help="cut ad spans out of episode audio with ffmpeg")
    p.add_argument("--limit", type=int, help="max episodes to process this run")
    p.add_argument("--dry-run", action="store_true",
                   help="only report how many episodes are pending")
    p.set_defaults(func=cmd_cut)

    p = sub.add_parser("serve", help="serve the cleaned feed(s) + cut audio over HTTP")
    p.add_argument("--bind", default=os.environ.get("ADSCRUB_BIND", "0.0.0.0:8711"),
                   help="host:port (default: $ADSCRUB_BIND or 0.0.0.0:8711)")
    p.add_argument("--base-url", default=os.environ.get("ADSCRUB_BASE_URL", "http://localhost:8711"),
                   help="externally-reachable URL this server is served at — embedded in "
                        "generated feeds' audio links, so it must resolve from wherever the "
                        "podcast player runs, not just from this host "
                        "(default: $ADSCRUB_BASE_URL or http://localhost:8711)")
    p.add_argument("--data-dir", default=os.environ.get("ADSCRUB_DATA_DIR", "data"),
                   help="directory holding cut/ audio (default: $ADSCRUB_DATA_DIR or data)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("stats", help="print database counts")
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
