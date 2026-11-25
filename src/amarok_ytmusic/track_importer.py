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
from .config import validate_cache_dir
from .metadata_store import CachedTrack, MetadataStore, amarok_rating_to_like_status
from .stream_resolver import StreamResolver, VIDEO_ID_RE
from .ytmusic_client import _best_thumbnail, _extract_album_name, _join_artists, _safe_year

log = logging.getLogger("amarok_ytmusic.importer")

UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
COMMENT_VID_RE = re.compile(r"ytmusic:([A-Za-z0-9_-]{6,20})\b")
FILENAME_VID_RE = re.compile(r"\[([A-Za-z0-9_-]{6,20})\]$")
SUPPORTED_AUDIO_EXTS = (".m4a", ".mp4", ".webm", ".opus", ".ogg", ".mp3", ".flac")


def sanitize_filename(name: str, fallback: str = "Unknown", max_len: int = 90) -> str:
    """Turn an artist, album, or track name into a safe cross-filesystem path component."""
    cleaned = UNSAFE_FILENAME_RE.sub("_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:max_len].rstrip(" .") or fallback


def read_ytmusic_video_id_from_file(audio_path: Path) -> str:
    """Read an embedded ytmusic:<videoId> tag from an M4A/MP4 file (or [videoId] from filename)."""
    if audio_path.suffix.lower() in (".m4a", ".mp4"):
        try:
            mp4 = MP4(str(audio_path))
            if mp4.tags:
                comments = mp4.tags.get("\xa9cmt") or []
                for c in comments:
                    m = COMMENT_VID_RE.search(str(c))
                    if m:
                        return m.group(1)
        except Exception:
            pass
    m = FILENAME_VID_RE.search(audio_path.stem.strip())
    if m and VIDEO_ID_RE.match(m.group(1)):
        return m.group(1)
    return ""


def write_mp4_tags(audio_path: Path, track: CachedTrack) -> bool:
    """Atomically write full metadata, cover art, lyrics, and Amarok FMPS stats tags into an M4A/MP4 file.

    Uses copy-on-write temp file + atomic os.replace() so any active HTTP stream reading the
    previous inode is never corrupted mid-playback.
    """
    tmp_path = audio_path.parent / f".{audio_path.name}.{os.getpid()}.tagtmp"
    try:
        shutil.copy2(audio_path, tmp_path)
        mp4 = MP4(str(tmp_path))
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
        os.replace(tmp_path, audio_path)
        return True
    except Exception as exc:
        log.warning("Failed to tag MP4 file %s for %s: %s", audio_path, track.video_id, exc)
        return False
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


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
            settings = self.auth.get_settings()
            transport = httpx.HTTPTransport(local_address="0.0.0.0") if settings.use_ipv4_only() else None
            with httpx.Client(transport=transport, timeout=15.0, follow_redirects=True) as client:
                resp = client.get(cover_url)
                resp.raise_for_status()
                data = resp.content
                content_type = resp.headers.get("content-type", "").lower()
            if len(data) <= 100:
                return ""
            is_png = data[:8] == b"\x89PNG\r\n\x1a\n" or "png" in content_type
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
        fetched_remote = False

        # 1. Watch playlist gives title, artists, album, year, likeStatus, inLibrary, thumbnail, and lyrics ID
        try:
            watch = yt.get_watch_playlist(videoId=video_id, limit=1) or {}
            tracks = watch.get("tracks") or []
            if tracks:
                fetched_remote = True
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
                if vd:
                    fetched_remote = True
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

        # If completely offline and we already have cached metadata, return it without bumping fetched_at
        if not fetched_remote and existing is not None:
            return existing

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
            fetched_at=time.time() if fetched_remote else 0.0,
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

    def _target_collection_path(self, track: CachedTrack, *, disambiguate: bool = False) -> Path:
        settings = self.auth.get_settings()
        base = settings.resolved_collection_dir()
        artist_dir = sanitize_filename(track.artist, fallback="Unknown Artist")
        album_dir = sanitize_filename(track.album, fallback="Singles")
        title_file = sanitize_filename(track.title, fallback=track.video_id)
        if disambiguate:
            title_file = f"{title_file} [{track.video_id}]"
        return base / artist_dir / album_dir / f"{title_file}.m4a"

    def find_local_file(self, video_id: str, track: CachedTrack | None = None) -> Path | None:
        """Locate an existing offline cached audio file for video_id (self-healing DB path if found)."""
        if not VIDEO_ID_RE.match(video_id):
            return None
        cached = track if track is not None else self.store.get_track(video_id)
        if cached and cached.local_file_path:
            p = Path(cached.local_file_path)
            try:
                if p.is_file() and p.stat().st_size > 1000:
                    return p
            except OSError:
                pass

        if cached and cached.title:
            for disambiguate in (False, True):
                base_target = self._target_collection_path(cached, disambiguate=disambiguate)
                for ext in SUPPORTED_AUDIO_EXTS:
                    cand = base_target.with_suffix(ext)
                    try:
                        if not cand.is_file() or cand.stat().st_size <= 1000:
                            continue
                    except OSError:
                        continue
                    embedded_vid = read_ytmusic_video_id_from_file(cand)
                    if embedded_vid == video_id or (not embedded_vid and not disambiguate):
                        owner = self.store.find_by_local_file_path(str(cand))
                        if owner is None or owner.video_id == video_id:
                            self.store.set_local_file_path(video_id, str(cand))
                            cached.local_file_path = str(cand)
                            return cand
        return None

    def _download_m4a_sync(self, video_id: str, out_base: Path) -> Path:
        import yt_dlp

        watch_url = f"https://music.youtube.com/watch?v={video_id}"
        cookies_path = self.auth.store.cookies_txt_path
        has_cookies = cookies_path.exists()
        has_js = StreamResolver._has_js_runtime()
        if has_cookies and has_js:
            attempts: list[tuple[bool, bool]] = [(True, True), (False, False), (True, False)]
        elif has_cookies:
            attempts = [(False, False), (True, False)]
        else:
            attempts = [(False, False)]

        out_base.parent.mkdir(parents=True, exist_ok=True)
        target_m4a = out_base.with_suffix(".m4a")
        if target_m4a.exists():
            target_m4a.unlink()

        settings = self.auth.get_settings()
        last_err: Exception | None = None
        for use_cookies, fast_tv in attempts:
            opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio[ext=mp4]/bestaudio",
                "outtmpl": str(out_base) + ".%(ext)s",
                "overwrites": True,
                "socket_timeout": 12,
                "retries": 3,
            }
            if settings.use_ipv4_only():
                opts["source_address"] = "0.0.0.0"
            if use_cookies and cookies_path.exists():
                opts["cookiefile"] = str(cookies_path)
            target_url = watch_url
            if fast_tv:
                opts["extractor_args"] = {
                    "youtube": {
                        "player_client": ["tv_samsung"],
                        "player_skip": ["webpage", "configs", "initial_data"],
                    }
                }
                target_url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target_url, download=True)
                    ext = str((info or {}).get("ext") or "m4a")
                    downloaded = out_base.with_suffix(f".{ext}")
                    if downloaded.exists() and downloaded.stat().st_size > 1000:
                        return downloaded
            except Exception as exc:
                last_err = exc
                log.warning(
                    "Audio cache download failed for %s (cookies=%s, fast_tv=%s): %s",
                    video_id,
                    use_cookies,
                    fast_tv,
                    exc,
                )

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
        """Download M4A audio, embed full metadata/cover/lyrics/Amarok FMPS stats, and store in cache folder."""
        track = self.fetch_and_cache_metadata_sync(video_id, force_refresh=force_refresh)

        # If already cached on disk, avoid touching the file unless force_refresh was requested
        existing_local = self.find_local_file(video_id, track)
        if existing_local is not None:
            track.local_file_path = str(existing_local)
            if force_refresh and existing_local.suffix.lower() in (".m4a", ".mp4"):
                write_mp4_tags(existing_local, track)
                self._notify_amarok_watcher(existing_local)
            return track

        final_path = self._target_collection_path(track)
        if final_path.exists() and final_path.stat().st_size > 1000:
            embedded_vid = read_ytmusic_video_id_from_file(final_path)
            db_owner = self.store.find_by_local_file_path(str(final_path))
            belongs_to_other = (embedded_vid and embedded_vid != video_id) or (
                db_owner is not None and db_owner.video_id != video_id
            )
            if belongs_to_other:
                final_path = self._target_collection_path(track, disambiguate=True)
            else:
                if final_path.suffix.lower() in (".m4a", ".mp4"):
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
            log.info("Cached tagged offline track: %s", dest)
            self._enforce_cache_limit()
            return track
        finally:
            if downloaded.exists():
                downloaded.unlink(missing_ok=True)

    def _notify_amarok_watcher(self, audio_path: Path) -> None:
        """Bump mtimes up to the cache folder so Amarok's KDirWatch scans the new/updated track within ~2s."""
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
        if not track:
            return False
        p = self.find_local_file(video_id, track)
        if not p or p.suffix.lower() not in (".m4a", ".mp4"):
            return False
        ok = write_mp4_tags(p, track)
        if ok:
            self._notify_amarok_watcher(p)
        return ok

    @staticmethod
    def _safe_move_file(src: Path, dest: Path) -> None:
        """Move src to dest atomically (or copy-to-.part + replace + unlink across filesystems)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dest)
            return
        except OSError:
            pass
        tmp_dest = dest.parent / f".{dest.name}.{os.getpid()}.part"
        try:
            shutil.copy2(src, tmp_dest)
            os.replace(tmp_dest, dest)
            src.unlink(missing_ok=True)
        finally:
            if tmp_dest.exists():
                tmp_dest.unlink(missing_ok=True)

    @staticmethod
    def _prune_empty_dirs(root_dir: Path) -> None:
        """Remove empty subdirectories inside root_dir without touching non-empty folders or root_dir itself."""
        if not root_dir.is_dir():
            return
        for dirpath, _dirnames, _filenames in os.walk(root_dir, topdown=False):
            p = Path(dirpath)
            if p == root_dir:
                continue
            try:
                p.rmdir()
            except OSError:
                pass

    def migrate_cache_dir_sync(self, old_dir: Path, new_dir: Path) -> dict[str, Any]:
        """Move existing cached audio files from old_dir to new_dir and update SQLite paths."""
        old_resolved = old_dir.expanduser().resolve(strict=False)
        new_resolved = validate_cache_dir(new_dir).resolve(strict=False)
        if old_resolved == new_resolved:
            return {
                "moved": 0,
                "skipped": 0,
                "errors": 0,
                "oldDir": str(old_resolved),
                "newDir": str(new_resolved),
            }

        moved = 0
        skipped = 0
        errors = 0
        tracks = self.store.list_all_with_local_file()
        for t in tracks:
            src = Path(t.local_file_path)
            if not src.is_file():
                skipped += 1
                continue
            try:
                src_resolved = src.resolve(strict=False)
                try:
                    rel = src_resolved.relative_to(old_resolved)
                    dest = new_resolved / rel
                except ValueError:
                    artist_dir = sanitize_filename(t.artist, fallback="Unknown Artist")
                    album_dir = sanitize_filename(t.album, fallback="Singles")
                    dest = new_resolved / artist_dir / album_dir / src.name

                if src_resolved == dest.resolve(strict=False):
                    skipped += 1
                    continue

                if dest.exists():
                    dest = dest.with_stem(f"{dest.stem} [{t.video_id}]")

                self._safe_move_file(src, dest)
                self.store.set_local_file_path(t.video_id, str(dest))
                moved += 1
            except Exception as exc:
                log.warning("Failed migrating cached file %s for %s: %s", src, t.video_id, exc)
                errors += 1

        self._prune_empty_dirs(old_resolved)
        self._notify_amarok_watcher(new_resolved)
        log.info(
            "Migrated offline cache %s -> %s (moved=%d, skipped=%d, errors=%d)",
            old_resolved,
            new_resolved,
            moved,
            skipped,
            errors,
        )
        return {
            "moved": moved,
            "skipped": skipped,
            "errors": errors,
            "oldDir": str(old_resolved),
            "newDir": str(new_resolved),
        }

    def scan_cache_dir_sync(self, cache_dir: Path | None = None) -> dict[str, Any]:
        """Scan the offline cache folder, index tagged audio files into SQLite, and prune missing paths."""
        settings = self.auth.get_settings()
        target_dir = (cache_dir or settings.resolved_collection_dir()).expanduser()

        # 1. Prune broken local_file_path references in SQLite
        pruned_missing = 0
        for t in self.store.list_all_with_local_file():
            try:
                p = Path(t.local_file_path)
                if not p.is_file() or p.stat().st_size <= 1000:
                    self.store.set_local_file_path(t.video_id, "")
                    pruned_missing += 1
            except OSError:
                self.store.set_local_file_path(t.video_id, "")
                pruned_missing += 1

        scanned_files = 0
        indexed_tracks = 0
        total_bytes = 0

        if target_dir.is_dir():
            for path in target_dir.rglob("*"):
                if not path.is_file() or path.name.startswith("."):
                    continue
                if path.suffix.lower() not in SUPPORTED_AUDIO_EXTS:
                    continue
                try:
                    fsize = path.stat().st_size
                except OSError:
                    continue
                if fsize <= 1000:
                    continue

                scanned_files += 1
                total_bytes += fsize
                video_id = read_ytmusic_video_id_from_file(path)
                if not video_id:
                    continue

                title = path.stem
                artist = ""
                album = ""
                year = 0
                duration_sec = 0
                lyrics = ""
                play_count = 0
                score = 0.0
                user_rating = -1
                like_status = ""
                cover_local_path = ""

                if path.suffix.lower() in (".m4a", ".mp4"):
                    try:
                        mp4 = MP4(str(path))
                        duration_sec = int(round(getattr(mp4.info, "length", 0) or 0))
                        tags = mp4.tags or {}
                        if tags.get("\xa9nam"):
                            title = str(tags["\xa9nam"][0]).strip() or title
                        if tags.get("\xa9ART"):
                            artist = str(tags["\xa9ART"][0]).strip()
                        if tags.get("\xa9alb"):
                            album = str(tags["\xa9alb"][0]).strip()
                        if tags.get("\xa9day"):
                            year = _safe_year(tags["\xa9day"][0])
                        if tags.get("\xa9lyr"):
                            lyrics = str(tags["\xa9lyr"][0])

                        raw_rating = tags.get("----:com.apple.iTunes:FMPS_Rating")
                        if raw_rating:
                            try:
                                rf = float(bytes(raw_rating[0]).decode("utf-8").strip())
                                if rf > 0.0:
                                    user_rating = max(0, min(10, int(round(rf * 10.0))))
                                    like_status = amarok_rating_to_like_status(user_rating)
                            except (ValueError, UnicodeDecodeError):
                                pass

                        raw_pc = tags.get("----:com.apple.iTunes:FMPS_Playcount")
                        if raw_pc:
                            try:
                                play_count = max(0, int(bytes(raw_pc[0]).decode("utf-8").strip()))
                            except (ValueError, UnicodeDecodeError):
                                pass

                        raw_score = tags.get("----:com.apple.iTunes:FMPS_Rating_Amarok_Score")
                        if raw_score:
                            try:
                                sf = float(bytes(raw_score[0]).decode("utf-8").strip())
                                score = round(max(0.0, min(100.0, sf * 100.0)), 1)
                            except (ValueError, UnicodeDecodeError):
                                pass

                        # Extract embedded cover art to covers_dir if not yet cached locally
                        existing_cover = next(
                            (
                                str(self.covers_dir / f"{video_id}{ext}")
                                for ext in (".jpg", ".png")
                                if (self.covers_dir / f"{video_id}{ext}").is_file()
                            ),
                            "",
                        )
                        if existing_cover:
                            cover_local_path = existing_cover
                        elif tags.get("covr"):
                            cov = bytes(tags["covr"][0])
                            if len(cov) > 100:
                                is_png = cov[:8] == b"\x89PNG\r\n\x1a\n"
                                cov_target = self.covers_dir / f"{video_id}{'.png' if is_png else '.jpg'}"
                                cov_tmp = cov_target.with_suffix(cov_target.suffix + ".tmp")
                                cov_tmp.write_bytes(cov)
                                os.replace(cov_tmp, cov_target)
                                cover_local_path = str(cov_target)
                    except Exception as exc:
                        log.debug("Could not parse MP4 metadata while scanning %s: %s", path, exc)

                if not artist or not album:
                    # Fallback to <Artist>/<Album>/<Title>.ext directory hierarchy when tags are sparse
                    try:
                        rel_parts = path.relative_to(target_dir).parts
                        if len(rel_parts) >= 3:
                            artist = artist or rel_parts[0]
                            album = album or rel_parts[1]
                    except ValueError:
                        pass

                existing = self.store.get_track(video_id)
                self.store.upsert_metadata(
                    CachedTrack(
                        video_id=video_id,
                        title=title,
                        artist=artist or "Unknown Artist",
                        album=album or "YouTube Music",
                        year=year,
                        duration_sec=duration_sec,
                        cover_local_path=cover_local_path,
                        like_status=like_status,
                        user_rating=user_rating,
                        lyrics=lyrics,
                        play_count=play_count,
                        score=score,
                        local_file_path=str(path),
                        fetched_at=existing.fetched_at if existing else 0.0,
                    )
                )
                self.store.set_local_file_path(video_id, str(path))
                indexed_tracks += 1

        log.info(
            "Scanned offline cache dir %s: scanned=%d, indexed=%d, pruned=%d (%.2f MB)",
            target_dir,
            scanned_files,
            indexed_tracks,
            pruned_missing,
            total_bytes / (1024 * 1024),
        )
        return {
            "cacheDir": str(target_dir),
            "scannedFiles": scanned_files,
            "indexedTracks": indexed_tracks,
            "prunedMissing": pruned_missing,
            "totalBytes": total_bytes,
            "totalMb": round(total_bytes / (1024 * 1024), 2),
        }

    def sync_missing_tracks_sync(self, limit: int = 50) -> dict[str, Any]:
        """Download and cache offline audio for tracks present in metadata DB but missing on disk."""
        missing = self.store.list_missing_audio_tracks(limit=limit)
        cached_count = 0
        errors: list[dict[str, str]] = []
        for t in missing:
            try:
                updated = self.import_to_local_collection_sync(t.video_id, force_refresh=False)
                if updated.local_file_path and Path(updated.local_file_path).is_file():
                    cached_count += 1
            except Exception as exc:
                errors.append({"videoId": t.video_id, "title": t.title, "error": str(exc)})
                log.warning("Failed syncing missing track %s (%s): %s", t.video_id, t.title, exc)
        return {
            "attempted": len(missing),
            "cached": cached_count,
            "failed": len(errors),
            "errors": errors,
        }

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
        should_download_audio = force_collection_import or settings.auto_import_collection
        if not settings.auto_import_metadata and not should_download_audio:
            return self.store.get_track(video_id)

        # Fast short-circuit: already cached offline with fresh metadata and no play-count update needed
        if not force_refresh and not record_play:
            cached = self.store.get_track(video_id)
            if self.store.is_metadata_fresh(cached):
                if not should_download_audio or self.find_local_file(video_id, cached) is not None:
                    return cached

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
                if should_download_audio:
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

    async def stop(self) -> None:
        """Cancel any pending background import tasks cleanly on daemon shutdown."""
        tasks = list(self._inflight.values())
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
