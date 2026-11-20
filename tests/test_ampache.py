"""Tests for the Amarok Ampache XML bridge."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from amarok_ytmusic.ampache_bridge import (
    AmpacheBridge,
    normalize_search_query,
    parse_duration_seconds,
)
from amarok_ytmusic.auth import AuthManager
from amarok_ytmusic.config import SettingsStore
from amarok_ytmusic.server import create_app
from amarok_ytmusic.ytmusic_client import YTMusicCatalogClient


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
    app.state.catalog.search_artists = MagicMock(return_value=[])
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
        flat_song = ET.fromstring(r_flat_songs.text).find("song")
        assert flat_song is not None
        assert flat_song.findtext("rating") == "5"
        song_id = int(flat_song.attrib["id"])

        # 8. Rating action via Ampache XML API
        app.state.catalog.rate_track = MagicMock(return_value={"ok": True, "videoId": "dQw4w9WgXcQ", "rating": "LIKE"})
        r_rate = client.get(
            "/ampache/server/xml.server.php",
            params={"action": "rate", "id": song_id, "rating": 5},
        )
        assert r_rate.status_code == 200
        app.state.catalog.rate_track.assert_called_once_with("dQw4w9WgXcQ", "LIKE")


def test_ampache_bridge_persists_state_across_restarts(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    auth = AuthManager(store)
    auth.is_authenticated = MagicMock(return_value=True)
    catalog = YTMusicCatalogClient(auth)
    catalog.get_root_folders = MagicMock(
        return_value=[
            {
                "level": 1,
                "itemName": "Playlist: Focus",
                "callbackData": "playlist:PL_FOCUS",
                "coverUrl": "",
            }
        ]
    )
    catalog.get_tracks_for_folder = MagicMock(
        return_value=[
            {
                "level": 0,
                "videoId": "vid_persist_1",
                "itemName": "Deep Work",
                "artist": "Synth",
                "album": "Focus",
                "playableUrl": "http://127.0.0.1:18732/stream/vid_persist_1",
                "duration": "2:00",
            }
        ]
    )

    state_file = tmp_path / "ampache_state.json"
    bridge1 = AmpacheBridge(catalog, auth, state_path=state_file)
    xml_albums = bridge1.handle_action("artist_albums", filter_val="2")
    alb_id = int(ET.fromstring(xml_albums).find("album").attrib["id"])
    xml_songs = bridge1.handle_action("album_songs", filter_val=str(alb_id))
    song_id = int(ET.fromstring(xml_songs).find("song").attrib["id"])
    assert state_file.exists()

    # Re-create a new AmpacheBridge from the saved state file and verify IDs are preserved
    bridge2 = AmpacheBridge(catalog, auth, state_path=state_file)
    assert bridge2.get_video_id_for_song_id(song_id) == "vid_persist_1"
    xml_songs_after = bridge2.handle_action("album_songs", filter_val=str(alb_id))
    assert int(ET.fromstring(xml_songs_after).find("song").attrib["id"]) == song_id


def test_ampache_search_by_artist_and_query_normalization(tmp_path: Path) -> None:
    assert normalize_search_query('"gustavo cerati"') == "gustavo cerati"
    assert normalize_search_query("gustavo+cerati") == "gustavo cerati"
    assert normalize_search_query("gustavo_cerati") == "gustavo cerati"
    assert normalize_search_query("+ bien") == "+ bien"

    store = SettingsStore(config_dir=tmp_path / "cfg")
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)
    app.state.auth.is_authenticated = MagicMock(return_value=True)
    app.state.catalog.search_artists = MagicMock(
        return_value=[
            {"browseId": "UC_CERATI", "name": "Gustavo Cerati", "coverUrl": "https://img.example/cerati.jpg"},
            {"browseId": "UC_SODA", "name": "Soda Stereo", "coverUrl": "https://img.example/soda.jpg"},
        ]
    )
    app.state.catalog.get_artist_sections = MagicMock(
        return_value=[
            {
                "level": 1,
                "itemName": "★ Top Songs",
                "callbackData": "playlist:OLAK_TOP",
                "coverUrl": "https://img.example/cerati.jpg",
                "year": 0,
            },
            {
                "level": 1,
                "itemName": "Bocanada",
                "callbackData": "album:MPREb_BOC",
                "coverUrl": "https://img.example/bocanada.jpg",
                "year": 1999,
            },
        ]
    )
    app.state.catalog.get_tracks_for_folder = MagicMock(
        return_value=[
            {
                "level": 0,
                "videoId": "4F-5KEN3Ljg",
                "itemName": "Puente",
                "artist": "Gustavo Cerati",
                "album": "Bocanada",
                "playableUrl": "http://127.0.0.1:18732/stream/4F-5KEN3Ljg",
                "coverUrl": "https://img.example/bocanada.jpg",
                "duration": "4:34",
                "year": 1999,
            }
        ]
    )

    with TestClient(app) as client:
        # Multi-word search using '+' separator normalizes cleanly and returns both Search bucket and real Artists
        r_art = client.get("/ampache/server/xml.server.php", params={"action": "artists", "filter": "gustavo+cerati"})
        assert r_art.status_code == 200
        artists = ET.fromstring(r_art.text).findall("artist")
        names = [a.findtext("name") for a in artists]
        assert names == ["★ Search: gustavo cerati", "Gustavo Cerati", "Soda Stereo"]
        app.state.catalog.search_artists.assert_called_once_with("gustavo cerati", limit=8)

        # Expanding Gustavo Cerati returns Top Songs and real Albums with years
        cerati_id = artists[1].attrib["id"]
        r_albs = client.get("/ampache/server/xml.server.php", params={"action": "artist_albums", "filter": cerati_id})
        albs = ET.fromstring(r_albs.text).findall("album")
        assert [(a.findtext("name"), a.findtext("year")) for a in albs] == [
            ("★ Top Songs", "0"),
            ("Bocanada", "1999"),
        ]

        # Expanding Bocanada returns clean track title without redundant artist suffix
        boc_id = albs[1].attrib["id"]
        r_songs = client.get("/ampache/server/xml.server.php", params={"action": "album_songs", "filter": boc_id})
        songs = ET.fromstring(r_songs.text).findall("song")
        assert len(songs) == 1
        assert songs[0].findtext("title") == "Puente"
        assert songs[0].findtext("artist") == "Gustavo Cerati"
