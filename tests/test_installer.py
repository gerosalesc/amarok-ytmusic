"""Tests for cross-distro Amarok installer and uninstaller."""

from __future__ import annotations

from pathlib import Path

from amarok_ytmusic.cli import main as cli_main
from amarok_ytmusic.installer import (
    KPACKAGE_APPLETS,
    MANAGED_MARKER,
    configure_amarokrc,
    find_asset_dir,
    remove_from_amarokrc,
    render_systemd_unit,
    run_setup,
    run_uninstall,
)


def test_find_bundled_assets() -> None:
    script_dir = find_asset_dir("amarok_script")
    kpkg_dir = find_asset_dir("amarok_kpackage")
    assert script_dir is not None and (script_dir / "main.js").is_file()
    assert kpkg_dir is not None and (kpkg_dir / "org.kde.amarok.currenttrack" / "contents" / "ui" / "main.qml").is_file()


def test_configure_and_remove_amarokrc_preserves_existing_servers(tmp_path: Path) -> None:
    rc = tmp_path / "amarokrc"
    rc.write_text(
        "[General]\nfoo=bar\n\n[Service_Ampache]\nserver0=MyHome,http://nas.local/ampache,me,secret\n",
        encoding="utf-8",
    )

    assert configure_amarokrc(rc) is True
    content = rc.read_text(encoding="utf-8")
    assert "amarok_service_ampacheEnabled=true" in content
    assert "server0=MyHome,http://nas.local/ampache,me,secret" in content
    assert "server1=YouTube Music,http://127.0.0.1:18732/ampache,ytmusic,ytmusic" in content

    # Idempotent second run
    assert configure_amarokrc(rc) is False

    # Removal deletes only YouTube Music entry and keeps existing server0 & other sections
    assert remove_from_amarokrc(rc) is True
    after = rc.read_text(encoding="utf-8")
    assert "server0=MyHome,http://nas.local/ampache,me,secret" in after
    assert "YouTube Music" not in after
    assert "foo=bar" in after

    # Second removal is a no-op
    assert remove_from_amarokrc(rc) is False


def test_run_setup_and_uninstall_flatpak_and_native(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".local" / "share" / "amarok").mkdir(parents=True)
    (home / ".var" / "app" / "org.kde.amarok" / "config").mkdir(parents=True)

    # Create fake stock Amarok kpackage directory
    stock = tmp_path / "stock_kpackage"
    for pkg in KPACKAGE_APPLETS:
        ui = stock / pkg / "contents" / "ui"
        ui.mkdir(parents=True)
        (ui / "stock_only.qml").write_text("// stock file\n", encoding="utf-8")
        (ui / "main.qml").write_text("// stock main\n", encoding="utf-8")

    res = run_setup(
        home=home,
        install_service=True,
        exec_cmd="/usr/local/bin/amarok-ytmusic",
        manage_systemctl=False,
        check_flatpak_cli=False,
        stock_kpackage_candidates=[stock],
    )

    assert len(res.scripts_installed) == 2
    assert (home / ".var/app/org.kde.amarok/data/amarok/scripts/youtube_music/main.js").is_file()
    assert (home / ".local/share/amarok/scripts/youtube_music/main.js").is_file()
    assert (home / ".var/app/org.kde.amarok/data/amarok/ytmusic_ipc/req").is_dir()

    # Both Flatpak and native got both applets (4 total) with stock + overlay merged
    assert len(res.applets_installed) == 4
    fp_ct = home / ".var/app/org.kde.amarok/data/kpackage/amarok/org.kde.amarok.currenttrack"
    assert (fp_ct / "contents/ui/stock_only.qml").is_file()
    assert "stock main" not in (fp_ct / "contents/ui/main.qml").read_text(encoding="utf-8")
    assert (fp_ct / MANAGED_MARKER).is_file()

    assert len(res.amarokrc_updated) == 2
    assert res.service_file == home / ".config/systemd/user/amarok-ytmusic.service"
    unit_text = res.service_file.read_text(encoding="utf-8")
    assert "ExecStart=/usr/local/bin/amarok-ytmusic serve --host 127.0.0.1 --port 18732" in unit_text

    # Uninstall cleans everything up cleanly
    un = run_uninstall(home=home, manage_systemctl=False)
    assert un.service_removed == res.service_file
    assert not res.service_file.exists()
    assert len(un.scripts_removed) == 2
    assert len(un.applets_removed) == 4
    assert len(un.amarokrc_updated) == 2
    assert not fp_ct.exists()


def test_setup_no_service_skips_systemd_unit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    res = run_setup(
        home=home,
        install_service=False,
        manage_systemctl=False,
        check_flatpak_cli=False,
        stock_kpackage_candidates=[],
    )
    assert res.service_file is None
    assert not (home / ".config/systemd/user/amarok-ytmusic.service").exists()
    assert len(res.amarokrc_updated) == 1


def test_render_systemd_unit_custom_port(tmp_path: Path) -> None:
    text = render_systemd_unit(
        exec_cmd="/home/u/.local/bin/amarok-ytmusic",
        home=tmp_path,
        host="127.0.0.1",
        port=19999,
    )
    assert "ExecStart=/home/u/.local/bin/amarok-ytmusic serve --host 127.0.0.1 --port 19999" in text
    assert f'Environment="PATH={tmp_path}/.deno/bin:{tmp_path}/.local/bin:/usr/local/bin:/usr/bin:/bin"' in text


def test_cli_version_flag(capsys) -> None:
    try:
        cli_main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "amarok-ytmusic" in out
