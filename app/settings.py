"""Persistent application settings (stored by QSettings)."""

from __future__ import annotations

from PySide6.QtCore import QSettings

from .sources import (
    SOURCE_MUSICBRAINZ,
    SOURCE_NAVIDROME_THEN_MUSICBRAINZ,
    SOURCES,
)

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

    def _bool(self, key: str, default: bool = False) -> bool:
        value = self._q.value(key, default)
        return str(value).lower() in ("1", "true", "yes") or value is True

    @property
    def export_require_track(self) -> bool:
        return self._bool("export/require_track")

    @export_require_track.setter
    def export_require_track(self, value: bool) -> None:
        self._q.setValue("export/require_track", bool(value))

    @property
    def export_require_artist(self) -> bool:
        return self._bool("export/require_artist")

    @export_require_artist.setter
    def export_require_artist(self, value: bool) -> None:
        self._q.setValue("export/require_artist", bool(value))

    @property
    def export_require_album(self) -> bool:
        return self._bool("export/require_album")

    @export_require_album.setter
    def export_require_album(self, value: bool) -> None:
        self._q.setValue("export/require_album", bool(value))

    @property
    def resolve_source(self) -> str:
        value = str(self._q.value("resolve/source", "", type=str)).strip()
        if value in SOURCES:
            return value
        # Migrate the old "prioritize Navidrome" checkbox.
        legacy = self._q.value("navidrome/enabled", False)
        if str(legacy).lower() in ("1", "true", "yes") or legacy is True:
            return SOURCE_NAVIDROME_THEN_MUSICBRAINZ
        return SOURCE_MUSICBRAINZ

    @resolve_source.setter
    def resolve_source(self, value: str) -> None:
        self._q.setValue(
            "resolve/source", value if value in SOURCES else SOURCE_MUSICBRAINZ
        )

    @property
    def hierarchical(self) -> bool:
        return self._bool("resolve/hierarchical", True)

    @hierarchical.setter
    def hierarchical(self, value: bool) -> None:
        self._q.setValue("resolve/hierarchical", bool(value))

    @property
    def navidrome_url(self) -> str:
        return str(self._q.value("navidrome/url", "", type=str)).strip()

    @navidrome_url.setter
    def navidrome_url(self, value: str) -> None:
        self._q.setValue("navidrome/url", value.strip())

    @property
    def navidrome_user(self) -> str:
        return str(self._q.value("navidrome/user", "", type=str)).strip()

    @navidrome_user.setter
    def navidrome_user(self, value: str) -> None:
        self._q.setValue("navidrome/user", value.strip())

    @property
    def navidrome_password(self) -> str:
        return str(self._q.value("navidrome/password", "", type=str))

    @navidrome_password.setter
    def navidrome_password(self, value: str) -> None:
        self._q.setValue("navidrome/password", value)

    @property
    def resolve_tracks(self) -> bool:
        value = self._q.value("resolve/tracks", True)
        return str(value).lower() in ("1", "true", "yes") or value is True

    @resolve_tracks.setter
    def resolve_tracks(self, value: bool) -> None:
        self._q.setValue("resolve/tracks", bool(value))

    @property
    def resolve_artists(self) -> bool:
        value = self._q.value("resolve/artists", True)
        return str(value).lower() in ("1", "true", "yes") or value is True

    @resolve_artists.setter
    def resolve_artists(self, value: bool) -> None:
        self._q.setValue("resolve/artists", bool(value))

    @property
    def resolve_albums(self) -> bool:
        value = self._q.value("resolve/albums", True)
        return str(value).lower() in ("1", "true", "yes") or value is True

    @resolve_albums.setter
    def resolve_albums(self, value: bool) -> None:
        self._q.setValue("resolve/albums", bool(value))

    def enabled_kinds(self) -> list[str]:
        kinds: list[str] = []
        if self.resolve_tracks:
            kinds.append("track")
        if self.resolve_albums:
            kinds.append("album")
        if self.resolve_artists:
            kinds.append("artist")
        return kinds
