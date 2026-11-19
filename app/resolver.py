"""Resolution engine: dedupe → selected source(s) → apply → persist (resumable)."""

from __future__ import annotations

import time
from typing import Any, Callable

from .db import Database
from .hierarchy import HierarchyResolver
from .mb import MBClient, MBError, match_exact
from .navidrome import NavidromeClient, try_navidrome_match
from .sources import (
    SOURCE_MUSICBRAINZ,
    SOURCE_NAVIDROME,
    SOURCE_NAVIDROME_THEN_MUSICBRAINZ,
    SOURCES,
)
from .util import normalize


class Resolver:
    def __init__(
        self,
        db: Database,
        client: MBClient | None,
        navidrome: NavidromeClient | None = None,
        source: str = SOURCE_MUSICBRAINZ,
        hierarchical: bool = True,
        kinds: list[str] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        log: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> None:
        self.db = db
        self.client = client
        self.navidrome = navidrome
        self.source = source if source in SOURCES else SOURCE_MUSICBRAINZ
        self.hierarchy = (
            HierarchyResolver(db, client, log) if hierarchical else None
        )
        self.kinds = kinds
        self.progress = progress
        self.log = log
        self.should_stop = should_stop or (lambda: False)
        self.should_pause = should_pause or (lambda: False)
        self.processed = 0

    def _emit_log(self, message: str) -> None:
        if self.log:
            self.log(message)

    def _wait_while_paused(self) -> None:
        while self.should_pause() and not self.should_stop():
            time.sleep(0.15)

    def run(self) -> int:
        """Process pending lookups until the queue is empty or the user stops."""
        self.db.reset_stale(self.kinds)
        total = self.db.pending_count(self.kinds)

        while not self.should_stop():
            self._wait_while_paused()
            if self.should_stop():
                break
            batch = self.db.pending_lookups(200, self.kinds)
            if not batch:
                break

            for row in batch:
                if self.should_stop():
                    break
                if not self.db.lookup_is_pending(row["id"]):
                    continue  # filled by a derived MBID or a manual edit meanwhile
                self._wait_while_paused()
                if self.should_stop():
                    break
                self._process(row)
                self.processed += 1
                label = self._label(row)
                if self.progress:
                    self.progress(self.processed, total, label)

        return self.processed

    @staticmethod
    def _label(row: Any) -> str:
        kind = row["kind"]
        artist = row["label_artist"] or ""
        title = row["label_title"] or ""
        affected = int(row["affected_count"] or 0)
        if kind == "artist":
            base = artist or row["key"]
        else:
            base = f"{artist} — {title}".strip(" —") or row["key"]
        return f"{kind}: {base} (affects {affected:,})"

    def _process(self, row: Any) -> None:
        kind = row["kind"]
        artist = row["label_artist"] or ""
        title = row["label_title"] if kind != "artist" else ""
        label = self._label(row)
        attempt = int(row["attempts"] or 0) + 1
        self._emit_log(f"Querying [{attempt}] {label}")

        if self.source != SOURCE_MUSICBRAINZ:
            fallback = self.source == SOURCE_NAVIDROME_THEN_MUSICBRAINZ
            if self._try_navidrome(row, kind, artist, title, attempt, fallback):
                return
            if self.source == SOURCE_NAVIDROME:
                return  # handled in _try_navidrome; never query MusicBrainz

        self._query_musicbrainz(row, kind, artist, title, attempt)

    def _apply_hierarchy(self, kind: str, row: Any, match: dict, source: str) -> None:
        if self.hierarchy is None:
            return
        try:
            self.hierarchy.apply(kind, row["key"], match, source, row)
        except Exception as exc:  # hierarchical fill must never stall resolution
            self._emit_log(f"  -> Hierarchical fill error: {exc}")

    def _try_navidrome(
        self,
        row: Any,
        kind: str,
        artist: str,
        title: str,
        attempt: int,
        fallback: bool,
    ) -> bool:
        """Try Navidrome; return True when the lookup has been fully handled."""
        if self.navidrome is None:
            if fallback:
                return False
            self.db.mark_error(row["id"], "Navidrome is not configured")
            self._emit_log("  -> Navidrome: not configured")
            return True
        try:
            match, candidates = try_navidrome_match(self.navidrome, kind, artist, title)
        except Exception as exc:  # Navidrome errors must not abort the queue
            if fallback:
                self._emit_log(f"  -> Navidrome error, falling back to MusicBrainz: {exc}")
                return False
            self.db.mark_error(row["id"], f"Navidrome error: {exc}")
            status = "error (max attempts reached)" if attempt >= 5 else "error, will retry"
            self._emit_log(f"  -> Navidrome {status}: {exc}")
            return True
        if match and match.get("mbid"):
            self.db.apply_mbid(kind, row["key"], match["mbid"], "navidrome", candidates)
            self._emit_log(f"  -> Navidrome: found (exact): {match['mbid']}")
            self._apply_hierarchy(kind, row, match, "navidrome")
            return True
        if fallback:
            self._emit_log("  -> Navidrome: no exact match, falling back to MusicBrainz")
            return False
        self.db.mark_not_found(row["id"], candidates)
        self._emit_log(
            f"  -> Navidrome: no exact match (saved {min(len(candidates), 8)} candidate(s))"
        )
        return True

    def _query_musicbrainz(
        self,
        row: Any,
        kind: str,
        artist: str,
        title: str,
        attempt: int,
    ) -> None:
        if self.client is None:
            self.db.mark_error(row["id"], "MusicBrainz is not configured")
            self._emit_log("  -> MusicBrainz: not configured")
            return
        try:
            candidates = self.client.search(kind, artist, title)
        except MBError as exc:
            self.db.mark_error(row["id"], str(exc))
            status = "error (max attempts reached)" if attempt >= 5 else "error, will retry"
            self._emit_log(f"  -> MusicBrainz {status}: {exc}")
            return
        except Exception as exc:  # defensive: never stall the whole queue
            self.db.mark_error(row["id"], f"Unexpected error: {exc}")
            status = "error (max attempts reached)" if attempt >= 5 else "error, will retry"
            self._emit_log(f"  -> MusicBrainz {status}: {exc}")
            return

        match = match_exact(kind, candidates, normalize(artist), normalize(title))
        if match and match.get("mbid"):
            self.db.apply_mbid(kind, row["key"], match["mbid"], "musicbrainz", candidates)
            self._emit_log(f"  -> MusicBrainz: found (exact): {match['mbid']}")
            self._apply_hierarchy(kind, row, match, "musicbrainz")
        else:
            self.db.mark_not_found(row["id"], candidates)
            self._emit_log(
                f"  -> MusicBrainz: no exact match (saved {min(len(candidates), 8)} candidate(s))"
            )
