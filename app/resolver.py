"""Resolution engine: dedupe → MusicBrainz → apply → persist (resumable)."""

from __future__ import annotations

import time
from typing import Any, Callable

from .db import Database
from .mb import MBClient, MBError, match_exact
from .util import normalize


class Resolver:
    def __init__(
        self,
        db: Database,
        client: MBClient,
        progress: Callable[[int, int, str], None] | None = None,
        log: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> None:
        self.db = db
        self.client = client
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
        self.db.reset_stale()
        total = self.db.pending_count()

        while not self.should_stop():
            self._wait_while_paused()
            if self.should_stop():
                break
            batch = self.db.pending_lookups(200)
            if not batch:
                break

            for row in batch:
                if self.should_stop():
                    break
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

        try:
            candidates = self.client.search(kind, artist, title)
        except MBError as exc:
            self.db.mark_error(row["id"], str(exc))
            status = "error (max attempts reached)" if attempt >= 5 else "error, will retry"
            self._emit_log(f"  -> {status}: {exc}")
            return
        except Exception as exc:  # defensive: never stall the whole queue
            self.db.mark_error(row["id"], f"Unexpected error: {exc}")
            status = "error (max attempts reached)" if attempt >= 5 else "error, will retry"
            self._emit_log(f"  -> {status}: {exc}")
            return

        match = match_exact(kind, candidates, normalize(artist), normalize(title))
        if match and match.get("mbid"):
            self.db.apply_mbid(kind, row["key"], match["mbid"], "musicbrainz", candidates)
            self._emit_log(f"  -> found (exact): {match['mbid']}")
        else:
            self.db.mark_not_found(row["id"], candidates)
            self._emit_log(
                f"  -> no exact match (saved {min(len(candidates), 8)} candidate(s))"
            )
