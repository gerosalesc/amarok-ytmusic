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
    settings.auto_import_collection = False
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
    settings.auto_import_collection = False
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
    settings.auto_import_collection = False
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
    settings.auto_import_collection = False
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


def test_rate_track_with_amarok_star_scale_updates_cache_and_now_playing(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)
    app.state.catalog.rate_track = MagicMock(return_value={"ok": True, "videoId": "RJRl5zKyEls", "rating": "LIKE"})

    with TestClient(app) as client:
        # Rate 4 stars (8/10) from the Amarok Current Track applet
        r = client.post("/api/tracks/RJRl5zKyEls/rate", json={"amarok_rating": 8})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["amarokRating"] == 8
        assert body["rating"] == "LIKE"

        cached = app.state.metadata_store.get_track("RJRl5zKyEls")
        assert cached is not None
        assert cached.effective_amarok_rating() == 8
        assert cached.like_status == "LIKE"

        # Rate 3 stars (6/10) -> preserves 6/10 locally and maps to INDIFFERENT on YT Music
        r2 = client.post("/api/tracks/RJRl5zKyEls/rate", json={"amarok_rating": 6})
        assert r2.status_code == 200
        assert r2.json()["amarokRating"] == 6
        assert r2.json()["rating"] == "INDIFFERENT"


def _make_synthetic_m4a(path: Path, video_id: str = "Ki7GrRTNYfU", title: str = "Engaña") -> Path:
    """Create a valid minimal MP4/M4A file tagged with mutagen (contains moov/udta/meta/ilst)."""
    import struct
    from mutagen.mp4 import MP4

    def box(btype: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(payload), btype) + payload

    ftyp = box(b"ftyp", b"M4A \x00\x00\x00\x00M4A isommp42")
    mvhd = box(b"mvhd", b"\x00" * 100)
    mdhd = box(b"mdhd", b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 44100, 44100 * 180) + b"\x00\x00\x00\x00")
    hdlr = box(b"hdlr", b"\x00" * 8 + b"soun" + b"\x00" * 12 + b"SoundHandler\x00")
    esds = box(
        b"esds",
        b"\x00\x00\x00\x00\x03\x19\x00\x01\x00\x04\x11\x40\x15\x00\x00\x00\x00\x01\xf4\x00\x00\x01\xf4\x00\x05\x02\x12\x10\x06\x01\x02",
    )
    mp4a = box(
        b"mp4a",
        b"\x00" * 6
        + struct.pack(">H", 1)
        + b"\x00" * 8
        + struct.pack(">HH", 2, 16)
        + b"\x00" * 4
        + struct.pack(">I", 44100 << 16)
        + esds,
    )
    stsd = box(b"stsd", b"\x00\x00\x00\x00" + struct.pack(">I", 1) + mp4a)
    stco = box(b"stco", b"\x00\x00\x00\x00" + struct.pack(">I", 0))
    stbl = box(b"stbl", stsd + stco)
    minf = box(b"minf", stbl)
    mdia = box(b"mdia", mdhd + hdlr + minf)
    trak = box(b"trak", mdia)
    moov = box(b"moov", mvhd + trak)
    mdat = box(b"mdat", b"\x00" * 2048)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ftyp + moov + mdat)
    m = MP4(str(path))
    m.add_tags()
    m.tags["\xa9nam"] = [title]
    m.tags["\xa9ART"] = ["Gustavo Cerati"]
    m.tags["\xa9alb"] = ["Bocanada"]
    m.tags["\xa9cmt"] = [f"ytmusic:{video_id}"]
    m.save()
    return path


