"""Ampache XML API bridge exposing YouTube Music natively inside Amarok 3.3+.

Amarok 3.3.3 ships a compiled C++/KF6 Ampache Internet Service plugin (`amarok_service_ampache.so`)
that talks to `<server_url>/server/xml.server.php`. Exposing our YouTube Music catalog over this
endpoint gives native Media Sources -> Internet -> Ampache (YouTube Music) browsing, search, cover
art, track durations, and playback via our local `/stream/<videoId>` Range proxy.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .auth import AuthManager
from .metadata_store import MetadataStore
from .ytmusic_client import YTMusicCatalogClient

log = logging.getLogger("amarok_ytmusic.ampache")

ARTIST_MY_LIBRARY = 1
ARTIST_MY_PLAYLISTS = 2
ARTIST_SAVED_ALBUMS = 3
ARTIST_IMPORTED_BASE = 40000
ARTIST_YT_SEARCH_BASE = 800000


def normalize_search_query(raw: str) -> str:
    """Clean search filter strings sent by Amarok's Ampache query maker.

    Amarok's AmpacheServiceQueryMaker::addFilter() has an upstream bug where unquoted spaces
    overwrite earlier words. Users can wrap multi-word queries in quotes ("gustavo cerati")
    or join terms with '+' or '_' (gustavo+cerati / gustavo_cerati), which we normalize cleanly.
    """
    q = (raw or "").strip()
    if len(q) >= 2 and ((q[0] == '"' and q[-1] == '"') or (q[0] == "'" and q[-1] == "'")):
        q = q[1:-1].strip()
    # Convert intra-word '+' or '_' separators to spaces without mangling leading '+ bien'
    q = re.sub(r"(?<=\S)[+_]+(?=\S)", " ", q)
    return " ".join(q.split())


@dataclass
class AlbumEntry:
    album_id: int
    artist_id: int
    artist_name: str
    title: str
    callback_data: str
    cover_url: str = ""
    year: int = 0


def parse_duration_seconds(duration: str) -> int:
    """Convert YouTube Music duration strings like '3:45' or '1:02:10' into integer seconds."""
    if not duration:
        return 0
    parts = duration.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 1:
        return nums[0]
    return 0


def _xml_response(root: ET.Element) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


class AmpacheBridge:
    """Translates Amarok's Ampache XML queries into authenticated YouTube Music catalog queries."""

    def __init__(
        self,
        catalog: YTMusicCatalogClient,
        auth_manager: AuthManager,
        state_path: Path | None = None,
        metadata_store: MetadataStore | None = None,
    ) -> None:
        self.catalog = catalog
        self.auth = auth_manager
        self.state_path = state_path
        self.metadata_store = metadata_store
        self._lock = threading.RLock()

        self._next_search_artist_id = 900001
        self._search_artist_by_query: dict[str, int] = {}
        self._search_query_by_artist: dict[int, str] = {}

        self._next_yt_artist_id = ARTIST_YT_SEARCH_BASE + 1
        self._yt_artist_id_by_browse_id: dict[str, int] = {}
        self._yt_artist_info_by_id: dict[int, tuple[str, str]] = {}

        self._next_cached_artist_id = ARTIST_IMPORTED_BASE
        self._cached_artist_id_by_name: dict[str, int] = {}
        self._cached_artist_name_by_id: dict[int, str] = {}

        self._next_album_id = 1000
        self._album_id_by_key: dict[str, int] = {}
        self._albums_by_id: dict[int, AlbumEntry] = {}

        self._next_song_id = 50000
        self._song_id_by_key: dict[tuple[int, str], int] = {}
        self._video_id_by_song_id: dict[int, str] = {}

        self._load_state()

        # Register core static library albums
        self._register_fixed_album(
            101, ARTIST_MY_LIBRARY, "★ YouTube Music — My Library", "Liked Music", "liked"
        )
        self._register_fixed_album(
            102, ARTIST_MY_LIBRARY, "★ YouTube Music — My Library", "Library Songs", "library_songs"
        )
        self._register_fixed_album(
            103, ARTIST_MY_LIBRARY, "★ YouTube Music — My Library", "Quick Picks & Home", "home"
        )

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._next_album_id = max(1000, int(raw.get("next_album_id", 1000)))
            self._next_song_id = max(50000, int(raw.get("next_song_id", 50000)))
            self._next_yt_artist_id = max(
                ARTIST_YT_SEARCH_BASE + 1, int(raw.get("next_yt_artist_id", ARTIST_YT_SEARCH_BASE + 1))
            )
            for aid_s, info in raw.get("yt_artists", {}).items():
                if isinstance(info, list) and len(info) == 2:
                    aid = int(aid_s)
                    bid, name = str(info[0]), str(info[1])
                    self._yt_artist_id_by_browse_id[bid] = aid
                    self._yt_artist_info_by_id[aid] = (bid, name)
            for item in raw.get("albums", []):
                entry = AlbumEntry(
                    album_id=int(item["album_id"]),
                    artist_id=int(item["artist_id"]),
                    artist_name=str(item["artist_name"]),
                    title=str(item["title"]),
                    callback_data=str(item["callback_data"]),
                    cover_url=str(item.get("cover_url", "")),
                    year=int(item.get("year", 0)),
                )
                self._album_id_by_key[f"{entry.artist_id}:{entry.callback_data}"] = entry.album_id
                self._albums_by_id[entry.album_id] = entry
            for k_str, sid_val in raw.get("songs", {}).items():
                if ":" in k_str:
                    alb_s, vid = k_str.split(":", 1)
                    alb_id = int(alb_s)
                    sid = int(sid_val)
                    self._song_id_by_key[(alb_id, vid)] = sid
                    self._video_id_by_song_id[sid] = vid
        except Exception as exc:
            log.warning("Could not load Ampache state from %s: %s", self.state_path, exc)

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            with self._lock:
                # Persist non-search albums (playlist/library albums) and recent songs & YT artists
                albums_payload = [
                    asdict(e)
                    for e in self._albums_by_id.values()
                    if e.artist_id in (ARTIST_MY_LIBRARY, ARTIST_MY_PLAYLISTS, ARTIST_SAVED_ALBUMS)
                ]
                songs_items = list(self._song_id_by_key.items())[-3000:]
                songs_payload = {f"{alb_id}:{vid}": sid for (alb_id, vid), sid in songs_items}
                yt_artists_items = list(self._yt_artist_info_by_id.items())[-500:]
                yt_artists_payload = {str(aid): [bid, name] for aid, (bid, name) in yt_artists_items}
                payload = {
                    "next_album_id": self._next_album_id,
                    "next_song_id": self._next_song_id,
                    "next_yt_artist_id": self._next_yt_artist_id,
                    "yt_artists": yt_artists_payload,
                    "albums": albums_payload,
                    "songs": songs_payload,
                }
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception as exc:
            log.warning("Could not save Ampache state to %s: %s", self.state_path, exc)

    def _register_fixed_album(
        self,
        album_id: int,
        artist_id: int,
        artist_name: str,
        title: str,
        callback_data: str,
        cover_url: str = "",
        year: int = 0,
    ) -> AlbumEntry:
        entry = AlbumEntry(
            album_id=album_id,
            artist_id=artist_id,
            artist_name=artist_name,
            title=title,
            callback_data=callback_data,
            cover_url=cover_url,
            year=year,
        )
        self._album_id_by_key[callback_data] = album_id
        self._albums_by_id[album_id] = entry
        return entry

    def _register_dynamic_album(
        self,
        artist_id: int,
        artist_name: str,
        title: str,
        callback_data: str,
        cover_url: str = "",
        year: int = 0,
    ) -> AlbumEntry:
        key = f"{artist_id}:{callback_data}"
        with self._lock:
            existing_id = self._album_id_by_key.get(key)
            if existing_id is None:
                self._next_album_id += 1
                existing_id = self._next_album_id
                self._album_id_by_key[key] = existing_id
            entry = AlbumEntry(
                album_id=existing_id,
                artist_id=artist_id,
                artist_name=artist_name,
                title=title,
                callback_data=callback_data,
                cover_url=cover_url,
                year=year,
            )
            self._albums_by_id[existing_id] = entry
            return entry

    def _get_or_create_search_artist(self, query: str) -> tuple[int, str]:
        norm = normalize_search_query(query)
        key = norm.lower()
        with self._lock:
            aid = self._search_artist_by_query.get(key)
            if aid is None:
                aid = self._next_search_artist_id
                self._next_search_artist_id += 1
                self._search_artist_by_query[key] = aid
                self._search_query_by_artist[aid] = norm
            return aid, f"★ Search: {norm}"

    def _get_or_create_yt_artist(self, browse_id: str, artist_name: str) -> int:
        bid = browse_id.strip()
        name = artist_name.strip()
        with self._lock:
            aid = self._yt_artist_id_by_browse_id.get(bid)
            if aid is None:
                aid = self._next_yt_artist_id
                self._next_yt_artist_id += 1
                self._yt_artist_id_by_browse_id[bid] = aid
            self._yt_artist_info_by_id[aid] = (bid, name)
            return aid

    def _get_or_create_cached_artist(self, artist_name: str) -> int:
        key = artist_name.strip().lower()
        with self._lock:
            aid = self._cached_artist_id_by_name.get(key)
            if aid is None:
                aid = self._next_cached_artist_id
                self._next_cached_artist_id += 1
                self._cached_artist_id_by_name[key] = aid
                self._cached_artist_name_by_id[aid] = artist_name.strip()
            return aid

    def _get_song_id(self, album_id: int, video_id: str) -> int:
        key = (album_id, video_id)
        with self._lock:
            sid = self._song_id_by_key.get(key)
            if sid is None:
                self._next_song_id += 1
                sid = self._next_song_id
                self._song_id_by_key[key] = sid
            self._video_id_by_song_id[sid] = video_id
            return sid

    def get_video_id_for_song_id(self, song_id: int) -> str | None:
        with self._lock:
            return self._video_id_by_song_id.get(song_id)

    def handle_action(
        self,
        action: str,
        filter_val: str = "",
        limit: int = 0,
        item_id: int = 0,
        rating: int = -1,
    ) -> str:
        action = (action or "").strip().lower()
        filter_val = (filter_val or "").strip()

        try:
            if action == "ping":
                return self._handle_ping()
            if action == "handshake":
                return self._handle_handshake()
            if action == "artists":
                res = self._handle_artists(filter_val)
                if filter_val:
                    self._save_state()
                return res
            if action == "artist_albums":
                res = self._handle_artist_albums(filter_val, limit=limit)
                self._save_state()
                return res
            if action == "albums":
                res = self._handle_albums(filter_val, limit=limit)
                self._save_state()
                return res
            if action == "album_songs":
                res = self._handle_album_songs(filter_val, limit=limit)
                self._save_state()
                return res
            if action == "artist_songs":
                res = self._handle_artist_songs(filter_val, limit=limit)
                self._save_state()
                return res
            if action == "songs":
                res = self._handle_songs(filter_val, limit=limit)
                self._save_state()
                return res
            if action == "rate":
                return self._handle_rate(item_id=item_id or (int(filter_val) if filter_val.isdigit() else 0), rating=rating)
        except Exception as exc:
            log.exception("Ampache action=%s (filter=%r) failed: %s", action, filter_val, exc)
            return _xml_response(ET.Element("root"))

        # Fallback empty valid XML root for any optional Ampache action
        return _xml_response(ET.Element("root"))

    def _handle_rate(self, item_id: int, rating: int) -> str:
        root = ET.Element("root")
        vid = self.get_video_id_for_song_id(item_id)
        if not vid:
            return _xml_response(root)
        yt_rating = "LIKE" if rating >= 4 else ("DISLIKE" if rating == 1 else "INDIFFERENT")
        if self.metadata_store:
            self.metadata_store.set_user_rating(vid, max(0, min(10, rating * 2)), yt_rating)
        self.catalog.rate_track(vid, yt_rating)
        ET.SubElement(root, "result").text = "ok"
        return _xml_response(root)

    def _handle_ping(self) -> str:
        root = ET.Element("root")
        ET.SubElement(root, "compatible").text = "350001"
        ET.SubElement(root, "version").text = "350001"
        ET.SubElement(root, "api").text = "350001"
        return _xml_response(root)

    def _handle_handshake(self) -> str:
        root = ET.Element("root")
        ET.SubElement(root, "auth").text = "ytmusic-local-session"
        ET.SubElement(root, "api").text = "350001"
        ET.SubElement(root, "update").text = "2026-11-19T00:00:00Z"
        ET.SubElement(root, "add").text = "2026-11-19T00:00:00Z"
        ET.SubElement(root, "clean").text = "2026-11-19T00:00:00Z"
        return _xml_response(root)

    def _append_artist(self, root: ET.Element, artist_id: int, name: str) -> None:
        el = ET.SubElement(root, "artist", {"id": str(artist_id)})
        ET.SubElement(el, "name").text = name

    def _append_album(self, root: ET.Element, entry: AlbumEntry) -> None:
        el = ET.SubElement(root, "album", {"id": str(entry.album_id)})
        ET.SubElement(el, "name").text = entry.title
        art_el = ET.SubElement(el, "artist", {"id": str(entry.artist_id)})
        art_el.text = entry.artist_name
        ET.SubElement(el, "year").text = str(entry.year or 0)
        ET.SubElement(el, "disk").text = "1"
        ET.SubElement(el, "art").text = entry.cover_url

    def _append_song(
        self,
        root: ET.Element,
        album_entry: AlbumEntry,
        track: dict[str, Any],
        track_num: int,
    ) -> None:
        video_id = str(track.get("videoId") or "")
        if not video_id:
            return
        song_id = self._get_song_id(album_entry.album_id, video_id)
        title = str(track.get("itemName") or "Untitled Track")
        artist_str = str(track.get("artist") or "").strip()
        # Show the actual track artist in the title when the parent Ampache artist is a virtual bucket
        # (omit redundant suffix when already browsing inside that exact artist node)
        same_as_parent = artist_str.lower() == album_entry.artist_name.strip().lower()
        display_title = (
            f"{title} — {artist_str}"
            if artist_str and artist_str != "Unknown Artist" and not same_as_parent
            else title
        )

        el = ET.SubElement(root, "song", {"id": str(song_id)})
        ET.SubElement(el, "title").text = display_title
        art_el = ET.SubElement(el, "artist", {"id": str(album_entry.artist_id)})
        art_el.text = album_entry.artist_name
        alb_el = ET.SubElement(el, "album", {"id": str(album_entry.album_id)})
        alb_el.text = album_entry.title
        ET.SubElement(el, "track").text = str(track_num)
        ET.SubElement(el, "time").text = str(parse_duration_seconds(str(track.get("duration") or "")))
        cached = self.metadata_store.get_track(video_id) if self.metadata_store else None
        if cached and cached.effective_amarok_rating() > 0:
            ET.SubElement(el, "rating").text = str(max(1, round(cached.effective_amarok_rating() / 2.0)))
        elif album_entry.callback_data == "liked":
            ET.SubElement(el, "rating").text = "5"
        ET.SubElement(el, "url").text = str(track.get("playableUrl") or "")
        art_url = str(track.get("coverUrl") or album_entry.cover_url or "")
        if cached and cached.cover_local_path:
            art_url = f"{self.auth.get_settings().base_url}/cover/{video_id}"
        ET.SubElement(el, "art").text = art_url

    def _handle_artists(self, filter_val: str) -> str:
        root = ET.Element("root")
        query = normalize_search_query(filter_val)
        if query:
            # 1. Unified search bucket (Top Songs, Videos, matching Albums & Playlists)
            aid, label = self._get_or_create_search_artist(query)
            self._append_artist(root, aid, label)

            # 2. Real YouTube Music artists matching the search query (expandable into Top Songs, Albums & Singles)
            seen_names: set[str] = set()
            for artist in self.catalog.search_artists(query, limit=8):
                bid = str(artist.get("browseId") or "").strip()
                name = str(artist.get("name") or "").strip()
                if not bid or not name:
                    continue
                yt_aid = self._get_or_create_yt_artist(bid, name)
                self._append_artist(root, yt_aid, name)
                seen_names.add(name.lower())

            # 3. Any matching cached/local artists not already returned by YouTube Music search
            if self.metadata_store:
                q_low = query.lower()
                for artist_name in self.metadata_store.list_cached_artists():
                    if q_low in artist_name.lower() and artist_name.lower() not in seen_names:
                        cached_aid = self._get_or_create_cached_artist(artist_name)
                        self._append_artist(root, cached_aid, artist_name)
                        seen_names.add(artist_name.lower())

            return _xml_response(root)

        if not self.auth.is_authenticated():
            settings = self.auth.get_settings()
            self._append_artist(
                root,
                ARTIST_MY_LIBRARY,
                f"Sign in at {settings.base_url} (or type in search bar)",
            )
            return _xml_response(root)

        self._append_artist(root, ARTIST_MY_LIBRARY, "★ YouTube Music — My Library")
        self._append_artist(root, ARTIST_MY_PLAYLISTS, "♫ YouTube Music — My Playlists")
        self._append_artist(root, ARTIST_SAVED_ALBUMS, "◎ YouTube Music — Saved Albums")

        # Also surface real artists from the cached/imported metadata store (with real albums & cover art!)
        if self.metadata_store:
            for artist_name in self.metadata_store.list_cached_artists():
                aid = self._get_or_create_cached_artist(artist_name)
                self._append_artist(root, aid, artist_name)

        return _xml_response(root)

    def _albums_for_artist(self, artist_id: int) -> list[AlbumEntry]:
        if artist_id == ARTIST_MY_LIBRARY:
            if not self.auth.is_authenticated():
                return []
            # Attach cover art from the most recently cached track if static albums have no cover yet
            fallback_cover = ""
            if self.metadata_store:
                recent = self.metadata_store.list_tracks(limit=1)
                if recent and (recent[0].cover_local_path or recent[0].cover_url):
                    settings = self.auth.get_settings()
                    fallback_cover = (
                        f"{settings.base_url}/cover/{recent[0].video_id}"
                        if recent[0].cover_local_path
                        else recent[0].cover_url
                    )
            entries = [
                self._albums_by_id[101],
                self._albums_by_id[102],
                self._albums_by_id[103],
            ]
            if fallback_cover:
                for e in entries:
                    if not e.cover_url:
                        e.cover_url = fallback_cover
            return entries

        if artist_id == ARTIST_MY_PLAYLISTS:
            folders = self.catalog.get_root_folders("")
            entries: list[AlbumEntry] = []
            for f in folders:
                cb = str(f.get("callbackData") or "")
                if cb.startswith("playlist:"):
                    name = str(f.get("itemName") or "").removeprefix("Playlist: ").strip()
                    entries.append(
                        self._register_dynamic_album(
                            ARTIST_MY_PLAYLISTS,
                            "♫ YouTube Music — My Playlists",
                            name or "Untitled Playlist",
                            cb,
                            cover_url=str(f.get("coverUrl") or ""),
                        )
                    )
            return entries

        if artist_id == ARTIST_SAVED_ALBUMS:
            folders = self.catalog.get_root_folders("")
            entries = []
            for f in folders:
                cb = str(f.get("callbackData") or "")
                if cb.startswith("album:"):
                    name = str(f.get("itemName") or "").removeprefix("Album: ").strip()
                    entries.append(
                        self._register_dynamic_album(
                            ARTIST_SAVED_ALBUMS,
                            "◎ YouTube Music — Saved Albums",
                            name or "Untitled Album",
                            cb,
                            cover_url=str(f.get("coverUrl") or ""),
                        )
                    )
            return entries

        # Real imported artist from SQLite metadata cache
        with self._lock:
            cached_artist_name = self._cached_artist_name_by_id.get(artist_id)
        if cached_artist_name and self.metadata_store:
            settings = self.auth.get_settings()
            tracks = self.metadata_store.list_tracks_by_artist(cached_artist_name)
            by_album: dict[str, tuple[str, int]] = {}
            for t in tracks:
                alb = t.album or "Singles"
                if alb not in by_album:
                    cov = f"{settings.base_url}/cover/{t.video_id}" if t.cover_local_path else t.cover_url
                    by_album[alb] = (cov, t.year)
            entries = []
            for alb_name, (cov, yr) in by_album.items():
                entries.append(
                    self._register_dynamic_album(
                        artist_id,
                        cached_artist_name,
                        alb_name,
                        f"cached_album:{cached_artist_name}::{alb_name}",
                        cover_url=cov,
                        year=yr,
                    )
                )
            return entries

        # Real YouTube Music artist discovered via search (Top Songs + full Albums + Singles)
        with self._lock:
            yt_artist = self._yt_artist_info_by_id.get(artist_id)
        if yt_artist:
            browse_id, artist_name = yt_artist
            sections = self.catalog.get_artist_sections(browse_id, artist_name)
            entries = []
            for f in sections:
                cb = str(f.get("callbackData") or "")
                if not cb or cb.startswith("info:"):
                    continue
                entries.append(
                    self._register_dynamic_album(
                        artist_id,
                        artist_name,
                        str(f.get("itemName") or "Results"),
                        cb,
                        cover_url=str(f.get("coverUrl") or ""),
                        year=int(f.get("year") or 0),
                    )
                )
            return entries

        # Dynamic search artist (Songs + Videos + matching Albums & Playlists)
        query = self._search_query_by_artist.get(artist_id)
        if query:
            folders = self.catalog.get_root_folders(query)
            entries = []
            for f in folders:
                cb = str(f.get("callbackData") or "")
                if not cb or cb.startswith("info:"):
                    continue
                entries.append(
                    self._register_dynamic_album(
                        artist_id,
                        f"★ Search: {query}",
                        str(f.get("itemName") or "Results"),
                        cb,
                        cover_url=str(f.get("coverUrl") or ""),
                        year=int(f.get("year") or 0),
                    )
                )
            return entries

        return []

    def _handle_artist_albums(self, filter_val: str, limit: int = 0) -> str:
        root = ET.Element("root")
        try:
            artist_id = int(filter_val)
        except ValueError:
            return _xml_response(root)

        entries = self._albums_for_artist(artist_id)
        if limit > 0:
            entries = entries[:limit]
        for entry in entries:
            self._append_album(root, entry)
        return _xml_response(root)

    def _handle_albums(self, filter_val: str, limit: int = 0) -> str:
        root = ET.Element("root")
        entries: list[AlbumEntry] = []
        query = normalize_search_query(filter_val)
        if query:
            aid, _ = self._get_or_create_search_artist(query)
            entries = self._albums_for_artist(aid)
        else:
            for aid in (ARTIST_MY_LIBRARY, ARTIST_MY_PLAYLISTS, ARTIST_SAVED_ALBUMS):
                entries.extend(self._albums_for_artist(aid))

        if limit > 0:
            entries = entries[:limit]
        for entry in entries:
            self._append_album(root, entry)
        return _xml_response(root)

    def _handle_album_songs(self, filter_val: str, limit: int = 0) -> str:
        root = ET.Element("root")
        try:
            album_id = int(filter_val)
        except ValueError:
            return _xml_response(root)

        with self._lock:
            entry = self._albums_by_id.get(album_id)
        if not entry:
            return _xml_response(root)

        if entry.callback_data.startswith("cached_album:") and self.metadata_store:
            rest = entry.callback_data.split(":", 1)[1]
            artist_name, alb_name = rest.split("::", 1) if "::" in rest else (rest, "")
            settings = self.auth.get_settings()
            cached_tracks = [
                t for t in self.metadata_store.list_tracks_by_artist(artist_name) if (t.album or "Singles") == alb_name
            ]
            if limit > 0:
                cached_tracks = cached_tracks[:limit]
            for idx, ct in enumerate(cached_tracks, start=1):
                track_dict = {
                    "videoId": ct.video_id,
                    "itemName": ct.title,
                    "artist": "",  # Parent Ampache artist is already the real artist name
                    "playableUrl": settings.stream_url(ct.video_id),
                    "duration": f"{ct.duration_sec // 60}:{ct.duration_sec % 60:02d}" if ct.duration_sec else "",
                    "coverUrl": f"{settings.base_url}/cover/{ct.video_id}" if ct.cover_local_path else ct.cover_url,
                }
                self._append_song(root, entry, track_dict, idx)
            return _xml_response(root)

        tracks = self.catalog.get_tracks_for_folder(entry.callback_data)
        if limit > 0:
            tracks = tracks[:limit]
        for idx, track in enumerate(tracks, start=1):
            self._append_song(root, entry, track, idx)
        return _xml_response(root)

    def _handle_artist_songs(self, filter_val: str, limit: int = 0) -> str:
        root = ET.Element("root")
        try:
            artist_id = int(filter_val)
        except ValueError:
            return _xml_response(root)

        albums = self._albums_for_artist(artist_id)
        count = 0
        # Keep artist-level bulk expansion responsive by capping at the first 3 albums if many exist
        for entry in albums[:3]:
            tracks = self.catalog.get_tracks_for_folder(entry.callback_data)
            for idx, track in enumerate(tracks, start=1):
                if limit > 0 and count >= limit:
                    return _xml_response(root)
                self._append_song(root, entry, track, idx)
                count += 1
        return _xml_response(root)

    def _handle_songs(self, filter_val: str, limit: int = 0) -> str:
        root = ET.Element("root")
        query = normalize_search_query(filter_val)
        if query:
            aid, label = self._get_or_create_search_artist(query)
            entry = self._register_dynamic_album(aid, label, f"★ Top Songs: {query}", f"search_songs:{query}")
            tracks = self.catalog.get_tracks_for_folder(entry.callback_data)
            if limit > 0:
                tracks = tracks[:limit]
            for idx, track in enumerate(tracks, start=1):
                self._append_song(root, entry, track, idx)
            return _xml_response(root)

        if self.auth.is_authenticated():
            entry = self._albums_by_id[101]
            tracks = self.catalog.get_tracks_for_folder("liked")
            if limit > 0:
                tracks = tracks[:limit]
            for idx, track in enumerate(tracks, start=1):
                self._append_song(root, entry, track, idx)
        return _xml_response(root)
