"""Persistent application settings (stored by QSettings)."""

from __future__ import annotations

from PySide6.QtCore import QSettings

_ORG = "MBIDCleaner"
_APP = "MBIDCleaner"


class AppSettings:
    """Thin typed wrapper around QSettings."""

    def __init__(self) -> None:
        self._q = QSettings(_ORG, _APP)

    @property
    def contact(self) -> str:
        return str(self._q.value("musicbrainz/contact", "", type=str)).strip()

    @contact.setter
    def contact(self, value: str) -> None:
        self._q.setValue("musicbrainz/contact", value.strip())

    @property
    def rate_limit(self) -> float:
        try:
            return max(1.0, float(self._q.value("musicbrainz/rate_limit", 1.05)))
        except (TypeError, ValueError):
            return 1.05

    @rate_limit.setter
    def rate_limit(self, value: float) -> None:
        self._q.setValue("musicbrainz/rate_limit", max(1.0, float(value)))

    @property
    def album_entity(self) -> str:
        value = str(self._q.value("musicbrainz/album_entity", "release", type=str))
        return value if value in ("release", "release-group") else "release"

    @album_entity.setter
    def album_entity(self, value: str) -> None:
        self._q.setValue("musicbrainz/album_entity", value)

    @property
    def db_path(self) -> str:
        return str(self._q.value("paths/database", "mbid_cleaner.sqlite3", type=str))

    @db_path.setter
    def db_path(self, value: str) -> None:
        self._q.setValue("paths/database", value)

    @property
    def last_dataset(self) -> int:
        try:
            return int(self._q.value("state/last_dataset", 0))
        except (TypeError, ValueError):
            return 0

    @last_dataset.setter
    def last_dataset(self, value: int) -> None:
        self._q.setValue("state/last_dataset", int(value))

    @property
    def last_dir(self) -> str:
        return str(self._q.value("paths/last_dir", "", type=str))

    @last_dir.setter
    def last_dir(self, value: str) -> None:
        self._q.setValue("paths/last_dir", value)
