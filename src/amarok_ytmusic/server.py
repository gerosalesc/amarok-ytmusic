"""FastAPI server providing Web Auth UI, catalog API, IPC watcher lifecycle, and audio stream proxy."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .ampache_bridge import AmpacheBridge
from .auth import AuthError, AuthManager, detect_browsers
from .config import SettingsStore, get_ipc_dir
from .ipc_watcher import IPCWatcher
from .metadata_store import CachedUserProfile, MetadataStore
from .mpris_monitor import ALLOWED_CONTROLS, MPRISMonitor
from .stream_resolver import StreamResolutionError, StreamResolver
from .track_importer import TrackImporter
from .ytmusic_client import YTMusicCatalogClient

log = logging.getLogger("amarok_ytmusic.server")

WEB_DIR = Path(__file__).parent / "web"
CHUNK_SIZE = 65536


class BrowserExtractRequest(BaseModel):
    browser: str
    profile: str | None = None
    keyring: str | None = None
    authuser: str = "0"


class HeadersAuthRequest(BaseModel):
    raw_headers: str


class OAuthStartRequest(BaseModel):
    client_id: str
    client_secret: str


class SettingsUpdateRequest(BaseModel):
    audio_quality: str | None = None
    scrobble_history: bool | None = None
    auto_import_metadata: bool | None = None
    auto_import_collection: bool | None = None
    collection_dir: str | None = None
    max_collection_cache_mb: int | None = None


class RateTrackRequest(BaseModel):
    rating: str = "LIKE"


class MPRISControlRequest(BaseModel):
    command: str


def _serve_local_file_with_range(file_path: Path, request: Request) -> Response:
    """Serve a locally cached audio file with HTTP Range (206 Partial Content) support."""
    file_size = file_path.stat().st_size
    media_type = mimetypes.guess_type(str(file_path))[0] or "audio/mp4"
    range_header = request.headers.get("range")

    start = 0
    end = file_size - 1
    status_code = 200
    headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }

    if range_header and range_header.startswith("bytes="):
        spec = range_header.replace("bytes=", "", 1).split(",")[0].strip()
        if "-" in spec:
            start_s, end_s = spec.split("-", 1)
            if start_s:
                start = int(start_s)
                end = int(end_s) if end_s else file_size - 1
            elif end_s:
                suffix = int(end_s)
                start = max(0, file_size - suffix)
                end = file_size - 1
            if start >= file_size or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
            end = min(end, file_size - 1)
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    length = end - start + 1
    headers["Content-Length"] = str(length)

    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers, media_type=media_type)

    def file_iter():
        with file_path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(file_iter(), status_code=status_code, headers=headers, media_type=media_type)


def create_app(
    store: SettingsStore | None = None,
    ipc_dir: Path | None = None,
    enable_ipc: bool = True,
    enable_mpris: bool = True,
) -> FastAPI:
    settings_store = store or SettingsStore()
    auth_manager = AuthManager(settings_store)
    metadata_store = MetadataStore(settings_store.metadata_db_path)
    importer = TrackImporter(auth_manager, metadata_store)
    catalog = YTMusicCatalogClient(auth_manager)
    resolver = StreamResolver(auth_manager)
    ampache = AmpacheBridge(
        catalog,
        auth_manager,
        state_path=settings_store.ampache_state_path,
        metadata_store=metadata_store,
    )
    watcher = IPCWatcher(catalog, auth_manager, ipc_dir=ipc_dir or get_ipc_dir()) if enable_ipc else None
    mpris = MPRISMonitor(catalog, auth_manager, metadata_store=metadata_store, importer=importer)

    http_client: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal http_client
        http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0),
        )
        if watcher:
            watcher.start()
        if enable_mpris:
            mpris.start()
        try:
            yield
        finally:
            if enable_mpris:
                await mpris.stop()
            if watcher:
                await watcher.stop()
            if http_client:
                await http_client.aclose()
                http_client = None

    app = FastAPI(title="Amarok YouTube Music Bridge", lifespan=lifespan)
    # Expose components on app.state for testing and introspection
    app.state.auth = auth_manager
    app.state.metadata_store = metadata_store
    app.state.importer = importer
    app.state.catalog = catalog
    app.state.resolver = resolver
    app.state.ampache = ampache
    app.state.watcher = watcher
    app.state.mpris = mpris

    @app.api_route("/ampache/server/xml.server.php", methods=["GET", "POST"])
    @app.api_route("/server/xml.server.php", methods=["GET", "POST"])
    async def ampache_xml_api(
        action: str = Query(default=""),
        filter: str = Query(default=""),
        limit: int = Query(default=0),
        id: int = Query(default=0),
        rating: int = Query(default=-1),
    ) -> Response:
        xml_body = await asyncio.to_thread(ampache.handle_action, action, filter, limit, id, rating)
        return Response(content=xml_body, media_type="application/xml; charset=utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html_path = WEB_DIR / "index.html"
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    def _cache_profile_from_info(info: dict[str, Any], auth_mode: str) -> None:
        if info and info.get("authenticated"):
            metadata_store.save_user_profile(
                CachedUserProfile(
                    account_name=str(info.get("accountName") or ""),
                    channel_handle=str(info.get("channelHandle") or ""),
                    account_photo_url=str(info.get("accountPhotoUrl") or ""),
                    auth_mode=auth_mode,
                    updated_at=time.time(),
                )
            )

    @app.get("/api/status")
    async def api_status(verify: bool = False) -> dict[str, Any]:
        settings = settings_store.load()
        authenticated = auth_manager.is_authenticated()
        account: dict[str, Any] | None = None
        verify_error: str | None = None
        if authenticated:
            cached_prof = None if verify else metadata_store.get_user_profile()
            if cached_prof is not None:
                account = cached_prof.to_dict()
            else:
                try:
                    account = await asyncio.to_thread(auth_manager.verify_account)
                    _cache_profile_from_info(account, settings.auth_mode)
                except AuthError as exc:
                    verify_error = str(exc)

        return {
            "authenticated": authenticated,
            "authMode": settings.auth_mode,
            "browserSource": settings.browser_source,
            "audioQuality": settings.audio_quality,
            "scrobbleHistory": settings.scrobble_history,
            "autoImportMetadata": settings.auto_import_metadata,
            "autoImportCollection": settings.auto_import_collection,
            "collectionDir": str(settings.resolved_collection_dir()),
            "maxCollectionCacheMb": settings.max_collection_cache_mb,
            "baseUrl": settings.base_url,
            "account": account,
            "verifyError": verify_error,
            "browsers": [asdict(b) for b in detect_browsers()],
            "nowPlaying": mpris.state.to_dict(),
            "cacheStats": metadata_store.stats_summary(),
        }

    @app.post("/api/auth/browser-extract")
    async def api_auth_browser(req: BrowserExtractRequest) -> dict[str, Any]:
        try:
            info = await asyncio.to_thread(
                auth_manager.authenticate_from_browser,
                req.browser,
                req.profile,
                req.keyring,
                req.authuser,
            )
            _cache_profile_from_info(info, "browser")
            catalog.clear_cache()
            resolver.clear_cache()
            return info
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/headers")
    async def api_auth_headers(req: HeadersAuthRequest) -> dict[str, Any]:
        try:
            info = await asyncio.to_thread(auth_manager.authenticate_from_raw_headers, req.raw_headers)
            _cache_profile_from_info(info, "browser")
            catalog.clear_cache()
            resolver.clear_cache()
            return info
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/oauth/start")
    async def api_oauth_start(req: OAuthStartRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                auth_manager.start_oauth_device_flow, req.client_id, req.client_secret
            )
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/oauth/complete")
    async def api_oauth_complete() -> dict[str, Any]:
        try:
            info = await asyncio.to_thread(auth_manager.complete_oauth_device_flow)
            _cache_profile_from_info(info, "oauth")
            catalog.clear_cache()
            resolver.clear_cache()
            return info
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/logout")
    async def api_logout() -> dict[str, Any]:
        auth_manager.logout()
        metadata_store.clear_user_profile()
        catalog.clear_cache()
        resolver.clear_cache()
        return {"ok": True}

    @app.post("/api/settings")
    async def api_settings(req: SettingsUpdateRequest) -> dict[str, Any]:
        settings = settings_store.load()
        if req.audio_quality is not None:
            if req.audio_quality not in ("best", "opus", "m4a", "medium"):
                raise HTTPException(status_code=400, detail="Invalid audio_quality option")
            settings.audio_quality = req.audio_quality
            resolver.clear_cache()
        if req.scrobble_history is not None:
            settings.scrobble_history = req.scrobble_history
        if req.auto_import_metadata is not None:
            settings.auto_import_metadata = req.auto_import_metadata
        if req.auto_import_collection is not None:
            settings.auto_import_collection = req.auto_import_collection
        if req.collection_dir is not None:
            settings.collection_dir = req.collection_dir.strip()
        if req.max_collection_cache_mb is not None:
            settings.max_collection_cache_mb = max(0, int(req.max_collection_cache_mb))
        settings_store.save(settings)
        return {
            "ok": True,
            "audioQuality": settings.audio_quality,
            "scrobbleHistory": settings.scrobble_history,
            "autoImportMetadata": settings.auto_import_metadata,
            "autoImportCollection": settings.auto_import_collection,
            "collectionDir": str(settings.resolved_collection_dir()),
            "maxCollectionCacheMb": settings.max_collection_cache_mb,
        }

    @app.get("/api/cache/tracks")
    async def api_cached_tracks(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        tracks = await asyncio.to_thread(metadata_store.list_tracks, limit)
        return {
            "ok": True,
            "stats": metadata_store.stats_summary(),
            "items": [t.to_dict() for t in tracks],
        }

    @app.post("/api/tracks/{video_id}/import")
    async def api_import_track(video_id: str, refresh: bool = False) -> dict[str, Any]:
        try:
            track = await importer.on_track_played(
                video_id,
                record_play=False,
                force_collection_import=True,
                force_refresh=refresh,
            )
            if not track:
                raise HTTPException(status_code=404, detail="Could not import track metadata")
            return {"ok": True, "track": track.to_dict()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/cover/{video_id}")
    async def get_cover_art(video_id: str) -> Response:
        cached = metadata_store.get_track(video_id)
        if cached and cached.cover_local_path and Path(cached.cover_local_path).exists():
            return FileResponse(cached.cover_local_path)
        for ext in (".jpg", ".png"):
            p = settings_store.covers_cache_dir / f"{video_id}{ext}"
            if p.exists():
                return FileResponse(p)
        raise HTTPException(status_code=404, detail="Cover art not cached")

    @app.get("/api/now-playing")
    async def api_now_playing() -> dict[str, Any]:
        state = await mpris.refresh_once()
        return state.to_dict()

    @app.post("/api/mpris/control")
    async def api_mpris_control(req: MPRISControlRequest) -> dict[str, Any]:
        if req.command not in ALLOWED_CONTROLS:
            raise HTTPException(status_code=400, detail=f"Unsupported command {req.command!r}")
        ok = await asyncio.to_thread(mpris.control_sync, req.command)
        if not ok:
            raise HTTPException(status_code=503, detail="Amarok is not reachable over MPRIS2 D-Bus")
        state = await mpris.refresh_once()
        return {"ok": True, "nowPlaying": state.to_dict()}

    @app.post("/api/tracks/{video_id}/rate")
    async def api_rate_track(video_id: str, req: RateTrackRequest) -> dict[str, Any]:
        try:
            res = await asyncio.to_thread(catalog.rate_track, video_id, req.rating)
            await asyncio.to_thread(metadata_store.update_like_status, video_id, res["rating"])
            await asyncio.to_thread(importer.sync_tags_for_track_sync, video_id)
            return res
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tracks/{video_id}/lyrics")
    async def api_track_lyrics(video_id: str) -> dict[str, Any]:
        cached = metadata_store.get_track(video_id)
        if cached and cached.lyrics:
            return {
                "videoId": video_id,
                "found": True,
                "lyrics": cached.lyrics,
                "source": cached.lyrics_source,
            }
        try:
            return await asyncio.to_thread(catalog.get_track_lyrics, video_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/cache/clear")
    async def api_clear_cache() -> dict[str, Any]:
        catalog.clear_cache()
        resolver.clear_cache()
        return {"ok": True}

    @app.get("/api/search")
    async def api_search(q: str = Query(..., min_length=1)) -> dict[str, Any]:
        try:
            items = await asyncio.to_thread(catalog.search_songs_direct, q.strip())
            return {"ok": True, "items": items}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def _open_upstream(video_id: str, method: str, range_header: str | None) -> tuple[httpx.Response, str]:
        assert http_client is not None
        for attempt in range(2):
            stream = await resolver.resolve(video_id, force_refresh=(attempt == 1))
            upstream_headers = {
                k: v for k, v in stream.http_headers.items() if k.lower() not in ("host", "accept-encoding")
            }
            if range_header:
                upstream_headers["Range"] = range_header
            req = http_client.build_request(method, stream.url, headers=upstream_headers)
            resp = await http_client.send(req, stream=True)
            if resp.status_code in (403, 410) and attempt == 0:
                await resp.aclose()
                resolver.invalidate(video_id)
                log.info("Upstream returned %d for %s; refreshing resolved stream URL", resp.status_code, video_id)
                continue
            return resp, stream.content_type
        raise StreamResolutionError(f"Upstream rejected stream for {video_id}")

    @app.api_route("/stream/{video_id}", methods=["GET", "HEAD"])
    async def stream_audio(video_id: str, request: Request) -> Response:
        # 1. Serve directly from cached local collection file if already imported (instant seek, zero latency)
        cached = metadata_store.get_track(video_id)
        if cached and cached.local_file_path:
            local_p = Path(cached.local_file_path)
            if local_p.exists() and local_p.stat().st_size > 1000:
                if request.method == "GET" and not metadata_store.is_metadata_fresh(cached):
                    asyncio.create_task(importer.on_track_played(video_id, record_play=False))
                return _serve_local_file_with_range(local_p, request)

        # 2. Kick off background metadata + Local Collection import whenever Amarok begins playing a track
        if request.method == "GET":
            settings = settings_store.load()
            if settings.auto_import_metadata:
                asyncio.create_task(importer.on_track_played(video_id, record_play=False))

        range_header = request.headers.get("range")
        try:
            upstream, fallback_mime = await _open_upstream(video_id, request.method, range_header)
        except StreamResolutionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Upstream network error: {exc}") from exc

        resp_headers: dict[str, str] = {
            "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
            "Cache-Control": "no-store",
        }
        for h in ("content-length", "content-range"):
            if h in upstream.headers:
                resp_headers[h.title()] = upstream.headers[h]

        media_type = upstream.headers.get("content-type") or fallback_mime

        if request.method == "HEAD":
            status_code = upstream.status_code
            await upstream.aclose()
            return Response(status_code=status_code, headers=resp_headers, media_type=media_type)

        async def body_iter():
            try:
                async for chunk in upstream.aiter_bytes(chunk_size=CHUNK_SIZE):
                    yield chunk
            except (httpx.HTTPError, asyncio.CancelledError):
                pass
            finally:
                await upstream.aclose()

        return StreamingResponse(
            body_iter(),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=media_type,
        )

    return app
