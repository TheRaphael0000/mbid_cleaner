"""MusicBrainz search client with strict rate limiting and exact matching."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

from .util import normalize

_LUCENE_SPECIAL = re.compile(r'([\\+\-&|!(){}\[\]^"~*?:;/])')


class MBError(RuntimeError):
    """Raised when a MusicBrainz request fails after retries."""


def lucene_escape(value: str) -> str:
    """Quote + escape a value for a MusicBrainz Lucene query."""
    return '"' + _LUCENE_SPECIAL.sub(r"\\\1", value) + '"'


def match_exact(
    kind: str,
    candidates: list[dict[str, Any]],
    artist_key: str,
    title_key: str,
) -> dict[str, Any] | None:
    """Return the first candidate that matches the source names exactly.

    Artist names are compared against every credited artist of a recording or
    release.  No fuzzy/partial matches are ever accepted, which avoids writing
    wrong MBIDs into the collection.
    """
    for candidate in candidates:
        name = normalize(candidate.get("name", ""))
        if kind == "artist":
            if name == artist_key and artist_key:
                return candidate
            continue
        if name != title_key or not title_key:
            continue
        credits = {normalize(n) for n in candidate.get("credit_names", [])}
        if artist_key and artist_key in credits:
            return candidate
    return None


class RateLimiter:
    """Simple thread-safe 'no more than one call per interval' limiter."""

    def __init__(self, interval: float = 1.05) -> None:
        self.interval = max(0.0, float(interval))
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            remaining = self.interval - (now - self._last)
            if remaining > 0:
                time.sleep(remaining)
            self._last = time.monotonic()


class MBClient:
    BASE = "https://musicbrainz.org/ws/2/"
    _PLURALS = {"release": "releases", "release-group": "release-groups"}

    def __init__(
        self,
        contact: str = "",
        rate_limit: float = 1.05,
        album_entity: str = "release",
        timeout: float = 25.0,
    ) -> None:
        self.album_entity = album_entity if album_entity in ("release", "release-group") else "release"
        self.limiter = RateLimiter(rate_limit)
        self.timeout = timeout
        ua = f"MBIDCleaner/1.0 ( {contact} )" if contact else "MBIDCleaner/1.0 (local use)"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": ua,
                "Accept": "application/json",
            }
        )

    def search(self, kind: str, artist: str, title: str = "") -> list[dict[str, Any]]:
        """Search for an entity and return normalized candidate dictionaries."""
        if kind == "artist":
            entity, key, query = "artist", "artists", lucene_escape(artist)
        elif kind == "album":
            entity = self.album_entity
            key = self._PLURALS[entity]
            field = "release" if entity == "release" else "releasegroup"
            query = f"{field}:{lucene_escape(title)} AND artist:{lucene_escape(artist)}"
        elif kind == "track":
            entity, key = "recording", "recordings"
            query = f"recording:{lucene_escape(title)} AND artist:{lucene_escape(artist)}"
        else:
            raise ValueError(f"Unknown lookup kind: {kind}")

        payload = self._get(entity, {"query": query, "fmt": "json", "limit": 25})
        return [self._parse(kind, item) for item in payload.get(key, [])]

    def _get(self, entity: str, params: dict[str, str | int]) -> dict[str, Any]:
        url = self.BASE + entity
        last_error = "unknown error"
        for attempt in range(5):
            self.limiter.wait()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code in (429, 503):
                    last_error = f"HTTP {response.status_code} (rate limited)"
                    time.sleep(min(2**attempt, 30))
                    continue
                if response.status_code >= 400:
                    raise MBError(f"HTTP {response.status_code}: {response.text[:200]}")
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = str(exc)
                time.sleep(min(2**attempt, 15))
        raise MBError(f"MusicBrainz request failed after 5 attempts: {last_error}")

    @staticmethod
    def _parse(kind: str, item: dict[str, Any]) -> dict[str, Any]:
        name = item.get("name") or item.get("title") or ""
        credits: list[str] = []
        for credit in item.get("artist-credit") or []:
            if not isinstance(credit, dict):
                continue
            artist = credit.get("artist") or {}
            credit_name = credit.get("name") or artist.get("name") or ""
            if credit_name:
                credits.append(credit_name)
            artist_name = artist.get("name") or ""
            if artist_name and artist_name != credit_name:
                credits.append(artist_name)

        mbid = item.get("id", "")
        entity = {"artist": "artist", "album": "release", "track": "recording"}[kind]
        return {
            "mbid": mbid,
            "name": name,
            "artist": " / ".join(credits[:3]),
            "credit_names": credits,
            "score": item.get("score", 0),
            "url": f"https://musicbrainz.org/{entity}/{mbid}" if mbid else "",
        }
