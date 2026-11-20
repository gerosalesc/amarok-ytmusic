#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLATPAK_SCRIPT_DIR="$HOME/.var/app/org.kde.amarok/data/amarok/scripts/youtube_music"
NATIVE_SCRIPT_DIR="$HOME/.local/share/amarok/scripts/youtube_music"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
LOCAL_BIN_DIR="$HOME/.local/bin"

echo "==> Syncing Python dependencies with uv..."
cd "$PROJECT_DIR"
uv sync

echo "==> Installing Amarok ScriptableService plugin..."
install_script_to() {
    local target="$1"
    mkdir -p "$target"
    cp "$PROJECT_DIR/amarok_script/script.json" "$target/script.json"
    cp "$PROJECT_DIR/amarok_script/script.spec" "$target/script.spec"
    cp "$PROJECT_DIR/amarok_script/main.js" "$target/main.js"
    echo "    Installed to $target"
}

if [[ -d "$HOME/.var/app/org.kde.amarok" ]] || flatpak info org.kde.amarok >/dev/null 2>&1; then
    install_script_to "$FLATPAK_SCRIPT_DIR"
    mkdir -p "$HOME/.var/app/org.kde.amarok/data/amarok/ytmusic_ipc/req" \
             "$HOME/.var/app/org.kde.amarok/data/amarok/ytmusic_ipc/resp"
fi

if [[ -d "$HOME/.local/share/amarok" ]]; then
    install_script_to "$NATIVE_SCRIPT_DIR"
fi

echo "==> Installing Amarok Context Applets (Current Track + Lyrics YouTube Music integration)..."
install_kpackage_applets() {
    local target_base="$1"
    local sys_base=""
    for candidate in \
        "/var/lib/flatpak/app/org.kde.amarok/current/active/files/share/kpackage/amarok" \
        "$HOME/.local/share/flatpak/app/org.kde.amarok/current/active/files/share/kpackage/amarok" \
        "/usr/share/kpackage/amarok" \
        "/usr/local/share/kpackage/amarok"; do
        if [[ -d "$candidate/org.kde.amarok.currenttrack" ]]; then
            sys_base="$candidate"
            break
        fi
    done

    if [[ -z "$sys_base" ]]; then
        echo "    Skipped KPackage applet install (could not locate stock Amarok kpackage directory)"
        return 0
    fi

    for pkg in org.kde.amarok.currenttrack org.kde.amarok.lyrics; do
        if [[ -d "$sys_base/$pkg" && -d "$PROJECT_DIR/amarok_kpackage/$pkg" ]]; then
            mkdir -p "$target_base/$pkg"
            cp -a "$sys_base/$pkg/." "$target_base/$pkg/"
            cp -a "$PROJECT_DIR/amarok_kpackage/$pkg/." "$target_base/$pkg/"
            echo "    Installed $pkg -> $target_base/$pkg"
        fi
    done
}

if [[ -d "$HOME/.var/app/org.kde.amarok" ]] || flatpak info org.kde.amarok >/dev/null 2>&1; then
    install_kpackage_applets "$HOME/.var/app/org.kde.amarok/data/kpackage/amarok"
fi

if [[ -d "$HOME/.local/share/amarok" ]]; then
    install_kpackage_applets "$HOME/.local/share/kpackage/amarok"
fi

echo "==> Configuring Amarok's native Ampache Internet Service for YouTube Music..."
python3 - <<'PY'
from pathlib import Path
import re

rc_candidates = [
    Path.home() / ".var" / "app" / "org.kde.amarok" / "config" / "amarokrc",
    Path.home() / ".config" / "amarokrc",
]

for rc in rc_candidates:
    if not rc.parent.exists():
        continue
    text = rc.read_text(encoding="utf-8") if rc.exists() else ""

    def set_ini_key(content: str, section: str, key: str, value: str) -> str:
        header = f"[{section}]"
        pattern = re.compile(rf"(?ms)^\[{re.escape(section)}\]\n(.*?)(?=^\[|\Z)")
        m = pattern.search(content)
        if not m:
            sep = "\n\n" if content and not content.endswith("\n\n") else ("\n" if content and not content.endswith("\n") else "")
            return f"{content}{sep}{header}\n{key}={value}\n"
        body = m.group(1)
        key_re = re.compile(rf"(?m)^{re.escape(key)}=.*$")
        if key_re.search(body):
            new_body = key_re.sub(f"{key}={value}", body)
        else:
            if body and not body.endswith("\n"):
                body += "\n"
            new_body = body + f"{key}={value}\n"
        return content[:m.start(1)] + new_body + content[m.end(1):]

    text = set_ini_key(text, "Plugins", "amarok_service_ampacheEnabled", "true")
    # Preserve existing Ampache servers if server0 is already something else
    if "http://127.0.0.1:18732/ampache" not in text:
        idx = 0
        while re.search(rf"(?m)^server{idx}=", text):
            idx += 1
        text = set_ini_key(
            text,
            "Service_Ampache",
            f"server{idx}",
            "YouTube Music,http://127.0.0.1:18732/ampache,ytmusic,ytmusic",
        )
    rc.write_text(text, encoding="utf-8")
    print(f"    Updated {rc}")
PY

echo "==> Linking CLI command to $LOCAL_BIN_DIR/amarok-ytmusic..."
mkdir -p "$LOCAL_BIN_DIR"
ln -sf "$PROJECT_DIR/.venv/bin/amarok-ytmusic" "$LOCAL_BIN_DIR/amarok-ytmusic"

echo "==> Installing systemd --user service..."
mkdir -p "$SYSTEMD_USER_DIR"
cat > "$SYSTEMD_USER_DIR/amarok-ytmusic.service" <<EOF
[Unit]
Description=Amarok YouTube Music Web Auth & Streaming Bridge
After=network-online.target graphical-session.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/amarok-ytmusic serve --host 127.0.0.1 --port 18732
Restart=on-failure
RestartSec=3
TimeoutStopSec=5
Environment=PYTHONUNBUFFERED=1
Environment="PATH=$HOME/.deno/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=default.target
EOF

if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable amarok-ytmusic.service
    systemctl --user restart amarok-ytmusic.service
    echo "==> systemd user service enabled and restarted!"
else
    echo "==> Note: systemd --user session not active; start manually with:"
    echo "    amarok-ytmusic serve --open-auth"
fi

echo ""
echo "Done! Next steps:"
echo "  1. Open http://127.0.0.1:18732 in your browser to sign in to YouTube Music."
echo "  2. Fully quit Amarok (Amarok -> Quit / Ctrl+Q, not just closing to tray) and relaunch:"
echo "       flatpak run org.kde.amarok"
echo "  3. Open Media Sources -> Internet -> Ampache (YouTube Music)!"
