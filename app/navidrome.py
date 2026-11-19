"""Navidrome/Subsonic client for retrieving MBIDs from a local library."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Any
from urllib.parse import urljoin

import requests

from .util import normalize

# Navidrome-only cleanup rules to improve matching against local library tags.
# Strip parenthesized/bracketed segments that are typically metadata variants.
_STRIP_PARENS = re.compile(
    r"\s*[\(\[][^)\]]*\b(?:remaster(?:ed|ing)?|deluxe|edition|version|mix|edit|"
    r"live|mono|stereo|bonus|anniversary|expanded|explicit|clean|single|acoustic|"
    r"demo|instrumental)\b[^)\]]*[\)\]]",
    re.IGNORECASE,
)
# Strip featuring credits (common in scrobble artist/title strings).
_FEAT = re.compile(
    r"\s*(?:[\(\[]\s*)?(?:feat\.?|ft\.?|featuring)\s+[^)\]]*(?:[\)\]])?\s*$",
    re.IGNORECASE,
)
_FEAT_INLINE = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring)\s+.+$",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")


def clean_for_navidrome(text: str | None) -> str:
    """Remove common variant markers (e.g. remaster/feat) for Navidrome matching."""
    if not text:
        return ""
    value = str(text)
    # Apply repeatedly in case multiple groups/markers are present.
    for _ in range(3):
        updated = _STRIP_PARENS.sub("", value)
        updated = _FEAT.sub("", updated)
        updated = _FEAT_INLINE.sub("", updated)
        updated = updated.strip(" -–—")
        updated = _WS.sub(" ", updated).strip()
        if updated == value:
            break
        value = updated
    return value


def _nav_key(text: str | None) -> str:
    cleaned = clean_for_navidrome(text)
    return normalize(cleaned) or normalize(text)


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
        """Search Navidrome and return candidates in MBClient-compatible shape.

        For Navidrome only, query terms are cleaned (remaster/feat/etc removed)
        to improve hit rate against local library metadata.
        """
        q_artist = clean_for_navidrome(artist) or artist
        q_title = clean_for_navidrome(title) or title
        if kind == "artist":
            query = q_artist
            counts = {"artistCount": 25, "albumCount": 0, "songCount": 0}
        elif kind == "album":
            query = f"{q_artist} {q_title}".strip()
            counts = {"artistCount": 0, "albumCount": 25, "songCount": 0}
        elif kind == "track":
            query = f"{q_artist} {q_title}".strip()
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


def _match_navidrome_exact(
    kind: str,
    candidates: list[dict[str, Any]],
    artist: str,
    title: str,
) -> dict[str, Any] | None:
    """Navidrome-only relaxed exact match (cleaned remaster/feat/etc)."""
    artist_key = _nav_key(artist)
    title_key = _nav_key(title)
    for candidate in candidates:
        name = _nav_key(candidate.get("name", ""))
        if kind == "artist":
            if name and name == artist_key and artist_key:
                return candidate
            continue
        if not title_key or name != title_key:
            continue
        credits = {_nav_key(n) for n in candidate.get("credit_names", [])}
        if artist_key and artist_key in credits:
            return candidate
    return None


def try_navidrome_match(
    client: NavidromeClient,
    kind: str,
    artist: str,
    title: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return (exact_match_or_none, candidates) from Navidrome.

    Matching uses Navidrome-only cleanup rules (e.g. strips remaster/feat
    markers) so common tag variants can still be treated as exact matches.
    """
    candidates = client.search(kind, artist, title)
    exact = _match_navidrome_exact(kind, candidates, artist, title)
    return exact, candidates
