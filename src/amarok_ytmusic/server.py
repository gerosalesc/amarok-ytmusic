"""FastAPI server providing Web Auth UI, catalog API, IPC watcher lifecycle, and audio stream proxy."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .ampache_bridge import AmpacheBridge
from .auth import AuthError, AuthManager, detect_browsers
from .config import SettingsStore, get_ipc_dir
from .ipc_watcher import IPCWatcher
from .stream_resolver import StreamResolutionError, StreamResolver
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
    audio_quality: str


def create_app(
    store: SettingsStore | None = None,
    ipc_dir: Path | None = None,
    enable_ipc: bool = True,
) -> FastAPI:
    settings_store = store or SettingsStore()
    auth_manager = AuthManager(settings_store)
    catalog = YTMusicCatalogClient(auth_manager)
    resolver = StreamResolver(auth_manager)
    ampache = AmpacheBridge(catalog, auth_manager)
    watcher = IPCWatcher(catalog, auth_manager, ipc_dir=ipc_dir or get_ipc_dir()) if enable_ipc else None

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
        try:
            yield
        finally:
            if watcher:
                await watcher.stop()
            if http_client:
                await http_client.aclose()
                http_client = None

    app = FastAPI(title="Amarok YouTube Music Bridge", lifespan=lifespan)
    # Expose components on app.state for testing and introspection
    app.state.auth = auth_manager
    app.state.catalog = catalog
    app.state.resolver = resolver
    app.state.ampache = ampache
    app.state.watcher = watcher

    @app.api_route("/ampache/server/xml.server.php", methods=["GET", "POST"])
    @app.api_route("/server/xml.server.php", methods=["GET", "POST"])
    async def ampache_xml_api(
        action: str = Query(default=""),
        filter: str = Query(default=""),
        limit: int = Query(default=0),
    ) -> Response:
        xml_body = await asyncio.to_thread(ampache.handle_action, action, filter, limit)
        return Response(content=xml_body, media_type="application/xml; charset=utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html_path = WEB_DIR / "index.html"
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    @app.get("/api/status")
    async def api_status(verify: bool = False) -> dict[str, Any]:
        settings = settings_store.load()
        authenticated = auth_manager.is_authenticated()
        account: dict[str, Any] | None = None
        verify_error: str | None = None
        if authenticated and verify:
            try:
                account = await asyncio.to_thread(auth_manager.verify_account)
            except AuthError as exc:
                verify_error = str(exc)
        elif authenticated:
            try:
                account = await asyncio.to_thread(auth_manager.verify_account)
            except AuthError as exc:
                verify_error = str(exc)

        return {
            "authenticated": authenticated,
            "authMode": settings.auth_mode,
            "browserSource": settings.browser_source,
            "audioQuality": settings.audio_quality,
            "baseUrl": settings.base_url,
            "account": account,
            "verifyError": verify_error,
            "browsers": [asdict(b) for b in detect_browsers()],
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
            catalog.clear_cache()
            resolver.clear_cache()
            return info
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/headers")
    async def api_auth_headers(req: HeadersAuthRequest) -> dict[str, Any]:
        try:
            info = await asyncio.to_thread(auth_manager.authenticate_from_raw_headers, req.raw_headers)
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
            catalog.clear_cache()
            resolver.clear_cache()
            return info
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/auth/logout")
    async def api_logout() -> dict[str, Any]:
        auth_manager.logout()
        catalog.clear_cache()
        resolver.clear_cache()
        return {"ok": True}

    @app.post("/api/settings")
    async def api_settings(req: SettingsUpdateRequest) -> dict[str, Any]:
        if req.audio_quality not in ("best", "opus", "m4a", "medium"):
            raise HTTPException(status_code=400, detail="Invalid audio_quality option")
        settings = settings_store.load()
        settings.audio_quality = req.audio_quality
        settings_store.save(settings)
        resolver.clear_cache()
        return {"ok": True, "audioQuality": settings.audio_quality}

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
