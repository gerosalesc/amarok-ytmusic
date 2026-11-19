"""Atomic file/directory IPC watcher bridging Amarok 3.3 QJSEngine and the local Python backend.

Amarok 3.3's QJSEngine lacks working HTTP/network bindings (qt.network is unimplemented and
AmarokNetworkScript's Downloader has upstream typos), while QDir/QFile/QTextStream in qt.core
work reliably. This watcher uses atomic POSIX primitives:

  Request  (Amarok JS -> Python): QDir.mkdir("<ipc_dir>/req/r.<req_id>.<op>.<b64url_payload>")
  Response (Python -> Amarok JS): atomic rename to "<ipc_dir>/resp/<req_id>.json"
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .auth import AuthManager
from .config import get_ipc_dir
from .ytmusic_client import YTMusicCatalogClient

log = logging.getLogger("amarok_ytmusic.ipc")

POLL_INTERVAL_SECONDS = 0.04
STALE_RESP_SECONDS = 60.0


def encode_ipc_request(req_id: str, op: str, payload: str = "") -> str:
    """Encode an IPC request into a collision-free POSIX directory name."""
    raw = payload.encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"r.{req_id}.{op}.{b64}"


def decode_ipc_request(dirname: str) -> tuple[str, str, str] | None:
    """Decode a directory name into (req_id, op, payload_str), or None if unrecognized."""
    parts = dirname.split(".", 3)
    if len(parts) != 4 or parts[0] != "r":
        return None
    _, req_id, op, b64 = parts
    if not req_id or not op:
        return None
    if not b64:
        return req_id, op, ""
    pad = "=" * ((4 - len(b64) % 4) % 4)
    try:
        payload = base64.urlsafe_b64decode((b64 + pad).encode("ascii")).decode("utf-8")
    except Exception:
        return None
    return req_id, op, payload


class IPCWatcher:
    """Async background task that services Amarok ScriptableService IPC requests."""

    def __init__(
        self,
        catalog: YTMusicCatalogClient,
        auth_manager: AuthManager,
        ipc_dir: Path | None = None,
    ) -> None:
        self.catalog = catalog
        self.auth = auth_manager
        self.ipc_dir = ipc_dir or get_ipc_dir()
        self.req_dir = self.ipc_dir / "req"
        self.resp_dir = self.ipc_dir / "resp"
        self.req_dir.mkdir(parents=True, exist_ok=True)
        self.resp_dir.mkdir(parents=True, exist_ok=True)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._inflight: set[str] = set()
        self._last_cleanup = 0.0

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="amarok-ytmusic-ipc")
        log.info("IPC watcher listening on %s", self.ipc_dir)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                entries = list(self.req_dir.iterdir())
                for entry in entries:
                    name = entry.name
                    if name in self._inflight:
                        continue
                    parsed = decode_ipc_request(name)
                    if parsed is None:
                        continue
                    self._inflight.add(name)
                    # Remove request directory immediately so it is processed exactly once
                    try:
                        if entry.is_dir():
                            entry.rmdir()
                        else:
                            entry.unlink(missing_ok=True)
                    except OSError:
                        shutil.rmtree(entry, ignore_errors=True)
                    asyncio.create_task(self._handle_request(name, *parsed))

                now = time.time()
                if now - self._last_cleanup > 30.0:
                    self._last_cleanup = now
                    self._cleanup_stale_responses(now)
            except Exception as exc:
                log.exception("IPC watcher iteration failed: %s", exc)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _cleanup_stale_responses(self, now: float) -> None:
        for p in self.resp_dir.iterdir():
            try:
                if now - p.stat().st_mtime > STALE_RESP_SECONDS:
                    p.unlink(missing_ok=True)
            except OSError:
                pass

    async def _handle_request(self, raw_name: str, req_id: str, op: str, payload: str) -> None:
        try:
            response = await asyncio.to_thread(self.dispatch_sync, op, payload)
        except Exception as exc:
            log.exception("IPC op=%s failed: %s", op, exc)
            response = {"ok": False, "error": str(exc)}
        finally:
            self._inflight.discard(raw_name)

        self._write_response(req_id, response)

    def dispatch_sync(self, op: str, payload: str) -> dict[str, Any]:
        settings = self.auth.get_settings()
        if op == "ping":
            return {
                "ok": True,
                "authenticated": self.auth.is_authenticated(),
                "authMode": settings.auth_mode,
                "baseUrl": settings.base_url,
            }
        if op == "folders":
            items = self.catalog.get_root_folders(query=payload)
            return {
                "ok": True,
                "authenticated": self.auth.is_authenticated(),
                "baseUrl": settings.base_url,
                "items": items,
            }
        if op == "tracks":
            items = self.catalog.get_tracks_for_folder(callback_data=payload)
            return {
                "ok": True,
                "items": items,
            }
        if op == "refresh":
            self.catalog.clear_cache()
            return {"ok": True}
        if op == "open_auth":
            url = settings.base_url
            try:
                subprocess.Popen(
                    ["xdg-open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as exc:
                log.warning("Could not launch xdg-open for %s: %s", url, exc)
            return {"ok": True, "url": url}
        return {"ok": False, "error": f"Unknown IPC op: {op}"}

    def _write_response(self, req_id: str, data: dict[str, Any]) -> None:
        safe_id = "".join(ch for ch in req_id if ch.isalnum() or ch in ("_", "-"))
        target = self.resp_dir / f"{safe_id}.json"
        tmp = self.resp_dir / f".{safe_id}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
