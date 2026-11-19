"""MPRIS2 D-Bus monitor for Amarok: Now-Playing state, auto metadata import, scrobbling, and rating sync."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from jeepney import DBusAddress, Properties, new_method_call
from jeepney.io.blocking import open_dbus_connection

from .auth import AuthManager
from .metadata_store import MetadataStore
from .track_importer import TrackImporter
from .ytmusic_client import YTMusicCatalogClient

log = logging.getLogger("amarok_ytmusic.mpris")

AMAROK_BUS_NAME = "org.mpris.MediaPlayer2.amarok"
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

PLAYER_ADDR = DBusAddress(
    MPRIS_OBJECT_PATH,
    bus_name=AMAROK_BUS_NAME,
    interface=MPRIS_PLAYER_IFACE,
)

STREAM_VID_RE = re.compile(r"/stream/([A-Za-z0-9_-]{6,20})(?:$|\?)")
COMMENT_VID_RE = re.compile(r"ytmusic:([A-Za-z0-9_-]{6,20})\b")
POLL_INTERVAL_SECONDS = 3.0
SCROBBLE_THRESHOLD_SECONDS = 15.0
ALLOWED_CONTROLS = {"PlayPause", "Play", "Pause", "Next", "Previous", "Stop"}


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
    url: str = ""
    position_sec: float = 0.0
    duration_sec: float = 0.0
    user_rating: float = 0.0
    like_status: str = ""
    play_count: int = 0
    score: float = 0.0
    last_played: float = 0.0
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

        # Track per-play listening duration & rating baseline
        self._active_vid: str = ""
        self._play_started_at: float | None = None
        self._accumulated_play_sec: float = 0.0
        self._scrobbled_current: bool = False
        self._last_seen_rating: float | None = None
        self._last_synced_like: str = ""

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

    def _enrich_snapshot_from_cache(self, snap: NowPlayingState) -> None:
        if not snap.is_ytmusic or not self.metadata_store:
            return
        cached = self.metadata_store.get_track(snap.video_id)
        if not cached:
            return
        if cached.title:
            snap.title = cached.title
        if cached.artist:
            snap.artist = cached.artist
        if cached.album:
            snap.album = cached.album
        snap.year = cached.year
        snap.cover_url = f"/cover/{snap.video_id}" if cached.cover_local_path else cached.cover_url
        snap.like_status = cached.like_status
        snap.play_count = cached.play_count
        snap.score = cached.score
        snap.last_played = cached.last_played
        snap.has_local_file = bool(cached.local_file_path)

    async def _process_snapshot(self, snap: NowPlayingState) -> None:
        now = time.time()

        # Reset per-track tracking when video_id changes
        if snap.video_id != self._active_vid:
            self._active_vid = snap.video_id
            self._play_started_at = now if (snap.is_ytmusic and snap.playback_status == "Playing") else None
            self._accumulated_play_sec = 0.0
            self._scrobbled_current = False
            self._last_seen_rating = snap.user_rating if snap.is_ytmusic else None
            self._last_synced_like = ""

            # Automatically import metadata (+ optional tagged M4A to Amarok Local Collection) on track start
            if snap.is_ytmusic and self.importer:
                asyncio.create_task(self.importer.on_track_played(snap.video_id, record_play=False))

        elif snap.is_ytmusic:
            # Update accumulated play time
            if snap.playback_status == "Playing":
                if self._play_started_at is not None:
                    self._accumulated_play_sec += max(0.0, now - self._play_started_at)
                self._play_started_at = now
            else:
                self._play_started_at = None

            # 1. Check if user changed the star rating inside Amarok
            if (
                self._last_seen_rating is not None
                and abs(snap.user_rating - self._last_seen_rating) >= 0.05
                and self.auth.is_authenticated()
            ):
                self._last_seen_rating = snap.user_rating
                target_like = rating_float_to_like_status(snap.user_rating)
                if target_like and target_like != self._last_synced_like:
                    try:
                        await asyncio.to_thread(self.catalog.rate_track, snap.video_id, target_like)
                        self._last_synced_like = target_like
                        if self.metadata_store:
                            await asyncio.to_thread(self.metadata_store.update_like_status, snap.video_id, target_like)
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
                    frac = (snap.position_sec / snap.duration_sec) if snap.duration_sec > 0 else 1.0
                    await asyncio.to_thread(self.metadata_store.record_play, snap.video_id, max(0.5, min(1.0, frac)))
                    if self.importer:
                        asyncio.create_task(asyncio.to_thread(self.importer.sync_tags_for_track_sync, snap.video_id))
                if settings.scrobble_history and self.auth.is_authenticated():
                    asyncio.create_task(asyncio.to_thread(self.catalog.report_playback_history, snap.video_id))

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
