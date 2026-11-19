"""PySide6 main window, dialogs and user interactions."""

from __future__ import annotations

import json
import os
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .db import Database
from .mb import MBClient
from .models import HEADERS, ScrobbleModel
from .navidrome import NavidromeClient
from .settings import AppSettings
from .util import is_uuid
from .workers import ExportWorker, ImportWorker, ResolveWorker, SearchWorker

_LABELS = {"track": "Track MBID", "artist": "Artist MBID", "album": "Album MBID"}
_KEYS = {
    "track": "track_lookup_key",
    "artist": "artist_key",
    "album": "album_lookup_key",
}


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"~{hours}h {minutes:02d}m"
    if minutes:
        return f"~{minutes}m {secs:02d}s"
    return f"~{secs}s"


class CandidateDialog(QDialog):
    """Pick an MBID from the candidates saved during a previous search."""

    def __init__(self, candidates: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review MusicBrainz candidates")
        self.resize(760, 460)
        self.list = QListWidget()
        for candidate in candidates:
            item = QListWidgetItem(
                f"{candidate.get('name', '')} — {candidate.get('artist', '')} "
                f"(score {candidate.get('score', '')})\n{candidate.get('mbid', '')}"
            )
            item.setData(Qt.UserRole, candidate)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(self._open_link)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        hint = QLabel("Double-click a candidate to open it on musicbrainz.org")
        hint.setStyleSheet("color: gray;")

        layout = QVBoxLayout(self)
        layout.addWidget(self.list)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _open_link(self, item: QListWidgetItem) -> None:
        url = item.data(Qt.UserRole).get("url", "")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def selected(self) -> dict | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None


class ManualSearchDialog(QDialog):
    """Manually query MusicBrainz and pick a match for a specific field."""

    def __init__(
        self,
        settings: AppSettings,
        kind: str,
        artist: str,
        title: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.kind = kind
        self._worker: SearchWorker | None = None
        self._candidates: list[dict] = []

        labels = {"track": "recording", "artist": "artist", "album": settings.album_entity}
        self.setWindowTitle(f"Search MusicBrainz ({kind}: {labels.get(kind, kind)})")
        self.resize(820, 560)

        self.artist_edit = QLineEdit(artist)
        self.artist_edit.setPlaceholderText("Artist name")
        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText("Title / name")
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.on_search)
        self.status = QLabel("Edit the fields and click Search.")
        self.status.setStyleSheet("color: gray;")

        form = QFormLayout()
        form.addRow("Artist:", self.artist_edit)
        if kind == "artist":
            self.title_edit.setVisible(False)
            title_label = QLabel("(not used for artist searches)")
            title_label.setStyleSheet("color: gray;")
            form.addRow("Title:", title_label)
        else:
            label = "Track title:" if kind == "track" else "Album title:"
            form.addRow(label, self.title_edit)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._open_link)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.Ok)
        self.apply_button.setText("Apply selected")
        self.apply_button.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.list.itemSelectionChanged.connect(
            lambda: self.apply_button.setEnabled(self.list.currentItem() is not None)
        )

        hint = QLabel("Results come from a live MusicBrainz query. Double-click to open on musicbrainz.org.")
        hint.setStyleSheet("color: gray;")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.status, 1)
        layout.addLayout(search_row)
        layout.addWidget(self.list, 1)
        layout.addWidget(hint)
        layout.addWidget(self.buttons)

    def on_search(self) -> None:
        artist = self.artist_edit.text().strip()
        title = self.title_edit.text().strip() if self.kind != "artist" else ""
        if self.kind == "artist" and not artist:
            self.status.setText("Enter an artist name.")
            return
        if self.kind != "artist" and (not artist or not title):
            self.status.setText("Enter both artist and title.")
            return
        if self._worker and self._worker.isRunning():
            return

        client = MBClient(
            contact=self.settings.contact,
            rate_limit=self.settings.rate_limit,
            album_entity=self.settings.album_entity,
        )
        self._candidates = []
        self.list.clear()
        self.search_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.status.setText("Searching MusicBrainz (respecting rate limit)…")

        worker = SearchWorker(client, self.kind, artist, title, self)
        self._worker = worker
        worker.succeeded.connect(self._on_results)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda: self.search_button.setEnabled(True))
        worker.start()

    def _on_results(self, candidates: list[dict]) -> None:
        self._candidates = candidates
        self.list.clear()
        for candidate in candidates:
            item = QListWidgetItem(
                f"{candidate.get('name', '')} — {candidate.get('artist', '')} "
                f"(score {candidate.get('score', '')})\n{candidate.get('mbid', '')}"
            )
            item.setData(Qt.UserRole, candidate)
            self.list.addItem(item)
        if candidates:
            self.list.setCurrentRow(0)
            self.status.setText(f"Found {len(candidates)} candidate(s).")
        else:
            self.status.setText("No results found.")

    def _on_failed(self, traceback_text: str) -> None:
        self.status.setText("Search failed (see details).")
        QMessageBox.critical(self, "Search failed", traceback_text[-1200:])

    def _open_link(self, item: QListWidgetItem) -> None:
        url = item.data(Qt.UserRole).get("url", "")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def selected(self) -> dict | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def candidates(self) -> list[dict]:
        return self._candidates

    def accept(self) -> None:
        if self.selected() is None:
            QMessageBox.information(self, "No selection", "Select a candidate to apply.")
            return
        super().accept()

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.wait(5000)
        super().closeEvent(event)



