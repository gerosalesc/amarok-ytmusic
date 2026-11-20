"""High-level YouTube Music catalog client for Amarok's 2-level ScriptableService."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from typing import Any

from .auth import AuthManager

log = logging.getLogger("amarok_ytmusic.client")

CACHE_TTL_SECONDS = 180.0
MAX_CACHE_ENTRIES = 256


@dataclass
class CatalogFolder:
    name: str
    callback_data: str
    info_html: str = ""
    cover_url: str = ""
    year: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": 1,
            "itemName": self.name,
            "callbackData": self.callback_data,
            "infoHtml": self.info_html,
            "coverUrl": self.cover_url,
            "year": self.year,
        }


@dataclass
class CatalogTrack:
    video_id: str
    title: str
    artist: str
    album: str
    playable_url: str
    cover_url: str = ""
    duration: str = ""
    year: int = 0
    info_html: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": 0,
            "videoId": self.video_id,
            "itemName": self.title,
            "artist": self.artist,
            "album": self.album,
            "playableUrl": self.playable_url,
            "coverUrl": self.cover_url,
            "duration": self.duration,
            "year": self.year,
            "infoHtml": self.info_html,
        }


def _best_thumbnail(obj: dict[str, Any]) -> str:
    thumbs = obj.get("thumbnails") or obj.get("thumbnail") or []
    if isinstance(thumbs, dict):
        thumbs = thumbs.get("thumbnails", [])
    if not isinstance(thumbs, list) or not thumbs:
        return ""
    best = max(thumbs, key=lambda t: int(t.get("width") or 0) * int(t.get("height") or 0))
    return str(best.get("url") or "")


def _join_artists(raw_artists: Any) -> str:
    if not raw_artists:
        return "Unknown Artist"
    if isinstance(raw_artists, str):
        return raw_artists
    names = [str(a.get("name") or "").strip() for a in raw_artists if isinstance(a, dict) and a.get("name")]
    return ", ".join(names) or "Unknown Artist"


def _extract_album_name(raw_album: Any, fallback: str = "YouTube Music") -> str:
    if isinstance(raw_album, dict) and raw_album.get("name"):
        return str(raw_album["name"])
    if isinstance(raw_album, str) and raw_album.strip():
        return raw_album.strip()
    return fallback


def _safe_year(raw: Any) -> int:
    try:
        y = int(str(raw).strip()[:4])
        return y if 1900 <= y <= 2100 else 0
    except Exception:
        return 0


def _track_info_html(title: str, artist: str, album: str, duration: str, cover_url: str, video_id: str) -> str:
    img = (
        f'<p><img src="{html.escape(cover_url)}" width="140" style="border-radius:8px;"/></p>'
        if cover_url
        else ""
    )
    dur_row = f"<br/><b>Duration:</b> {html.escape(duration)}" if duration else ""
    return (
        f"<div style='font-family:sans-serif;'>"
        f"{img}"
        f"<h3>{html.escape(title)}</h3>"
        f"<p><b>Artist:</b> {html.escape(artist)}<br/>"
        f"<b>Album:</b> {html.escape(album)}"
        f"{dur_row}<br/>"
        f"<small>YouTube Music ID: {html.escape(video_id)}</small></p>"
        f"</div>"
    )


def _folder_info_html(title: str, subtitle: str, cover_url: str = "") -> str:
    img = (
        f'<p><img src="{html.escape(cover_url)}" width="140" style="border-radius:8px;"/></p>'
        if cover_url
        else ""
    )
    return (
        f"<div style='font-family:sans-serif;'>"
        f"{img}"
        f"<h3>{html.escape(title)}</h3>"
        f"<p>{html.escape(subtitle)}</p>"
        f"</div>"
    )


class YTMusicCatalogClient:
    """Provides level-1 folders and level-0 tracks for Amarok's YouTube Music service."""

    def __init__(self, auth_manager: AuthManager) -> None:
        self.auth = auth_manager
        self._cache: dict[str, tuple[float, Any]] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    def _get_cached(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if not entry:
            return None
        ts, val = entry
        if time.time() - ts > CACHE_TTL_SECONDS:
            self._cache.pop(key, None)
            return None
        return val

    def _set_cached(self, key: str, value: Any) -> Any:
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            now = time.time()
            expired = [k for k, (ts, _) in self._cache.items() if now - ts > CACHE_TTL_SECONDS]
            for k in expired:
                self._cache.pop(k, None)
            if len(self._cache) >= MAX_CACHE_ENTRIES:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest_key, None)
        self._cache[key] = (time.time(), value)
        return value

    def _to_track(self, raw: dict[str, Any], default_album: str = "YouTube Music") -> CatalogTrack | None:
        video_id = raw.get("videoId")
        if not video_id:
            return None
        settings = self.auth.get_settings()
        title = str(raw.get("title") or "Untitled Track").strip()
        artist = _join_artists(raw.get("artists"))
        album = _extract_album_name(raw.get("album"), fallback=default_album)
        cover_url = _best_thumbnail(raw)
        duration = str(raw.get("duration") or "")
        if not duration and raw.get("duration_seconds"):
            try:
                secs = int(raw["duration_seconds"])
                duration = f"{secs // 60}:{secs % 60:02d}"
            except (ValueError, TypeError):
                duration = ""
        year = _safe_year(raw.get("year"))
        return CatalogTrack(
            video_id=str(video_id),
            title=title,
            artist=artist,
            album=album,
            playable_url=settings.stream_url(str(video_id)),
            cover_url=cover_url,
            duration=duration,
            year=year,
            info_html=_track_info_html(title, artist, album, duration, cover_url, str(video_id)),
        )

    def get_root_folders(self, query: str = "") -> list[dict[str, Any]]:
        """Populate level-1 entries in Amarok's YouTube Music service tree."""
        query = query.strip()
        settings = self.auth.get_settings()

        if query:
            return self._search_folders(query)

        cache_key = f"root:{settings.auth_mode}:{settings.browser_source}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        folders: list[CatalogFolder] = []

        if not self.auth.is_authenticated():
            folders.append(
                CatalogFolder(
                    name="Sign in to YouTube Music (open Web Auth)",
                    callback_data="info:not_signed_in",
                    info_html=(
                        "<h3>YouTube Music — Web Auth Required</h3>"
                        f"<p>Open <a href='{settings.base_url}'>{settings.base_url}</a> in your browser "
                        "to connect your YouTube Music account.</p>"
                        "<p>You can still type in Amarok's search bar above to search public YouTube Music tracks right now!</p>"
                    ),
                )
            )
            return [f.to_dict() for f in folders]

        yt = self.auth.create_ytmusic()

        # Core personal sections
        folders.append(
            CatalogFolder(
                name="★ Liked Music",
                callback_data="liked",
                info_html=_folder_info_html("Liked Music", "Your liked tracks on YouTube Music."),
            )
        )
        folders.append(
            CatalogFolder(
                name="♫ Library Songs",
                callback_data="library_songs",
                info_html=_folder_info_html("Library Songs", "Songs saved to your YouTube Music library."),
            )
        )
        folders.append(
            CatalogFolder(
                name="⚡ Quick Picks & Home",
                callback_data="home",
                info_html=_folder_info_html("Quick Picks & Home", "Personalized recommendations from your YouTube Music home feed."),
            )
        )

        # User playlists
        try:
            playlists = yt.get_library_playlists(limit=50) or []
            for pl in playlists:
                pl_id = pl.get("playlistId")
                title = str(pl.get("title") or "Untitled Playlist").strip()
                if not pl_id or pl_id == "LM":  # LM is already shown as Liked Music
                    continue
                count = pl.get("count")
                subtitle = f"Playlist ({count} tracks)" if count else "YouTube Music Playlist"
                cover = _best_thumbnail(pl)
                folders.append(
                    CatalogFolder(
                        name=f"Playlist: {title}",
                        callback_data=f"playlist:{pl_id}",
                        cover_url=cover,
                        info_html=_folder_info_html(title, subtitle, cover),
                    )
                )
        except Exception as exc:
            log.warning("Failed to fetch library playlists: %s", exc)
            folders.append(
                CatalogFolder(
                    name="⚠ Could not load playlists (check Web Auth)",
                    callback_data="info:auth_error",
                    info_html=f"<h3>Session Error</h3><p>{html.escape(str(exc))}</p><p>Refresh cookies at {settings.base_url}</p>",
                )
            )

        # Saved library albums
        try:
            albums = yt.get_library_albums(limit=30) or []
            for alb in albums:
                browse_id = alb.get("browseId")
                title = str(alb.get("title") or "Untitled Album").strip()
                if not browse_id:
                    continue
                artist = _join_artists(alb.get("artists"))
                cover = _best_thumbnail(alb)
                folders.append(
                    CatalogFolder(
                        name=f"Album: {title} — {artist}",
                        callback_data=f"album:{browse_id}",
                        cover_url=cover,
                        info_html=_folder_info_html(title, f"Album by {artist}", cover),
                    )
                )
        except Exception as exc:
            log.warning("Failed to fetch library albums: %s", exc)

        result = [f.to_dict() for f in folders]
        return self._set_cached(cache_key, result)

    def search_artists(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search YouTube Music for matching artists (with browseId and thumbnail)."""
        query = query.strip()
        if not query:
            return []
        cache_key = f"search_artists:{query.lower()}:{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        yt = self.auth.create_ytmusic()
        artists: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            raw = yt.search(query, filter="artists", limit=limit) or []
            for item in raw:
                browse_id = str(item.get("browseId") or "").strip()
                name = str(item.get("artist") or item.get("name") or "").strip()
                if not browse_id or not name or browse_id in seen:
                    continue
                seen.add(browse_id)
                artists.append(
                    {
                        "browseId": browse_id,
                        "name": name,
                        "subscribers": str(item.get("subscribers") or ""),
                        "coverUrl": _best_thumbnail(item),
                    }
                )
        except Exception as exc:
            log.warning("Artist search failed for %r: %s", query, exc)

        return self._set_cached(cache_key, artists)

    def get_artist_sections(self, browse_id: str, artist_name: str = "") -> list[dict[str, Any]]:
        """Return expandable sections for a YouTube Music artist: Top Songs, full Albums, and Singles."""
        browse_id = browse_id.strip()
        if not browse_id:
            return []
        cache_key = f"artist_sections:{browse_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        yt = self.auth.create_ytmusic()
        try:
            detail = yt.get_artist(browse_id) or {}
        except Exception as exc:
            log.warning("get_artist failed for %s: %s", browse_id, exc)
            return []

        resolved_name = str(detail.get("name") or artist_name or "Artist").strip()
        artist_cover = _best_thumbnail(detail)
        folders: list[CatalogFolder] = []

        # 1. Top Songs (full YouTube Music Top Songs playlist when browseId is present)
        songs_sec = detail.get("songs") or {}
        songs_bid = str(songs_sec.get("browseId") or "").strip()
        if songs_bid:
            pl_id = songs_bid[2:] if songs_bid.startswith("VL") else songs_bid
            folders.append(
                CatalogFolder(
                    name="★ Top Songs",
                    callback_data=f"playlist:{pl_id}",
                    cover_url=artist_cover,
                    year=0,
                    info_html=_folder_info_html(f"Top Songs — {resolved_name}", "Most popular tracks on YouTube Music.", artist_cover),
                )
            )
        elif songs_sec.get("results"):
            folders.append(
                CatalogFolder(
                    name="★ Top Songs",
                    callback_data=f"artist_top:{browse_id}",
                    cover_url=artist_cover,
                    year=0,
                    info_html=_folder_info_html(f"Top Songs — {resolved_name}", "Most popular tracks on YouTube Music.", artist_cover),
                )
            )

        seen_albums: set[str] = set()

        # 2. Full Albums discography (fetch complete list via get_artist_albums when available)
        alb_sec = detail.get("albums") or {}
        raw_albums: list[dict[str, Any]] = []
        if alb_sec.get("browseId") and alb_sec.get("params"):
            try:
                raw_albums = yt.get_artist_albums(
                    str(alb_sec["browseId"]), str(alb_sec["params"]), limit=25
                ) or []
            except Exception as exc:
                log.debug("get_artist_albums fallback for %s: %s", browse_id, exc)
        if not raw_albums:
            raw_albums = list(alb_sec.get("results") or [])

        for alb in raw_albums:
            alb_bid = str(alb.get("browseId") or "").strip()
            title = str(alb.get("title") or "").strip()
            if not alb_bid or not title or alb_bid in seen_albums:
                continue
            seen_albums.add(alb_bid)
            cover = _best_thumbnail(alb) or artist_cover
            year = _safe_year(alb.get("year"))
            folders.append(
                CatalogFolder(
                    name=title,
                    callback_data=f"album:{alb_bid}",
                    cover_url=cover,
                    year=year,
                    info_html=_folder_info_html(title, f"Album by {resolved_name}", cover),
                )
            )

        # 3. Singles & EPs
        singles_sec = detail.get("singles") or {}
        for item in (singles_sec.get("results") or [])[:15]:
            s_bid = str(item.get("browseId") or "").strip()
            title = str(item.get("title") or "").strip()
            if not s_bid or not title or s_bid in seen_albums:
                continue
            seen_albums.add(s_bid)
            cover = _best_thumbnail(item) or artist_cover
            year = _safe_year(item.get("year"))
            folders.append(
                CatalogFolder(
                    name=f"Single: {title}",
                    callback_data=f"album:{s_bid}",
                    cover_url=cover,
                    year=year,
                    info_html=_folder_info_html(title, f"Single / EP by {resolved_name}", cover),
                )
            )

        result = [f.to_dict() for f in folders]
        return self._set_cached(cache_key, result)

    def _search_folders(self, query: str) -> list[dict[str, Any]]:
        """Return search result groups (Top Songs + Videos + matching Albums & Playlists)."""
        cache_key = f"search_folders:{query.lower()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        yt = self.auth.create_ytmusic()
        folders: list[CatalogFolder] = [
            CatalogFolder(
                name=f"★ Top Songs: {query}",
                callback_data=f"search_songs:{query}",
                year=0,
                info_html=_folder_info_html(f"Songs: {query}", "Top YouTube Music song results."),
            ),
            CatalogFolder(
                name=f"★ Videos & Live: {query}",
                callback_data=f"search_videos:{query}",
                year=0,
                info_html=_folder_info_html(f"Videos: {query}", "Top YouTube Music video & live performance results."),
            ),
        ]

        # Also surface top matching albums and playlists directly as expandable folders!
        try:
            albums = yt.search(query, filter="albums", limit=8) or []
            for alb in albums:
                browse_id = alb.get("browseId")
                title = str(alb.get("title") or "").strip()
                if not browse_id or not title:
                    continue
                artist = _join_artists(alb.get("artists"))
                cover = _best_thumbnail(alb)
                year = _safe_year(alb.get("year"))
                folders.append(
                    CatalogFolder(
                        name=f"Album: {title} — {artist}",
                        callback_data=f"album:{browse_id}",
                        cover_url=cover,
                        year=year,
                        info_html=_folder_info_html(title, f"Album by {artist}", cover),
                    )
                )
        except Exception as exc:
            log.warning("Album search failed for %r: %s", query, exc)

        try:
            playlists = yt.search(query, filter="community_playlists", limit=5) or []
            for pl in playlists:
                browse_id = pl.get("browseId") or pl.get("playlistId")
                title = str(pl.get("title") or "").strip()
                if not browse_id or not title:
                    continue
                pl_id = browse_id[2:] if str(browse_id).startswith("VL") else str(browse_id)
                author = str(pl.get("author") or "Community Playlist")
                cover = _best_thumbnail(pl)
                folders.append(
                    CatalogFolder(
                        name=f"Playlist: {title}",
                        callback_data=f"playlist:{pl_id}",
                        cover_url=cover,
                        info_html=_folder_info_html(title, f"Playlist by {author}", cover),
                    )
                )
        except Exception as exc:
            log.warning("Playlist search failed for %r: %s", query, exc)

        result = [f.to_dict() for f in folders]
        return self._set_cached(cache_key, result)

    def get_tracks_for_folder(self, callback_data: str) -> list[dict[str, Any]]:
        """Populate level-0 playable tracks for an expanded Amarok folder."""
        callback_data = callback_data.strip()
        if not callback_data or callback_data.startswith("info:"):
            return []

        cache_key = f"tracks:{callback_data}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        yt = self.auth.create_ytmusic()
        raw_tracks: list[dict[str, Any]] = []
        default_album = "YouTube Music"

        if callback_data == "liked":
            data = yt.get_liked_songs(limit=150) or {}
            raw_tracks = data.get("tracks") or []
            default_album = "Liked Music"
        elif callback_data == "library_songs":
            raw_tracks = yt.get_library_songs(limit=150) or []
            default_album = "Library Songs"
        elif callback_data == "home":
            try:
                sections = yt.get_home(limit=2) or []
            except Exception as exc:
                log.warning("Failed to fetch home feed: %s", exc)
                sections = []
            seen: set[str] = set()
            for sec in sections:
                for item in sec.get("contents") or []:
                    vid = item.get("videoId")
                    if vid and vid not in seen:
                        seen.add(vid)
                        raw_tracks.append(item)
            default_album = "Quick Picks"
        elif callback_data.startswith("playlist:"):
            pl_id = callback_data.split(":", 1)[1]
            data = yt.get_playlist(pl_id, limit=200) or {}
            default_album = str(data.get("title") or "YouTube Music Playlist")
            raw_tracks = data.get("tracks") or []
        elif callback_data.startswith("album:"):
            browse_id = callback_data.split(":", 1)[1]
            data = yt.get_album(browse_id) or {}
            default_album = str(data.get("title") or "YouTube Music Album")
            album_cover = _best_thumbnail(data)
            album_year = data.get("year")
            for t in data.get("tracks") or []:
                merged = dict(t)
                if not merged.get("thumbnails") and album_cover:
                    merged["thumbnails"] = [{"url": album_cover, "width": 544, "height": 544}]
                if not merged.get("year") and album_year:
                    merged["year"] = album_year
                raw_tracks.append(merged)
        elif callback_data.startswith("artist_top:"):
            browse_id = callback_data.split(":", 1)[1]
            detail = yt.get_artist(browse_id) or {}
            default_album = f"Top Songs — {detail.get('name') or 'Artist'}"
            raw_tracks = (detail.get("songs") or {}).get("results") or []
        elif callback_data.startswith("search_songs:"):
            q = callback_data.split(":", 1)[1]
            raw_tracks = yt.search(q, filter="songs", limit=25) or []
            default_album = f"Search: {q}"
        elif callback_data.startswith("search_videos:"):
            q = callback_data.split(":", 1)[1]
            raw_tracks = yt.search(q, filter="videos", limit=20) or []
            default_album = f"Videos: {q}"
        else:
            log.warning("Unknown folder callback_data: %r", callback_data)
            return []

        tracks: list[dict[str, Any]] = []
        for raw in raw_tracks:
            track = self._to_track(raw, default_album=default_album)
            if track is not None:
                tracks.append(track.to_dict())

        return self._set_cached(cache_key, tracks)

    def search_songs_direct(self, query: str, limit: int = 15) -> list[dict[str, Any]]:
        """Direct song search helper for the Web Dashboard test panel."""
        yt = self.auth.create_ytmusic()
        raw_tracks = yt.search(query, filter="songs", limit=limit) or []
        results: list[dict[str, Any]] = []
        for raw in raw_tracks:
            t = self._to_track(raw, default_album="Search")
            if t is not None:
                results.append(t.to_dict())
        return results

    def rate_track(self, video_id: str, rating: str = "LIKE") -> dict[str, Any]:
        """Rate a YouTube Music song ('LIKE', 'DISLIKE', or 'INDIFFERENT')."""
        from ytmusicapi.models.content.enums import LikeStatus

        if not self.auth.is_authenticated():
            raise RuntimeError("Sign in to YouTube Music before rating tracks.")

        norm = rating.strip().upper()
        status_map = {
            "LIKE": LikeStatus.LIKE,
            "DISLIKE": LikeStatus.DISLIKE,
            "INDIFFERENT": LikeStatus.INDIFFERENT,
            "NONE": LikeStatus.INDIFFERENT,
        }
        if norm not in status_map:
            raise ValueError(f"Unsupported rating {rating!r}; use LIKE, DISLIKE, or INDIFFERENT.")

        yt = self.auth.create_ytmusic()
        yt.rate_song(video_id, status_map[norm])
        # Invalidate cached Liked Music & Library Songs so changes show up immediately
        self._cache.pop("tracks:liked", None)
        self._cache.pop("tracks:library_songs", None)
        log.info("Rated YouTube Music track %s -> %s", video_id, norm)
        return {"ok": True, "videoId": video_id, "rating": norm}

    def report_playback_history(self, video_id: str) -> bool:
        """Report a played track to YouTube Music listening history."""
        if not self.auth.is_authenticated():
            return False
        yt = self.auth.create_ytmusic()
        try:
            song = yt.get_song(video_id)
            resp = yt.add_history_item(song)
            ok = getattr(resp, "status_code", 204) in (200, 204)
            log.info("Reported playback history for %s (ok=%s)", video_id, ok)
            return ok
        except Exception as exc:
            log.warning("Could not report playback history for %s: %s", video_id, exc)
            return False

    def get_track_lyrics(self, video_id: str) -> dict[str, Any]:
        """Fetch plain or timed lyrics for a YouTube Music track."""
        yt = self.auth.create_ytmusic()
        watch = yt.get_watch_playlist(videoId=video_id, limit=1) or {}
        lyrics_browse_id = watch.get("lyrics")
        if not lyrics_browse_id:
            return {"videoId": video_id, "found": False, "lyrics": "", "source": ""}

        lyrics_obj = yt.get_lyrics(str(lyrics_browse_id))
        if not lyrics_obj:
            return {"videoId": video_id, "found": False, "lyrics": "", "source": ""}

        if isinstance(lyrics_obj, dict):
            text = str(lyrics_obj.get("lyrics") or "")
            source = str(lyrics_obj.get("source") or "")
        else:
            text = str(getattr(lyrics_obj, "lyrics", "") or "")
            source = str(getattr(lyrics_obj, "source", "") or "")

        return {
            "videoId": video_id,
            "found": bool(text.strip()),
            "lyrics": text,
            "source": source,
        }
