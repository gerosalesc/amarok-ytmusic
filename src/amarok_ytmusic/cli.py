"""Command-line interface for amarok-ytmusic."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
import urllib.request

import uvicorn

from .auth import AuthError, AuthManager, detect_browsers
from .config import DEFAULT_HOST, DEFAULT_PORT, SettingsStore
from .metadata_store import MetadataStore
from .mpris_monitor import MPRISMonitor
from .server import create_app
from .track_importer import TrackImporter
from .ytmusic_client import YTMusicCatalogClient


def _open_browser_later(url: str, delay: float = 1.0) -> None:
    def _worker() -> None:
        time.sleep(delay)
        try:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    store = SettingsStore()
    settings = store.load()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    store.save(settings)

    app = create_app(store=store)
    url = settings.base_url
    logging.getLogger("amarok_ytmusic").info("Web Auth & Stream Bridge running at %s", url)

    if args.open_auth:
        _open_browser_later(url)

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="debug" if args.debug else "info")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    store = SettingsStore()
    auth = AuthManager(store)

    if args.browser:
        try:
            info = auth.authenticate_from_browser(
                args.browser,
                profile=args.profile,
                keyring=args.keyring,
                authuser=args.authuser,
            )
            print(f"Authenticated as: {info.get('accountName')} ({info.get('channelHandle', '')})")
            return 0
        except AuthError as exc:
            print(f"Authentication failed: {exc}", file=sys.stderr)
            return 1

    settings = store.load()
    url = settings.base_url
    print(f"Opening Web Auth UI at {url} ...")
    try:
        urllib.request.urlopen(f"{url}/api/status", timeout=1.5)
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    except Exception:
        print("Daemon is not currently running. Start it with: amarok-ytmusic serve --open-auth")
        return 1


def cmd_status(_args: argparse.Namespace) -> int:
    store = SettingsStore()
    settings = store.load()
    auth = AuthManager(store)
    print(f"Config dir : {store.config_dir}")
    print(f"Server URL : {settings.base_url}")
    print(f"Auth mode  : {settings.auth_mode} ({settings.browser_source or 'none'})")
    print(f"Quality    : {settings.audio_quality}")

    browsers = detect_browsers()
    if browsers:
        print("Browsers   : " + ", ".join(f"{b.id}{'*' if b.available else ''}" for b in browsers))

    if auth.is_authenticated():
        try:
            info = auth.verify_account()
            print(f"Account    : {info.get('accountName')} {info.get('channelHandle', '')}")
        except AuthError as exc:
            print(f"Account    : ERROR ({exc})")
    else:
        print("Account    : Not signed in")

    try:
        with urllib.request.urlopen(f"{settings.base_url}/api/status", timeout=1.5) as resp:
            print(f"Daemon     : RUNNING (HTTP {resp.status})")
    except Exception:
        print("Daemon     : STOPPED")
    return 0


def cmd_now(_args: argparse.Namespace) -> int:
    store = SettingsStore()
    auth = AuthManager(store)
    catalog = YTMusicCatalogClient(auth)
    monitor = MPRISMonitor(catalog, auth)
    snap = monitor.read_mpris_snapshot_sync()
    if not snap.amarok_running:
        print("Amarok is not currently running.")
        return 1
    print(f"Status   : {snap.playback_status}")
    print(f"Title    : {snap.title or '(none)'}")
    print(f"Album    : {snap.album or '(none)'}")
    if snap.is_ytmusic:
        print(f"Video ID : {snap.video_id}")
        print(f"Progress : {snap.position_sec:.1f}s / {snap.duration_sec:.1f}s")
        print(f"Rating   : {snap.user_rating:.2f}")
    return 0


def cmd_like(args: argparse.Namespace) -> int:
    store = SettingsStore()
    auth = AuthManager(store)
    catalog = YTMusicCatalogClient(auth)

    video_id = args.video_id
    if not video_id:
        monitor = MPRISMonitor(catalog, auth)
        snap = monitor.read_mpris_snapshot_sync()
        if not snap.is_ytmusic or not snap.video_id:
            print("No active YouTube Music track playing in Amarok. Pass a video_id explicitly.", file=sys.stderr)
            return 1
        video_id = snap.video_id

    rating = "DISLIKE" if args.dislike else ("INDIFFERENT" if args.clear else "LIKE")
    res = catalog.rate_track(video_id, rating)
    print(f"Rated YouTube Music track {res['videoId']} -> {res['rating']}")
    return 0


def cmd_lyrics(args: argparse.Namespace) -> int:
    store = SettingsStore()
    auth = AuthManager(store)
    catalog = YTMusicCatalogClient(auth)

    video_id = args.video_id
    title_hint = ""
    if not video_id:
        monitor = MPRISMonitor(catalog, auth)
        snap = monitor.read_mpris_snapshot_sync()
        if not snap.is_ytmusic or not snap.video_id:
            print("No active YouTube Music track playing in Amarok. Pass a video_id explicitly.", file=sys.stderr)
            return 1
        video_id = snap.video_id
        title_hint = snap.title

    data = catalog.get_track_lyrics(video_id)
    if title_hint:
        print(f"=== {title_hint} ({video_id}) ===\n")
    if not data.get("found"):
        print("No lyrics available on YouTube Music for this track.")
        return 1
    print(data["lyrics"])
    if data.get("source"):
        print(f"\n— {data['source']}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    store = SettingsStore()
    auth = AuthManager(store)
    meta_store = MetadataStore(store.metadata_db_path)
    importer = TrackImporter(auth, meta_store)
    catalog = YTMusicCatalogClient(auth)

    video_id = args.video_id
    if not video_id:
        monitor = MPRISMonitor(catalog, auth, metadata_store=meta_store, importer=importer)
        snap = monitor.read_mpris_snapshot_sync()
        if not snap.is_ytmusic or not snap.video_id:
            print("No active YouTube Music track playing in Amarok. Pass a video_id explicitly.", file=sys.stderr)
            return 1
        video_id = snap.video_id

    if args.metadata_only:
        track = importer.fetch_and_cache_metadata_sync(video_id, force_refresh=args.refresh)
    else:
        track = importer.import_to_local_collection_sync(video_id, force_refresh=args.refresh)

    print(f"Title    : {track.title}")
    print(f"Artist   : {track.artist}")
    print(f"Album    : {track.album} ({track.year or 'N/A'})")
    print(f"Liked    : {track.like_status or 'NONE'} (inLibrary={track.in_library})")
    print(f"Stats    : plays={track.play_count}, score={track.score}")
    print(f"Cover    : {track.cover_local_path or track.cover_url or '(none)'}")
    print(f"Lyrics   : {'yes (' + str(len(track.lyrics)) + ' chars)' if track.lyrics else 'no'}")
    print(f"Local    : {track.local_file_path or '(metadata only)'}")
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    store = SettingsStore()
    meta_store = MetadataStore(store.metadata_db_path)
    stats = meta_store.stats_summary()
    print(
        f"Cached tracks: {stats['totalTracks']} | In Local Collection: {stats['localFiles']} | "
        f"Liked: {stats['likedCached']} | Total plays: {stats['totalPlays']}"
    )
    tracks = meta_store.list_tracks(limit=args.limit)
    for t in tracks:
        like_s = "♥" if t.like_status == "LIKE" else " "
        local_s = "[local]" if t.local_file_path else "[meta] "
        print(f"  {like_s} {local_s} {t.video_id}  {t.title} — {t.artist} [{t.album}] (plays={t.play_count}, score={int(t.score)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="amarok-ytmusic",
        description="YouTube Music web-auth and streaming bridge for Amarok 3.3+ on KDE",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run the local Web Auth, IPC watcher, and streaming proxy daemon")
    p_serve.add_argument("--host", default=DEFAULT_HOST)
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--open-auth", action="store_true", help="Open the Web Auth UI in your browser on startup")
    p_serve.add_argument("--debug", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_auth = sub.add_parser("auth", help="Authenticate via Web Auth UI or directly from a local browser")
    p_auth.add_argument("--browser", help="Extract cookies directly from browser (chrome, chromium, firefox, brave...)")
    p_auth.add_argument("--profile", help="Optional browser profile name")
    p_auth.add_argument("--keyring", help="Optional Linux keyring (KWALLET5, KWALLET6, GNOMEKEYRING, BASICTEXT)")
    p_auth.add_argument("--authuser", default="0", help="Google account index (default: 0)")
    p_auth.set_defaults(func=cmd_auth)

    p_status = sub.add_parser("status", help="Show daemon and YouTube Music authentication status")
    p_status.set_defaults(func=cmd_status)

    p_now = sub.add_parser("now", help="Show the currently playing YouTube Music track in Amarok via MPRIS2")
    p_now.set_defaults(func=cmd_now)

    p_like = sub.add_parser("like", help="Like, dislike, or clear rating for the current Amarok track on YouTube Music")
    p_like.add_argument("video_id", nargs="?", help="Optional video ID (defaults to currently playing Amarok track)")
    p_like.add_argument("--dislike", action="store_true", help="Dislike the track instead of liking")
    p_like.add_argument("--clear", action="store_true", help="Reset rating to indifferent")
    p_like.set_defaults(func=cmd_like)

    p_lyrics = sub.add_parser("lyrics", help="Fetch lyrics for the current Amarok track from YouTube Music")
    p_lyrics.add_argument("video_id", nargs="?", help="Optional video ID (defaults to currently playing Amarok track)")
    p_lyrics.set_defaults(func=cmd_lyrics)

    p_import = sub.add_parser("import", help="Import full metadata, cover art, lyrics, and tagged M4A to Amarok Local Collection")
    p_import.add_argument("video_id", nargs="?", help="Optional video ID (defaults to currently playing Amarok track)")
    p_import.add_argument("--metadata-only", action="store_true", help="Only cache SQLite metadata & cover art (skip M4A download)")
    p_import.add_argument("--refresh", action="store_true", help="Force re-fetching metadata from YouTube Music")
    p_import.set_defaults(func=cmd_import)

    p_cache = sub.add_parser("cache", help="List auto-imported & cached YouTube Music tracks and playback stats")
    p_cache.add_argument("--limit", type=int, default=30, help="Maximum tracks to display")
    p_cache.set_defaults(func=cmd_cache)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
