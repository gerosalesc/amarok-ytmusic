"""Tests for MPRIS2 D-Bus monitor, rating sync, and history scrobbling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from amarok_ytmusic.auth import AuthManager
from amarok_ytmusic.config import SettingsStore
from amarok_ytmusic.mpris_monitor import (
    MPRISMonitor,
    NowPlayingState,
    extract_video_id_from_url,
    rating_float_to_like_status,
)
from amarok_ytmusic.ytmusic_client import YTMusicCatalogClient


def test_extract_video_id_and_rating_mapping() -> None:
    assert extract_video_id_from_url("http://127.0.0.1:18732/stream/5gXrbaDHMrM") == "5gXrbaDHMrM"
    assert extract_video_id_from_url("file:///home/user/Music/song.mp3") == ""
    assert extract_video_id_from_url("") == ""

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
