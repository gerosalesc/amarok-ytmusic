"""Tests for atomic directory/file IPC protocol and watcher."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from amarok_ytmusic.auth import AuthManager
from amarok_ytmusic.config import SettingsStore
from amarok_ytmusic.ipc_watcher import IPCWatcher, decode_ipc_request, encode_ipc_request
from amarok_ytmusic.ytmusic_client import YTMusicCatalogClient


def test_ipc_encode_decode_roundtrip() -> None:
    for payload in ["", "liked", "playlist:PL12345_abc", "daft punk — random access memories 🎵"]:
        encoded = encode_ipc_request("170000_1", "folders", payload)
        assert "/" not in encoded
        assert len(encoded.encode("utf-8")) <= 255
        decoded = decode_ipc_request(encoded)
        assert decoded == ("170000_1", "folders", payload)


def test_ipc_decode_ignores_invalid_names() -> None:
    assert decode_ipc_request("not_a_request") is None
    assert decode_ipc_request("x.1.ping.") is None
    assert decode_ipc_request("r..ping.") is None


async def test_ipc_watcher_handles_ping_and_folders(tmp_path: Path) -> None:
    store = SettingsStore(config_dir=tmp_path / "cfg")
    auth = AuthManager(store)
    catalog = YTMusicCatalogClient(auth)
    ipc_dir = tmp_path / "ipc"

    watcher = IPCWatcher(catalog, auth, ipc_dir=ipc_dir)
    watcher.start()
    try:
        # 1. Ping request
        ping_dir = encode_ipc_request("req_1", "ping", "")
        (ipc_dir / "req" / ping_dir).mkdir()

        resp_file = ipc_dir / "resp" / "req_1.json"
        for _ in range(50):
            if resp_file.exists():
                break
            await asyncio.sleep(0.03)

        assert resp_file.exists()
        data = json.loads(resp_file.read_text(encoding="utf-8"))
        assert data["ok"] is True
        assert data["authenticated"] is False
        assert data["baseUrl"] == "http://127.0.0.1:18732"

        # 2. Unauthenticated root folders request returns sign-in prompt item
        folders_dir = encode_ipc_request("req_2", "folders", "")
        (ipc_dir / "req" / folders_dir).mkdir()

        resp_file_2 = ipc_dir / "resp" / "req_2.json"
        for _ in range(50):
            if resp_file_2.exists():
                break
            await asyncio.sleep(0.03)

        assert resp_file_2.exists()
        data2 = json.loads(resp_file_2.read_text(encoding="utf-8"))
        assert data2["ok"] is True
        assert len(data2["items"]) == 1
        assert data2["items"][0]["callbackData"] == "info:not_signed_in"
    finally:
        await watcher.stop()
