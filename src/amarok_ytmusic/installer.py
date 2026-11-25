"""Cross-distro installer and uninstaller for Amarok integration & systemd user service."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_HOST, DEFAULT_PORT

KPACKAGE_APPLETS = ("org.kde.amarok.currenttrack", "org.kde.amarok.lyrics")
MANAGED_MARKER = ".amarok-ytmusic-managed"
SCRIPT_FILES = ("script.json", "script.spec", "main.js")
SERVICE_NAME = "amarok-ytmusic.service"


@dataclass
class SetupResult:
    scripts_installed: list[Path] = field(default_factory=list)
    applets_installed: list[Path] = field(default_factory=list)
    applets_skipped_reason: str = ""
    amarokrc_updated: list[Path] = field(default_factory=list)
    service_file: Path | None = None
    service_started: bool = False
    service_note: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class UninstallResult:
    scripts_removed: list[Path] = field(default_factory=list)
    applets_removed: list[Path] = field(default_factory=list)
    amarokrc_updated: list[Path] = field(default_factory=list)
    service_removed: Path | None = None
    service_stopped: bool = False


def find_asset_dir(subdir: str) -> Path | None:
    """Locate bundled Amarok assets whether running from wheel, source tree, or /usr/share."""
    pkg_dir = Path(__file__).resolve().parent
    candidates = [
        pkg_dir / "assets" / subdir,
        pkg_dir.parent.parent / subdir,
        Path("/usr/share/amarok-ytmusic") / subdir,
        Path("/usr/local/share/amarok-ytmusic") / subdir,
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def set_ini_key(content: str, section: str, key: str, value: str) -> str:
    """Set or update a key inside an INI section without disturbing unrelated sections."""
    header = f"[{section}]"
    pattern = re.compile(rf"(?ms)^\[{re.escape(section)}\]\n(.*?)(?=^\[|\Z)")
    m = pattern.search(content)
    if not m:
        if content and not content.endswith("\n\n"):
            sep = "\n" if content.endswith("\n") else "\n\n"
        else:
            sep = ""
        return f"{content}{sep}{header}\n{key}={value}\n"
    body = m.group(1)
    key_re = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    if key_re.search(body):
        new_body = key_re.sub(f"{key}={value}", body)
    else:
        if body and not body.endswith("\n"):
            body += "\n"
        new_body = body + f"{key}={value}\n"
    return content[: m.start(1)] + new_body + content[m.end(1) :]


def configure_amarokrc(
    rc_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> bool:
    """Enable Amarok's native Ampache plugin and register the local YouTube Music server."""
    if not rc_path.parent.exists():
        return False
    original = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    text = set_ini_key(original, "Plugins", "amarok_service_ampacheEnabled", "true")
    ampache_url = f"http://{host}:{port}/ampache"
    if ampache_url not in text:
        idx = 0
        while re.search(rf"(?m)^server{idx}=", text):
            idx += 1
        text = set_ini_key(
            text,
            "Service_Ampache",
            f"server{idx}",
            f"YouTube Music,{ampache_url},ytmusic,ytmusic",
        )
    if text != original or not rc_path.exists():
        rc_path.write_text(text, encoding="utf-8")
        return True
    return False


