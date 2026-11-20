"""Unit tests for SQLite MetadataStore (track metadata, stats, user profile cache)."""

from __future__ import annotations

import time
from pathlib import Path

from amarok_ytmusic.metadata_store import (
    CachedTrack,
    CachedUserProfile,
    MetadataStore,
    amarok_rating_to_like_status,
    format_last_played,
)


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


def test_user_rating_score_refinement_and_local_merge(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "metadata_cache.db")

    # Star rating persistence (0..10 scale) + mapping to YT Music like_status
    assert amarok_rating_to_like_status(10) == "LIKE"
    assert amarok_rating_to_like_status(8) == "LIKE"
    assert amarok_rating_to_like_status(6) == "INDIFFERENT"
    assert amarok_rating_to_like_status(2) == "DISLIKE"
    assert amarok_rating_to_like_status(0) == "INDIFFERENT"

    rated = store.set_user_rating("RJRl5zKyEls", 7)
    assert rated.effective_amarok_rating() == 7
    assert rated.amarok_rating_float() == 0.7
    assert rated.like_status == "INDIFFERENT"

    # Score refinement on current play (no play_count double-increment)
    p1 = store.record_play("RJRl5zKyEls", played_fraction=0.5)
    assert p1.play_count == 1
    assert p1.score == 50.0

    refined = store.update_last_play_score("RJRl5zKyEls", played_fraction=1.0, pre_play_score=0.0)
    assert refined is not None
    assert refined.play_count == 1
    assert refined.score == 75.0

    # Merging higher local Amarok SqlCollection stats preserves explicit user_rating while adopting higher counts/scores
    merged = store.merge_local_amarok_stats(
        "RJRl5zKyEls",
        play_count=4,
        score=88.5,
        user_rating=10,  # should not overwrite explicit 7
        first_played=1700000000.0,
        last_played=1795182900.0,
    )
    assert merged is not None
    assert merged.play_count == 4
    assert merged.score == 88.5
    assert merged.effective_amarok_rating() == 7
    assert merged.first_played == 1700000000.0
    assert merged.last_played >= 1795182900.0

    # Human-readable last played labels
    now = 1795183000.0
    assert format_last_played(0.0, now=now) == "Never"
    assert format_last_played(now - 20, now=now) == "Just now"
    assert format_last_played(now - 300, now=now) == "5 minutes ago"
    assert format_last_played(now - 7200, now=now) == "2 hours ago"