def test_stream_serves_local_offline_cache_first_with_mp4_tag_masking(tmp_path: Path) -> None:
    from mutagen.mp4 import MP4
    from amarok_ytmusic.metadata_store import CachedTrack
    from amarok_ytmusic.server import _find_mp4_tag_box_type_offsets
    from amarok_ytmusic.stream_resolver import StreamResolutionError

    store = SettingsStore(config_dir=tmp_path / "cfg")
    cache_dir = tmp_path / "offline_cache"
    settings = store.load()
    settings.collection_dir = str(cache_dir)
    store.save(settings)

    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)
    # Simulate completely offline environment: resolver must NEVER be called for cached tracks
    app.state.resolver.resolve = AsyncMock(side_effect=StreamResolutionError("Offline — no network"))

    m4a_path = _make_synthetic_m4a(cache_dir / "Gustavo Cerati" / "Bocanada" / "Engaña.m4a", "Ki7GrRTNYfU", "Engaña")
    raw_disk_bytes = m4a_path.read_bytes()
    udta_offsets = _find_mp4_tag_box_type_offsets(m4a_path)
    assert len(udta_offsets) >= 1
    udta_off = udta_offsets[0]
    assert raw_disk_bytes[udta_off : udta_off + 4] == b"udta"

    app.state.metadata_store.upsert_metadata(
        CachedTrack(
            video_id="Ki7GrRTNYfU",
            title="Engaña",
            artist="Gustavo Cerati",
            album="Bocanada",
            local_file_path=str(m4a_path),
        )
    )

    with TestClient(app) as client:
        # HEAD works offline with exact file length and cache HIT header
        r_head = client.head("/stream/Ki7GrRTNYfU")
        assert r_head.status_code == 200
        assert r_head.headers["x-amarok-ytmusic-cache"] == "HIT"
        assert r_head.headers["content-type"] == "audio/mp4"
        assert int(r_head.headers["content-length"]) == len(raw_disk_bytes)

        # Full GET serves local file with udta masked as 'free' (no tags emitted to GStreamer!)
        r_full = client.get("/stream/Ki7GrRTNYfU")
        assert r_full.status_code == 200
        assert r_full.headers["x-amarok-ytmusic-cache"] == "HIT"
        assert len(r_full.content) == len(raw_disk_bytes)
        assert r_full.content[udta_off : udta_off + 4] == b"free"

        # Verify streamed M4A is valid MP4 with 0 embedded tags while disk file still keeps full tags
        streamed_tmp = tmp_path / "streamed.m4a"
        streamed_tmp.write_bytes(r_full.content)
        streamed_mp4 = MP4(str(streamed_tmp))
        assert streamed_mp4.info.length == pytest.approx(180.0, abs=0.5)
        assert streamed_mp4.tags is None
        assert MP4(str(m4a_path)).tags["\xa9nam"] == ["Engaña"]

        # Partial Range request slicing directly across the middle of the 4-byte udta box type
        r_slice = client.get(
            "/stream/Ki7GrRTNYfU",
            headers={"Range": f"bytes={udta_off + 1}-{udta_off + 2}"},
        )
        assert r_slice.status_code == 206
        assert r_slice.content == b"re"

        # Invalid range returns 416
        r_bad = client.get("/stream/Ki7GrRTNYfU", headers={"Range": f"bytes={len(raw_disk_bytes) + 10}-"})
        assert r_bad.status_code == 416

        # Upstream resolver was never touched
        app.state.resolver.resolve.assert_not_called()


def test_settings_validates_and_migrates_collection_dir(tmp_path: Path) -> None:
    from amarok_ytmusic.metadata_store import CachedTrack

    store = SettingsStore(config_dir=tmp_path / "cfg")
    old_dir = tmp_path / "cache_old"
    new_dir = tmp_path / "cache_new"
    settings = store.load()
    settings.collection_dir = str(old_dir)
    store.save(settings)

    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)
    old_file = _make_synthetic_m4a(old_dir / "Artist A" / "Album B" / "Song C.m4a", "VidMigrate01", "Song C")
    app.state.metadata_store.upsert_metadata(
        CachedTrack(
            video_id="VidMigrate01",
            title="Song C",
            artist="Artist A",
            album="Album B",
            local_file_path=str(old_file),
        )
    )

    # Reject a regular file path as collection_dir
    not_a_dir = tmp_path / "regular_file.txt"
    not_a_dir.write_text("hi", encoding="utf-8")

    with TestClient(app) as client:
        r_err = client.post("/api/settings", json={"collection_dir": str(not_a_dir)})
        assert r_err.status_code == 400

        # Migrate to new_dir
        r_mig = client.post(
            "/api/settings",
            json={"collection_dir": str(new_dir), "migrate_collection": True, "max_collection_cache_mb": 0},
        )
        assert r_mig.status_code == 200
        body = r_mig.json()
        assert body["collectionDir"] == str(new_dir)
        assert body["maxCollectionCacheMb"] == 0
        assert body["migration"]["moved"] == 1
        assert body["migration"]["errors"] == 0

        expected_new = new_dir / "Artist A" / "Album B" / "Song C.m4a"
        assert expected_new.is_file()
        assert not old_file.exists()
        # Empty artist/album subdirs cleaned up
        assert not (old_dir / "Artist A").exists()

        updated = app.state.metadata_store.get_track("VidMigrate01")
        assert updated is not None
        assert updated.local_file_path == str(expected_new)

        # Streaming immediately serves from migrated path offline
        r_stream = client.head("/stream/VidMigrate01")
        assert r_stream.status_code == 200
        assert r_stream.headers["x-amarok-ytmusic-cache"] == "HIT"