def remove_from_amarokrc(
    rc_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> bool:
    """Remove the YouTube Music Ampache server entry from amarokrc while keeping other servers."""
    if not rc_path.exists():
        return False
    original = rc_path.read_text(encoding="utf-8")
    ampache_url = f"http://{host}:{port}/ampache"
    lines = original.splitlines(keepends=True)
    new_lines: list[str] = []
    in_ampache = False
    removed = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_ampache = stripped == "[Service_Ampache]"
            new_lines.append(line)
            continue
        if in_ampache and re.match(r"^server\d+=", stripped):
            if ampache_url in stripped or (
                "YouTube Music," in stripped and "/ampache,ytmusic,ytmusic" in stripped
            ):
                removed = True
                continue
        new_lines.append(line)
    if not removed:
        return False
    rc_path.write_text("".join(new_lines), encoding="utf-8")
    return True


def _detect_flatpak_amarok(home: Path, *, check_cli: bool = True) -> bool:
    if (home / ".var" / "app" / "org.kde.amarok").is_dir():
        return True
    if not check_cli or not shutil.which("flatpak"):
        return False
    try:
        res = subprocess.run(
            ["flatpak", "info", "org.kde.amarok"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return res.returncode == 0
    except OSError:
        return False


def _locate_stock_kpackage_dir(
    home: Path,
    extra_candidates: list[Path] | None = None,
) -> Path | None:
    candidates: list[Path] = []
    if extra_candidates:
        candidates.extend(extra_candidates)
    else:
        candidates.extend(
            [
                Path("/var/lib/flatpak/app/org.kde.amarok/current/active/files/share/kpackage/amarok"),
                home / ".local" / "share" / "flatpak" / "app" / "org.kde.amarok" / "current" / "active" / "files" / "share" / "kpackage" / "amarok",
                Path("/usr/share/kpackage/amarok"),
                Path("/usr/local/share/kpackage/amarok"),
            ]
        )
    for candidate in candidates:
        if (candidate / "org.kde.amarok.currenttrack").is_dir():
            return candidate
    return None


def _copy_tree_overlay(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def resolve_executable(explicit_exec: str | None = None) -> str:
    """Resolve the command prefix used in systemd ExecStart."""
    if explicit_exec:
        return explicit_exec
    # Prefer the currently invoked console script if it is `amarok-ytmusic`
    argv0 = Path(sys.argv[0]) if sys.argv else Path("")
    if argv0.name == "amarok-ytmusic":
        resolved = argv0 if argv0.is_absolute() else Path(shutil.which(str(argv0)) or argv0.resolve())
        if resolved.exists() and os.access(resolved, os.X_OK):
            return str(resolved)
    found = shutil.which("amarok-ytmusic")
    if found:
        return str(Path(found))
    return f"{sys.executable} -m amarok_ytmusic"


def render_systemd_unit(
    *,
    exec_cmd: str,
    home: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    working_dir: Path | None = None,
) -> str:
    work_dir = working_dir or home
    return (
        "[Unit]\n"
        "Description=Amarok YouTube Music Web Auth & Streaming Bridge\n"
        "After=network-online.target graphical-session.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={work_dir}\n"
        f"ExecStart={exec_cmd} serve --host {host} --port {port}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "TimeoutStopSec=5\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        f'Environment="PATH={home}/.deno/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"\n'
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemctl_user_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        res = subprocess.run(
            ["systemctl", "--user", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        # systemctl status returns 0 (running) or 3 (degraded/inactive units); both mean systemd --user is live
        return res.returncode in (0, 3)
    except OSError:
        return False


def run_setup(
    *,
    home: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    install_service: bool = True,
    exec_cmd: str | None = None,
    manage_systemctl: bool = True,
    check_flatpak_cli: bool = True,
    stock_kpackage_candidates: list[Path] | None = None,
    script_src_dir: Path | None = None,
    kpackage_src_dir: Path | None = None,
) -> SetupResult:
    """Install Amarok scripts, Context Applets, amarokrc config, and systemd --user service."""
    home_dir = (home or Path.home()).expanduser()
    result = SetupResult()

    has_flatpak = _detect_flatpak_amarok(home_dir, check_cli=check_flatpak_cli)
    native_amarok_dir = home_dir / ".local" / "share" / "amarok"
    has_native = native_amarok_dir.is_dir() or (home_dir / ".config" / "amarokrc").exists()

    # 1. ScriptableService files
    script_src = script_src_dir or find_asset_dir("amarok_script")
    if script_src is None:
        result.warnings.append("Bundled amarok_script assets not found; skipped ScriptableService install.")
    else:
        script_targets: list[Path] = []
        if has_flatpak:
            script_targets.append(
                home_dir / ".var" / "app" / "org.kde.amarok" / "data" / "amarok" / "scripts" / "youtube_music"
            )
            (home_dir / ".var" / "app" / "org.kde.amarok" / "data" / "amarok" / "ytmusic_ipc" / "req").mkdir(
                parents=True, exist_ok=True
            )
            (home_dir / ".var" / "app" / "org.kde.amarok" / "data" / "amarok" / "ytmusic_ipc" / "resp").mkdir(
                parents=True, exist_ok=True
            )
        if has_native:
            script_targets.append(native_amarok_dir / "scripts" / "youtube_music")
        for target in script_targets:
            target.mkdir(parents=True, exist_ok=True)
            for fname in SCRIPT_FILES:
                src_file = script_src / fname
                if src_file.exists():
                    shutil.copy2(src_file, target / fname)
            result.scripts_installed.append(target)

    # 2. KPackage Context Applets (Current Track + Lyrics)
    kpkg_src = kpackage_src_dir or find_asset_dir("amarok_kpackage")
    stock_kpkg = _locate_stock_kpackage_dir(home_dir, extra_candidates=stock_kpackage_candidates)
    if kpkg_src is None:
        result.applets_skipped_reason = "Bundled amarok_kpackage assets not found"
    elif stock_kpkg is None:
        result.applets_skipped_reason = "Could not locate stock Amarok kpackage directory"
    else:
        applet_bases: list[Path] = []
        if has_flatpak:
            applet_bases.append(home_dir / ".var" / "app" / "org.kde.amarok" / "data" / "kpackage" / "amarok")
        if has_native:
            applet_bases.append(home_dir / ".local" / "share" / "kpackage" / "amarok")
        for base in applet_bases:
            for pkg in KPACKAGE_APPLETS:
                stock_pkg = stock_kpkg / pkg
                overlay_pkg = kpkg_src / pkg
                if stock_pkg.is_dir() and overlay_pkg.is_dir():
                    target = base / pkg
                    _copy_tree_overlay(stock_pkg, target)
                    _copy_tree_overlay(overlay_pkg, target)
                    (target / MANAGED_MARKER).write_text("managed by amarok-ytmusic\n", encoding="utf-8")
                    result.applets_installed.append(target)

    # 3. Configure amarokrc (Flatpak and/or native)
    rc_candidates = [
        home_dir / ".var" / "app" / "org.kde.amarok" / "config" / "amarokrc",
        home_dir / ".config" / "amarokrc",
    ]
    for rc in rc_candidates:
        if rc.parent.is_dir():
            if configure_amarokrc(rc, host=host, port=port):
                result.amarokrc_updated.append(rc)

    # 4. Systemd user service
    if install_service:
        unit_dir = home_dir / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / SERVICE_NAME
        resolved_exec = resolve_executable(exec_cmd)
        unit_text = render_systemd_unit(
            exec_cmd=resolved_exec,
            home=home_dir,
            host=host,
            port=port,
        )
        unit_path.write_text(unit_text, encoding="utf-8")
        result.service_file = unit_path

        if manage_systemctl and _systemctl_user_available():
            try:
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=10)
                subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], check=True, timeout=10)
                subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], check=True, timeout=15)
                result.service_started = True
            except (OSError, subprocess.SubprocessError) as exc:
                result.service_note = f"Failed to start systemd --user service ({exc}); start manually with: amarok-ytmusic serve --open-auth"
        else:
            result.service_note = "systemd --user session not active; start manually with: amarok-ytmusic serve --open-auth"

    return result


def run_uninstall(
    *,
    home: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    manage_systemctl: bool = True,
) -> UninstallResult:
    """Remove Amarok integration files, amarokrc entry, and user systemd service (preserves cache/config)."""
    home_dir = (home or Path.home()).expanduser()
    result = UninstallResult()

    # 1. Stop & remove user systemd service if present
    unit_path = home_dir / ".config" / "systemd" / "user" / SERVICE_NAME
    if unit_path.exists():
        if manage_systemctl and _systemctl_user_available():
            try:
                subprocess.run(
                    ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                result.service_stopped = True
            except OSError:
                pass
        unit_path.unlink(missing_ok=True)
        result.service_removed = unit_path
        if manage_systemctl and _systemctl_user_available():
            try:
                subprocess.run(
                    ["systemctl", "--user", "daemon-reload"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except OSError:
                pass

    # 2. Remove ScriptableService directories
    script_candidates = [
        home_dir / ".var" / "app" / "org.kde.amarok" / "data" / "amarok" / "scripts" / "youtube_music",
        home_dir / ".local" / "share" / "amarok" / "scripts" / "youtube_music",
    ]
    for path in script_candidates:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            result.scripts_removed.append(path)

    # 3. Remove managed KPackage applet overrides
    applet_bases = [
        home_dir / ".var" / "app" / "org.kde.amarok" / "data" / "kpackage" / "amarok",
        home_dir / ".local" / "share" / "kpackage" / "amarok",
    ]
    for base in applet_bases:
        for pkg in KPACKAGE_APPLETS:
            target = base / pkg
            if target.is_dir() and (target / MANAGED_MARKER).exists():
                shutil.rmtree(target, ignore_errors=True)
                result.applets_removed.append(target)

    # 4. Remove Ampache server entry from amarokrc
    rc_candidates = [
        home_dir / ".var" / "app" / "org.kde.amarok" / "config" / "amarokrc",
        home_dir / ".config" / "amarokrc",
    ]
    for rc in rc_candidates:
        if remove_from_amarokrc(rc, host=host, port=port):
            result.amarokrc_updated.append(rc)

    return result
