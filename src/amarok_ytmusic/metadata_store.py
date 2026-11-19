"""SQLite persistence for YouTube Music track metadata, user profile cache, and playback stats."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

METADATA_TTL_SECONDS = 24 * 3600  # Refresh remote metadata/like_status after 24 hours
PROFILE_TTL_SECONDS = 6 * 3600    # Refresh user profile cache after 6 hours


@dataclass
class CachedTrack:
    video_id: str
    title: str = ""
    artist: str = ""
    album: str = ""
    album_browse_id: str = ""
    year: int = 0
    duration_sec: int = 0
    cover_url: str = ""
    cover_local_path: str = ""
    like_status: str = ""  # "LIKE" | "DISLIKE" | "INDIFFERENT" | ""
    in_library: bool = False
    lyrics: str = ""
    lyrics_source: str = ""
    play_count: int = 0
    score: float = 0.0
    first_played: float = 0.0
    last_played: float = 0.0
    local_file_path: str = ""
    fetched_at: float = 0.0

    def amarok_rating_float(self) -> float:
        """Convert YouTube Music like_status into Amarok FMPS_Rating (0.0..1.0)."""
        if self.like_status == "LIKE":
            return 1.0
        if self.like_status == "DISLIKE":
            return 0.2
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ratingFloat"] = self.amarok_rating_float()
        d["hasLocalFile"] = bool(self.local_file_path and Path(self.local_file_path).exists())
        return d


@dataclass
class CachedUserProfile:
    account_name: str = ""
    channel_handle: str = ""
    account_photo_url: str = ""
    auth_mode: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "authenticated": bool(self.account_name),
            "accountName": self.account_name,
            "channelHandle": self.channel_handle,
            "accountPhotoUrl": self.account_photo_url,
            "authMode": self.auth_mode,
            "updatedAt": self.updated_at,
        }


class MetadataStore:
    """Thread-safe SQLite cache for rich song metadata, user info, and Amarok playback statistics."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    video_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    artist TEXT NOT NULL DEFAULT '',
                    album TEXT NOT NULL DEFAULT '',
                    album_browse_id TEXT NOT NULL DEFAULT '',
                    year INTEGER NOT NULL DEFAULT 0,
                    duration_sec INTEGER NOT NULL DEFAULT 0,
                    cover_url TEXT NOT NULL DEFAULT '',
                    cover_local_path TEXT NOT NULL DEFAULT '',
                    like_status TEXT NOT NULL DEFAULT '',
                    in_library INTEGER NOT NULL DEFAULT 0,
                    lyrics TEXT NOT NULL DEFAULT '',
                    lyrics_source TEXT NOT NULL DEFAULT '',
                    play_count INTEGER NOT NULL DEFAULT 0,
                    score REAL NOT NULL DEFAULT 0.0,
                    first_played REAL NOT NULL DEFAULT 0.0,
                    last_played REAL NOT NULL DEFAULT 0.0,
                    local_file_path TEXT NOT NULL DEFAULT '',
                    fetched_at REAL NOT NULL DEFAULT 0.0
                );

                CREATE INDEX IF NOT EXISTS idx_tracks_last_played ON tracks(last_played DESC);
                CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);

                CREATE TABLE IF NOT EXISTS user_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    account_name TEXT NOT NULL DEFAULT '',
                    channel_handle TEXT NOT NULL DEFAULT '',
                    account_photo_url TEXT NOT NULL DEFAULT '',
                    auth_mode TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0.0
                );
                """
            )

    @staticmethod
    def _row_to_track(row: sqlite3.Row) -> CachedTrack:
        return CachedTrack(
            video_id=str(row["video_id"]),
            title=str(row["title"]),
            artist=str(row["artist"]),
            album=str(row["album"]),
            album_browse_id=str(row["album_browse_id"]),
            year=int(row["year"]),
            duration_sec=int(row["duration_sec"]),
            cover_url=str(row["cover_url"]),
            cover_local_path=str(row["cover_local_path"]),
            like_status=str(row["like_status"]),
            in_library=bool(row["in_library"]),
            lyrics=str(row["lyrics"]),
            lyrics_source=str(row["lyrics_source"]),
            play_count=int(row["play_count"]),
            score=float(row["score"]),
            first_played=float(row["first_played"]),
            last_played=float(row["last_played"]),
            local_file_path=str(row["local_file_path"]),
            fetched_at=float(row["fetched_at"]),
        )

    def get_track(self, video_id: str) -> CachedTrack | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
            return self._row_to_track(row) if row else None

    def is_metadata_fresh(self, track: CachedTrack | None, ttl: float = METADATA_TTL_SECONDS) -> bool:
        if track is None or not track.title:
            return False
        return (time.time() - track.fetched_at) < ttl

    def upsert_metadata(self, track: CachedTrack) -> CachedTrack:
        """Insert or update metadata while preserving existing play statistics and local_file_path."""
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT * FROM tracks WHERE video_id = ?", (track.video_id,)).fetchone()
            if existing:
                ex = self._row_to_track(existing)
                merged = CachedTrack(
                    video_id=track.video_id,
                    title=track.title or ex.title,
                    artist=track.artist or ex.artist,
                    album=track.album or ex.album,
                    album_browse_id=track.album_browse_id or ex.album_browse_id,
                    year=track.year or ex.year,
                    duration_sec=track.duration_sec or ex.duration_sec,
                    cover_url=track.cover_url or ex.cover_url,
                    cover_local_path=track.cover_local_path or ex.cover_local_path,
                    like_status=track.like_status if track.like_status else ex.like_status,
                    in_library=track.in_library or ex.in_library,
                    lyrics=track.lyrics or ex.lyrics,
                    lyrics_source=track.lyrics_source or ex.lyrics_source,
                    play_count=max(track.play_count, ex.play_count),
                    score=max(track.score, ex.score),
                    first_played=ex.first_played or track.first_played,
                    last_played=max(track.last_played, ex.last_played),
                    local_file_path=track.local_file_path or ex.local_file_path,
                    fetched_at=track.fetched_at or time.time(),
                )
            else:
                merged = track
                if not merged.fetched_at:
                    merged.fetched_at = time.time()

            conn.execute(
                """
                INSERT OR REPLACE INTO tracks (
                    video_id, title, artist, album, album_browse_id, year, duration_sec,
                    cover_url, cover_local_path, like_status, in_library, lyrics, lyrics_source,
                    play_count, score, first_played, last_played, local_file_path, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    merged.video_id,
                    merged.title,
                    merged.artist,
                    merged.album,
                    merged.album_browse_id,
                    merged.year,
                    merged.duration_sec,
                    merged.cover_url,
                    merged.cover_local_path,
                    merged.like_status,
                    int(merged.in_library),
                    merged.lyrics,
                    merged.lyrics_source,
                    merged.play_count,
                    merged.score,
                    merged.first_played,
                    merged.last_played,
                    merged.local_file_path,
                    merged.fetched_at,
                ),
            )
            return merged

    def record_play(self, video_id: str, played_fraction: float = 1.0) -> CachedTrack:
        """Increment play count, update last_played, and recalculate Amarok-style score (0..100)."""
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
            track = self._row_to_track(row) if row else CachedTrack(video_id=video_id)

            new_count = track.play_count + 1
            pct = max(10.0, min(100.0, played_fraction * 100.0))
            if track.play_count == 0:
                new_score = round((50.0 + pct) / 2.0, 1)  # Amarok's standard first-play score formula
            else:
                new_score = round(((track.score * track.play_count) + pct) / new_count, 1)

            track.play_count = new_count
            track.score = min(100.0, new_score)
            if not track.first_played:
                track.first_played = now
            track.last_played = now
            return self.upsert_metadata(track)

    def update_like_status(self, video_id: str, like_status: str) -> CachedTrack | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
            if not row:
                track = CachedTrack(video_id=video_id, like_status=like_status)
                return self.upsert_metadata(track)
            conn.execute(
                "UPDATE tracks SET like_status = ? WHERE video_id = ?",
                (like_status, video_id),
            )
            return self.get_track(video_id)

    def set_local_file_path(self, video_id: str, file_path: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tracks SET local_file_path = ? WHERE video_id = ?",
                (file_path, video_id),
            )

    def list_tracks(self, limit: int = 200) -> list[CachedTrack]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tracks ORDER BY last_played DESC, fetched_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def list_cached_artists(self) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT artist FROM tracks WHERE artist != '' ORDER BY artist COLLATE NOCASE ASC"
            ).fetchall()
            return [str(r["artist"]) for r in rows]

    def list_tracks_by_artist(self, artist: str) -> list[CachedTrack]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tracks WHERE artist = ? ORDER BY album COLLATE NOCASE ASC, title COLLATE NOCASE ASC",
                (artist,),
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def list_local_cached_files_lru(self) -> list[CachedTrack]:
        """Return tracks with local audio files ordered oldest-played first (for cache eviction)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tracks WHERE local_file_path != '' ORDER BY last_played ASC, fetched_at ASC"
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def stats_summary(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_tracks,
                    COALESCE(SUM(play_count), 0) AS total_plays,
                    SUM(CASE WHEN like_status = 'LIKE' THEN 1 ELSE 0 END) AS liked_cached,
                    SUM(CASE WHEN local_file_path != '' THEN 1 ELSE 0 END) AS local_files
                FROM tracks
                """
            ).fetchone()
            return {
                "totalTracks": int(row["total_tracks"] or 0),
                "totalPlays": int(row["total_plays"] or 0),
                "likedCached": int(row["liked_cached"] or 0),
                "localFiles": int(row["local_files"] or 0),
            }

    def save_user_profile(self, profile: CachedUserProfile) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_profile (id, account_name, channel_handle, account_photo_url, auth_mode, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (
                    profile.account_name,
                    profile.channel_handle,
                    profile.account_photo_url,
                    profile.auth_mode,
                    profile.updated_at or time.time(),
                ),
            )

    def get_user_profile(self, max_age: float = PROFILE_TTL_SECONDS) -> CachedUserProfile | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
            if not row or not row["account_name"]:
                return None
            prof = CachedUserProfile(
                account_name=str(row["account_name"]),
                channel_handle=str(row["channel_handle"]),
                account_photo_url=str(row["account_photo_url"]),
                auth_mode=str(row["auth_mode"]),
                updated_at=float(row["updated_at"]),
            )
            if max_age > 0 and (time.time() - prof.updated_at) > max_age:
                return None
            return prof

    def clear_user_profile(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM user_profile")
