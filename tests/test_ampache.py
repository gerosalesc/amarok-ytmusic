"""Tests for the Amarok Ampache XML bridge."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from amarok_ytmusic.ampache_bridge import parse_duration_seconds
from amarok_ytmusic.config import SettingsStore
from amarok_ytmusic.server import create_app


def test_parse_duration_seconds() -> None:
    assert parse_duration_seconds("3:45") == 225
    assert parse_duration_seconds("1:02:10") == 3730
    assert parse_duration_seconds("") == 0
    assert parse_duration_seconds("invalid") == 0


def test_ampache_ping_handshake_and_browse(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False)

    # Simulate authenticated state & mocked catalog responses
    app.state.auth.is_authenticated = MagicMock(return_value=True)
    app.state.catalog.get_root_folders = MagicMock(
        side_effect=lambda q="": (
            [
                {
                    "level": 1,
                    "itemName": "Playlist: Synthwave Chill",
                    "callbackData": "playlist:PL_SYNTH",
                    "coverUrl": "https://img.example/synth.jpg",
                }
            ]
            if not q
            else [
                {
                    "level": 1,
                    "itemName": f"Songs matching '{q}'",
                    "callbackData": f"search_songs:{q}",
                    "coverUrl": "",
                }
            ]
        )
    )
    app.state.catalog.get_tracks_for_folder = MagicMock(
        return_value=[
            {
                "level": 0,
                "videoId": "dQw4w9WgXcQ",
                "itemName": "Test Song",
                "artist": "Test Artist",
                "album": "Synthwave Chill",
                "playableUrl": "http://127.0.0.1:18732/stream/dQw4w9WgXcQ",
                "coverUrl": "https://img.example/track.jpg",
                "duration": "3:32",
                "year": 2024,
            }
        ]
    )

    with TestClient(app) as client:
        # 1. Ping
        r_ping = client.get("/ampache/server/xml.server.php", params={"action": "ping"})
        assert r_ping.status_code == 200
        root = ET.fromstring(r_ping.text)
        assert root.findtext("compatible") == "350001"

        # 2. Handshake
        r_hs = client.get("/ampache/server/xml.server.php", params={"action": "handshake"})
        assert r_hs.status_code == 200
        assert ET.fromstring(r_hs.text).findtext("auth")

        # 3. Root artists
        r_art = client.get("/ampache/server/xml.server.php", params={"action": "artists"})
        art_root = ET.fromstring(r_art.text)
        artist_ids = [el.attrib["id"] for el in art_root.findall("artist")]
        assert artist_ids == ["1", "2", "3"]

        # 4. Expand My Playlists (artist id=2)
        r_alb = client.get("/ampache/server/xml.server.php", params={"action": "artist_albums", "filter": "2"})
        alb_root = ET.fromstring(r_alb.text)
        albums = alb_root.findall("album")
        assert len(albums) == 1
        assert albums[0].findtext("name") == "Synthwave Chill"
        playlist_album_id = albums[0].attrib["id"]

        # 5. Expand Playlist tracks
        r_songs = client.get(
            "/ampache/server/xml.server.php",
            params={"action": "album_songs", "filter": playlist_album_id},
        )
        songs_root = ET.fromstring(r_songs.text)
        songs = songs_root.findall("song")
        assert len(songs) == 1
        assert songs[0].findtext("title") == "Test Song — Test Artist"
        assert songs[0].findtext("time") == "212"
        assert songs[0].findtext("url") == "http://127.0.0.1:18732/stream/dQw4w9WgXcQ"
        assert songs[0].find("album").attrib["id"] == playlist_album_id
        assert songs[0].find("artist").attrib["id"] == "2"

        # 6. Search filter on artists
        r_search = client.get("/ampache/server/xml.server.php", params={"action": "artists", "filter": "daft punk"})
        s_root = ET.fromstring(r_search.text)
        s_artists = s_root.findall("artist")
        assert len(s_artists) == 1
        assert "daft punk" in (s_artists[0].findtext("name") or "")

        # 7. Flat albums, artist_songs, and songs endpoints + POST support
        r_all_albums = client.post("/ampache/server/xml.server.php", params={"action": "albums", "limit": 2})
        assert r_all_albums.status_code == 200
        assert len(ET.fromstring(r_all_albums.text).findall("album")) == 2

        r_artist_songs = client.get("/ampache/server/xml.server.php", params={"action": "artist_songs", "filter": "2"})
        assert r_artist_songs.status_code == 200
        assert len(ET.fromstring(r_artist_songs.text).findall("song")) == 1

        r_flat_songs = client.get("/ampache/server/xml.server.php", params={"action": "songs", "limit": 1})
        assert r_flat_songs.status_code == 200
        assert len(ET.fromstring(r_flat_songs.text).findall("song")) == 1
