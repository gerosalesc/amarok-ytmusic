"""Tests for TrackImporter (metadata fetch, filename sanitization, MP4 FMPS tagging, local Range stream)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from amarok_ytmusic.auth import AuthManager
from amarok_ytmusic.config import SettingsStore
from amarok_ytmusic.metadata_store import CachedTrack, MetadataStore
from amarok_ytmusic.server import create_app
from amarok_ytmusic.track_importer import TrackImporter, sanitize_filename


def test_sanitize_filename() -> None:
    assert sanitize_filename('Artist / Name: "Live"?') == "Artist _ Name_ _Live_"
    assert sanitize_filename("   ...  ", fallback="Unknown") == "Unknown"


def test_fetch_and_cache_metadata_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    auth = AuthManager(store)
    meta_store = MetadataStore(store.metadata_db_path)

    fake_yt = MagicMock()
    fake_yt.get_watch_playlist.return_value = {
        "tracks": [
            {
                "videoId": "5gXrbaDHMrM",
                "title": "She's Knocking",
                "artists": [{"name": "Ikebe Shakedown"}],
                "album": {"name": "The Way Home", "id": "MPREb_123"},
                "year": "2017",
                "length": "3:15",
                "likeStatus": "LIKE",
                "inLibrary": True,
                "thumbnail": [{"url": "https://img.example/cover.jpg", "width": 544, "height": 544}],
            }
        ],
        "lyrics": "MPLYt_123",
    }
    fake_yt.get_lyrics.return_value = {"lyrics": "Verse 1\nChorus", "source": "Musixmatch"}
    auth.create_ytmusic = MagicMock(return_value=fake_yt)

    importer = TrackImporter(auth, meta_store)
    # Mock cover download
    cover_file = store.covers_cache_dir / "5gXrbaDHMrM.jpg"
    cover_file.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 200)
    monkeypatch.setattr(importer, "_download_cover_sync", lambda vid, url: str(cover_file))

    track = importer.fetch_and_cache_metadata_sync("5gXrbaDHMrM")
    assert track.title == "She's Knocking"
    assert track.artist == "Ikebe Shakedown"
    assert track.album == "The Way Home"
    assert track.year == 2017
    assert track.duration_sec == 195
    assert track.like_status == "LIKE"
    assert track.in_library is True
    assert track.lyrics == "Verse 1\nChorus"
    assert track.cover_local_path == str(cover_file)

    # Second call uses SQLite cache without hitting ytmusicapi again
    track2 = importer.fetch_and_cache_metadata_sync("5gXrbaDHMrM")
    assert track2.title == "She's Knocking"
    assert fake_yt.get_watch_playlist.call_count == 1


def test_stream_endpoint_serves_locally_cached_file_with_range(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)

    fake_audio_file = tmp_path / "cached.m4a"
    payload = b"M4A_FAKE_AUDIO_BYTES_0123456789" * 100
    fake_audio_file.write_bytes(payload)

    app.state.metadata_store.upsert_metadata(
        CachedTrack(
            video_id="5gXrbaDHMrM",
            title="She's Knocking",
            artist="Ikebe Shakedown",
            album="The Way Home",
            local_file_path=str(fake_audio_file),
        )
    )

    with TestClient(app) as client:
        r_head = client.head("/stream/5gXrbaDHMrM")
        assert r_head.status_code == 200
        assert r_head.headers["content-length"] == str(len(payload))
        assert r_head.headers["accept-ranges"] == "bytes"

        r_range = client.get("/stream/5gXrbaDHMrM", headers={"Range": "bytes=0-19"})
        assert r_range.status_code == 206
        assert r_range.content == payload[:20]
        assert r_range.headers["content-range"] == f"bytes 0-19/{len(payload)}"

        r_cache = client.get("/api/cache/tracks")
        assert r_cache.status_code == 200
        body = r_cache.json()
        assert body["stats"]["totalTracks"] == 1
        assert body["items"][0]["hasLocalFile"] is True
