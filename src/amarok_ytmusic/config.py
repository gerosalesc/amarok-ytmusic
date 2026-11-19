"""Configuration and filesystem path helpers for amarok-ytmusic."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18732

FLATPAK_AMAROK_DATA = Path.home() / ".var" / "app" / "org.kde.amarok" / "data" / "amarok"
NATIVE_AMAROK_DATA = Path.home() / ".local" / "share" / "amarok"


def get_config_dir() -> Path:
    base = Path(os.environ.get("AMAROK_YTMUSIC_CONFIG_DIR", Path.home() / ".config" / "amarok-ytmusic"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_amarok_data_dir() -> Path:
    """Return the Amarok data directory (prefers Flatpak if present)."""
    override = os.environ.get("AMAROK_DATA_DIR")
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if FLATPAK_AMAROK_DATA.parent.exists() or FLATPAK_AMAROK_DATA.exists():
        FLATPAK_AMAROK_DATA.mkdir(parents=True, exist_ok=True)
        return FLATPAK_AMAROK_DATA
    NATIVE_AMAROK_DATA.mkdir(parents=True, exist_ok=True)
    return NATIVE_AMAROK_DATA


def get_ipc_dir() -> Path:
    """Return the IPC directory shared between the host daemon and Amarok Flatpak."""
    override = os.environ.get("AMAROK_YTMUSIC_IPC_DIR")
    if override:
        ipc = Path(override)
    else:
        ipc = get_amarok_data_dir() / "ytmusic_ipc"
    (ipc / "req").mkdir(parents=True, exist_ok=True)
    (ipc / "resp").mkdir(parents=True, exist_ok=True)
    return ipc


@dataclass
class AppSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    audio_quality: str = "best"  # "best" | "m4a" | "opus" | "medium"
    auth_mode: str = "none"  # "none" | "browser" | "oauth"
    browser_source: str = ""  # e.g. "chrome", "firefox", "headers"
    authuser: str = "0"
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    scrobble_history: bool = True

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stream_url(self, video_id: str) -> str:
        return f"{self.base_url}/stream/{video_id}"

    def ytdlp_format_selector(self) -> str:
        if self.audio_quality == "opus":
            return "bestaudio[acodec*=opus]/bestaudio/best"
        if self.audio_quality == "m4a":
            return "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"
        if self.audio_quality == "medium":
            return "bestaudio[abr<=160]/bestaudio/best"
        # Default "best": prefer clean audio-only containers that GStreamer plays reliably
        return "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best"


class SettingsStore:
    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = config_dir or get_config_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.config_dir / "settings.json"
        self.browser_auth_path = self.config_dir / "browser.json"
        self.oauth_auth_path = self.config_dir / "oauth.json"
        self.cookies_txt_path = self.config_dir / "cookies.txt"
        self.ampache_state_path = self.config_dir / "ampache_state.json"

    def load(self) -> AppSettings:
        if not self.settings_path.exists():
            return AppSettings()
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            valid = {k: v for k, v in raw.items() if k in AppSettings.__dataclass_fields__}
            return AppSettings(**valid)
        except Exception:
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.settings_path)
