"""MPRIS2 D-Bus monitor for Amarok: Now-Playing state, auto metadata import, scrobbling, and rating sync."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import quoteattr

from jeepney import DBusAddress, Properties, new_method_call
from jeepney.io.blocking import open_dbus_connection

from .auth import AuthManager
from .metadata_store import MetadataStore, format_last_played
from .track_importer import TrackImporter
from .ytmusic_client import YTMusicCatalogClient

log = logging.getLogger("amarok_ytmusic.mpris")

AMAROK_BUS_NAME = "org.mpris.MediaPlayer2.amarok"
AMAROK_COLLECTION_BUS = "org.kde.amarok"
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

PLAYER_ADDR = DBusAddress(
    MPRIS_OBJECT_PATH,
    bus_name=AMAROK_BUS_NAME,
    interface=MPRIS_PLAYER_IFACE,
)
COLLECTION_ADDR = DBusAddress(
    "/Collection",
    bus_name=AMAROK_COLLECTION_BUS,
    interface="org.kde.amarok.Collection",
)

STREAM_VID_RE = re.compile(r"/stream/([A-Za-z0-9_-]{6,20})(?:$|\?)")
COMMENT_VID_RE = re.compile(r"ytmusic:([A-Za-z0-9_-]{6,20})\b")
POLL_INTERVAL_SECONDS = 3.0
SCROBBLE_THRESHOLD_SECONDS = 15.0
LOCAL_COLLECTION_TTL_SECONDS = 15.0
ALLOWED_CONTROLS = {"PlayPause", "Play", "Pause", "Next", "Previous", "Stop"}


def parse_amarok_dbus_datetime(val: Any) -> float:
    """Convert Amarok's D-Bus QDateTime representation (or timestamp/ISO string) to Unix epoch seconds."""
    if not val:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val) if val > 0 else 0.0
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return max(0.0, dt.timestamp())
        except ValueError:
            return 0.0
    # Qt D-Bus QDateTime struct: ((year, month, day), (hour, minute, second, msec), timespec)
    if isinstance(val, (tuple, list)) and len(val) >= 2:
        date_part, time_part = val[0], val[1]
        if (
            isinstance(date_part, (tuple, list))
            and len(date_part) >= 3
            and isinstance(time_part, (tuple, list))
            and len(time_part) >= 3
        ):
            y, mo, d = int(date_part[0]), int(date_part[1]), int(date_part[2])
            h, mi, s = int(time_part[0]), int(time_part[1]), int(time_part[2])
            if y >= 1970 and 1 <= mo <= 12 and 1 <= d <= 31 and 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59:
                spec = int(val[2]) if len(val) >= 3 else 0
                try:
                    if spec == 1:  # Qt::UTC
                        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()
                    return datetime(y, mo, d, h, mi, s).timestamp()
                except ValueError:
                    return 0.0
    return 0.0


def extract_video_id_from_url(url: str) -> str:
    """Extract a YouTube video_id from a local /stream/<videoId> URL, or empty string."""
    if not url:
        return ""
    m = STREAM_VID_RE.search(url)
    return m.group(1) if m else ""


def extract_video_id_from_comment(comments: Any) -> str:
    """Extract a YouTube video_id from MPRIS xesam:comment (used for imported Local Collection files)."""
    if not comments:
        return ""
    items = comments if isinstance(comments, (list, tuple)) else [comments]
    for c in items:
        m = COMMENT_VID_RE.search(str(c))
        if m:
            return m.group(1)
    return ""


def rating_float_to_like_status(rating: float) -> str | None:
    """Map an MPRIS2 userRating float (0.0..1.0) to a YouTube Music LikeStatus string."""
    if rating <= 0.0:
        return None
    if rating >= 0.75:  # 4 or 5 stars in Amarok
        return "LIKE"
    if rating <= 0.25:  # 1 star in Amarok
        return "DISLIKE"
    return "INDIFFERENT"  # 2 or 3 stars


