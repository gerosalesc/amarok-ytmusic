"""Tests for FastAPI Web Auth API and Range-capable /stream proxy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from amarok_ytmusic.config import SettingsStore
from amarok_ytmusic.server import create_app
from amarok_ytmusic.stream_resolver import ResolvedStream


def test_web_ui_and_status_endpoints(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)

    app.state.catalog.rate_track = MagicMock(return_value={"ok": True, "videoId": "5gXrbaDHMrM", "rating": "LIKE"})
    app.state.catalog.get_track_lyrics = MagicMock(
        return_value={"videoId": "5gXrbaDHMrM", "found": True, "lyrics": "La la la", "source": "Musixmatch"}
    )

    with TestClient(app) as client:
        r_html = client.get("/")
        assert r_html.status_code == 200
        assert "Amarok × YouTube Music" in r_html.text

        r_status = client.get("/api/status")
        assert r_status.status_code == 200
        body = r_status.json()
        assert body["authenticated"] is False
        assert body["audioQuality"] == "best"
        assert body["scrobbleHistory"] is True

        r_set = client.post("/api/settings", json={"audio_quality": "m4a", "scrobble_history": False})
        assert r_set.status_code == 200
        assert store.load().audio_quality == "m4a"
        assert store.load().scrobble_history is False

        r_rate = client.post("/api/tracks/5gXrbaDHMrM/rate", json={"rating": "LIKE"})
        assert r_rate.status_code == 200
        assert r_rate.json()["rating"] == "LIKE"

        r_lyrics = client.get("/api/tracks/5gXrbaDHMrM/lyrics")
        assert r_lyrics.status_code == 200
        assert r_lyrics.json()["found"] is True
        assert r_lyrics.json()["lyrics"] == "La la la"


def test_stream_proxy_supports_range_and_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    settings = store.load()
    settings.auto_import_metadata = False
    store.save(settings)
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)

    fake_audio = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    async def fake_resolve(video_id: str, force_refresh: bool = False) -> ResolvedStream:
        return ResolvedStream(
            video_id=video_id,
            url="https://mock-googlevideo.local/audio.webm",
            content_type="audio/webm",
            content_length=len(fake_audio),
            http_headers={"User-Agent": "TestAgent"},
            expires_at=9999999999.0,
        )

    app.state.resolver.resolve = AsyncMock(side_effect=fake_resolve)

    # Patch httpx.AsyncClient.send to serve fake_audio with HTTP Range support
    async def mock_send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs
    ) -> httpx.Response:
        rng = request.headers.get("range")
        if rng and rng.startswith("bytes="):
            start_s, end_s = rng.replace("bytes=", "").split("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(fake_audio) - 1
            chunk = fake_audio[start : end + 1]
            return httpx.Response(
                206,
                content=b"" if request.method == "HEAD" else chunk,
                headers={
                    "Content-Type": "audio/webm",
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{len(fake_audio)}",
                    "Content-Length": str(len(chunk)),
                },
                request=request,
            )
        return httpx.Response(
            200,
            content=b"" if request.method == "HEAD" else fake_audio,
            headers={
                "Content-Type": "audio/webm",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(fake_audio)),
            },
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    with TestClient(app) as client:
        # HEAD probe (used by GStreamer souphttpsrc)
        r_head = client.head("/stream/dQw4w9WgXcQ")
        assert r_head.status_code == 200
        assert r_head.headers["accept-ranges"] == "bytes"
        assert r_head.headers["content-length"] == str(len(fake_audio))

        # Full GET
        r_full = client.get("/stream/dQw4w9WgXcQ")
        assert r_full.status_code == 200
        assert r_full.content == fake_audio

        # Byte-Range GET (seeking in Amarok)
        r_range = client.get("/stream/dQw4w9WgXcQ", headers={"Range": "bytes=10-19"})
        assert r_range.status_code == 206
        assert r_range.content == fake_audio[10:20]
        assert r_range.headers["content-range"] == f"bytes 10-19/{len(fake_audio)}"


def test_stream_proxy_retries_on_upstream_403(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    settings = store.load()
    settings.auto_import_metadata = False
    store.save(settings)
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)

    resolve_calls: list[bool] = []

    async def fake_resolve(video_id: str, force_refresh: bool = False) -> ResolvedStream:
        resolve_calls.append(force_refresh)
        url = (
            "https://mock-googlevideo.local/fresh.webm"
            if force_refresh
            else "https://mock-googlevideo.local/expired.webm"
        )
        return ResolvedStream(
            video_id=video_id,
            url=url,
            content_type="audio/webm",
            content_length=4,
            expires_at=9999999999.0,
        )

    app.state.resolver.resolve = AsyncMock(side_effect=fake_resolve)

    async def mock_send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs
    ) -> httpx.Response:
        if "expired.webm" in str(request.url):
            return httpx.Response(403, content=b"", request=request)
        return httpx.Response(
            200,
            content=b"OK!!",
            headers={"Content-Type": "audio/webm", "Content-Length": "4"},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    with TestClient(app) as client:
        r = client.get("/stream/dQw4w9WgXcQ")
        assert r.status_code == 200
        assert r.content == b"OK!!"
        assert resolve_calls[-1] is True
        assert False in resolve_calls


def test_stream_proxy_assembles_multiple_upstream_range_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import amarok_ytmusic.server as server_mod

    monkeypatch.setattr(server_mod, "UPSTREAM_RANGE_CHUNK", 10)
    store = SettingsStore(config_dir=tmp_path / "cfg")
    settings = store.load()
    settings.auto_import_metadata = False
    store.save(settings)
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)

    fake_audio = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    async def fake_resolve(video_id: str, force_refresh: bool = False) -> ResolvedStream:
        return ResolvedStream(
            video_id=video_id,
            url="https://mock-googlevideo.local/chunked.webm",
            content_type="audio/webm",
            content_length=len(fake_audio),
            expires_at=9999999999.0,
        )

    app.state.resolver.resolve = AsyncMock(side_effect=fake_resolve)
    seen_ranges: list[str] = []

    async def mock_send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs
    ) -> httpx.Response:
        rng = request.headers.get("range", "")
        seen_ranges.append(rng)
        start_s, end_s = rng.replace("bytes=", "").split("-")
        start, end = int(start_s), int(end_s)
        chunk = fake_audio[start : end + 1]
        return httpx.Response(
            206,
            content=chunk,
            headers={
                "Content-Type": "audio/webm",
                "Content-Range": f"bytes {start}-{end}/{len(fake_audio)}",
                "Content-Length": str(len(chunk)),
            },
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    with TestClient(app) as client:
        r = client.get("/stream/dQw4w9WgXcQ")
        assert r.status_code == 200
        assert r.content == fake_audio
        assert seen_ranges == ["bytes=0-9", "bytes=10-19", "bytes=20-29", "bytes=30-35"]


def test_stream_proxy_returns_silent_wav_for_amarok_on_unavailable_track(tmp_path: Path) -> None:
    from amarok_ytmusic.stream_resolver import StreamResolutionError

    store = SettingsStore(config_dir=tmp_path / "cfg")
    settings = store.load()
    settings.auto_import_metadata = False
    store.save(settings)
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)
    app.state.resolver.resolve = AsyncMock(side_effect=StreamResolutionError("Region blocked"))

    with TestClient(app) as client:
        # Normal HTTP clients still get 404
        r_curl = client.get("/stream/hhWxbqD45EM", headers={"User-Agent": "curl/8.0"})
        assert r_curl.status_code == 404

        # Amarok / GStreamer gets a valid 120ms silent WAV so GStreamer hits clean EOS and skips without wedging
        r_amarok = client.get("/stream/hhWxbqD45EM", headers={"User-Agent": "Amarok/3.3.3"})
        assert r_amarok.status_code == 200
        assert r_amarok.headers["content-type"] == "audio/wav"
        assert r_amarok.content[:4] == b"RIFF"
        assert r_amarok.content[8:12] == b"WAVE"

        r_gst = client.get(
            "/stream/hhWxbqD45EM",
            headers={"User-Agent": "GStreamer souphttpsrc 1.24.0", "Range": "bytes=0-11"},
        )
        assert r_gst.status_code == 206
        assert r_gst.content == r_amarok.content[:12]
