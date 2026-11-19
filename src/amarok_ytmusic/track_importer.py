"""Automated on-play YouTube Music metadata importer and Amarok Local Collection audio cacher."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from .ampache_bridge import parse_duration_seconds
from .auth import AuthManager
from .metadata_store import CachedTrack, MetadataStore
from .stream_resolver import StreamResolver, VIDEO_ID_RE
from .ytmusic_client import _best_thumbnail, _extract_album_name, _join_artists, _safe_year

log = logging.getLogger("amarok_ytmusic.importer")

UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def sanitize_filename(name: str, fallback: str = "Unknown", max_len: int = 90) -> str:
    """Turn an artist, album, or track name into a safe cross-filesystem path component."""
    cleaned = UNSAFE_FILENAME_RE.sub("_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:max_len].rstrip(" .") or fallback


def write_mp4_tags(audio_path: Path, track: CachedTrack) -> bool:
    """Write full metadata, cover art, lyrics, and Amarok FMPS stats tags into an M4A/MP4 file."""
    try:
        mp4 = MP4(str(audio_path))
        if mp4.tags is None:
            mp4.add_tags()
        tags = mp4.tags
        assert tags is not None

        if track.title:
            tags["\xa9nam"] = [track.title]
        if track.artist:
            tags["\xa9ART"] = [track.artist]
            tags["aART"] = [track.artist]
        if track.album:
            tags["\xa9alb"] = [track.album]
        if track.year > 0:
            tags["\xa9day"] = [str(track.year)]
        tags["\xa9gen"] = ["YouTube Music"]
        tags["\xa9cmt"] = [f"ytmusic:{track.video_id}"]
        if track.lyrics:
            tags["\xa9lyr"] = [track.lyrics]

        # Amarok MP4TagHelper reads these exact FMPS freeform atoms on collection scan!
        rating_val = track.amarok_rating_float()
        if rating_val > 0.0:
            tags["----:com.apple.iTunes:FMPS_Rating"] = [
                MP4FreeForm(f"{rating_val:.2f}".encode("utf-8"))
            ]
        elif "----:com.apple.iTunes:FMPS_Rating" in tags:
            del tags["----:com.apple.iTunes:FMPS_Rating"]

        if track.play_count > 0:
            tags["----:com.apple.iTunes:FMPS_Playcount"] = [
                MP4FreeForm(str(track.play_count).encode("utf-8"))
            ]
        if track.score > 0.0:
            tags["----:com.apple.iTunes:FMPS_Rating_Amarok_Score"] = [
                MP4FreeForm(f"{track.score / 100.0:.2f}".encode("utf-8"))
            ]

        # Embed high-resolution album cover art
        if track.cover_local_path and Path(track.cover_local_path).exists():
            img_bytes = Path(track.cover_local_path).read_bytes()
            if len(img_bytes) > 100:
                is_png = img_bytes[:8] == b"\x89PNG\r\n\x1a\n"
                fmt = MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG
                tags["covr"] = [MP4Cover(img_bytes, imageformat=fmt)]

        mp4.save()
        return True
    except Exception as exc:
        log.warning("Failed to tag MP4 file %s for %s: %s", audio_path, track.video_id, exc)
        return False


class TrackImporter:
    """Automatically imports YouTube Music metadata, cover art, lyrics, and tagged M4A files on play."""

    def __init__(self, auth_manager: AuthManager, metadata_store: MetadataStore) -> None:
        self.auth = auth_manager
        self.store = metadata_store
        self.covers_dir = auth_manager.store.covers_cache_dir
        self.tmp_dir = auth_manager.store.config_dir / "tmp_imports"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._inflight: dict[str, asyncio.Task[CachedTrack | None]] = {}

    def _download_cover_sync(self, video_id: str, cover_url: str) -> str:
        if not cover_url:
            return ""
        # Return existing cached image if already downloaded
        for ext in (".jpg", ".png"):
            existing = self.covers_dir / f"{video_id}{ext}"
            if existing.exists() and existing.stat().st_size > 100:
                return str(existing)

        try:
            resp = httpx.get(cover_url, timeout=15.0, follow_redirects=True)
            resp.raise_for_status()
            data = resp.content
            if len(data) <= 100:
                return ""
            is_png = data[:8] == b"\x89PNG\r\n\x1a\n" or "png" in resp.headers.get("content-type", "").lower()
            target = self.covers_dir / f"{video_id}{'.png' if is_png else '.jpg'}"
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return str(target)
        except Exception as exc:
            log.debug("Could not download cover art for %s: %s", video_id, exc)
            return ""

    def fetch_and_cache_metadata_sync(
        self,
        video_id: str,
        force_refresh: bool = False,
    ) -> CachedTrack:
        """Fetch full YouTube Music track + user metadata and store it in SQLite."""
        if not VIDEO_ID_RE.match(video_id):
            raise ValueError(f"Invalid YouTube video ID: {video_id!r}")

        existing = self.store.get_track(video_id)
        if not force_refresh and self.store.is_metadata_fresh(existing):
            assert existing is not None
            if existing.cover_url and not existing.cover_local_path:
                existing.cover_local_path = self._download_cover_sync(video_id, existing.cover_url)
                if existing.cover_local_path:
                    self.store.upsert_metadata(existing)
            return existing

        yt = self.auth.create_ytmusic()
        title = ""
        artist = ""
        album = ""
        album_browse_id = ""
        year = 0
        duration_sec = 0
        cover_url = ""
        like_status = ""
        in_library = False
        lyrics = ""
        lyrics_source = ""

        # 1. Watch playlist gives title, artists, album, year, likeStatus, inLibrary, thumbnail, and lyrics ID
        try:
            watch = yt.get_watch_playlist(videoId=video_id, limit=1) or {}
            tracks = watch.get("tracks") or []
            if tracks:
                t0 = tracks[0]
                title = str(t0.get("title") or "").strip()
                artist = _join_artists(t0.get("artists"))
                raw_album = t0.get("album")
                album = _extract_album_name(raw_album, fallback=title or "YouTube Music")
                if isinstance(raw_album, dict) and raw_album.get("id"):
                    album_browse_id = str(raw_album["id"])
                year = _safe_year(t0.get("year"))
                duration_sec = parse_duration_seconds(str(t0.get("length") or ""))
                cover_url = _best_thumbnail(t0)
                like_status = str(t0.get("likeStatus") or "").upper()
                in_library = bool(t0.get("inLibrary"))

            lyrics_id = watch.get("lyrics")
            if lyrics_id:
                try:
                    lyr = yt.get_lyrics(str(lyrics_id))
                    if isinstance(lyr, dict):
                        lyrics = str(lyr.get("lyrics") or "")
                        lyrics_source = str(lyr.get("source") or "")
                    elif lyr is not None:
                        lyrics = str(getattr(lyr, "lyrics", "") or "")
                        lyrics_source = str(getattr(lyr, "source", "") or "")
                except Exception as exc:
                    log.debug("Lyrics lookup failed for %s: %s", video_id, exc)
        except Exception as exc:
            log.warning("get_watch_playlist failed for %s: %s", video_id, exc)

        # 2. Fallback to get_song if title or duration was missing
        if not title or duration_sec == 0:
            try:
                song = yt.get_song(video_id) or {}
                vd = song.get("videoDetails") or {}
                if not title:
                    title = str(vd.get("title") or "").strip()
                if not artist or artist == "Unknown Artist":
                    artist = str(vd.get("author") or "Unknown Artist").strip()
                if duration_sec == 0 and vd.get("lengthSeconds"):
                    duration_sec = int(vd["lengthSeconds"])
                if not cover_url:
                    cover_url = _best_thumbnail(vd)
                if year == 0:
                    mf = (song.get("microformat") or {}).get("microformatDataRenderer") or {}
                    year = _safe_year(mf.get("publishDate"))
            except Exception as exc:
                log.debug("get_song fallback failed for %s: %s", video_id, exc)

        if not title:
            title = existing.title if existing and existing.title else video_id
        if not artist:
            artist = existing.artist if existing and existing.artist else "Unknown Artist"
        if not album:
            album = existing.album if existing and existing.album else "YouTube Music"

        cover_local_path = self._download_cover_sync(video_id, cover_url)

        track = CachedTrack(
            video_id=video_id,
            title=title,
            artist=artist,
            album=album,
            album_browse_id=album_browse_id,
            year=year,
            duration_sec=duration_sec,
            cover_url=cover_url,
            cover_local_path=cover_local_path,
            like_status=like_status,
            in_library=in_library,
            lyrics=lyrics,
            lyrics_source=lyrics_source,
            fetched_at=time.time(),
        )
        saved = self.store.upsert_metadata(track)
        log.info(
            "Cached YouTube Music metadata for %s: %s — %s [%s] (like=%s)",
            video_id,
            saved.title,
            saved.artist,
            saved.album,
            saved.like_status or "NONE",
        )
        return saved

    def _target_collection_path(self, track: CachedTrack) -> Path:
        settings = self.auth.get_settings()
        base = settings.resolved_collection_dir()
        artist_dir = sanitize_filename(track.artist, fallback="Unknown Artist")
        album_dir = sanitize_filename(track.album, fallback="Singles")
        title_file = sanitize_filename(track.title, fallback=track.video_id)
        return base / artist_dir / album_dir / f"{title_file}.m4a"

    def _download_m4a_sync(self, video_id: str, out_base: Path) -> Path:
        import yt_dlp

        watch_url = f"https://music.youtube.com/watch?v={video_id}"
        cookies_path = self.auth.store.cookies_txt_path
        has_cookies = cookies_path.exists()
        has_js = StreamResolver._has_js_runtime()
        attempts = [True, False] if (has_cookies and has_js) else ([False, True] if has_cookies else [False])

        out_base.parent.mkdir(parents=True, exist_ok=True)
        target_m4a = out_base.with_suffix(".m4a")
        if target_m4a.exists():
            target_m4a.unlink()

        last_err: Exception | None = None
        for use_cookies in attempts:
            opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio[ext=mp4]/bestaudio",
                "outtmpl": str(out_base) + ".%(ext)s",
                "overwrites": True,
                "socket_timeout": 10,
                "retries": 2,
            }
            if use_cookies and cookies_path.exists():
                opts["cookiefile"] = str(cookies_path)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(watch_url, download=True)
                    ext = str((info or {}).get("ext") or "m4a")
                    downloaded = out_base.with_suffix(f".{ext}")
                    if downloaded.exists() and downloaded.stat().st_size > 1000:
                        return downloaded
            except Exception as exc:
                last_err = exc
                log.warning("Audio cache download failed for %s (cookies=%s): %s", video_id, use_cookies, exc)

        raise RuntimeError(f"Could not download M4A audio for {video_id}: {last_err}")

    def _enforce_cache_limit(self) -> None:
        settings = self.auth.get_settings()
        max_mb = int(settings.max_collection_cache_mb or 0)
        if max_mb <= 0:
            return
        max_bytes = max_mb * 1024 * 1024

        tracks = self.store.list_local_cached_files_lru()
        sized: list[tuple[CachedTrack, Path, int]] = []
        total = 0
        for t in tracks:
            p = Path(t.local_file_path)
            if p.exists():
                sz = p.stat().st_size
                sized.append((t, p, sz))
                total += sz
            else:
                self.store.set_local_file_path(t.video_id, "")

        for t, p, sz in sized:
            if total <= max_bytes:
                break
            try:
                p.unlink(missing_ok=True)
                self.store.set_local_file_path(t.video_id, "")
                total -= sz
                log.info("Evicted cached collection file %s to stay under %d MB", p, max_mb)
            except OSError:
                pass

    def import_to_local_collection_sync(
        self,
        video_id: str,
        force_refresh: bool = False,
    ) -> CachedTrack:
        """Download M4A audio, embed full metadata/cover/lyrics/Amarok FMPS stats, and move into ~/Music/YouTube Music."""
        track = self.fetch_and_cache_metadata_sync(video_id, force_refresh=force_refresh)

        # If already cached on disk, simply refresh tags in place
        if track.local_file_path and Path(track.local_file_path).exists():
            if track.local_file_path.endswith(".m4a"):
                write_mp4_tags(Path(track.local_file_path), track)
            return track

        final_path = self._target_collection_path(track)
        if final_path.exists() and final_path.stat().st_size > 1000:
            write_mp4_tags(final_path, track)
            self.store.set_local_file_path(video_id, str(final_path))
            track.local_file_path = str(final_path)
            return track

        tmp_base = self.tmp_dir / f"import_{video_id}"
        downloaded = self._download_m4a_sync(video_id, tmp_base)

        try:
            if downloaded.suffix.lower() in (".m4a", ".mp4"):
                write_mp4_tags(downloaded, track)
                dest = final_path
            else:
                dest = final_path.with_suffix(downloaded.suffix)

            dest.parent.mkdir(parents=True, exist_ok=True)
            # Atomic move across same filesystem, or safe copy+replace across mounts
            tmp_dest = dest.parent / f".{dest.name}.part"
            shutil.move(str(downloaded), str(tmp_dest))
            os.replace(tmp_dest, dest)

            self.store.set_local_file_path(video_id, str(dest))
            track.local_file_path = str(dest)
            self._notify_amarok_watcher(dest)
            log.info("Imported tagged track into Amarok Local Collection: %s", dest)
            self._enforce_cache_limit()
            return track
        finally:
            if downloaded.exists():
                downloaded.unlink(missing_ok=True)

    def _notify_amarok_watcher(self, audio_path: Path) -> None:
        """Bump mtimes up to ~/Music so Amarok's KDirWatch scans the new/updated track within ~2s."""
        settings = self.auth.get_settings()
        base = settings.resolved_collection_dir()
        now = time.time()
        targets = [audio_path, audio_path.parent, base, base.parent]
        for p in targets:
            try:
                if p.exists():
                    os.utime(p, (now, now))
            except OSError:
                pass

    def sync_tags_for_track_sync(self, video_id: str) -> bool:
        """Re-write updated rating/playcount/score tags onto an already-imported local .m4a file."""
        track = self.store.get_track(video_id)
        if not track or not track.local_file_path:
            return False
        p = Path(track.local_file_path)
        if not p.exists() or p.suffix.lower() not in (".m4a", ".mp4"):
            return False
        ok = write_mp4_tags(p, track)
        if ok:
            self._notify_amarok_watcher(p)
        return ok

    async def on_track_played(
        self,
        video_id: str,
        record_play: bool = False,
        force_collection_import: bool = False,
        force_refresh: bool = False,
    ) -> CachedTrack | None:
        """Async background entrypoint invoked whenever a YouTube Music track is played."""
        if not VIDEO_ID_RE.match(video_id):
            return None

        settings = self.auth.get_settings()
        if not settings.auto_import_metadata and not force_collection_import:
            return self.store.get_track(video_id)

        existing_task = self._inflight.get(video_id)
        if existing_task and not existing_task.done() and not force_refresh:
            track = await existing_task
            if record_play and track:
                updated = await asyncio.to_thread(self.store.record_play, video_id)
                await asyncio.to_thread(self.sync_tags_for_track_sync, video_id)
                return updated
            return track

        async def _worker() -> CachedTrack | None:
            try:
                # Yield briefly when auto-triggered so Amarok's GStreamer stream gets priority on bandwidth & YouTube
                if not force_collection_import and not force_refresh:
                    await asyncio.sleep(2.5)
                if record_play:
                    await asyncio.to_thread(self.store.record_play, video_id)
                if force_collection_import or settings.auto_import_collection:
                    return await asyncio.to_thread(
                        self.import_to_local_collection_sync, video_id, force_refresh
                    )
                return await asyncio.to_thread(
                    self.fetch_and_cache_metadata_sync, video_id, force_refresh
                )
            except Exception as exc:
                log.warning("Automated track import failed for %s: %s", video_id, exc)
                return self.store.get_track(video_id)

        task = asyncio.create_task(_worker(), name=f"import-{video_id}")
        self._inflight[video_id] = task
        try:
            return await task
        finally:
            if self._inflight.get(video_id) is task:
                self._inflight.pop(video_id, None)
