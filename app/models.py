"""Qt table model backed by paged SQL queries over the scrobbles table."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from .db import Database

HEADERS = [
    "Date",
    "Artist",
    "Track",
    "Album",
    "Track MBID",
    "Artist MBID",
    "Album MBID",
    "State",
]
PAGE_SIZE = 500


class ScrobbleModel(QAbstractTableModel):
    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.dataset_id = 0
        self._rows: list[Any] = []
        self._total = 0
        self._missing_only = False
        self._search = ""
        self._sort_col = 0
        self._descending = True

    # -------------------------------------------------------------- Qt plumbing

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal and 0 <= section < len(HEADERS):
            return HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == 0:
                return _format_date(row["uts"])
            if col == 1:
                return row["artist_name"]
            if col == 2:
                return row["track_name"]
            if col == 3:
                return row["album_name"]
            if col in (4, 5, 6):
                return row[("track_mbid", "artist_mbid", "album_mbid")[col - 4]]
            if col == 7:
                return _state(row)
        if role == Qt.ToolTipRole and col in (4, 5, 6):
            source = row[("source_track", "source_artist", "source_album")[col - 4]]
            return f"Source: {source}"
        if role == Qt.TextAlignmentRole and col == 0:
            return int(Qt.AlignVCenter | Qt.AlignRight)
        return None

    def sort(self, col: int, order=Qt.AscendingOrder) -> None:
        self._sort_col = col
        self._descending = order == Qt.DescendingOrder
        self.reload()

    def canFetchMore(self, parent=QModelIndex()) -> bool:
        return bool(self.dataset_id) and len(self._rows) < self._total

    def fetchMore(self, parent=QModelIndex()) -> None:
        if not self.canFetchMore():
            return
        rows, self._total = self._query(len(self._rows), PAGE_SIZE)
        if not rows:
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self._rows.extend(rows)
        self.endInsertRows()

    # -------------------------------------------------------------- model state

    def get_row(self, row: int) -> Any | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def set_dataset(self, dataset_id: int) -> None:
        self.dataset_id = int(dataset_id)
        self.reload()

    def set_filter(self, missing_only: bool, search: str) -> None:
        self._missing_only = missing_only
        self._search = search
        self.reload()

    def clear(self) -> None:
        self.dataset_id = 0
        self.reload()

    def reload(self) -> None:
        self.beginResetModel()
        if self.dataset_id:
            self._rows, self._total = self._query(0, PAGE_SIZE)
        else:
            self._rows, self._total = [], 0
        self.endResetModel()

    def _query(self, offset: int, limit: int):
        return self.db.fetch_scrobbles(
            self.dataset_id,
            offset,
            limit,
            missing_only=self._missing_only,
            search=self._search,
            sort_col=self._sort_col,
            descending=self._descending,
        )

    @property
    def total(self) -> int:
        return self._total


def _format_date(uts: str) -> str:
    try:
        return datetime.fromtimestamp(int(uts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return uts or ""


def _state(row: Any) -> str:
    missing = [
        name
        for name, value in (
            ("track", row["track_mbid"]),
            ("artist", row["artist_mbid"]),
            ("album", row["album_mbid"]),
        )
        if not value
    ]
    sources = {row["source_track"], row["source_artist"], row["source_album"]}
    if missing:
        text = "Missing: " + ", ".join(missing)
    else:
        text = "Complete"
    if "manual" in sources:
        text += " · manual"
    elif "navidrome" in sources:
        text += " · Navidrome"
    elif "musicbrainz" in sources:
        text += " · MB"
    return text