class UnresolvedCandidatesDialog(QDialog):
    """Browse lookups that had no exact match and apply a saved candidate."""

    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self._lookups: list = []
        self._candidates: list[dict] = []
        self.applied_count = 0

        self.setWindowTitle("Review unresolved candidates")
        self.resize(1100, 700)

        self.kind_filter = QComboBox()
        self.kind_filter.addItem("All kinds", None)
        self.kind_filter.addItem("Track", "track")
        self.kind_filter.addItem("Artist", "artist")
        self.kind_filter.addItem("Album", "album")
        self.kind_filter.currentIndexChanged.connect(self.reload)

        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")

        self.lookup_list = QListWidget()
        self.lookup_list.currentRowChanged.connect(self._on_lookup_selected)

        self.candidate_list = QListWidget()
        self.candidate_list.itemDoubleClicked.connect(self._open_link)
        self.candidate_list.itemSelectionChanged.connect(
            lambda: self.apply_button.setEnabled(
                self.candidate_list.currentItem() is not None
            )
        )

        self.apply_button = QPushButton("Apply selected candidate")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_selected)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)

        header = QHBoxLayout()
        header.addWidget(QLabel("Kind:"))
        header.addWidget(self.kind_filter)
        header.addStretch(1)
        header.addWidget(self.refresh_button)

        left = QVBoxLayout()
        left.addWidget(QLabel("Unresolved lookups (queried, no exact match):"))
        left.addWidget(self.lookup_list, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel("Saved candidates for selected lookup:"))
        right.addWidget(self.candidate_list, 1)
        hint = QLabel("Double-click a candidate to open it on musicbrainz.org")
        hint.setStyleSheet("color: gray;")
        right.addWidget(hint)
        right.addWidget(self.apply_button)

        lists = QHBoxLayout()
        lists.addLayout(left, 2)
        lists.addLayout(right, 3)

        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        footer.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addLayout(lists, 1)
        layout.addLayout(footer)

        self.reload()

    def reload(self) -> None:
        kind = self.kind_filter.currentData()
        self._lookups = self.db.unresolved_lookups(kind)
        self.lookup_list.clear()
        self.candidate_list.clear()
        self._candidates = []
        self.apply_button.setEnabled(False)

        for row in self._lookups:
            try:
                candidates = json.loads(row["candidates_json"] or "[]")
            except Exception:
                candidates = []
            count = len(candidates)
            if row["kind"] == "artist":
                label = row["label_artist"] or row["key"]
            else:
                artist = row["label_artist"] or ""
                title = row["label_title"] or ""
                label = f"{artist} — {title}".strip(" —") or row["key"]
            item = QListWidgetItem(
                f"[{row['kind']}] {label}\n"
                f"affects {row['affected_count']:,} scrobble(s), {count} candidate(s)"
            )
            item.setData(Qt.UserRole, (row, candidates))
            self.lookup_list.addItem(item)

        if self._lookups:
            self.status.setText(f"{len(self._lookups):,} unresolved lookup(s) loaded.")
        else:
            self.status.setText("No unresolved lookups.")

    def _on_lookup_selected(self, index: int) -> None:
        self.candidate_list.clear()
        self._candidates = []
        self.apply_button.setEnabled(False)
        if index < 0:
            return
        item = self.lookup_list.item(index)
        if item is None:
            return
        _, candidates = item.data(Qt.UserRole)
        self._candidates = candidates
        for candidate in candidates:
            citem = QListWidgetItem(
                f"{candidate.get('name', '')} — {candidate.get('artist', '')} "
                f"(score {candidate.get('score', '')})\n{candidate.get('mbid', '')}"
            )
            citem.setData(Qt.UserRole, candidate)
            self.candidate_list.addItem(citem)
        if candidates:
            self.candidate_list.setCurrentRow(0)

    def apply_selected(self) -> None:
        lookup_item = self.lookup_list.currentItem()
        cand_item = self.candidate_list.currentItem()
        if lookup_item is None or cand_item is None:
            return
        row, _ = lookup_item.data(Qt.UserRole)
        candidate = cand_item.data(Qt.UserRole)
        mbid = candidate.get("mbid", "")
        if not mbid:
            QMessageBox.warning(self, "No MBID", "Selected candidate has no MBID.")
            return
        affected = self.db.apply_mbid(
            row["kind"], row["key"], mbid, "manual", self._candidates
        )
        self.applied_count += 1
        self.status.setText(
            f"Applied {mbid} to {affected:,} scrobble(s). "
            f"({self.applied_count} applied this session)"
        )
        self.reload()

    def _open_link(self, item: QListWidgetItem) -> None:
        url = item.data(Qt.UserRole).get("url", "")
        if url:
            QDesktopServices.openUrl(QUrl(url))



