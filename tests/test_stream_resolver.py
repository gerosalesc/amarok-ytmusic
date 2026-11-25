"""Unit tests for StreamResolver (caching, deduplication, format validation, and cookie fallback)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from amarok_ytmusic.auth import AuthManager
from amarok_ytmusic.config import SettingsStore
from amarok_ytmusic.stream_resolver import StreamResolutionError, StreamResolver


async def test_stream_resolver_rejects_invalid_video_id(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    resolver = StreamResolver(AuthManager(store))

    with pytest.raises(StreamResolutionError, match="Invalid YouTube video ID"):
        await resolver.resolve("bad id/../../etc")


async def test_stream_resolver_falls_back_when_first_attempt_returns_storyboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    store.cookies_txt_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    resolver = StreamResolver(AuthManager(store))

    # Pretend a JS runtime exists so it tries cookies=True first, then cookies=False
    monkeypatch.setattr(StreamResolver, "_has_js_runtime", staticmethod(lambda: True))

    calls: list[bool] = []

    class FakeYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            self.use_cookies = "cookiefile" in opts

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            calls.append(self.use_cookies)
            if self.use_cookies:
                # Simulate EJS failure returning only storyboard images
                return {
                    "id": "c3UzLS2eDzI",
                    "url": "https://i.ytimg.com/sb/c3UzLS2eDzI/storyboard.mhtml",
                    "ext": "mhtml",
                    "acodec": "none",
                }
            return {
                "id": "c3UzLS2eDzI",
                "title": "Lounge Jungle",
                "url": "https://googlevideo.example/audio.webm",
                "ext": "webm",
                "acodec": "opus",
                "filesize": 2574017,
                "abr": 128.0,
                "http_headers": {"User-Agent": "TestUA"},
            }

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)

    resolved = await resolver.resolve("c3UzLS2eDzI")
    assert calls == [True, False]
    assert resolved.video_id == "c3UzLS2eDzI"
    assert resolved.content_type == "audio/webm"
    assert resolved.content_length == 2574017

    # Second call should hit cache without invoking YoutubeDL again
    resolved_cached = await resolver.resolve("c3UzLS2eDzI")
    assert resolved_cached.url == resolved.url
    assert len(calls) == 2

    # force_refresh=True should re-extract
    await resolver.resolve("c3UzLS2eDzI", force_refresh=True)
    assert len(calls) == 4


async def test_stream_resolver_deduplicates_concurrent_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    resolver = StreamResolver(AuthManager(store))

    extract_count = 0

    def slow_resolve_sync(video_id: str):
        nonlocal extract_count
        extract_count += 1
        import time

        time.sleep(0.05)
        from amarok_ytmusic.stream_resolver import ResolvedStream

        return ResolvedStream(
            video_id=video_id,
            url="https://googlevideo.example/dedup.webm",
            content_type="audio/webm",
            expires_at=time.time() + 3600,
        )

    monkeypatch.setattr(resolver, "_resolve_sync", slow_resolve_sync)

    r1, r2, r3 = await asyncio.gather(
        resolver.resolve("dQw4w9WgXcQ"),
        resolver.resolve("dQw4w9WgXcQ"),
        resolver.resolve("dQw4w9WgXcQ"),
    )
    assert extract_count == 1
    assert r1.url == r2.url == r3.url


async def test_stream_resolver_forces_ipv4_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    resolver = StreamResolver(AuthManager(store))
    seen_opts: list[dict[str, Any]] = []

    class FakeYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            seen_opts.append(dict(opts))

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            return {
                "id": "dQw4w9WgXcQ",
                "title": "Test",
                "url": "https://googlevideo.example/audio.webm",
                "ext": "webm",
                "acodec": "opus",
            }

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.delenv("AMAROK_YTMUSIC_ALLOW_IPV6", raising=False)

    await resolver.resolve("dQw4w9WgXcQ")
    assert seen_opts[-1].get("source_address") == "0.0.0.0"

    monkeypatch.setenv("AMAROK_YTMUSIC_ALLOW_IPV6", "1")
    await resolver.resolve("dQw4w9WgXcQ", force_refresh=True)
    assert "source_address" not in seen_opts[-1]
