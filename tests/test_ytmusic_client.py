"""Unit tests for YTMusicCatalogClient folder and track normalization."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from amarok_ytmusic.auth import AuthManager
from amarok_ytmusic.config import SettingsStore
from amarok_ytmusic.ytmusic_client import (
    YTMusicCatalogClient,
    _best_thumbnail,
    _extract_album_name,
    _join_artists,
    _safe_year,
)


def test_metadata_helpers() -> None:
    assert (
        _best_thumbnail(
            {
                "thumbnails": [
                    {"url": "https://img/small.jpg", "width": 60, "height": 60},
                    {"url": "https://img/large.jpg", "width": 544, "height": 544},
                ]
            }
        )
        == "https://img/large.jpg"
    )
    assert _best_thumbnail({}) == ""
    assert _join_artists([{"name": "Daft Punk"}, {"name": "Pharrell Williams"}]) == "Daft Punk, Pharrell Williams"
    assert _join_artists([]) == "Unknown Artist"
    assert _extract_album_name({"name": "Random Access Memories"}) == "Random Access Memories"
    assert _extract_album_name(None, fallback="Single") == "Single"
    assert _safe_year("2013") == 2013
    assert _safe_year("invalid") == 0


def test_catalog_root_folders_and_tracks(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    auth = AuthManager(store)
    auth.is_authenticated = MagicMock(return_value=True)

    fake_yt = MagicMock()
    fake_yt.get_library_playlists.return_value = [
        {"playlistId": "LM", "title": "Liked Music"},
        {"playlistId": "PL_TEST", "title": "Chill Coding", "count": "12", "thumbnails": [{"url": "https://img/pl.jpg", "width": 200, "height": 200}]},
    ]
    fake_yt.get_library_albums.return_value = [
        {"browseId": "MPREb_ALB", "title": "Discovery", "artists": [{"name": "Daft Punk"}], "thumbnails": []},
    ]
    fake_yt.get_liked_songs.return_value = {
        "tracks": [
            {
                "videoId": "vid_liked_1",
                "title": "Digital Love",
                "artists": [{"name": "Daft Punk"}],
                "album": {"name": "Discovery"},
                "duration_seconds": 301,
                "year": "2001",
            }
        ]
    }
    fake_yt.get_album.return_value = {
        "title": "Discovery",
        "year": "2001",
        "thumbnails": [{"url": "https://img/discovery.jpg", "width": 544, "height": 544}],
        "tracks": [
            {
                "videoId": "vid_alb_1",
                "title": "One More Time",
                "artists": [{"name": "Daft Punk"}],
                "duration": "5:20",
            }
        ],
    }
    fake_yt.get_home.side_effect = RuntimeError("Simulated timeout")

    auth.create_ytmusic = MagicMock(return_value=fake_yt)
    catalog = YTMusicCatalogClient(auth)

    # 1. Root folders (authenticated)
    folders = catalog.get_root_folders("")
    callbacks = [f["callbackData"] for f in folders]
    assert "liked" in callbacks
    assert "library_songs" in callbacks
    assert "home" in callbacks
    assert "playlist:PL_TEST" in callbacks
    assert "album:MPREb_ALB" in callbacks
    assert "playlist:LM" not in callbacks  # Deduplicated against Liked Music

    # 2. Liked tracks with duration_seconds fallback
    liked_tracks = catalog.get_tracks_for_folder("liked")
    assert len(liked_tracks) == 1
    assert liked_tracks[0]["itemName"] == "Digital Love"
    assert liked_tracks[0]["duration"] == "5:01"
    assert liked_tracks[0]["year"] == 2001
    assert liked_tracks[0]["playableUrl"].endswith("/stream/vid_liked_1")

    # 3. Album tracks inherit album cover and year
    album_tracks = catalog.get_tracks_for_folder("album:MPREb_ALB")
    assert len(album_tracks) == 1
    assert album_tracks[0]["coverUrl"] == "https://img/discovery.jpg"
    assert album_tracks[0]["year"] == 2001

    # 4. Home feed gracefully handles timeout without raising
    home_tracks = catalog.get_tracks_for_folder("home")
    assert home_tracks == []
