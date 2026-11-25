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


def test_api_cache_tracks_reports_local_file_status(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)

    fake_audio_file = tmp_path / "cached.m4a"
    fake_audio_file.write_bytes(b"M4A_FAKE_AUDIO_BYTES_0123456789" * 100)

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
        r_cache = client.get("/api/cache/tracks")
        assert r_cache.status_code == 200
        body = r_cache.json()
        assert body["stats"]["totalTracks"] == 1
        assert body["items"][0]["hasLocalFile"] is True
        assert body["items"][0]["fileSizeBytes"] == fake_audio_file.stat().st_size


def _make_blank_m4a(path: Path) -> Path:
    """Create a valid minimal untagged MP4/M4A file for mutagen testing."""
    import struct

    def box(btype: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(payload), btype) + payload

    ftyp = box(b"ftyp", b"M4A \x00\x00\x00\x00M4A isommp42")
    mvhd = box(b"mvhd", b"\x00" * 100)
    mdhd = box(b"mdhd", b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 44100, 44100 * 210) + b"\x00\x00\x00\x00")
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
    return path


def test_default_cache_limit_is_unlimited_and_env_override_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    settings = store.load()
    assert settings.max_collection_cache_mb == 0
    assert settings.auto_import_collection is True

    custom_env = tmp_path / "env_cache"
    monkeypatch.setenv("AMAROK_YTMUSIC_CACHE_DIR", str(custom_env))
    assert settings.resolved_collection_dir() == custom_env


def test_scan_cache_dir_reindexes_tagged_m4a_extracts_cover_and_prunes_stale(tmp_path: Path) -> None:
    from amarok_ytmusic.track_importer import write_mp4_tags

    store = SettingsStore(config_dir=tmp_path / "cfg")
    cache_dir = tmp_path / "my_offline_songs"
    settings = store.load()
    settings.collection_dir = str(cache_dir)
    store.save(settings)

    # Create a real tagged M4A file with cover art, lyrics, rating, playcount & score
    cover_src = tmp_path / "cover_src.jpg"
    cover_bytes = b"\xff\xd8\xff\xe0" + b"J" * 300
    cover_src.write_bytes(cover_bytes)

    m4a_file = _make_blank_m4a(cache_dir / "Gustavo Cerati" / "Bocanada" / "Puente.m4a")
    src_track = CachedTrack(
        video_id="4F-5KEN3Ljg",
        title="Puente",
        artist="Gustavo Cerati",
        album="Bocanada",
        year=1999,
        cover_local_path=str(cover_src),
        like_status="LIKE",
        user_rating=10,
        lyrics="Hoy te busqué en la rima que duerme",
        play_count=7,
        score=91.0,
    )
    assert write_mp4_tags(m4a_file, src_track) is True

    # Start with a fresh app/DB containing one stale entry pointing to a nonexistent file
    app = create_app(store=store, ipc_dir=tmp_path / "ipc", enable_ipc=False, enable_mpris=False)
    app.state.metadata_store.upsert_metadata(
        CachedTrack(video_id="StaleTrack1", title="Deleted", local_file_path=str(cache_dir / "gone.m4a"))
    )

    with TestClient(app) as client:
        r_scan = client.post("/api/cache/scan")
        assert r_scan.status_code == 200
        body = r_scan.json()
        assert body["scan"]["scannedFiles"] == 1
        assert body["scan"]["indexedTracks"] == 1
        assert body["scan"]["prunedMissing"] == 1
        assert body["stats"]["offlineFiles"] == 1

        reindexed = app.state.metadata_store.get_track("4F-5KEN3Ljg")
        assert reindexed is not None
        assert reindexed.title == "Puente"
        assert reindexed.artist == "Gustavo Cerati"
        assert reindexed.album == "Bocanada"
        assert reindexed.year == 1999
        assert reindexed.duration_sec == 210
        assert reindexed.play_count == 7
        assert reindexed.score == 91.0
        assert reindexed.effective_amarok_rating() == 10
        assert reindexed.lyrics == "Hoy te busqué en la rima que duerme"
        assert reindexed.local_file_path == str(m4a_file)
        assert reindexed.cover_local_path and Path(reindexed.cover_local_path).read_bytes() == cover_bytes

        stale = app.state.metadata_store.get_track("StaleTrack1")
        assert stale is not None
        assert stale.local_file_path == ""


