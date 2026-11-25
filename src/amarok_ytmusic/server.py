"""FastAPI server providing Web Auth UI, catalog API, IPC watcher lifecycle, and audio stream proxy."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import struct
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
from .config import SettingsStore, configure_ipv4_preference, get_ipc_dir, validate_cache_dir
from .ipc_watcher import IPCWatcher
from .metadata_store import (
    CachedUserProfile,
    MetadataStore,
    amarok_rating_to_like_status,
    format_last_played,
)
from .mpris_monitor import ALLOWED_CONTROLS, MPRISMonitor
from .stream_resolver import StreamResolutionError, StreamResolver
from .track_importer import TrackImporter
from .ytmusic_client import YTMusicCatalogClient

log = logging.getLogger("amarok_ytmusic.server")

WEB_DIR = Path(__file__).parent / "web"
CHUNK_SIZE = 65536
UPSTREAM_RANGE_CHUNK = 512 * 1024  # 512 KiB chunks bypass YouTube single-stream CDN throttling


def _build_silent_wav(duration_ms: int = 120, sample_rate: int = 8000) -> bytes:
    """Build a valid tiny PCM WAV of silence so Amarok hits clean EOS instead of wedging GStreamer on 404."""
    num_samples = int(sample_rate * duration_ms / 1000)
    data_size = num_samples * 2  # 16-bit mono
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        data_size,
    )
    return header + (b"\x00" * data_size)


SILENT_SKIP_WAV = _build_silent_wav()


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
    migrate_collection: bool = False


class RateTrackRequest(BaseModel):
    rating: str | None = None  # "LIKE" | "DISLIKE" | "INDIFFERENT"
    amarok_rating: int | None = None  # 0..10 Amarok star scale (10 = 5 stars)


class MPRISControlRequest(BaseModel):
    command: str


_EXT_MIME_MAP = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".webm": "audio/webm",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
}


def _media_type_for_path(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in _EXT_MIME_MAP:
        return _EXT_MIME_MAP[ext]
    return mimetypes.guess_type(str(file_path))[0] or "audio/mp4"


def _walk_mp4_boxes(fh: Any, start: int, end: int) -> list[tuple[int, int, int, bytes]]:
    """Parse ISO/IEC 14496-12 MP4 box headers within [start, end) -> (offset, header_size, size, box_type)."""
    boxes: list[tuple[int, int, int, bytes]] = []
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        hdr = fh.read(8)
        if len(hdr) < 8:
            break
        size, btype = struct.unpack(">I4s", hdr)
        header_size = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                break
            size = struct.unpack(">Q", ext)[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if size < header_size or pos + size > end:
            break
        boxes.append((pos, header_size, size, btype))
        pos += size
    return boxes


def _find_mp4_tag_box_type_offsets_fh(fh: Any, file_size: int) -> list[int]:
    """Locate 4-byte box-type offsets of MP4 udta/meta tag atoms for on-the-fly 'free' masking.

    Replacing 'udta'/'meta' with 'free' in the HTTP response stream preserves exact byte length and
    stco/co64 offsets while preventing Amarok's GStreamer MetaStream::Track from seeing embedded
    metadata tags and resetting HTTP stream duration to 0:00. The file on disk is never modified.
    """
    if file_size < 16:
        return []
    top_boxes = _walk_mp4_boxes(fh, 0, file_size)
    if not any(btype in (b"ftyp", b"moov") for _, _, _, btype in top_boxes):
        return []
    offsets: list[int] = []
    for pos, hdr_sz, sz, btype in top_boxes:
        if btype in (b"udta", b"meta"):
            offsets.append(pos + 4)
        elif btype == b"moov":
            for c_pos, _c_hdr, _c_sz, c_type in _walk_mp4_boxes(fh, pos + hdr_sz, pos + sz):
                if c_type in (b"udta", b"meta"):
                    offsets.append(c_pos + 4)
    return sorted(offsets)


def _find_mp4_tag_box_type_offsets(file_path: Path) -> list[int]:
    """Locate 4-byte box-type offsets of MP4 udta/meta tag atoms for an M4A/MP4 file on disk."""
    if file_path.suffix.lower() not in (".m4a", ".mp4"):
        return []
    try:
        with file_path.open("rb") as fh:
            return _find_mp4_tag_box_type_offsets_fh(fh, os.fstat(fh.fileno()).st_size)
    except OSError:
        return []


def _apply_mp4_tag_mask(chunk: bytes, chunk_start: int, patch_offsets: list[int]) -> bytes:
    """Mask any 4-byte udta/meta box types overlapping [chunk_start, chunk_start + len(chunk)) as b'free'."""
    if not patch_offsets:
        return chunk
    chunk_end = chunk_start + len(chunk)
    overlapping = [off for off in patch_offsets if off < chunk_end and (off + 4) > chunk_start]
    if not overlapping:
        return chunk
    buf = bytearray(chunk)
    replacement = b"free"
    for off in overlapping:
        ov_start = max(chunk_start, off)
        ov_end = min(chunk_end, off + 4)
        buf[ov_start - chunk_start : ov_end - chunk_start] = replacement[ov_start - off : ov_end - off]
    return bytes(buf)


def _serve_local_file_with_range(
    file_path: Path,
    request: Request,
    *,
    mask_mp4_tags: bool = True,
    cache_header: str = "HIT",
) -> Response:
    """Serve a locally cached audio file with HTTP Range (206 Partial Content) support.

    Opens the file once up front so concurrent atomic tag rewrites (os.replace) cannot alter the
    inode or box offsets mid-stream.
    """
    fh = file_path.open("rb")
    try:
        file_size = os.fstat(fh.fileno()).st_size
        if file_size <= 0:
            fh.close()
            raise HTTPException(status_code=404, detail="Cached audio file is empty")
        media_type = _media_type_for_path(file_path)
        patch_offsets = (
            _find_mp4_tag_box_type_offsets_fh(fh, file_size)
            if mask_mp4_tags and file_path.suffix.lower() in (".m4a", ".mp4")
            else []
        )

        range_header = request.headers.get("range")
        start = 0
        end = file_size - 1
        status_code = 200
        headers: dict[str, str] = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "X-Amarok-YTMusic-Cache": cache_header,
        }

        if range_header and range_header.startswith("bytes="):
            spec = range_header.replace("bytes=", "", 1).split(",")[0].strip()
            if "-" in spec:
                start_s, end_s = spec.split("-", 1)
                try:
                    if start_s:
                        start = int(start_s)
                        end = int(end_s) if end_s else file_size - 1
                    elif end_s:
                        suffix = int(end_s)
                        start = max(0, file_size - suffix)
                        end = file_size - 1
                    else:
                        start = 0
                        end = file_size - 1
                except ValueError:
                    start = 0
                    end = file_size - 1
                else:
                    if start < 0 or start >= file_size or start > end:
                        fh.close()
                        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
                    end = min(end, file_size - 1)
                    status_code = 206
                    headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        length = end - start + 1
        headers["Content-Length"] = str(length)

        if request.method == "HEAD":
            fh.close()
            return Response(status_code=status_code, headers=headers, media_type=media_type)
    except Exception:
        fh.close()
        raise

    def file_iter():
        try:
            fh.seek(start)
            remaining = length
            cursor = start
            while remaining > 0:
                chunk = fh.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                if patch_offsets:
                    chunk = _apply_mp4_tag_mask(chunk, cursor, patch_offsets)
                cursor += len(chunk)
                remaining -= len(chunk)
                yield chunk
        finally:
            fh.close()

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
        startup_settings = settings_store.load()
        use_v4 = startup_settings.use_ipv4_only()
        configure_ipv4_preference(use_v4)
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0") if use_v4 else None
        http_client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
            timeout=httpx.Timeout(connect=6.0, read=20.0, write=10.0, pool=6.0),
        )
        if watcher:
            watcher.start()
        if enable_mpris:
            mpris.start()
        try:
            yield
        finally:
            await importer.stop()
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
            elif not verify:
                # Never block the dashboard when offline: return any cached profile immediately
                # and refresh in the background if reachable.
                stale_prof = metadata_store.get_user_profile(max_age=0)
                if stale_prof is not None:
                    account = stale_prof.to_dict()

                    async def _bg_refresh_profile() -> None:
                        try:
                            info = await asyncio.wait_for(
                                asyncio.to_thread(auth_manager.verify_account), timeout=6.0
                            )
                            _cache_profile_from_info(info, settings.auth_mode)
                        except Exception:
                            pass

                    asyncio.create_task(_bg_refresh_profile())
                else:
                    try:
                        account = await asyncio.wait_for(
                            asyncio.to_thread(auth_manager.verify_account), timeout=3.0
                        )
                        _cache_profile_from_info(account, settings.auth_mode)
                    except (AuthError, asyncio.TimeoutError) as exc:
                        verify_error = str(exc) or "Timed out contacting YouTube Music (offline?)"
            else:
                try:
                    account = await asyncio.wait_for(
                        asyncio.to_thread(auth_manager.verify_account), timeout=8.0
                    )
                    _cache_profile_from_info(account, settings.auth_mode)
                except asyncio.TimeoutError:
                    verify_error = "Timed out verifying session with YouTube Music (offline?)"
                except AuthError as exc:
                    verify_error = str(exc)

        cache_stats = await asyncio.to_thread(metadata_store.disk_stats)
        cache_stats["cacheDir"] = str(settings.resolved_collection_dir())
        cache_stats["maxCacheMb"] = settings.max_collection_cache_mb
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
            "cacheStats": cache_stats,
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
        old_dir = settings.resolved_collection_dir()
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
            new_dir = settings.resolved_collection_dir()
            try:
                validate_cache_dir(new_dir)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if req.max_collection_cache_mb is not None:
            settings.max_collection_cache_mb = max(0, int(req.max_collection_cache_mb))
        settings_store.save(settings)

        migration: dict[str, Any] | None = None
        new_dir = settings.resolved_collection_dir()
        if req.migrate_collection and old_dir.resolve(strict=False) != new_dir.resolve(strict=False):
            try:
                migration = await asyncio.to_thread(importer.migrate_cache_dir_sync, old_dir, new_dir)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        cache_stats = await asyncio.to_thread(metadata_store.disk_stats)
        cache_stats["cacheDir"] = str(new_dir)
        cache_stats["maxCacheMb"] = settings.max_collection_cache_mb
        return {
            "ok": True,
            "audioQuality": settings.audio_quality,
            "scrobbleHistory": settings.scrobble_history,
            "autoImportMetadata": settings.auto_import_metadata,
            "autoImportCollection": settings.auto_import_collection,
            "collectionDir": str(new_dir),
            "maxCollectionCacheMb": settings.max_collection_cache_mb,
            "migration": migration,
            "cacheStats": cache_stats,
        }

    @app.get("/api/cache/tracks")
    async def api_cached_tracks(
        limit: int = Query(default=100, ge=1, le=500),
        q: str = Query(default=""),
        offline_only: bool = Query(default=False),
    ) -> dict[str, Any]:
        settings = settings_store.load()
        tracks = await asyncio.to_thread(metadata_store.list_tracks, limit, q, offline_only)
        stats = await asyncio.to_thread(metadata_store.disk_stats)
        stats["cacheDir"] = str(settings.resolved_collection_dir())
        stats["maxCacheMb"] = settings.max_collection_cache_mb
        return {
            "ok": True,
            "stats": stats,
            "items": [t.to_dict() for t in tracks],
        }

    @app.post("/api/cache/scan")
    async def api_scan_cache() -> dict[str, Any]:
        settings = settings_store.load()
        scan = await asyncio.to_thread(importer.scan_cache_dir_sync)
        stats = await asyncio.to_thread(metadata_store.disk_stats)
        stats["cacheDir"] = str(settings.resolved_collection_dir())
        stats["maxCacheMb"] = settings.max_collection_cache_mb
        return {"ok": True, "scan": scan, "stats": stats}

    @app.post("/api/cache/sync-missing")
    async def api_sync_missing_cache(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        settings = settings_store.load()
        sync = await asyncio.to_thread(importer.sync_missing_tracks_sync, limit)
        stats = await asyncio.to_thread(metadata_store.disk_stats)
        stats["cacheDir"] = str(settings.resolved_collection_dir())
        stats["maxCacheMb"] = settings.max_collection_cache_mb
        return {"ok": True, "sync": sync, "stats": stats}

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

    @app.api_route("/cover/{video_id}", methods=["GET", "HEAD"])
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
        state = await mpris.refresh_once(force=True)
        return {"ok": True, "nowPlaying": state.to_dict()}

    @app.post("/api/tracks/{video_id}/rate")
    async def api_rate_track(video_id: str, req: RateTrackRequest) -> dict[str, Any]:
        try:
            if req.amarok_rating is not None:
                clamped = max(0, min(10, int(req.amarok_rating)))
                like_status = amarok_rating_to_like_status(clamped)
                saved = await asyncio.to_thread(
                    metadata_store.set_user_rating, video_id, clamped, like_status
                )
                # Keep live MPRIS snapshot immediately consistent for the Current Track applet
                if mpris.state.video_id == video_id:
                    mpris.state.amarok_rating = saved.effective_amarok_rating()
                    mpris.state.user_rating = saved.amarok_rating_float()
                    mpris.state.like_status = saved.like_status

                # Sync to YouTube Music and local .m4a FMPS_Rating in the background so star clicks are instant
                async def _bg_sync() -> None:
                    if auth_manager.is_authenticated():
                        try:
                            await asyncio.to_thread(catalog.rate_track, video_id, like_status)
                        except Exception as exc:
                            log.warning("YouTube Music rating sync failed for %s: %s", video_id, exc)
                    try:
                        await asyncio.to_thread(importer.sync_tags_for_track_sync, video_id)
                    except Exception as exc:
                        log.warning("Local M4A rating tag sync failed for %s: %s", video_id, exc)

                asyncio.create_task(_bg_sync())
                return {
                    "ok": True,
                    "videoId": video_id,
                    "amarokRating": saved.effective_amarok_rating(),
                    "rating": like_status,
                    "playCount": saved.play_count,
                    "score": saved.score,
                    "scoreDisplay": int(round(saved.score)),
                    "lastPlayed": saved.last_played,
                    "lastPlayedDisplay": format_last_played(saved.last_played),
                }

            target_rating = req.rating or "LIKE"
            res = await asyncio.to_thread(catalog.rate_track, video_id, target_rating)
            saved = await asyncio.to_thread(metadata_store.update_like_status, video_id, res["rating"])
            await asyncio.to_thread(importer.sync_tags_for_track_sync, video_id)
            if saved:
                res["amarokRating"] = saved.effective_amarok_rating()
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

    async def _probe_stream_length(video_id: str) -> tuple[Any, int | None, str]:
        """Resolve stream metadata and ensure total content_length is known for chunked Range fetching."""
        assert http_client is not None
        for attempt in range(2):
            stream = await resolver.resolve(video_id, force_refresh=(attempt == 1))
            if stream.content_length and stream.content_length > 0:
                return stream, stream.content_length, stream.content_type

            upstream_headers = {
                k: v for k, v in stream.http_headers.items() if k.lower() not in ("host", "accept-encoding")
            }
            resp = await http_client.head(stream.url, headers=upstream_headers)
            if resp.status_code in (403, 410) and attempt == 0:
                resolver.invalidate(video_id)
                continue
            cl = resp.headers.get("content-length")
            mime = resp.headers.get("content-type") or stream.content_type
            total = int(cl) if (cl and cl.isdigit()) else None
            if total:
                stream.content_length = total
            return stream, total, mime
        raise StreamResolutionError(f"Upstream rejected stream probe for {video_id}")

    async def _fetch_upstream_chunk(
        video_id: str,
        start: int,
        end: int,
    ) -> tuple[bytes, str]:
        """Fetch an exact byte range from YouTube's CDN with automatic URL refresh & retry."""
        assert http_client is not None
        last_err: Exception | None = None
        for attempt in range(3):
            stream = await resolver.resolve(video_id, force_refresh=(attempt > 0))
            upstream_headers = {
                k: v for k, v in stream.http_headers.items() if k.lower() not in ("host", "accept-encoding")
            }
            upstream_headers["Range"] = f"bytes={start}-{end}"
            try:
                resp = await http_client.get(stream.url, headers=upstream_headers)
                if resp.status_code in (403, 410) and attempt < 2:
                    resolver.invalidate(video_id)
                    log.info("Upstream returned %d on chunk %d-%d for %s; refreshing URL", resp.status_code, start, end, video_id)
                    continue
                if resp.status_code in (200, 206):
                    data = resp.content
                    mime = resp.headers.get("content-type") or stream.content_type
                    # Guard against upstream returning full body (200) instead of slice (206)
                    if resp.status_code == 200 and (start > 0 or len(data) > (end - start + 1)):
                        data = data[start : end + 1]
                    if data:
                        return data, mime
                last_err = RuntimeError(f"Upstream HTTP {resp.status_code}")
            except httpx.HTTPError as exc:
                last_err = exc
                log.warning("Upstream chunk %d-%d failed for %s (attempt %d): %s", start, end, video_id, attempt + 1, exc)
                if attempt == 1:
                    resolver.invalidate(video_id)
                await asyncio.sleep(0.15 * (attempt + 1))
        raise StreamResolutionError(f"Failed fetching chunk {start}-{end} for {video_id}: {last_err}")

    def _parse_range_header(range_header: str | None, total_size: int) -> tuple[int, int, bool] | None:
        """Parse an HTTP Range header into (start, end, is_partial), or None if unsatisfiable."""
        if not range_header or not range_header.startswith("bytes="):
            return 0, total_size - 1, False
        spec = range_header.replace("bytes=", "", 1).split(",")[0].strip()
        if "-" not in spec:
            return 0, total_size - 1, False
        start_s, end_s = spec.split("-", 1)
        try:
            if start_s:
                start = int(start_s)
                end = int(end_s) if end_s else total_size - 1
            elif end_s:
                suffix = int(end_s)
                start = max(0, total_size - suffix)
                end = total_size - 1
            else:
                return 0, total_size - 1, False
        except ValueError:
            return 0, total_size - 1, False
        if start < 0 or start >= total_size or start > end:
            return None
        return start, min(end, total_size - 1), True

    def _amarok_safe_skip_response(video_id: str, request: Request, reason: str) -> Response | None:
        """If Amarok's GStreamer requests an unplayable track, return 120ms silence so Amarok skips cleanly.

        Amarok 3.3.3's EngineGstPipeline::cb_error() never resets its GStreamer pipeline on HTTP 404/502,
        permanently wedging playback until restart. A valid 120ms silent WAV triggers clean EOS instead.
        """
        ua = (request.headers.get("user-agent") or "").lower()
        if not any(token in ua for token in ("amarok", "gstreamer", "souphttpsrc")):
            return None
        log.warning(
            "Track %s unavailable (%s); returning 120ms silent WAV so Amarok advances without wedging",
            video_id,
            reason,
        )
        total = len(SILENT_SKIP_WAV)
        parsed = _parse_range_header(request.headers.get("range"), total)
        if parsed is None:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        start, end, is_partial = parsed
        payload = SILENT_SKIP_WAV[start : end + 1]
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "Content-Length": str(len(payload)),
        }
        if is_partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        return Response(
            content=b"" if request.method == "HEAD" else payload,
            status_code=206 if is_partial else 200,
            headers=headers,
            media_type="audio/wav",
        )

    @app.api_route("/stream/{video_id}", methods=["GET", "HEAD"])
    async def stream_audio(video_id: str, request: Request) -> Response:
        # 1. Local-cache-first playback: serve directly from the offline cache folder whenever available.
        # On-the-fly 4-byte MP4 udta/meta -> free masking keeps exact byte offsets/seeking while preventing
        # Amarok's GStreamer MetaStream::Track from resetting HTTP stream duration to 0:00.
        local_path = importer.find_local_file(video_id)
        if local_path is not None:
            log.debug("Serving %s from local offline cache: %s", video_id, local_path)
            return _serve_local_file_with_range(
                local_path, request, mask_mp4_tags=True, cache_header="HIT"
            )

        # 2. Cache MISS: resolve and proxy upstream YouTube audio
        try:
            stream, total_size, media_type = await _probe_stream_length(video_id)
        except StreamResolutionError as exc:
            skip = _amarok_safe_skip_response(video_id, request, str(exc))
            if skip is not None:
                return skip
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            skip = _amarok_safe_skip_response(video_id, request, str(exc))
            if skip is not None:
                return skip
            raise HTTPException(status_code=502, detail=f"Upstream network error: {exc}") from exc

        # Kick off background metadata + offline audio caching once the stream resolves cleanly.
        if request.method == "GET":
            settings = settings_store.load()
            if settings.auto_import_collection or settings.auto_import_metadata:
                asyncio.create_task(importer.on_track_played(video_id, record_play=False))

        range_header = request.headers.get("range")

        # Fast path: chunked 512 KiB Range fetching (bypasses YouTube single-stream throttling & pause timeouts!)
        if total_size and total_size > 0:
            parsed = _parse_range_header(range_header, total_size)
            if parsed is None:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total_size}"})
            req_start, req_end, is_partial = parsed
            expected_len = req_end - req_start + 1

            resp_headers: dict[str, str] = {
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
                "Content-Length": str(expected_len),
                "X-Amarok-YTMusic-Cache": "MISS",
            }
            if is_partial:
                resp_headers["Content-Range"] = f"bytes {req_start}-{req_end}/{total_size}"
            status_code = 206 if is_partial else 200

            if request.method == "HEAD":
                return Response(status_code=status_code, headers=resp_headers, media_type=media_type)

            # Prefetch the very first chunk BEFORE sending 200/206 headers so if the cached URL expired (403)
            # or fails completely we can still refresh cleanly or return a proper HTTP error status!
            first_end = min(req_start + UPSTREAM_RANGE_CHUNK - 1, req_end)
            try:
                first_chunk, chunk_mime = await _fetch_upstream_chunk(video_id, req_start, first_end)
                if chunk_mime:
                    media_type = chunk_mime
            except StreamResolutionError as exc:
                skip = _amarok_safe_skip_response(video_id, request, str(exc))
                if skip is not None:
                    return skip
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            async def chunked_body_iter():
                yield first_chunk
                cursor = req_start + len(first_chunk)
                while cursor <= req_end:
                    c_end = min(cursor + UPSTREAM_RANGE_CHUNK - 1, req_end)
                    try:
                        data, _ = await _fetch_upstream_chunk(video_id, cursor, c_end)
                    except (StreamResolutionError, httpx.HTTPError) as exc:
                        log.warning(
                            "Mid-stream chunk %d-%d failed for %s: %s", cursor, c_end, video_id, exc
                        )
                        return
                    yield data
                    cursor += len(data)

            return StreamingResponse(
                chunked_body_iter(),
                status_code=status_code,
                headers=resp_headers,
                media_type=media_type,
            )

        # Fallback when total content_length is unknown (e.g., live stream)
        assert http_client is not None
        upstream_headers = {
            k: v for k, v in stream.http_headers.items() if k.lower() not in ("host", "accept-encoding")
        }
        if range_header:
            upstream_headers["Range"] = range_header
        try:
            req = http_client.build_request(request.method, stream.url, headers=upstream_headers)
            upstream = await http_client.send(req, stream=True)
        except httpx.HTTPError as exc:
            skip = _amarok_safe_skip_response(video_id, request, str(exc))
            if skip is not None:
                return skip
            raise HTTPException(status_code=502, detail=f"Upstream network error: {exc}") from exc

        fb_headers = {
            "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
            "Cache-Control": "no-store",
            "X-Amarok-YTMusic-Cache": "MISS",
        }
        fb_mime = upstream.headers.get("content-type") or media_type
        if request.method == "HEAD":
            st = upstream.status_code
            await upstream.aclose()
            return Response(status_code=st, headers=fb_headers, media_type=fb_mime)

        async def fallback_iter():
            try:
                async for chunk in upstream.aiter_bytes(chunk_size=CHUNK_SIZE):
                    yield chunk
            except (httpx.HTTPError, asyncio.CancelledError):
                pass
            finally:
                await upstream.aclose()

        return StreamingResponse(
            fallback_iter(),
            status_code=upstream.status_code,
            headers=fb_headers,
            media_type=fb_mime,
        )

    return app