class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)

        self.contact = QLineEdit(settings.contact)
        self.contact.setPlaceholderText("you@example.com (required by MusicBrainz)")
        self.rate = QDoubleSpinBox()
        self.rate.setRange(1.0, 10.0)
        self.rate.setSingleStep(0.05)
        self.rate.setDecimals(2)
        self.rate.setValue(settings.rate_limit)
        self.album_entity = QComboBox()
        self.album_entity.addItems(["release", "release-group"])
        self.album_entity.setCurrentText(settings.album_entity)

        self.navidrome_enabled = QCheckBox(
            "Prioritize Navidrome for MBID lookups (fall back to MusicBrainz if no exact match)"
        )
        self.navidrome_enabled.setChecked(settings.navidrome_enabled)
        self.navidrome_url = QLineEdit(settings.navidrome_url)
        self.navidrome_url.setPlaceholderText("http://localhost:4533")
        self.navidrome_user = QLineEdit(settings.navidrome_user)
        self.navidrome_user.setPlaceholderText("Username")
        self.navidrome_password = QLineEdit(settings.navidrome_password)
        self.navidrome_password.setPlaceholderText("Password")
        self.navidrome_password.setEchoMode(QLineEdit.Password)

        self.resolve_tracks = QCheckBox("Tracks")
        self.resolve_tracks.setChecked(settings.resolve_tracks)
        self.resolve_artists = QCheckBox("Artists")
        self.resolve_artists.setChecked(settings.resolve_artists)
        self.resolve_albums = QCheckBox("Albums")
        self.resolve_albums.setChecked(settings.resolve_albums)
        kinds_row = QHBoxLayout()
        kinds_row.addWidget(self.resolve_tracks)
        kinds_row.addWidget(self.resolve_artists)
        kinds_row.addWidget(self.resolve_albums)
        kinds_row.addStretch(1)

        form = QFormLayout()
        form.addRow(QLabel("<b>MusicBrainz</b>"))
        form.addRow("Contact:", self.contact)
        form.addRow("Min. seconds per request:", self.rate)
        form.addRow("Album entity:", self.album_entity)
        form.addRow("Resolve kinds:", kinds_row)
        form.addRow(QLabel(""))
        form.addRow(QLabel("<b>Navidrome (Subsonic API)</b>"))
        form.addRow("", self.navidrome_enabled)
        form.addRow("URL:", self.navidrome_url)
        form.addRow("Username:", self.navidrome_user)
        form.addRow("Password:", self.navidrome_password)
        form.addRow(QLabel(""))
        form.addRow("Database:", QLabel(settings.db_path))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def save(self) -> None:
        self.settings.contact = self.contact.text()
        self.settings.rate_limit = self.rate.value()
        self.settings.album_entity = self.album_entity.currentText()
        self.settings.navidrome_enabled = self.navidrome_enabled.isChecked()
        self.settings.navidrome_url = self.navidrome_url.text()
        self.settings.navidrome_user = self.navidrome_user.text()
        self.settings.navidrome_password = self.navidrome_password.text()
        self.settings.resolve_tracks = self.resolve_tracks.isChecked()
        self.settings.resolve_artists = self.resolve_artists.isChecked()
        self.settings.resolve_albums = self.resolve_albums.isChecked()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MBID Cleaner")
        self.resize(1500, 900)

        self.settings = AppSettings()
        self.db = Database(self.settings.db_path)
        self.db.initialize()
        self.dataset_id = 0
        self.worker = None
        self._logged_progress_step = -1

        self.model = ScrobbleModel(self.db, self)
        self._build_ui()

        datasets = self.db.datasets()
        if datasets:
            ids = {row["id"] for row in datasets}
            wanted = self.settings.last_dataset
            target = wanted if wanted in ids else datasets[0]["id"]
            self._load_dataset(int(target))
        else:
            self.log("No dataset imported yet. Click ‘Import JSON’ to begin.")
            self._enable_actions()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.act_import = toolbar.addAction("Import JSON", self.on_import)
        self.act_export = toolbar.addAction("Export JSON", self.on_export)
        toolbar.addSeparator()
        self.act_start = toolbar.addAction("Start resolving", self.on_start_resolve)
        self.act_pause = toolbar.addAction("Pause", self.on_pause)
        self.act_stop = toolbar.addAction("Stop", self.on_stop)
        toolbar.addSeparator()
        toolbar.addAction("Review unresolved", self.on_review_unresolved)
        toolbar.addAction("Retry errors", self.on_retry_errors)
        toolbar.addAction("Retry unresolved", self.on_retry_not_found)
        toolbar.addSeparator()
        toolbar.addAction("Settings", self.on_settings)
        toolbar.addAction("Refresh", self.refresh_stats)

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        stats = QHBoxLayout()
        self.lbl_total = QLabel("— scrobbles")
        self.lbl_missing = QLabel("— missing")
        self.lbl_queue = QLabel("— queue")
        self.lbl_eta = QLabel("—")
        for label in (self.lbl_total, self.lbl_missing, self.lbl_queue, self.lbl_eta):
            label.setStyleSheet("padding: 2px 10px;")
            stats.addWidget(label)
        stats.addStretch(1)
        root.addLayout(stats)

        filters = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter artist / track / album / MBID…")
        self.missing_box = QCheckBox("Missing only")
        clear_button = QPushButton("Clear filter")
        clear_button.clicked.connect(self.on_clear_filter)
        filters.addWidget(self.filter_edit, 1)
        filters.addWidget(self.missing_box)
        filters.addWidget(clear_button)
        root.addLayout(filters)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_context_menu)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 230)
        self.table.setColumnWidth(2, 260)
        self.table.setColumnWidth(3, 200)
        for col in (4, 5, 6):
            self.table.setColumnWidth(col, 270)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().sortIndicatorChanged.connect(
            lambda col, order: self.model.sort(col, order)
        )
        root.addWidget(self.table, 4)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(QFont("Consolas", 9))
        root.addWidget(self.log_view, 1)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self.apply_filter)
        self.filter_edit.textChanged.connect(lambda: self._filter_timer.start(250))
        self.missing_box.stateChanged.connect(self.apply_filter)

        self._enable_actions()
        self.statusBar().showMessage("Ready")

    def _enable_actions(self) -> None:
        has_data = bool(self.dataset_id)
        busy = bool(self.worker and self.worker.isRunning())
        self.act_import.setEnabled(not busy)
        self.act_export.setEnabled(has_data and not busy)
        self.act_start.setEnabled(has_data and not busy)
        self.act_pause.setEnabled(busy and isinstance(self.worker, ResolveWorker))
        self.act_stop.setEnabled(busy and isinstance(self.worker, ResolveWorker))

    # -------------------------------------------------------------------- logging

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {message}")
        scroll = self.log_view.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    # -------------------------------------------------------------------- datasets

    def _load_dataset(self, dataset_id: int) -> None:
        self.dataset_id = dataset_id
        self.settings.last_dataset = dataset_id
        self.model.set_dataset(dataset_id)
        self.refresh_stats()
        self._enable_actions()
        self.log(f"Loaded dataset #{dataset_id}")
        self.statusBar().showMessage(f"Dataset #{dataset_id} loaded")

    def refresh_stats(self) -> None:
        if not self.dataset_id:
            self.lbl_total.setText("— scrobbles")
            self.lbl_missing.setText("— missing")
            self.lbl_queue.setText("— queue")
            self.lbl_eta.setText("—")
            return
        kinds = self.settings.enabled_kinds()
        stats = self.db.stats(self.dataset_id, kinds=kinds)
        self.lbl_total.setText(f"<b>{stats['total']:,}</b> scrobbles")
        self.lbl_missing.setText(
            f"Missing — track <b>{stats['miss_track']:,}</b> · "
            f"artist <b>{stats['miss_artist']:,}</b> · "
            f"album <b>{stats['miss_album']:,}</b>"
        )
        kinds_label = ", ".join(kinds) if kinds else "none"
        self.lbl_queue.setText(
            f"Queue [{kinds_label}] <b>{stats['pending']:,}</b> · "
            f"unresolved <b>{stats['not_found']:,}</b> · "
            f"errors <b>{stats['errors']:,}</b> · cached <b>{stats['found']:,}</b>"
        )
        self.lbl_eta.setText("ETA " + _duration(stats["pending"] * self.settings.rate_limit))

    def apply_filter(self) -> None:
        if not self.dataset_id:
            return
        self.model.set_filter(self.missing_box.isChecked(), self.filter_edit.text())
        self.statusBar().showMessage(f"{self.model.total:,} matching scrobbles")

    def on_clear_filter(self) -> None:
        self.filter_edit.clear()
        self.missing_box.setChecked(False)
        self.apply_filter()

    # ---------------------------------------------------------------------- import

    def on_import(self) -> None:
        if self._busy():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Last.fm export",
            self.settings.last_dir or os.getcwd(),
            "Last.fm export (*.json);;All files (*.*)",
        )
        if not path:
            return
        self.settings.last_dir = os.path.dirname(path)
        self.log(f"Importing {path} …")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)

        worker = ImportWorker(self.db, path, self)
        self.worker = worker
        worker.progress.connect(self._on_generic_progress)
        worker.succeeded.connect(self._on_import_done)
        worker.failed.connect(self._on_worker_failed)
        worker.started.connect(lambda: self._enable_actions())
        worker.finished.connect(self._on_worker_finished)
        worker.start()

    def _on_import_done(self, result: dict) -> None:
        self.log(
            f"Imported {result['track_count']:,} scrobbles / {result['page_count']:,} pages "
            f"from {result['file_name']}"
            + (" (dataset already existed)" if result.get("reused") else "")
        )
        self._load_dataset(int(result["dataset_id"]))

    # -------------------------------------------------------------------- resolving

    def on_start_resolve(self) -> None:
        if self._busy():
            return
        if not self.dataset_id:
            QMessageBox.information(self, "No dataset", "Import a JSON export first.")
            return
        if not self.settings.contact:
            answer = QMessageBox.question(
                self,
                "MusicBrainz contact",
                "MusicBrainz asks every client to send a contact address. "
                "None is configured yet. Continue with a generic User-Agent?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.No:
                self.on_settings()
                return

        kinds = self.settings.enabled_kinds()
        if not kinds:
            QMessageBox.information(
                self,
                "No kinds selected",
                "Enable at least one of Tracks, Artists, or Albums in Settings.",
            )
            return
        client = MBClient(
            contact=self.settings.contact,
            rate_limit=self.settings.rate_limit,
            album_entity=self.settings.album_entity,
        )
        navidrome = self._make_navidrome_client()
        self._logged_progress_step = -1
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        mode = (
            f"{self.settings.rate_limit:.2f}s/request "
            f"(album entity: {self.settings.album_entity}, kinds: {', '.join(kinds)})"
        )
        if navidrome is not None:
            mode += f"; Navidrome prioritized ({self.settings.navidrome_url})"
        self.log(f"Resolving with {mode} …")

        worker = ResolveWorker(self.db, client, navidrome=navidrome, kinds=kinds, parent=self)
        self.worker = worker
        worker.progress.connect(self._on_resolve_progress)
        worker.log.connect(self.log)
        worker.succeeded.connect(self._on_resolve_done)
        worker.failed.connect(self._on_worker_failed)
        worker.started.connect(lambda: self._enable_actions())
        worker.finished.connect(self._on_worker_finished)
        worker.start()

    def _on_resolve_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.statusBar().showMessage(f"[{done:,}/{max(total, 1):,}] {message}")
        # Refresh stats periodically to keep counters/ETA up to date without
        # hammering the database on every single query.
        if done == total or done % 25 == 0:
            self.refresh_stats()

    def _on_resolve_done(self, count: int) -> None:
        self.log(f"Resolve pass finished ({count:,} lookups processed).")
        self.refresh_stats()
        self.model.reload()
        self.statusBar().showMessage(f"Processed {count:,} lookups")

    def on_pause(self) -> None:
        worker = self.worker
        if isinstance(worker, ResolveWorker):
            if worker.paused:
                worker.resume()
                self.act_pause.setText("Pause")
                self.log("Resumed")
            else:
                worker.pause()
                self.act_pause.setText("Resume")
                self.log("Paused — current request will finish first")

    def on_stop(self) -> None:
        if isinstance(self.worker, ResolveWorker):
            self.worker.stop()
            self.act_stop.setEnabled(False)
            self.log("Stop requested — progress is saved and can be resumed")

    def on_retry_errors(self) -> None:
        self.db.reset_errors()
        self.refresh_stats()
        self.log("Error rows reset to pending")

    def on_retry_not_found(self) -> None:
        self.db.retry_not_found()
        self.refresh_stats()
        self.log("Unresolved rows reset to pending (they will be queried again)")

    def on_review_unresolved(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(
                self,
                "Busy",
                "Stop or wait for the current operation before reviewing unresolved candidates.",
            )
            return
        dialog = UnresolvedCandidatesDialog(self.db, self)
        dialog.exec()
        self.refresh_stats()
        self.model.reload()
        if dialog.applied_count:
            self.log(
                f"Applied {dialog.applied_count} candidate(s) from unresolved review."
            )

    # ---------------------------------------------------------------------- export

    def on_export(self) -> None:
        if self._busy():
            return
        if not self.dataset_id:
            return
        with self.db.connect() as con:
            row = con.execute(
                "SELECT file_name FROM dataset WHERE id=?", (self.dataset_id,)
            ).fetchone()
        original = row["file_name"] if row else "export.json"
        base, ext = os.path.splitext(original)
        suggested = f"{base}_with_mbid{ext or '.json'}"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export completed JSON",
            os.path.join(self.settings.last_dir or os.getcwd(), suggested),
            "JSON (*.json)",
        )
        if not path:
            return
        self.settings.last_dir = os.path.dirname(path)
        self.log(f"Exporting to {path} …")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)

        worker = ExportWorker(self.db, path, self.dataset_id, self)
        self.worker = worker
        worker.progress.connect(self._on_generic_progress)
        worker.succeeded.connect(self._on_export_done)
        worker.failed.connect(self._on_worker_failed)
        worker.started.connect(lambda: self._enable_actions())
        worker.finished.connect(self._on_worker_finished)
        worker.start()

    def _on_export_done(self, result: dict) -> None:
        validation = result.get("validation", {})
        self.log(
            f"Exported {result['tracks']:,} scrobbles / {validation.get('pages', 0):,} pages "
            f"to {result['path']} (schema check passed)"
        )
        self.statusBar().showMessage("Export complete")

    # ----------------------------------------------------------------------- edits

    def on_context_menu(self, position) -> None:
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        row = self.model.get_row(index.row())
        if row is None:
            return
        menu = QMenu(self)
        menu.addAction("Copy track name", lambda: self._copy(row["track_name"]))
        menu.addAction("Copy artist name", lambda: self._copy(row["artist_name"]))
        menu.addSeparator()
        for field, label in _LABELS.items():
            menu.addAction(f"Set {label} manually…", lambda f=field: self.set_manual(f))
        menu.addSeparator()
        for field, label in _LABELS.items():
            menu.addAction(f"Review saved candidates ({label})…", lambda f=field: self.review(f))
        menu.addAction("Search MusicBrainz for track…", lambda: self.search_manual("track"))
        menu.addAction("Search MusicBrainz for artist…", lambda: self.search_manual("artist"))
        menu.addAction("Search MusicBrainz for album…", lambda: self.search_manual("album"))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _copy(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)

    def set_manual(self, field: str) -> None:
        index = self.table.currentIndex()
        row = self.model.get_row(index.row()) if index.isValid() else None
        if row is None:
            return
        current = row[f"{field}_mbid"]
        text, ok = QInputDialog.getText(
            self, _LABELS[field], f"{_LABELS[field]} (UUID):", text=current
        )
        if not ok:
            return
        text = text.strip()
        if not text or not is_uuid(text):
            QMessageBox.warning(self, "Invalid MBID", "Enter a full MusicBrainz UUID.")
            return
        affected = self.db.set_manual(row["id"], field, text)
        self.model.reload()
        self.refresh_stats()
        self.log(f"Manually set {field} MBID {text} (applied to {affected:,} scrobbles)")

    def review(self, field: str) -> None:
        index = self.table.currentIndex()
        row = self.model.get_row(index.row()) if index.isValid() else None
        if row is None:
            return
        candidates = self.db.candidates_for_key(field, row[_KEYS[field]])
        if not candidates:
            QMessageBox.information(
                self,
                "No candidates",
                "No MusicBrainz candidates were saved for this entry. "
                "Use 'Search MusicBrainz' or set an MBID manually.",
            )
            return
        dialog = CandidateDialog(candidates, self)
        if dialog.exec() != QDialog.Accepted:
            return
        candidate = dialog.selected()
        if not candidate or not candidate.get("mbid"):
            return
        affected = self.db.apply_mbid(
            field, row[_KEYS[field]], candidate["mbid"], "manual", candidates
        )
        self.model.reload()
        self.refresh_stats()
        self.log(
            f"Applied saved candidate {candidate['mbid']} to {affected:,} scrobbles "
            f"({field})"
        )

    def search_manual(self, field: str) -> None:
        if self._busy():
            return
        index = self.table.currentIndex()
        row = self.model.get_row(index.row()) if index.isValid() else None
        if row is None:
            return
        artist = row["artist_name"]
        if field == "track":
            title = row["track_name"]
        elif field == "album":
            title = row["album_name"]
        else:
            title = ""
        dialog = ManualSearchDialog(self.settings, field, artist, title, self)
        if dialog.exec() != QDialog.Accepted:
            return
        candidate = dialog.selected()
        if not candidate or not candidate.get("mbid"):
            return
        affected = self.db.apply_mbid(
            field, row[_KEYS[field]], candidate["mbid"], "manual", dialog.candidates()
        )
        self.model.reload()
        self.refresh_stats()
        self.log(
            f"Applied manual search match {candidate['mbid']} to {affected:,} scrobbles "
            f"({field})"
        )

    # -------------------------------------------------------------------- settings

    def on_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            dialog.save()
            self.refresh_stats()
            self.log("Settings saved")

    def _make_navidrome_client(self) -> NavidromeClient | None:
        if not self.settings.navidrome_enabled:
            return None
        url = self.settings.navidrome_url
        user = self.settings.navidrome_user
        password = self.settings.navidrome_password
        if not url or not user:
            self.log("Navidrome enabled but URL/username missing; skipping Navidrome.")
            return None
        try:
            return NavidromeClient(url, user, password)
        except Exception as exc:
            self.log(f"Navidrome configuration error: {exc}")
            return None

    # --------------------------------------------------------------------- helpers

    def _busy(self) -> bool:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "Wait for the current operation to finish.")
            return True
        return False

    def _on_generic_progress(self, done, total, message: str) -> None:
        if total:
            self.progress.setRange(0, int(total))
            self.progress.setValue(int(done))
        else:
            self.progress.setRange(0, 0)

    def _on_worker_finished(self) -> None:
        self.progress.setVisible(False)
        self.worker = None
        self._enable_actions()

    def _on_worker_failed(self, traceback_text: str) -> None:
        self.log("ERROR:\n" + traceback_text)
        QMessageBox.critical(self, "Error", traceback_text[-1200:])

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            if isinstance(self.worker, ResolveWorker):
                self.worker.stop()
            self.worker.wait(5000)
        super().closeEvent(event)
