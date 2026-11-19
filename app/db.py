"""SQLite storage: import, query, edit and export Last.fm scrobbles."""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Callable

from .util import compact_json, file_sha256, iter_json_array, normalize

SEP = "\x1f"  # separator for composite lookup keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name   TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,
    user        TEXT NOT NULL DEFAULT '',
    page_count  INTEGER NOT NULL DEFAULT 0,
    track_count INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    attr_json  TEXT NOT NULL,
    UNIQUE(dataset_id, idx)
);

CREATE TABLE IF NOT EXISTS scrobble (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id       INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
    page_id          INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    pos              INTEGER NOT NULL,
    raw_json         TEXT NOT NULL,
    track_name       TEXT NOT NULL DEFAULT '',
    track_key        TEXT NOT NULL DEFAULT '',
    track_lookup_key TEXT NOT NULL DEFAULT '',
    artist_name      TEXT NOT NULL DEFAULT '',
    artist_key       TEXT NOT NULL DEFAULT '',
    album_name       TEXT NOT NULL DEFAULT '',
    album_key        TEXT NOT NULL DEFAULT '',
    album_lookup_key TEXT NOT NULL DEFAULT '',
    uts              TEXT NOT NULL DEFAULT '',
    track_mbid       TEXT NOT NULL DEFAULT '',
    artist_mbid      TEXT NOT NULL DEFAULT '',
    album_mbid       TEXT NOT NULL DEFAULT '',
    source_track     TEXT NOT NULL DEFAULT 'file',
    source_artist    TEXT NOT NULL DEFAULT 'file',
    source_album     TEXT NOT NULL DEFAULT 'file',
    UNIQUE(dataset_id, page_id, pos)
);

CREATE TABLE IF NOT EXISTS lookup (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK(kind IN ('track', 'artist', 'album')),
    key             TEXT NOT NULL,
    label_artist    TEXT NOT NULL DEFAULT '',
    label_title     TEXT NOT NULL DEFAULT '',
    affected_count  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    mbid            TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    queried_at      TEXT,
    UNIQUE(kind, key)
);

CREATE TABLE IF NOT EXISTS override (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scrobble_id INTEGER NOT NULL REFERENCES scrobble(id) ON DELETE CASCADE,
    field       TEXT NOT NULL CHECK(field IN ('track', 'artist', 'album')),
    mbid        TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE(scrobble_id, field)
);