@dataclass
class NowPlayingState:
    amarok_running: bool = False
    is_ytmusic: bool = False
    playback_status: str = "Stopped"
    video_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    year: int = 0
    cover_url: str = ""
    cover_url_full: str = ""
    url: str = ""
    source: str = ""  # "ampache" | "local" | ""
    position_sec: float = 0.0
    duration_sec: float = 0.0
    user_rating: float = 0.0
    amarok_rating: int = 0  # 0..10 Amarok star scale
    like_status: str = ""
    play_count: int = 0
    score: float = 0.0
    score_display: int = 0
    last_played: float = 0.0
    last_played_display: str = "Never"
    lyrics: str = ""
    lyrics_source: str = ""
    has_local_file: bool = False
    scrobbled: bool = False
    last_synced_rating: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MPRISMonitor:
    """Monitors Amarok via MPRIS2 D-Bus for auto-import, history scrobbling, and two-way rating sync."""

    def __init__(
        self,
        catalog: YTMusicCatalogClient,
        auth_manager: AuthManager,
        metadata_store: MetadataStore | None = None,
        importer: TrackImporter | None = None,
    ) -> None:
        self.catalog = catalog
        self.auth = auth_manager
        self.metadata_store = metadata_store
        self.importer = importer
        self.state = NowPlayingState()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        # Track per-play listening duration, score baseline & rating baseline
        self._active_vid: str = ""
        self._play_started_at: float | None = None
        self._accumulated_play_sec: float = 0.0
        self._max_position_sec: float = 0.0
        self._active_duration_sec: float = 0.0
        self._pre_play_score: float = 0.0
        self._last_score_milestone: float = 0.0
        self._scrobbled_current: bool = False
        self._last_seen_rating: float | None = None
        self._last_synced_like: str = ""
        self._local_lookup_ts: dict[str, float] = {}

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="amarok-ytmusic-mpris")
        log.info("MPRIS2 monitor started for %s", AMAROK_BUS_NAME)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def read_mpris_snapshot_sync(self) -> NowPlayingState:
        """Query Amarok's current MPRIS2 Player properties over the session D-Bus."""
        try:
            with open_dbus_connection(bus="SESSION") as conn:
                reply = conn.send_and_get_reply(Properties(PLAYER_ADDR).get_all(), timeout=1.5)
        except Exception:
            return NowPlayingState(amarok_running=False)

        if not reply or not reply.body or not isinstance(reply.body[0], dict):
            return NowPlayingState(amarok_running=False)

        props = {k: v[1] for k, v in reply.body[0].items() if isinstance(v, tuple) and len(v) == 2}
        raw_meta = props.get("Metadata") or {}
        meta = {k: v[1] for k, v in raw_meta.items() if isinstance(v, tuple) and len(v) == 2}

        url = str(meta.get("xesam:url") or "")
        vid = extract_video_id_from_url(url) or extract_video_id_from_comment(meta.get("xesam:comment"))
        pos_us = int(props.get("Position") or 0)
        len_us = int(meta.get("mpris:length") or 0)
        rating = float(meta.get("xesam:userRating") or 0.0)

        raw_artists = meta.get("xesam:artist") or []
        artist_str = ", ".join(str(a) for a in raw_artists) if isinstance(raw_artists, (list, tuple)) else str(raw_artists)

        return NowPlayingState(
            amarok_running=True,
            is_ytmusic=bool(vid),
            playback_status=str(props.get("PlaybackStatus") or "Stopped"),
            video_id=vid,
            title=str(meta.get("xesam:title") or ""),
            artist=artist_str,
            album=str(meta.get("xesam:album") or ""),
            url=url,
            position_sec=round(max(0.0, pos_us / 1_000_000.0), 1),
            duration_sec=round(max(0.0, len_us / 1_000_000.0), 1),
            user_rating=round(rating, 2),
        )

    def control_sync(self, command: str) -> bool:
        """Send an MPRIS2 playback control method call to Amarok."""
        if command not in ALLOWED_CONTROLS:
            raise ValueError(f"Unsupported MPRIS2 command: {command!r}")
        try:
            with open_dbus_connection(bus="SESSION") as conn:
                msg = new_method_call(PLAYER_ADDR, command)
                conn.send_and_get_reply(msg, timeout=1.5)
            return True
        except Exception as exc:
            log.warning("MPRIS2 command %s failed: %s", command, exc)
            return False

    async def refresh_once(self) -> NowPlayingState:
        snap = await asyncio.to_thread(self.read_mpris_snapshot_sync)
        await self._process_snapshot(snap)
        return self.state

    def query_local_collection_stats_sync(
        self,
        video_id: str,
        title: str = "",
        artist: str = "",
    ) -> dict[str, Any] | None:
        """Query Amarok's Local Collection via D-Bus for local playcount, score, rating, and lastUsed."""
        queries: list[str] = []
        if video_id:
            queries.append(
                f'<query version="1.0"><filters>'
                f'<include field="comment" value={quoteattr(f"ytmusic:{video_id}")}/>'
                f'</filters><limit value="1"/></query>'
            )
        if title:
            artist_filter = (
                f'<include field="artist" value={quoteattr(artist)}/>'
                if artist and not artist.startswith(("★", "♫", "◎"))
                else ""
            )
            queries.append(
                f'<query version="1.0"><filters><and>'
                f'<include field="title" value={quoteattr(title)}/>'
                f"{artist_filter}"
                f'</and></filters><limit value="1"/></query>'
            )
        if not queries:
            return None

        try:
            with open_dbus_connection(bus="SESSION") as conn:
                for qxml in queries:
                    msg = new_method_call(COLLECTION_ADDR, "Query", "s", (qxml,))
                    reply = conn.send_and_get_reply(msg, timeout=1.0)
                    if not reply or not reply.body:
                        continue
                    rows = reply.body[0]
                    if not isinstance(rows, (list, tuple)) or not rows:
                        continue
                    raw = rows[0]
                    if not isinstance(raw, dict):
                        continue
                    item = {
                        k: (v[1] if isinstance(v, tuple) and len(v) == 2 else v)
                        for k, v in raw.items()
                    }
                    return {
                        "play_count": int(item.get("xesam:useCount") or 0),
                        "score": float(item.get("xesam:autoRating") or 0.0),
                        "user_rating": int(item.get("xesam:userRating") or 0),
                        "first_played": parse_amarok_dbus_datetime(item.get("xesam:firstUsed")),
                        "last_played": parse_amarok_dbus_datetime(item.get("xesam:lastUsed")),
                        "local_url": str(item.get("xesam:url") or ""),
                    }
        except Exception as exc:
            log.debug("Amarok Collection D-Bus query failed for %s: %s", video_id, exc)
        return None

    async def _sync_local_collection_stats(self, video_id: str, title: str = "", artist: str = "") -> None:
        """Pull local Amarok SqlCollection stats into our SQLite cache (throttled per video_id)."""
        if not video_id or not self.metadata_store:
            return
        now = time.time()
        last_check = self._local_lookup_ts.get(video_id, 0.0)
        if (now - last_check) < LOCAL_COLLECTION_TTL_SECONDS:
            return
        self._local_lookup_ts[video_id] = now

        cached = self.metadata_store.get_track(video_id)
        q_title = (cached.title if cached and cached.title else title) or ""
        q_artist = (cached.artist if cached and cached.artist else artist) or ""
        local = await asyncio.to_thread(self.query_local_collection_stats_sync, video_id, q_title, q_artist)
        if not local:
            return
        await asyncio.to_thread(
            self.metadata_store.merge_local_amarok_stats,
            video_id,
            play_count=local["play_count"],
            score=local["score"],
            user_rating=local["user_rating"] if local["user_rating"] > 0 else -1,
            first_played=local["first_played"],
            last_played=local["last_played"],
        )

    def _enrich_snapshot_from_cache(self, snap: NowPlayingState) -> None:
        if not snap.is_ytmusic:
            snap.amarok_rating = max(0, min(10, int(round(snap.user_rating * 10))))
            snap.score_display = int(round(snap.score))
            snap.last_played_display = format_last_played(snap.last_played)
            return

        snap.source = "ampache" if "/stream/" in snap.url else "local"
        settings = self.auth.get_settings()

        if not self.metadata_store:
            snap.amarok_rating = max(0, min(10, int(round(snap.user_rating * 10))))
            snap.score_display = int(round(snap.score))
            snap.last_played_display = format_last_played(snap.last_played)
            return

        cached = self.metadata_store.get_track(snap.video_id)
        if not cached:
            snap.amarok_rating = max(0, min(10, int(round(snap.user_rating * 10))))
            snap.score_display = int(round(snap.score))
            snap.last_played_display = format_last_played(snap.last_played)
            return

        if cached.title:
            snap.title = cached.title
        if cached.artist:
            snap.artist = cached.artist
        if cached.album:
            snap.album = cached.album
        snap.year = cached.year
        if cached.cover_local_path:
            snap.cover_url = f"/cover/{snap.video_id}"
            snap.cover_url_full = f"{settings.base_url}/cover/{snap.video_id}"
        else:
            snap.cover_url = cached.cover_url
            snap.cover_url_full = cached.cover_url
        snap.like_status = cached.like_status
        snap.amarok_rating = cached.effective_amarok_rating()
        snap.user_rating = cached.amarok_rating_float()
        snap.play_count = cached.play_count
        snap.score = cached.score
        snap.score_display = int(round(cached.score))
        snap.last_played = cached.last_played
        snap.last_played_display = format_last_played(cached.last_played)
        snap.lyrics = cached.lyrics
        snap.lyrics_source = cached.lyrics_source
        snap.has_local_file = bool(cached.local_file_path)

    async def _process_snapshot(self, snap: NowPlayingState) -> None:
        now = time.time()

        # Reset per-track tracking when video_id changes
        if snap.video_id != self._active_vid:
            # Finalize previous track's Amarok score using actual fraction listened
            if self._active_vid and self._scrobbled_current and self.metadata_store:
                prev_frac = (
                    self._max_position_sec / self._active_duration_sec
                    if self._active_duration_sec > 0
                    else 1.0
                )
                prev_frac = max(0.25, min(1.0, prev_frac))
                await asyncio.to_thread(
                    self.metadata_store.update_last_play_score,
                    self._active_vid,
                    prev_frac,
                    self._pre_play_score,
                )
                if self.importer:
                    asyncio.create_task(
                        asyncio.to_thread(self.importer.sync_tags_for_track_sync, self._active_vid)
                    )

            self._active_vid = snap.video_id
            self._play_started_at = now if (snap.is_ytmusic and snap.playback_status == "Playing") else None
            self._accumulated_play_sec = 0.0
            self._max_position_sec = snap.position_sec if snap.is_ytmusic else 0.0
            self._active_duration_sec = snap.duration_sec if snap.is_ytmusic else 0.0
            self._last_score_milestone = 0.0
            self._scrobbled_current = False
            self._last_seen_rating = snap.user_rating if snap.is_ytmusic else None
            self._last_synced_like = ""

            if snap.is_ytmusic:
                # Pull any local Amarok SqlCollection history/score/rating before recording this play
                self._local_lookup_ts.pop(snap.video_id, None)
                await self._sync_local_collection_stats(snap.video_id, snap.title, snap.artist)
                cached = self.metadata_store.get_track(snap.video_id) if self.metadata_store else None
                self._pre_play_score = cached.score if cached else 0.0

                # Automatically import metadata (+ optional tagged M4A to Amarok Local Collection) on track start
                if self.importer:
                    asyncio.create_task(self.importer.on_track_played(snap.video_id, record_play=False))

        elif snap.is_ytmusic:
            # Periodically merge any Local Collection updates (e.g., if user rated/played in Local Collection)
            await self._sync_local_collection_stats(snap.video_id, snap.title, snap.artist)

            # Update accumulated play time & max observed playback position
            if snap.duration_sec > 0:
                self._active_duration_sec = snap.duration_sec
            if snap.playback_status == "Playing":
                if self._play_started_at is not None:
                    self._accumulated_play_sec += max(0.0, now - self._play_started_at)
                self._play_started_at = now
                self._max_position_sec = max(self._max_position_sec, snap.position_sec, self._accumulated_play_sec)
            else:
                self._play_started_at = None

            # 1. Check if user changed the star rating inside Amarok (e.g. on a Local Collection track)
            if (
                self._last_seen_rating is not None
                and abs(snap.user_rating - self._last_seen_rating) >= 0.05
                and snap.user_rating > 0.0
                and self.auth.is_authenticated()
            ):
                self._last_seen_rating = snap.user_rating
                amarok_int = max(0, min(10, int(round(snap.user_rating * 10))))
                target_like = rating_float_to_like_status(snap.user_rating)
                if target_like and target_like != self._last_synced_like:
                    try:
                        await asyncio.to_thread(self.catalog.rate_track, snap.video_id, target_like)
                        self._last_synced_like = target_like
                        if self.metadata_store:
                            await asyncio.to_thread(
                                self.metadata_store.set_user_rating, snap.video_id, amarok_int, target_like
                            )
                        if self.importer:
                            await asyncio.to_thread(self.importer.sync_tags_for_track_sync, snap.video_id)
                    except Exception as exc:
                        log.warning("Auto-rating sync failed for %s: %s", snap.video_id, exc)
            else:
                self._last_seen_rating = snap.user_rating

            # 2. Check if track qualifies for playback history scrobbling & play-count recording
            settings = self.auth.get_settings()
            effective_listened = max(
                self._accumulated_play_sec,
                snap.position_sec if snap.playback_status == "Playing" else 0.0,
            )
            if (
                not self._scrobbled_current
                and snap.playback_status == "Playing"
                and effective_listened >= SCROBBLE_THRESHOLD_SECONDS
            ):
                self._scrobbled_current = True
                if self.metadata_store:
                    frac = (self._max_position_sec / self._active_duration_sec) if self._active_duration_sec > 0 else 1.0
                    self._last_score_milestone = max(0.5, min(1.0, frac))
                    await asyncio.to_thread(
                        self.metadata_store.record_play, snap.video_id, self._last_score_milestone
                    )
                    if self.importer:
                        asyncio.create_task(asyncio.to_thread(self.importer.sync_tags_for_track_sync, snap.video_id))
                if settings.scrobble_history and self.auth.is_authenticated():
                    asyncio.create_task(asyncio.to_thread(self.catalog.report_playback_history, snap.video_id))
            elif (
                self._scrobbled_current
                and snap.playback_status == "Playing"
                and self.metadata_store
                and self._active_duration_sec > 0
            ):
                # Progressively update Amarok score as playback crosses 65% and 90%
                cur_frac = min(1.0, self._max_position_sec / self._active_duration_sec)
                if cur_frac >= self._last_score_milestone + 0.15 or (cur_frac >= 0.90 and self._last_score_milestone < 0.90):
                    self._last_score_milestone = cur_frac
                    await asyncio.to_thread(
                        self.metadata_store.update_last_play_score,
                        snap.video_id,
                        cur_frac,
                        self._pre_play_score,
                    )

        self._enrich_snapshot_from_cache(snap)
        snap.scrobbled = self._scrobbled_current
        snap.last_synced_rating = self._last_synced_like
        self.state = snap

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.refresh_once()
            except Exception as exc:
                log.debug("MPRIS poll error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
