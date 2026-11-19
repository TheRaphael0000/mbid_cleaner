"""Navidrome/Subsonic client for retrieving MBIDs from a local library."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urljoin

import requests

from .mb import match_exact
from .util import normalize


class NavidromeError(RuntimeError):
    """Raised when a Navidrome/Subsonic request fails."""


def _first_str(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return ""


class NavidromeClient:
    """Minimal Subsonic API client focused on MBID lookup."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        timeout: float = 15.0,
    ) -> None:
        base = (url or "").strip()
        if not base:
            raise ValueError("Navidrome URL is required")
        # Ensure base ends with '/' so urljoin appends the endpoint correctly.
        self.base = base.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()

    def _auth_params(self) -> dict[str, str]:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "MBIDCleaner",
            "f": "json",
        }

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        url = urljoin(self.base, endpoint)
        payload = {**self._auth_params(), **params}
        last_error = "unknown error"
        for attempt in range(3):
            try:
                response = self.session.get(url, params=payload, timeout=self.timeout)
                if response.status_code >= 400:
                    raise NavidromeError(
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                data = response.json()
                body = data.get("subsonic-response") or data.get("subsonicResponse") or {}
                if body.get("status") != "ok":
                    err = body.get("error") or {}
                    message = err.get("message") or str(err) or "Subsonic request failed"
                    raise NavidromeError(str(message))
                return body
            except (requests.RequestException, ValueError, NavidromeError) as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                else:
                    raise NavidromeError(last_error) from exc
        raise NavidromeError(last_error)

    def ping(self) -> bool:
        self._request("rest/ping.view", {})
        return True

    def search(self, kind: str, artist: str, title: str = "") -> list[dict[str, Any]]:
        """Search Navidrome and return candidates in MBClient-compatible shape."""
        if kind == "artist":
            query = artist
            counts = {"artistCount": 25, "albumCount": 0, "songCount": 0}
        elif kind == "album":
            query = f"{artist} {title}".strip()
            counts = {"artistCount": 0, "albumCount": 25, "songCount": 0}
        elif kind == "track":
            query = f"{artist} {title}".strip()
            counts = {"artistCount": 0, "albumCount": 0, "songCount": 25}
        else:
            raise ValueError(f"Unknown lookup kind: {kind}")

        body = self._request("rest/search3.view", {"query": query, **counts})
        result = body.get("searchResult3") or {}
        if kind == "artist":
            items = result.get("artist") or []
            return [self._parse_artist(i) for i in items if isinstance(i, dict)]
        if kind == "album":
            items = result.get("album") or []
            return [self._parse_album(i) for i in items if isinstance(i, dict)]
        items = result.get("song") or []
        return [self._parse_song(i) for i in items if isinstance(i, dict)]

    @staticmethod
    def _parse_artist(item: dict[str, Any]) -> dict[str, Any]:
        name = _first_str(item, "name")
        mbid = _first_str(item, "musicBrainzId", "musicBrainzID", "mbid")
        return {
            "mbid": mbid,
            "name": name,
            "artist": name,
            "credit_names": [name] if name else [],
            "score": 100 if mbid else 0,
            "url": "",
            "source": "navidrome",
        }

    @staticmethod
    def _parse_album(item: dict[str, Any]) -> dict[str, Any]:
        name = _first_str(item, "name", "title", "album")
        artist = _first_str(item, "artist")
        mbid = _first_str(item, "musicBrainzId", "musicBrainzID", "mbid")
        credits = [artist] if artist else []
        return {
            "mbid": mbid,
            "name": name,
            "artist": artist,
            "credit_names": credits,
            "score": 100 if mbid else 0,
            "url": "",
            "source": "navidrome",
        }

    @staticmethod
    def _parse_song(item: dict[str, Any]) -> dict[str, Any]:
        name = _first_str(item, "title", "name")
        artist = _first_str(item, "artist")
        mbid = _first_str(item, "musicBrainzId", "musicBrainzID", "mbid")
        credits = [artist] if artist else []
        return {
            "mbid": mbid,
            "name": name,
            "artist": artist,
            "credit_names": credits,
            "score": 100 if mbid else 0,
            "url": "",
            "source": "navidrome",
        }


def try_navidrome_match(
    client: NavidromeClient,
    kind: str,
    artist: str,
    title: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return (exact_match_or_none, candidates) from Navidrome."""
    candidates = client.search(kind, artist, title)
    exact = match_exact(kind, candidates, normalize(artist), normalize(title))
    return exact, candidates
