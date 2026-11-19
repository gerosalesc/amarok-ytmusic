"""Unit tests for SQLite MetadataStore (track metadata, stats, user profile cache)."""

from __future__ import annotations

import time
from pathlib import Path

from amarok_ytmusic.metadata_store import CachedTrack, CachedUserProfile, MetadataStore


def test_metadata_store_upsert_merges_and_preserves_stats(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata_cache.db")

    t1 = store.upsert_metadata(
        CachedTrack(
            video_id="5gXrbaDHMrM",
            title="She's Knocking",
            artist="Ikebe Shakedown",
            album="The Way Home",
            year=2017,
            duration_sec=195,
            like_status="LIKE",
            in_library=True,
            lyrics="Instrumental groove",
        )
    )
    assert t1.title == "She's Knocking"
    assert t1.amarok_rating_float() == 1.0

    # Record plays and verify play_count, score, first_played, last_played
    p1 = store.record_play("5gXrbaDHMrM", played_fraction=1.0)
    assert p1.play_count == 1
    assert p1.score == 75.0
    assert p1.first_played > 0
    assert p1.last_played >= p1.first_played

    p2 = store.record_play("5gXrbaDHMrM", played_fraction=1.0)
    assert p2.play_count == 2
    assert p2.score == 87.5

    # Subsequent metadata refresh preserves existing play_count, score, and local_file_path
    store.set_local_file_path("5gXrbaDHMrM", "/tmp/fake.m4a")
    refreshed = store.upsert_metadata(
        CachedTrack(
            video_id="5gXrbaDHMrM",
            title="She's Knocking",
            artist="Ikebe Shakedown",
            album="The Way Home",
            year=2017,
        )
    )
    assert refreshed.play_count == 2
    assert refreshed.score == 87.5
    assert refreshed.local_file_path == "/tmp/fake.m4a"
    assert refreshed.like_status == "LIKE"

    # Artist index & summary stats
    assert store.list_cached_artists() == ["Ikebe Shakedown"]
    summary = store.stats_summary()
    assert summary["totalTracks"] == 1
    assert summary["totalPlays"] == 2
    assert summary["likedCached"] == 1
    assert summary["localFiles"] == 1


def test_user_profile_cache_and_expiry(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata_cache.db")
    assert store.get_user_profile() is None

    store.save_user_profile(
        CachedUserProfile(
            account_name="German Rosales",
            channel_handle="@gerosalesc",
            account_photo_url="https://img.example/avatar.jpg",
            auth_mode="browser",
            updated_at=time.time(),
        )
    )
    prof = store.get_user_profile(max_age=3600)
    assert prof is not None
    assert prof.account_name == "German Rosales"
    assert prof.channel_handle == "@gerosalesc"

    # Expired check
    store.save_user_profile(
        CachedUserProfile(
            account_name="Old User",
            updated_at=time.time() - 99999,
        )
    )
    assert store.get_user_profile(max_age=3600) is None

    store.clear_user_profile()
    assert store.get_user_profile(max_age=0) is None
