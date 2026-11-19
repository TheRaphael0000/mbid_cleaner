"""Hierarchical MBID filling: derive related MBIDs from an exact match.

A matched recording can yield the artist MBID and (when its release title
matches the scrobbled album) the album MBID. A matched album can yield the
artist MBID and, via one extra MusicBrainz lookup, its recordings' MBIDs.
Navidrome results contribute parent MBIDs only when they are already tagged;
no extra Subsonic requests are made.
"""

from __future__ import annotations

from typing import Any, Callable

from .db import Database
from .mb import MBClient, match_exact
from .util import normalize


def _norm_set(values: Any) -> set[str]:
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {normalize(str(v)) for v in values if v}


class HierarchyResolver:
    def __init__(
        self,
        db: Database,
        mb_client: MBClient | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.db = db
        self.mb = mb_client
        self.log = log
        self._recordings: dict[str, list[dict[str, Any]]] = {}

    def _emit(self, message: str) -> None:
        if self.log:
            self.log(message)

    def apply(
        self,
        kind: str,
        key: str,
        candidate: dict[str, Any],
        source: str,
        row: Any,
    ) -> int:
        """Apply all safely derivable MBIDs from ``candidate``; return the count."""
        related = self.db.related_keys(kind, key)
        tag = f"{source}-derived"
        applied = 0

        if kind != "artist":
            artist_mbid = str(candidate.get("artist_mbid") or "")
            artist_names = _norm_set(candidate.get("artist_names"))
            artist_names |= _norm_set(candidate.get("credit_names"))
            if candidate.get("artist"):
                artist_names.add(normalize(str(candidate["artist"])))
            if artist_mbid and artist_names:
                for name, artist_key in related["artist"]:
                    if normalize(name) in artist_names:
                        applied += self._fill("artist", artist_key, artist_mbid, tag, name)
                        break

        if kind == "track":
            applied += self._albums_from_track(candidate, related["album"], tag)

        if kind == "album":
            applied += self._tracks_from_album(candidate, row, related["track"], tag)

        return applied

    def _albums_from_track(
        self,
        candidate: dict[str, Any],
        related_albums: list[tuple[str, str]],
        tag: str,
    ) -> int:
        applied = 0
        album_specs: list[tuple[str, str, str]] = []
        for album in candidate.get("albums") or []:
            if not isinstance(album, dict) or not album.get("mbid"):
                continue
            names = _norm_set([album.get("name"), album.get("release_group_name")])
            album_specs.append((str(album["mbid"]), "", names))
        # Some recording results carry only the release group.
        group_mbid = str(candidate.get("release_group_mbid") or "")
        if group_mbid:
            album_specs.append((group_mbid, "release group", _norm_set([candidate.get("release_group_name")])))

        for name, album_key in related_albums:
            name_key = normalize(name)
            for mbid, label, names in album_specs:
                if name_key in names:
                    applied += self._fill(
                        "album", album_key, mbid, tag, f"{name} ({label})" if label else name
                    )
                    break
        return applied

    def _tracks_from_album(
        self,
        candidate: dict[str, Any],
        row: Any,
        related_tracks: list[tuple[str, str]],
        tag: str,
    ) -> int:
        if self.mb is None or not related_tracks:
            return 0
        album_mbid = str(candidate.get("mbid") or "")
        if not album_mbid:
            return 0
        if album_mbid not in self._recordings:
            self._recordings[album_mbid] = self.mb.album_recordings(album_mbid)
        recordings = self._recordings[album_mbid]
        if not recordings:
            return 0
        artist = str(row["label_artist"] or "")
        artist_key = normalize(artist)
        applied = 0
        for name, track_key in related_tracks:
            match = match_exact("track", recordings, artist_key, normalize(name))
            if match and match.get("mbid"):
                applied += self._fill("track", track_key, str(match["mbid"]), tag, name)
        return applied

    def _fill(self, kind: str, key: str, mbid: str, tag: str, name: str) -> int:
        count = self.db.apply_derived_mbid(kind, key, mbid, tag)
        if count:
            self._emit(
                f"  -> Derived {kind} MBID {mbid} for '{name}' "
                f"({tag}, {count} scrobble(s))"
            )
        return count
