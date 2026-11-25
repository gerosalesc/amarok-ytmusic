"""Command-line interface for amarok-ytmusic."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
import urllib.request

from pathlib import Path

import uvicorn

from .auth import AuthError, AuthManager, detect_browsers
from .config import DEFAULT_HOST, DEFAULT_PORT, SettingsStore, configure_ipv4_preference, validate_cache_dir
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

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="debug" if args.debug else "info",
        timeout_graceful_shutdown=3,
    )
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


def _format_size_mb(size_bytes: int) -> str:
    mb = size_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


def cmd_status(args: argparse.Namespace) -> int:
    store = SettingsStore()
    settings = store.load()
    auth = AuthManager(store)
    meta_store = MetadataStore(store.metadata_db_path)
    disk = meta_store.disk_stats()
    cap_label = "unlimited" if settings.max_collection_cache_mb <= 0 else f"{settings.max_collection_cache_mb} MB"
    print(f"Config dir : {store.config_dir}")
    print(f"Server URL : {settings.base_url}")
    print(f"Auth mode  : {settings.auth_mode} ({settings.browser_source or 'none'})")
    print(f"Quality    : {settings.audio_quality}")
    print(f"Cache dir  : {settings.resolved_collection_dir()}")
    print(f"Auto-cache : {'enabled' if settings.auto_import_collection else 'disabled'} (limit: {cap_label})")
    print(
        f"Offline    : {disk['offlineFiles']} songs cached ({_format_size_mb(disk['diskUsageBytes'])})"
        + (f", {disk['missingFiles']} missing audio" if disk["missingFiles"] else "")
    )

    browsers = detect_browsers()
    if browsers:
        print("Browsers   : " + ", ".join(f"{b.id}{'*' if b.available else ''}" for b in browsers))

    if auth.is_authenticated():
        cached_prof = None if getattr(args, "verify", False) else meta_store.get_user_profile(max_age=0)
        if cached_prof is not None:
            print(f"Account    : {cached_prof.account_name} {cached_prof.channel_handle}".rstrip())
        else:
            try:
                info = auth.verify_account()
                print(f"Account    : {info.get('accountName')} {info.get('channelHandle', '')}".rstrip())
            except AuthError as exc:
                print(f"Account    : ERROR ({exc})")
    else:
        print("Account    : Not signed in")

    try:
        with urllib.request.urlopen(f"{settings.base_url}/api/status", timeout=2.5) as resp:
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
    settings = store.load()
    auth = AuthManager(store)
    meta_store = MetadataStore(store.metadata_db_path)
    importer = TrackImporter(auth, meta_store)

    settings_changed = False
    old_dir = settings.resolved_collection_dir()

    if args.enable and args.disable:
        print("Error: cannot pass both --enable and --disable.", file=sys.stderr)
        return 1
    if args.enable:
        settings.auto_import_collection = True
        settings_changed = True
    elif args.disable:
        settings.auto_import_collection = False
        settings_changed = True

    if args.limit_mb is not None:
        settings.max_collection_cache_mb = max(0, int(args.limit_mb))
        settings_changed = True

    if args.set_dir is not None:
        raw_dir = args.set_dir.strip()
        settings.collection_dir = raw_dir
        target_dir = settings.resolved_collection_dir()
        try:
            validate_cache_dir(target_dir)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        settings_changed = True

    if settings_changed:
        store.save(settings)
        new_dir = settings.resolved_collection_dir()
        cap_label = "unlimited" if settings.max_collection_cache_mb <= 0 else f"{settings.max_collection_cache_mb} MB"
        print(
            f"Updated offline cache settings -> dir={new_dir}, "
            f"auto_cache={'enabled' if settings.auto_import_collection else 'disabled'}, limit={cap_label}"
        )
        if args.move and args.set_dir is not None and old_dir.resolve(strict=False) != new_dir.resolve(strict=False):
            mig = importer.migrate_cache_dir_sync(old_dir, new_dir)
            print(
                f"Migrated cached files: moved={mig['moved']}, skipped={mig['skipped']}, errors={mig['errors']} "
                f"({mig['oldDir']} -> {mig['newDir']})"
            )

    if args.scan:
        scan = importer.scan_cache_dir_sync()
        print(
            f"Scanned {scan['cacheDir']}: {scan['scannedFiles']} audio files, "
            f"{scan['indexedTracks']} indexed, {scan['prunedMissing']} stale removed "
            f"({_format_size_mb(scan['totalBytes'])})"
        )

    if args.sync_missing:
        sync = importer.sync_missing_tracks_sync(limit=args.limit if args.limit > 0 else 50)
        print(
            f"Synced missing tracks: cached={sync['cached']}/{sync['attempted']}, failed={sync['failed']}"
        )
        for err in sync["errors"]:
            print(f"  ! {err['videoId']} ({err['title']}): {err['error']}", file=sys.stderr)

    # If user only performed mutating actions without asking to list, still show concise summary
    stats = meta_store.disk_stats()
    cap_label = "unlimited" if settings.max_collection_cache_mb <= 0 else f"{settings.max_collection_cache_mb} MB"
    print(
        f"Cache folder : {settings.resolved_collection_dir()} "
        f"(auto-cache={'on' if settings.auto_import_collection else 'off'}, limit={cap_label})"
    )
    print(
        f"Cached tracks: {stats['totalTracks']} | Offline audio: {stats['offlineFiles']} "
        f"({_format_size_mb(stats['diskUsageBytes'])}) | Missing: {stats['missingFiles']} | "
        f"Liked: {stats['likedCached']} | Total plays: {stats['totalPlays']}"
    )

    if not (args.set_dir is not None or args.limit_mb is not None or args.enable or args.disable or args.scan or args.sync_missing) or args.search or args.offline_only:
        tracks = meta_store.list_tracks(limit=args.limit, query=args.search or "", offline_only=args.offline_only)
        for t in tracks:
            like_s = "♥" if t.like_status == "LIKE" else " "
            fsize = 0
            has_file = False
            if t.local_file_path:
                try:
                    st = Path(t.local_file_path).stat()
                    if st.st_size > 1000:
                        has_file = True
                        fsize = st.st_size
                except OSError:
                    pass
            local_s = f"[offline {fsize / (1024 * 1024):4.1f}M]" if has_file else "[meta only    ]"
            print(
                f"  {like_s} {local_s} {t.video_id}  {t.title} — {t.artist} [{t.album}] "
                f"(plays={t.play_count}, score={int(t.score)})"
            )
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
    p_status.add_argument("--verify", action="store_true", help="Verify account credentials live with YouTube Music")
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

    p_cache = sub.add_parser("cache", help="Configure, scan, migrate, and list the offline song cache")
    p_cache.add_argument("--set-dir", help="Set the configurable offline cache folder path (use '' to reset to default)")
    p_cache.add_argument("--move", action="store_true", help="Move existing cached audio files when changing --set-dir")
    p_cache.add_argument("--limit-mb", type=int, help="Set max cache size in MB (0 = unlimited / keep every song)")
    p_cache.add_argument("--enable", action="store_true", help="Enable automatic offline caching of every listened song")
    p_cache.add_argument("--disable", action="store_true", help="Disable automatic offline caching")
    p_cache.add_argument("--scan", action="store_true", help="Scan & index cached audio files in the configured folder")
    p_cache.add_argument("--sync-missing", action="store_true", help="Download audio for previously played tracks missing local files")
    p_cache.add_argument("--offline-only", action="store_true", help="Only list tracks currently available offline")
    p_cache.add_argument("--search", help="Filter cached tracks by title, artist, album, or video ID")
    p_cache.add_argument("--limit", type=int, default=30, help="Maximum tracks to display or sync")
    p_cache.set_defaults(func=cmd_cache)

    args = parser.parse_args(argv)
    try:
        configure_ipv4_preference(SettingsStore().load().use_ipv4_only())
    except Exception:
        pass
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
