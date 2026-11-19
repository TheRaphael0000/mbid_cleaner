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
        artist_names: list[str] = []
        artist_mbid = ""
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
            if artist_name and artist_name not in artist_names:
                artist_names.append(artist_name)
            if not artist_mbid and artist.get("id"):
                artist_mbid = str(artist["id"])

        # Recordings carry their releases; releases carry their release group.
        # Keep them so the resolver can derive album/artist MBIDs without extra
        # requests for track matches.
        albums: list[dict[str, str]] = []
        if kind == "track":
            for release in item.get("releases") or []:
                if not isinstance(release, dict) or not release.get("id"):
                    continue
                group = release.get("release-group") or {}
                albums.append(
                    {
                        "mbid": str(release["id"]),
                        "name": str(release.get("title") or ""),
                        "release_group_mbid": str(group.get("id") or ""),
                        "release_group_name": str(group.get("title") or ""),
                    }
                )
        group = item.get("release-group") or {}

        mbid = item.get("id", "")
        entity = {"artist": "artist", "album": "release", "track": "recording"}[kind]
        return {
            "mbid": mbid,
            "name": name,
            "artist": " / ".join(credits[:3]),
            "credit_names": credits,
            "artist_names": artist_names,
            "artist_mbid": artist_mbid,
            "albums": albums,
            "release_group_mbid": str(group.get("id") or ""),
            "release_group_name": str(group.get("title") or ""),
            "score": item.get("score", 0),
            "url": f"https://musicbrainz.org/{entity}/{mbid}" if mbid else "",
        }

    def album_recordings(self, album_mbid: str) -> list[dict[str, Any]]:
        """Fetch recordings of a matched album so track MBIDs can be derived.

        Uses an extra MusicBrainz lookup (still rate-limited). For a
        release-group, the first release is used as the representative source.
        """
        if not album_mbid:
            return []
        try:
            if self.album_entity == "release-group":
                body = self._get(f"release-group/{album_mbid}", {"fmt": "json", "inc": "releases"})
                releases = body.get("releases") or []
                album_mbid = str((releases[0] or {}).get("id") or "") if releases else ""
                if not album_mbid:
                    return []
            body = self._get(f"release/{album_mbid}", {"fmt": "json", "inc": "recordings"})
        except MBError:
            return []
        recordings: list[dict[str, Any]] = []
        for media in body.get("media") or []:
            for track in media.get("tracks") or []:
                recording = track.get("recording") or {}
                if recording.get("id"):
                    recordings.append(self._parse("track", recording))
        return recordings
