"""QThread workers so the GUI never blocks on import, API calls or export."""

from __future__ import annotations

import threading
import traceback
from typing import Any

from PySide6.QtCore import QThread, Signal

from .db import Database
from .mb import MBClient
from .resolver import Resolver


class ImportWorker(QThread):
    progress = Signal(int, object, str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, db: Database, path: str, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.path = path

    def run(self) -> None:
        try:
            result = self.db.import_file(self.path, self.progress.emit)
            self.succeeded.emit(result)
        except Exception:
            self.failed.emit(traceback.format_exc())


class ResolveWorker(QThread):
    progress = Signal(int, int, str)
    log = Signal(str)
    succeeded = Signal(int)
    failed = Signal(str)

    def __init__(self, db: Database, client: MBClient, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.client = client
        self._stop = threading.Event()
        self._pause = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def run(self) -> None:
        try:
            resolver = Resolver(
                self.db,
                self.client,
                progress=self.progress.emit,
                log=self.log.emit,
                should_stop=self._stop.is_set,
                should_pause=self._pause.is_set,
            )
            count = resolver.run()
            self.succeeded.emit(count)
        except Exception:
            self.failed.emit(traceback.format_exc())


class ExportWorker(QThread):
    progress = Signal(int, object, str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, db: Database, path: str, dataset_id: int, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.path = path
        self.dataset_id = dataset_id

    def run(self) -> None:
        try:
            result = self.db.export_file(self.path, self.dataset_id, self.progress.emit)
            result["validation"] = Database.validate_export(self.path)
            self.succeeded.emit(result)
        except Exception:
            self.failed.emit(traceback.format_exc())


class SearchWorker(QThread):
    """Run a single MusicBrainz search without blocking the UI."""

    succeeded = Signal(list)
    failed = Signal(str)

    def __init__(self, client: MBClient, kind: str, artist: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.client = client
        self.kind = kind
        self.artist = artist
        self.title = title

    def run(self) -> None:
        try:
            results = self.client.search(self.kind, self.artist, self.title)
            self.succeeded.emit(results)
        except Exception:
            self.failed.emit(traceback.format_exc())
