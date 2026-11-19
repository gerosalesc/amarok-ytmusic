"""Tests for browser cookie conversion and header generation."""

from __future__ import annotations

import http.cookiejar

import pytest
from requests.structures import CaseInsensitiveDict
from ytmusicapi.auth.browser import is_browser

from amarok_ytmusic.auth import (
    AuthError,
    build_browser_headers_from_cookies,
    cookies_to_netscape,
    raw_cookie_header_to_netscape,
)


def test_build_browser_headers_passes_ytmusicapi_validation() -> None:
    headers = build_browser_headers_from_cookies(
        {"__Secure-3PAPISID": "abc123/test", "SID": "sidval"},
        authuser="0",
    )
    assert is_browser(CaseInsensitiveDict(headers))
    assert headers["authorization"].startswith("SAPISIDHASH ")
    assert "__Secure-3PAPISID=abc123/test" in headers["cookie"]
    assert headers["x-goog-authuser"] == "0"


def test_build_browser_headers_falls_back_from_sapisid() -> None:
    headers = build_browser_headers_from_cookies({"SAPISID": "legacy_sapisid"})
    assert is_browser(CaseInsensitiveDict(headers))
    assert "__Secure-3PAPISID=legacy_sapisid" in headers["cookie"]


def test_build_browser_headers_raises_without_session_cookie() -> None:
    with pytest.raises(AuthError):
        build_browser_headers_from_cookies({"CONSENT": "YES+1"})


def test_cookies_to_netscape_format() -> None:
    c = http.cookiejar.Cookie(
        version=0,
        name="__Secure-3PAPISID",
        value="tok123",
        port=None,
        port_specified=False,
        domain=".youtube.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=1893456000,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
    )
    txt = cookies_to_netscape([c])
    assert "# Netscape HTTP Cookie File" in txt
    assert ".youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-3PAPISID\ttok123" in txt


def test_raw_cookie_header_to_netscape() -> None:
    txt = raw_cookie_header_to_netscape("__Secure-3PAPISID=abc; SID=xyz")
    assert "__Secure-3PAPISID\tabc" in txt
    assert "SID\txyz" in txt