def test_filename_collision_disambiguates_different_video_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    cache_dir = tmp_path / "cache"
    settings = store.load()
    settings.collection_dir = str(cache_dir)
    store.save(settings)

    auth = AuthManager(store)
    meta_store = MetadataStore(store.metadata_db_path)
    importer = TrackImporter(auth, meta_store)

    # Pre-seed two distinct video_ids with identical Artist / Album / Title metadata
    t1 = meta_store.upsert_metadata(
        CachedTrack(video_id="VidFirst001", title="Same Title", artist="Same Artist", album="Same Album")
    )
    t2 = meta_store.upsert_metadata(
        CachedTrack(video_id="VidSecond02", title="Same Title", artist="Same Artist", album="Same Album")
    )
    monkeypatch.setattr(importer, "fetch_and_cache_metadata_sync", lambda vid, force_refresh=False: meta_store.get_track(vid))
    monkeypatch.setattr(importer, "_download_m4a_sync", lambda vid, out_base: _make_blank_m4a(out_base.with_suffix(".m4a")))

    res1 = importer.import_to_local_collection_sync(t1.video_id)
    res2 = importer.import_to_local_collection_sync(t2.video_id)

    assert Path(res1.local_file_path).name == "Same Title.m4a"
    assert Path(res2.local_file_path).name == "Same Title [VidSecond02].m4a"
    assert Path(res1.local_file_path).is_file()
    assert Path(res2.local_file_path).is_file()
    assert res1.local_file_path != res2.local_file_path


def test_offline_metadata_fetch_preserves_existing_without_bumping_fetched_at(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    auth = AuthManager(store)
    meta_store = MetadataStore(store.metadata_db_path)

    old_ts = 1000.0
    meta_store.upsert_metadata(
        CachedTrack(
            video_id="OffVid00001",
            title="Offline Song",
            artist="Offline Artist",
            album="Offline Album",
            lyrics="Keep these lyrics",
            fetched_at=old_ts,
        )
    )

    fake_yt = MagicMock()
    fake_yt.get_watch_playlist.side_effect = RuntimeError("No internet")
    fake_yt.get_song.side_effect = RuntimeError("No internet")
    auth.create_ytmusic = MagicMock(return_value=fake_yt)

    importer = TrackImporter(auth, meta_store)
    got = importer.fetch_and_cache_metadata_sync("OffVid00001")
    assert got.title == "Offline Song"
    assert got.lyrics == "Keep these lyrics"
    assert got.fetched_at == old_ts


def test_cli_cache_configure_move_scan_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from amarok_ytmusic.cli import main as cli_main
    from amarok_ytmusic.track_importer import write_mp4_tags

    cfg_dir = tmp_path / "cli_cfg"
    monkeypatch.setenv("AMAROK_YTMUSIC_CONFIG_DIR", str(cfg_dir))

    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"

    # Set initial cache dir and limit via CLI
    assert cli_main(["cache", "--set-dir", str(dir_a), "--limit-mb", "0"]) == 0
    store = SettingsStore(config_dir=cfg_dir)
    assert store.load().resolved_collection_dir() == dir_a
    assert store.load().max_collection_cache_mb == 0

    # Place a tagged M4A into dir_a and scan via CLI
    m4a = _make_blank_m4a(dir_a / "Band" / "LP" / "Track.m4a")
    write_mp4_tags(m4a, CachedTrack(video_id="CliVid00001", title="Track", artist="Band", album="LP"))
    assert cli_main(["cache", "--scan"]) == 0
    out_scan = capsys.readouterr().out
    assert "1 indexed" in out_scan

    # Move cache dir_a -> dir_b via CLI
    assert cli_main(["cache", "--set-dir", str(dir_b), "--move"]) == 0
    out_move = capsys.readouterr().out
    assert "moved=1" in out_move
    assert (dir_b / "Band" / "LP" / "Track.m4a").is_file()
    assert not m4a.exists()

    # Check status command displays offline cache folder & counts
    assert cli_main(["status"]) == 0
    out_status = capsys.readouterr().out
    assert str(dir_b) in out_status
    assert "1 songs cached" in out_status
