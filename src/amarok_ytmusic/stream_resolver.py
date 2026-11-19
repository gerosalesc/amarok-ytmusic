"""Audio stream URL resolver backed by yt-dlp with TTL caching and cookie auth."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from .auth import AuthManager

log = logging.getLogger("amarok_ytmusic.stream")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
DEFAULT_STREAM_TTL = 2.5 * 3600  # YouTube signed URLs typically live ~6h; refresh after 2.5h
MAX_STREAM_CACHE_ENTRIES = 500
RESOLVE_TIMEOUT_SECONDS = 12.0  # Stay safely below GStreamer souphttpsrc's 15s default timeout


@dataclass
class ResolvedStream:
    video_id: str
    url: str
    content_type: str
    content_length: int | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    title: str = ""
    ext: str = ""
    abr: float | None = None
    expires_at: float = 0.0


class StreamResolutionError(RuntimeError):
    """Raised when a YouTube Music video_id cannot be resolved to an audio stream."""


def _guess_mime(ext: str, acodec: str) -> str:
    ext = (ext or "").lower()
    acodec = (acodec or "").lower()
    if ext == "webm" or "opus" in acodec:
        return "audio/webm"
    if ext in ("m4a", "mp4") or "mp4a" in acodec:
        return "audio/mp4"
    if ext == "ogg":
        return "audio/ogg"
    if ext == "mp3":
        return "audio/mpeg"
    return "application/octet-stream"


class StreamResolver:
    """Resolves YouTube Music video IDs to fresh upstream audio URLs via yt-dlp."""

    def __init__(self, auth_manager: AuthManager) -> None:
        self.auth = auth_manager
        self._cache: dict[str, ResolvedStream] = {}
        self._inflight: dict[str, asyncio.Task[ResolvedStream]] = {}

    def invalidate(self, video_id: str) -> None:
        self._cache.pop(video_id, None)

    def clear_cache(self) -> None:
        self._cache.clear()

    async def resolve(self, video_id: str, force_refresh: bool = False) -> ResolvedStream:
        if not VIDEO_ID_RE.match(video_id):
            raise StreamResolutionError(f"Invalid YouTube video ID: {video_id!r}")

        if not force_refresh:
            cached = self._cache.get(video_id)
            if cached and time.time() < cached.expires_at:
                return cached

        # Deduplicate concurrent HEAD + GET requests from GStreamer for the same track
        existing = self._inflight.get(video_id)
        if existing and not existing.done() and not force_refresh:
            return await existing

        task = asyncio.create_task(asyncio.to_thread(self._resolve_sync, video_id))
        self._inflight[video_id] = task
        try:
            resolved = await asyncio.wait_for(asyncio.shield(task), timeout=RESOLVE_TIMEOUT_SECONDS)
            if len(self._cache) >= MAX_STREAM_CACHE_ENTRIES:
                now = time.time()
                expired = [k for k, v in self._cache.items() if now >= v.expires_at]
                for k in expired:
                    self._cache.pop(k, None)
                if len(self._cache) >= MAX_STREAM_CACHE_ENTRIES:
                    oldest = min(self._cache, key=lambda k: self._cache[k].expires_at)
                    self._cache.pop(oldest, None)
            self._cache[video_id] = resolved
            return resolved
        except asyncio.TimeoutError as exc:
            raise StreamResolutionError(
                f"Timed out resolving YouTube Music stream for {video_id} after {RESOLVE_TIMEOUT_SECONDS:.0f}s"
            ) from exc
        finally:
            if self._inflight.get(video_id) is task:
                self._inflight.pop(video_id, None)

    @staticmethod
    def _has_js_runtime() -> bool:
        return any(shutil.which(rt) is not None for rt in ("deno", "node", "bun", "qjs"))

    def _extract_with_opts(self, watch_url: str, base_opts: dict[str, Any], use_cookies: bool) -> ResolvedStream:
        import yt_dlp

        opts = dict(base_opts)
        cookies_path = self.auth.store.cookies_txt_path
        if use_cookies and cookies_path.exists():
            opts["cookiefile"] = str(cookies_path)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(watch_url, download=False)

        if not isinstance(info, dict):
            raise StreamResolutionError("Empty metadata returned")

        stream_url = info.get("url")
        chosen = info
        if not stream_url and info.get("requested_formats"):
            audio_fmt = next(
                (f for f in info["requested_formats"] if f.get("acodec") not in (None, "none")),
                info["requested_formats"][0],
            )
            chosen = audio_fmt
            stream_url = audio_fmt.get("url")

        ext = str(chosen.get("ext") or "")
        acodec = str(chosen.get("acodec") or "")
        if not stream_url or ext.lower() == "mhtml" or acodec.lower() == "none":
            raise StreamResolutionError("No playable audio format found")

        headers = dict(chosen.get("http_headers") or info.get("http_headers") or {})
        filesize = chosen.get("filesize") or chosen.get("filesize_approx")

        return ResolvedStream(
            video_id=str(info.get("id") or ""),
            url=str(stream_url),
            content_type=_guess_mime(ext, acodec),
            content_length=int(filesize) if filesize else None,
            http_headers=headers,
            title=str(info.get("title") or ""),
            ext=ext,
            abr=float(chosen["abr"]) if chosen.get("abr") is not None else None,
            expires_at=time.time() + DEFAULT_STREAM_TTL,
        )

    def _resolve_sync(self, video_id: str) -> ResolvedStream:
        settings = self.auth.get_settings()
        cookies_path = self.auth.store.cookies_txt_path

        base_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "format": settings.ytdlp_format_selector(),
            "extract_flat": False,
            "socket_timeout": 8,
            "retries": 1,
        }

        watch_url = f"https://music.youtube.com/watch?v={video_id}"
        has_cookies = cookies_path.exists()
        has_js = self._has_js_runtime()

        # yt-dlp's authenticated web clients require a JS runtime (deno/node/bun) to solve YouTube's
        # signature/n challenges; without one, cookie-less extraction (android_vr etc.) resolves audio cleanly.
        if has_cookies and has_js:
            attempts = [True, False]
        elif has_cookies:
            attempts = [False, True]
        else:
            attempts = [False]

        log.info(
            "Resolving YouTube Music audio stream for %s (quality=%s, js_runtime=%s)",
            video_id,
            settings.audio_quality,
            has_js,
        )

        last_err: Exception | None = None
        for use_cookies in attempts:
            try:
                resolved = self._extract_with_opts(watch_url, base_opts, use_cookies=use_cookies)
                resolved.video_id = video_id
                return resolved
            except Exception as exc:
                last_err = exc
                log.warning(
                    "yt-dlp resolution failed for %s (cookies=%s): %s",
                    video_id,
                    use_cookies,
                    exc,
                )

        raise StreamResolutionError(f"yt-dlp failed for {video_id}: {last_err}") from last_err
