"""Tests for MPRIS2 D-Bus monitor, rating sync, and history scrobbling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from amarok_ytmusic.auth import AuthManager
from amarok_ytmusic.config import SettingsStore
from amarok_ytmusic.metadata_store import MetadataStore
from amarok_ytmusic.mpris_monitor import (
    MPRISMonitor,
    NowPlayingState,
    extract_video_id_from_comment,
    extract_video_id_from_url,
    parse_amarok_dbus_datetime,
    rating_float_to_like_status,
)
from amarok_ytmusic.ytmusic_client import YTMusicCatalogClient


def test_extract_video_id_and_rating_mapping() -> None:
    assert extract_video_id_from_url("http://127.0.0.1:18732/stream/5gXrbaDHMrM") == "5gXrbaDHMrM"
    assert extract_video_id_from_url("file:///home/user/Music/song.mp3") == ""
    assert extract_video_id_from_url("") == ""

    assert extract_video_id_from_comment(["ytmusic:5gXrbaDHMrM"]) == "5gXrbaDHMrM"
    assert extract_video_id_from_comment(["regular comment"]) == ""

    assert rating_float_to_like_status(0.0) is None
    assert rating_float_to_like_status(1.0) == "LIKE"
    assert rating_float_to_like_status(0.8) == "LIKE"
    assert rating_float_to_like_status(0.2) == "DISLIKE"
    assert rating_float_to_like_status(0.6) == "INDIFFERENT"


async def test_mpris_monitor_scrobbles_and_syncs_rating(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    auth = AuthManager(store)
    auth.is_authenticated = MagicMock(return_value=True)

    catalog = YTMusicCatalogClient(auth)
    catalog.report_playback_history = MagicMock(return_value=True)
    catalog.rate_track = MagicMock(return_value={"ok": True, "videoId": "5gXrbaDHMrM", "rating": "LIKE"})

    monitor = MPRISMonitor(catalog, auth)

    # 1. Initial load at position 2.0s (below 15s scrobble threshold), rating 0.0
    snap1 = NowPlayingState(
        amarok_running=True,
        is_ytmusic=True,
        playback_status="Playing",
        video_id="5gXrbaDHMrM",
        title="She's Knocking — Ikebe Shakedown",
        album="Library Songs",
        url="http://127.0.0.1:18732/stream/5gXrbaDHMrM",
        position_sec=2.0,
        duration_sec=195.0,
        user_rating=0.0,
    )
    await monitor._process_snapshot(snap1)
    assert monitor.state.scrobbled is False
    catalog.report_playback_history.assert_not_called()
    catalog.rate_track.assert_not_called()

    # 2. User plays past 15s and rates the track 5 stars (1.0) in Amarok
    snap2 = NowPlayingState(
        amarok_running=True,
        is_ytmusic=True,
        playback_status="Playing",
        video_id="5gXrbaDHMrM",
        title="She's Knocking — Ikebe Shakedown",
        album="Library Songs",
        url="http://127.0.0.1:18732/stream/5gXrbaDHMrM",
        position_sec=20.0,
        duration_sec=195.0,
        user_rating=1.0,
    )
    await monitor._process_snapshot(snap2)
    await asyncio.sleep(0.05)  # allow background scrobble task to complete

    assert monitor.state.scrobbled is True
    assert monitor.state.last_synced_rating == "LIKE"
    catalog.report_playback_history.assert_called_once_with("5gXrbaDHMrM")
    catalog.rate_track.assert_called_once_with("5gXrbaDHMrM", "LIKE")

    # 3. Subsequent poll on same track does not duplicate scrobble or rating call
    await monitor._process_snapshot(snap2)
    await asyncio.sleep(0.02)
    assert catalog.report_playback_history.call_count == 1
    assert catalog.rate_track.call_count == 1


async def test_mpris_merges_local_collection_stats_and_refines_score(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    auth = AuthManager(store)
    auth.is_authenticated = MagicMock(return_value=False)
    catalog = YTMusicCatalogClient(auth)
    meta_store = MetadataStore(tmp_path / "metadata_cache.db")

    monitor = MPRISMonitor(catalog, auth, metadata_store=meta_store)
    # Simulate Amarok Local Collection returning existing local play history for an Ampache track
    monitor.query_local_collection_stats_sync = MagicMock(
        return_value={
            "play_count": 3,
            "score": 82.0,
            "user_rating": 8,
            "first_played": 1700000000.0,
            "last_played": 1795000000.0,
            "local_url": "/home/user/Music/YouTube Music/Artist/Album/Track.m4a",
        }
    )

    # Parse Qt D-Bus QDateTime tuples cleanly
    assert parse_amarok_dbus_datetime(((0, 0, 0), (-1, -1, -1, -1), 0)) == 0.0
    assert parse_amarok_dbus_datetime(((2026, 11, 20), (12, 30, 0, 0), 1)) > 1795000000.0

    # 1. Track starts playing from Ampache: local collection stats appear immediately
    snap_start = NowPlayingState(
        amarok_running=True,
        is_ytmusic=True,
        playback_status="Playing",
        video_id="RJRl5zKyEls",
        title="Great Days Full Theme — Josephine cherry joestar",
        artist="★ YouTube Music — My Library",
        album="Liked Music",
        url="http://127.0.0.1:18732/stream/RJRl5zKyEls",
        position_sec=2.0,
        duration_sec=240.0,
        user_rating=0.0,
    )
    await monitor._process_snapshot(snap_start)
    assert monitor.state.source == "ampache"
    assert monitor.state.play_count == 3
    assert monitor.state.score == 82.0
    assert monitor.state.amarok_rating == 8
    assert monitor.state.last_played_display != "Never"

    # 2. Listening past 15s increments play_count to 4, then progressing to 95% refines score upward
    snap_mid = NowPlayingState(**{**snap_start.__dict__, "position_sec": 20.0})
    await monitor._process_snapshot(snap_mid)
    assert monitor.state.play_count == 4

    snap_end = NowPlayingState(**{**snap_start.__dict__, "position_sec": 228.0})
    await monitor._process_snapshot(snap_end)
    assert monitor.state.play_count == 4
    assert monitor.state.score > 82.0


async def test_mpris_ignores_transient_dbus_miss_and_never_double_scrobbles(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    auth = AuthManager(store)
    auth.is_authenticated = MagicMock(return_value=True)
    catalog = YTMusicCatalogClient(auth)
    catalog.report_playback_history = MagicMock(return_value=True)
    meta_store = MetadataStore(tmp_path / "metadata_cache.db")

    monitor = MPRISMonitor(catalog, auth, metadata_store=meta_store)
    monitor.query_local_collection_stats_sync = MagicMock(return_value=None)

    playing = NowPlayingState(
        amarok_running=True,
        is_ytmusic=True,
        playback_status="Playing",
        video_id="pknZlCdgZf4",
        title="Perdonar Es Divino",
        url="http://127.0.0.1:18732/stream/pknZlCdgZf4",
        position_sec=30.0,
        duration_sec=300.0,
    )
    await monitor._process_snapshot(playing)
    await monitor._process_snapshot(NowPlayingState(**{**playing.__dict__, "position_sec": 35.0}))
    await asyncio.sleep(0.05)
    assert catalog.report_playback_history.call_count == 1
    assert meta_store.get_track("pknZlCdgZf4").play_count == 1

    # Single transient D-Bus timeout (amarok_running=False) must NOT reset scrobble state
    await monitor._process_snapshot(NowPlayingState(amarok_running=False))
    await monitor._process_snapshot(NowPlayingState(**{**playing.__dict__, "position_sec": 45.0}))
    await asyncio.sleep(0.05)
    assert catalog.report_playback_history.call_count == 1
    assert meta_store.get_track("pknZlCdgZf4").play_count == 1

    # Even two consecutive D-Bus misses (full reset) followed by same track mid-song must NOT double-scrobble
    await monitor._process_snapshot(NowPlayingState(amarok_running=False))
    await monitor._process_snapshot(NowPlayingState(amarok_running=False))
    await monitor._process_snapshot(NowPlayingState(**{**playing.__dict__, "position_sec": 55.0}))
    await monitor._process_snapshot(NowPlayingState(**{**playing.__dict__, "position_sec": 60.0}))
    await asyncio.sleep(0.05)
    assert catalog.report_playback_history.call_count == 1
    assert meta_store.get_track("pknZlCdgZf4").play_count == 1
