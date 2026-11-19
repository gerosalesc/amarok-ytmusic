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
from .server import create_app


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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