CREATE INDEX IF NOT EXISTS idx_scrobble_ds_page ON scrobble(dataset_id, page_id, pos);
CREATE INDEX IF NOT EXISTS idx_scrobble_track_key ON scrobble(dataset_id, track_key);
CREATE INDEX IF NOT EXISTS idx_scrobble_artist_key ON scrobble(dataset_id, artist_key);
CREATE INDEX IF NOT EXISTS idx_scrobble_album_key ON scrobble(dataset_id, album_key);
CREATE INDEX IF NOT EXISTS idx_scrobble_track_lookup ON scrobble(track_lookup_key);
CREATE INDEX IF NOT EXISTS idx_scrobble_artist_lookup ON scrobble(artist_key);
CREATE INDEX IF NOT EXISTS idx_scrobble_album_lookup ON scrobble(album_lookup_key);
CREATE INDEX IF NOT EXISTS idx_lookup_status ON lookup(status, affected_count DESC);
"""

_FIELDS = {
    "track": ("track_mbid", "source_track", "track_lookup_key"),
    "artist": ("artist_mbid", "source_artist", "artist_key"),
    "album": ("album_mbid", "source_album", "album_lookup_key"),
}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class Database:
    """A small wrapper around a SQLite connection-per-operation design.

    Every worker thread opens its own connection, so the GUI thread never has
    to share a connection with the import / resolver / export workers.
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=60)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 60000")
        return con

    def initialize(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self.connect() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
            con.executescript(SCHEMA)

    # ---------------------------------------------------------------- datasets

    def datasets(self) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT id, file_name, user, page_count, track_count, imported_at "
                "FROM dataset ORDER BY id DESC"
            ).fetchall()

    # ------------------------------------------------------------------ import

    def import_file(
        self,
        path: str,
        progress: Callable[[int, int | None, str], None] | None = None,
    ) -> dict[str, Any]:
        """Import an export JSON file. Re-importing the same file is idempotent."""
        digest = file_sha256(path)
        with self.connect() as con:
            existing = con.execute(
                "SELECT * FROM dataset WHERE sha256 = ?", (digest,)
            ).fetchone()
        if existing is not None:
            return {
                "dataset_id": existing["id"],
                "reused": True,
                "track_count": existing["track_count"],
                "page_count": existing["page_count"],
                "file_name": existing["file_name"],
            }

        stream = iter_json_array(path)
        try:
            first = next(stream)
        except StopIteration as exc:
            raise ValueError("The selected file contains no pages") from exc
        pages = itertools.chain([first], stream)

        attr = first.get("@attr") or {}
        user = _text(attr.get("user"))
        try:
            total_pages = int(attr.get("totalPages") or 0)
        except (TypeError, ValueError):
            total_pages = 0

        now = datetime.now().isoformat(timespec="seconds")
        con = self.connect()
        try:
            con.execute("BEGIN")
            cur = con.execute(
                "INSERT INTO dataset(file_name, sha256, user, imported_at) VALUES(?,?,?,?)",
                (os.path.basename(path), digest, user, now),
            )
            dataset_id = int(cur.lastrowid)

            insert_track = (
                "INSERT INTO scrobble(dataset_id, page_id, pos, raw_json, track_name, "
                "track_key, track_lookup_key, artist_name, artist_key, album_name, "
                "album_key, album_lookup_key, uts, track_mbid, artist_mbid, album_mbid, "
                "source_track, source_artist, source_album) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'file', 'file', 'file')"
            )

            page_count = 0
            track_count = 0
            for page_idx, page in enumerate(pages, start=1):
                if not isinstance(page, dict):
                    raise ValueError(f"Page {page_idx} is not an object")
                page_attr = page.get("@attr") or {}
                cur = con.execute(
                    "INSERT INTO page(dataset_id, idx, attr_json) VALUES(?,?,?)",
                    (dataset_id, page_idx, compact_json(page_attr)),
                )
                page_id = int(cur.lastrowid)
                page_count += 1

                batch: list[tuple[Any, ...]] = []
                for pos, track in enumerate(page.get("track") or [], start=1):
                    if not isinstance(track, dict):
                        continue
                    artist = track.get("artist") or {}
                    album = track.get("album") or {}
                    if not isinstance(artist, dict):
                        artist = {}
                    if not isinstance(album, dict):
                        album = {}
                    date = track.get("date") or {}
                    if not isinstance(date, dict):
                        date = {}

                    name = _text(track.get("name"))
                    artist_name = _text(artist.get("#text"))
                    album_name = _text(album.get("#text"))
                    name_key = normalize(name)
                    artist_key = normalize(artist_name)
                    album_key = normalize(album_name)

                    batch.append(
                        (
                            dataset_id,
                            page_id,
                            pos,
                            compact_json(track),
                            name,
                            name_key,
                            artist_key + SEP + name_key,
                            artist_name,
                            artist_key,
                            album_name,
                            album_key,
                            artist_key + SEP + album_key,
                            _text(date.get("uts")),
                            _text(track.get("mbid")),
                            _text(artist.get("mbid")),
                            _text(album.get("mbid")),
                        )
                    )
                    track_count += 1
                    if len(batch) >= 2000:
                        con.executemany(insert_track, batch)
                        batch.clear()

                if batch:
                    con.executemany(insert_track, batch)
                if progress:
                    progress(track_count, None, f"Importing page {page_idx}...")

            self._seed_lookups(con, dataset_id)
            con.execute(
                "UPDATE dataset SET page_count=?, track_count=? WHERE id=?",
                (page_count, track_count, dataset_id),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

        return {
            "dataset_id": dataset_id,
            "reused": False,
            "track_count": track_count,
            "page_count": page_count,
            "file_name": os.path.basename(path),
        }

    @staticmethod
    def _seed_lookups(con: sqlite3.Connection, dataset_id: int) -> None:
        """Seed the global lookup cache from MBIDs already in the export."""
        specs = [
            ("track", "track_key", "track_mbid", "artist_name", "track_name", "track_lookup_key"),
            ("artist", "artist_key", "artist_mbid", "artist_name", "''", "artist_key"),
            ("album", "album_key", "album_mbid", "artist_name", "album_name", "album_lookup_key"),
        ]
        for kind, key_col, mbid_col, label_artist, label_title, lookup_col in specs:
            con.execute(
                f"""
                INSERT OR IGNORE INTO lookup
                    (kind, key, label_artist, label_title, affected_count,
                     status, mbid, source, queried_at)
                SELECT '{kind}', {key_col}, {label_artist}, {label_title}, 0,
                       'found', {mbid_col}, 'file', datetime('now')
                FROM scrobble
                WHERE dataset_id = ? AND {mbid_col} <> '' AND {key_col} <> ''
                GROUP BY {key_col}
                """,
                (dataset_id,),
            )
            con.execute(
                f"""
                INSERT OR IGNORE INTO lookup
                    (kind, key, label_artist, label_title, affected_count, status)
                SELECT '{kind}', {lookup_col}, {label_artist}, {label_title},
                       COUNT(*), 'pending'
                FROM scrobble
                WHERE dataset_id = ? AND ({mbid_col} = '' OR {mbid_col} IS NULL)
                      AND {key_col} <> ''
                GROUP BY {lookup_col}
                """,
                (dataset_id,),
            )

    # ------------------------------------------------------------------- stats

    def stats(self, dataset_id: int, kinds: list[str] | None = None) -> dict[str, Any]:
        if kinds is not None:
            kinds = [k for k in kinds if k in ("track", "artist", "album")]
        with self.connect() as con:
            row = con.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(track_mbid = ''), 0) AS miss_track,
                       COALESCE(SUM(artist_mbid = ''), 0) AS miss_artist,
                       COALESCE(SUM(album_mbid = ''), 0) AS miss_album,
                       COALESCE(SUM(track_mbid = '' AND artist_mbid = ''
                                    AND album_mbid = ''), 0) AS miss_all
                FROM scrobble WHERE dataset_id = ?
                """,
                (dataset_id,),
            ).fetchone()
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                lookup_rows = con.execute(
                    f"SELECT kind, status, COUNT(*) AS n FROM lookup "
                    f"WHERE kind IN ({placeholders}) GROUP BY kind, status",
                    kinds,
                ).fetchall()
            elif kinds == []:
                lookup_rows = []
            else:
                lookup_rows = con.execute(
                    "SELECT kind, status, COUNT(*) AS n FROM lookup GROUP BY kind, status"
                ).fetchall()
        lookups = {(r["kind"], r["status"]): r["n"] for r in lookup_rows}
        return {
            "total": row["total"],
            "miss_track": row["miss_track"],
            "miss_artist": row["miss_artist"],
            "miss_album": row["miss_album"],
            "miss_all": row["miss_all"],
            "lookups": lookups,
            "pending": sum(v for (k, s), v in lookups.items() if s == "pending"),
            "not_found": sum(v for (k, s), v in lookups.items() if s == "not_found"),
            "errors": sum(v for (k, s), v in lookups.items() if s == "error"),
            "found": sum(v for (k, s), v in lookups.items() if s == "found"),
        }

    # --------------------------------------------------------------- scrobbles

    _SORT = {
        0: "s.uts",
        1: "s.artist_name",
        2: "s.track_name",
        3: "s.album_name",
        4: "s.track_mbid",
        5: "s.artist_mbid",
        6: "s.album_mbid",
    }

    def fetch_scrobbles(
        self,
        dataset_id: int,
        offset: int,
        limit: int,
        missing_only: bool = False,
        search: str = "",
        sort_col: int = 0,
        descending: bool = True,
    ) -> tuple[list[sqlite3.Row], int]:
        where = ["s.dataset_id = ?"]
        params: list[Any] = [dataset_id]
        if missing_only:
            where.append("(s.track_mbid = '' OR s.artist_mbid = '' OR s.album_mbid = '')")
        if search.strip():
            like = f"%{search.strip()}%"
            where.append(
                "(s.artist_name LIKE ? OR s.track_name LIKE ? OR s.album_name LIKE ? "
                "OR s.track_mbid LIKE ? OR s.artist_mbid LIKE ? OR s.album_mbid LIKE ?)"
            )
            params.extend([like] * 6)
        where_sql = " AND ".join(where)
        order_col = self._SORT.get(sort_col, "s.uts")
        direction = "DESC" if descending else "ASC"

        with self.connect() as con:
            total = con.execute(
                f"SELECT COUNT(*) FROM scrobble s WHERE {where_sql}", params
            ).fetchone()[0]
            rows = con.execute(
                f"""
                SELECT s.id, s.uts, s.artist_name, s.track_name, s.album_name,
                       s.track_mbid, s.artist_mbid, s.album_mbid,
                       s.source_track, s.source_artist, s.source_album,
                       s.track_lookup_key, s.artist_key, s.album_lookup_key
                FROM scrobble s
                WHERE {where_sql}
                ORDER BY {order_col} {direction}, s.id {direction}
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return rows, total

    def get_scrobble(self, scrobble_id: int) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute("SELECT * FROM scrobble WHERE id = ?", (scrobble_id,)).fetchone()

    # --------------------------------------------------------------- lookups

    def get_lookup(self, kind: str, key: str) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM lookup WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()

    def pending_lookups(self, limit: int = 200, kinds: list[str] | None = None) -> list[sqlite3.Row]:
        if kinds is not None:
            kinds = [k for k in kinds if k in ("track", "artist", "album")]
            if not kinds:
                return []
        with self.connect() as con:
            params: list[Any] = []
            where = "status = 'pending'"
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                where += f" AND kind IN ({placeholders})"
                params.extend(kinds)
            params.append(limit)
            return con.execute(
                f"""
                SELECT id, kind, key, label_artist, label_title, affected_count, attempts
                FROM lookup WHERE {where}
                ORDER BY affected_count DESC, id ASC LIMIT ?
                """,
                params,
            ).fetchall()

    def reset_stale(self, kinds: list[str] | None = None) -> None:
        if kinds is not None:
            kinds = [k for k in kinds if k in ("track", "artist", "album")]
            if not kinds:
                return
        with self.connect() as con:
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                con.execute(
                    f"UPDATE lookup SET status = 'pending' "
                    f"WHERE status = 'querying' AND kind IN ({placeholders})",
                    kinds,
                )
            else:
                con.execute("UPDATE lookup SET status = 'pending' WHERE status = 'querying'")

    def reset_errors(self) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE lookup SET status='pending', attempts=0, last_error='' "
                "WHERE status='error'"
            )

    def retry_not_found(self) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE lookup SET status='pending', attempts=0 WHERE status='not_found'"
            )

    def pending_count(self, kinds: list[str] | None = None) -> int:
        if kinds is not None:
            kinds = [k for k in kinds if k in ("track", "artist", "album")]
            if not kinds:
                return 0
        with self.connect() as con:
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                row = con.execute(
                    f"SELECT COUNT(*) AS n FROM lookup WHERE status = 'pending' "
                    f"AND kind IN ({placeholders})",
                    kinds,
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT COUNT(*) AS n FROM lookup WHERE status = 'pending'"
                ).fetchone()
        return int(row["n"] if row else 0)

    def unresolved_lookups(self, kind: str | None = None) -> list[sqlite3.Row]:
        """Return lookups that were queried but not auto-matched (status='not_found')."""
        query = """
            SELECT kind, key, label_artist, label_title, affected_count, candidates_json
            FROM lookup
            WHERE status = 'not_found'
        """
        params: list[Any] = []
        if kind in ("track", "artist", "album"):
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY affected_count DESC, label_artist, label_title"
        with self.connect() as con:
            return con.execute(query, params).fetchall()

    def apply_mbid(
        self,
        kind: str,
        key: str,
        mbid: str,
        source: str,
        candidates: list[dict[str, Any]] | None = None,
    ) -> int:
        """Resolve a lookup key and fill every still-empty matching scrobble."""
        mbid_col, source_col, key_col = _FIELDS[kind]
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as con:
            con.execute(
                """
                UPDATE lookup
                SET mbid=?, status='found', source=?, candidates_json=?,
                    last_error='', queried_at=?
                WHERE kind=? AND key=?
                """,
                (mbid, source, compact_json(candidates or []), now, kind, key),
            )
            cur = con.execute(
                f"""
                UPDATE scrobble
                SET {mbid_col}=?, {source_col}=?
                WHERE {key_col}=? AND ({mbid_col}='' OR {mbid_col} IS NULL)
                """,
                (mbid, source, key),
            )
            return cur.rowcount

    def mark_not_found(
        self, lookup_id: int, candidates: list[dict[str, Any]]
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                UPDATE lookup SET status='not_found', mbid='',
                    candidates_json=?, queried_at=datetime('now'), last_error=''
                WHERE id=?
                """,
                (compact_json(candidates[:8]), lookup_id),
            )

    def mark_error(self, lookup_id: int, error: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as con:
            row = con.execute(
                "SELECT attempts FROM lookup WHERE id=?", (lookup_id,)
            ).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            status = "error" if attempts >= 5 else "pending"
            con.execute(
                """
                UPDATE lookup SET status=?, attempts=?, last_error=?, queried_at=?
                WHERE id=?
                """,
                (status, attempts, error[:500], now, lookup_id),
            )

    def set_manual(self, scrobble_id: int, field: str, mbid: str, note: str = "") -> int:
        """Manually set one MBID, record the override and apply it to identical rows."""
        if field not in _FIELDS:
            raise ValueError(f"Unknown field: {field}")
        row = self.get_scrobble(scrobble_id)
        if row is None:
            raise ValueError("Scrobble not found")
        mbid_col, _, key_col = _FIELDS[field]
        key = row[key_col]
        with self.connect() as con:
            con.execute(
                f"UPDATE scrobble SET {mbid_col}=?, source_{field}='manual' WHERE id=?",
                (mbid, scrobble_id),
            )
            con.execute(
                """
                INSERT INTO override(scrobble_id, field, mbid, note, created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(scrobble_id, field) DO UPDATE SET
                    mbid=excluded.mbid, note=excluded.note, created_at=excluded.created_at
                """,
                (scrobble_id, field, mbid, note, datetime.now().isoformat(timespec="seconds")),
            )
        if key:
            return self.apply_mbid(field, key, mbid, "manual")
        return 0

    def candidates_for_key(self, kind: str, key: str) -> list[dict[str, Any]]:
        row = self.get_lookup(kind, key)
        if row is None:
            return []
        try:
            return json.loads(row["candidates_json"] or "[]")
        except json.JSONDecodeError:
            return []

    # ------------------------------------------------------------------ export

    def export_file(
        self,
        path: str,
        dataset_id: int,
        progress: Callable[[int, int | None, str], None] | None = None,
    ) -> dict[str, Any]:
        """Write a JSON file with the exact original schema and filled MBIDs."""
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with self.connect() as con:
            total = con.execute(
                "SELECT COUNT(*) FROM scrobble WHERE dataset_id=?", (dataset_id,)
            ).fetchone()[0]
            cursor = con.execute(
                """
                SELECT p.idx, p.attr_json, s.raw_json, s.track_mbid,
                       s.artist_mbid, s.album_mbid
                FROM scrobble s JOIN page p ON p.id = s.page_id
                WHERE s.dataset_id = ?
                ORDER BY p.idx, s.pos
                """,
                (dataset_id,),
            )

            written = 0
            current_page: int | None = None
            page_attr = ""
            tracks: list[dict[str, Any]] = []
            first = True

            def flush() -> None:
                nonlocal first
                page_obj = {"track": tracks, "@attr": json.loads(page_attr)}
                if not first:
                    out.write(",")
                out.write(compact_json(page_obj))
                first = False

            with open(path, "w", encoding="utf-8", newline="\n") as out:
                out.write("[")
                for row in cursor:
                    if row["idx"] != current_page:
                        if current_page is not None:
                            flush()
                        current_page = row["idx"]
                        page_attr = row["attr_json"]
                        tracks = []
                    track = json.loads(row["raw_json"])
                    track["mbid"] = row["track_mbid"]
                    artist = track.get("artist")
                    if isinstance(artist, dict):
                        artist["mbid"] = row["artist_mbid"]
                    else:
                        track["artist"] = {"mbid": row["artist_mbid"], "#text": artist or ""}
                    album = track.get("album")
                    if isinstance(album, dict):
                        album["mbid"] = row["album_mbid"]
                    else:
                        track["album"] = {"mbid": row["album_mbid"], "#text": album or ""}
                    tracks.append(track)
                    written += 1
                    if progress and written % 5000 == 0:
                        progress(written, total, "Exporting JSON...")
                if current_page is not None:
                    flush()
                out.write("]")

        return {"path": path, "tracks": written}

    @staticmethod
    def validate_export(path: str) -> dict[str, Any]:
        """Lightweight structural validation of an exported file."""
        pages = 0
        tracks = 0
        required_track = {"album", "artist", "date", "image", "mbid", "name", "streamable", "url"}
        required_attr = {"page", "perPage", "total", "totalPages", "user"}
        for page in iter_json_array(path):
            pages += 1
            if not isinstance(page, dict) or "track" not in page or "@attr" not in page:
                raise ValueError(f"Page {pages} is missing 'track' or '@attr'")
            if not required_attr.issubset(page["@attr"] or {}):
                raise ValueError(f"Page {pages} has an incomplete '@attr'")
            for track in page["track"] or []:
                tracks += 1
                if not isinstance(track, dict) or not required_track.issubset(track):
                    raise ValueError(f"Track {tracks} on page {pages} has an invalid schema")
        return {"pages": pages, "tracks": tracks}
