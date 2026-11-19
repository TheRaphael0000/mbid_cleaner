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
    QFrame,
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
    QScrollArea,
    QStackedWidget,
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
from .sources import (
    SOURCE_MUSICBRAINZ,
    SOURCE_NAVIDROME,
    SOURCE_NAVIDROME_THEN_MUSICBRAINZ,
    SOURCE_LABELS,
)
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
    """Browse unresolved lookups: apply saved candidates or manually query MB."""

    def __init__(self, db: Database, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self._all_lookups: list = []
        self._current_row = None
        self._current_saved: list[dict] = []
        self._search_candidates: list[dict] = []
        self._worker: SearchWorker | None = None
        self.applied_count = 0

        self.setWindowTitle("Review unresolved candidates")
        self.resize(1200, 760)

        self.kind_filter = QComboBox()
        self.kind_filter.addItem("All kinds", None)
        self.kind_filter.addItem("Track", "track")
        self.kind_filter.addItem("Artist", "artist")
        self.kind_filter.addItem("Album", "album")
        self.kind_filter.currentIndexChanged.connect(self.reload)

        self.hide_zero = QCheckBox("Hide 0-candidate entries")
        self.hide_zero.setChecked(True)
        self.hide_zero.stateChanged.connect(self.reload)

        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")

        self.lookup_list = QListWidget()
        self.lookup_list.currentRowChanged.connect(self._on_lookup_selected)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)

        header = QHBoxLayout()
        header.addWidget(QLabel("Kind:"))
        header.addWidget(self.kind_filter)
        header.addWidget(self.hide_zero)
        header.addStretch(1)
        header.addWidget(self.refresh_button)

        left = QVBoxLayout()
        left.addWidget(QLabel("Unresolved lookups (queried, no exact match):"))
        left.addWidget(self.lookup_list, 1)

        # Right side: saved candidates page + manual search page
        self.right_title = QLabel("Select a lookup")
        self.right_title.setStyleSheet("font-weight: bold;")

        # --- Saved candidates page ---
        self.saved_page = QWidget()
        saved_layout = QVBoxLayout(self.saved_page)
        saved_layout.setContentsMargins(0, 0, 0, 0)
        self.saved_area = QScrollArea()
        self.saved_area.setWidgetResizable(True)
        self.saved_container = QWidget()
        self.saved_list_layout = QVBoxLayout(self.saved_container)
        self.saved_list_layout.setContentsMargins(0, 0, 0, 0)
        self.saved_list_layout.setAlignment(Qt.AlignTop)
        self.saved_area.setWidget(self.saved_container)
        saved_layout.addWidget(self.saved_area, 1)

        # --- Manual search page (for 0-candidate lookups) ---
        self.manual_page = QWidget()
        manual_layout = QVBoxLayout(self.manual_page)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.search_artist = QLineEdit()
        self.search_artist.setPlaceholderText("Artist")
        self.search_title = QLineEdit()
        self.search_title.setPlaceholderText("Title / name")
        self.search_title_label = QLabel("Title:")
        form.addRow("Artist:", self.search_artist)
        form.addRow(self.search_title_label, self.search_title)
        manual_layout.addLayout(form)

        search_row = QHBoxLayout()
        self.search_button = QPushButton("Search MusicBrainz")
        self.search_button.clicked.connect(self.on_manual_search)
        self.search_status = QLabel("Edit fields and search.")
        self.search_status.setStyleSheet("color: gray;")
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.search_status, 1)
        manual_layout.addLayout(search_row)

        self.search_area = QScrollArea()
        self.search_area.setWidgetResizable(True)
        self.search_container = QWidget()
        self.search_list_layout = QVBoxLayout(self.search_container)
        self.search_list_layout.setContentsMargins(0, 0, 0, 0)
        self.search_list_layout.setAlignment(Qt.AlignTop)
        self.search_area.setWidget(self.search_container)
        manual_layout.addWidget(self.search_area, 1)

        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.saved_page)   # 0
        self.right_stack.addWidget(self.manual_page)  # 1

        right = QVBoxLayout()
        right.addWidget(self.right_title)
        right.addWidget(self.right_stack, 1)

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

    # ------------------------------------------------------------------ data/load

    def reload(self) -> None:
        current_key = None
        if self._current_row is not None:
            current_key = (self._current_row["kind"], self._current_row["key"])

        kind = self.kind_filter.currentData()
        self._all_lookups = self.db.unresolved_lookups(kind)
        self.lookup_list.clear()

        select_row = -1
        for row in self._all_lookups:
            try:
                candidates = json.loads(row["candidates_json"] or "[]")
            except Exception:
                candidates = []
            count = len(candidates)
            if self.hide_zero.isChecked() and count == 0:
                continue
            label = self._lookup_label(row)
            item = QListWidgetItem(
                f"[{row['kind']}] {label}\n"
                f"affects {row['affected_count']:,} scrobble(s), {count} candidate(s)"
            )
            item.setData(Qt.UserRole, (row, candidates))
            self.lookup_list.addItem(item)
            if current_key == (row["kind"], row["key"]):
                select_row = self.lookup_list.count() - 1

        visible = self.lookup_list.count()
        total = len(self._all_lookups)
        if total == 0:
            self.status.setText("No unresolved lookups.")
        elif visible == 0:
            self.status.setText(
                f"0 shown ({total:,} hidden by filter; uncheck 'Hide 0-candidate entries' to see them)."
            )
        else:
            hidden = total - visible
            extra = f", {hidden:,} hidden" if hidden else ""
            self.status.setText(f"{visible:,} unresolved lookup(s) shown{extra}.")

        if visible > 0:
            self.lookup_list.setCurrentRow(select_row if select_row >= 0 else 0)
        else:
            self._show_none_selected()

    @staticmethod
    def _lookup_label(row) -> str:
        if row["kind"] == "artist":
            return row["label_artist"] or row["key"]
        artist = row["label_artist"] or ""
        title = row["label_title"] or ""
        return f"{artist} — {title}".strip(" —") or row["key"]

    def _show_none_selected(self) -> None:
        self._current_row = None
        self._current_saved = []
        self._search_candidates = []
        self.right_title.setText("Select a lookup")
        self._clear_layout(self.saved_list_layout)
        self._clear_layout(self.search_list_layout)
        self.right_stack.setCurrentIndex(0)

    def _on_lookup_selected(self, index: int) -> None:
        if index < 0:
            self._show_none_selected()
            return
        item = self.lookup_list.item(index)
        if item is None:
            self._show_none_selected()
            return
        row, candidates = item.data(Qt.UserRole)
        self._current_row = row
        self._current_saved = candidates
        self._search_candidates = []
        self._clear_layout(self.saved_list_layout)
        self._clear_layout(self.search_list_layout)

        label = self._lookup_label(row)
        if candidates:
            self.right_title.setText(
                f"[{row['kind']}] {label} — {len(candidates)} saved candidate(s)"
            )
            self._populate_saved(candidates)
            self.right_stack.setCurrentIndex(0)
        else:
            self.right_title.setText(
                f"[{row['kind']}] {label} — no saved candidates (manual MusicBrainz search)"
            )
            self._prefill_manual(row)
            self.right_stack.setCurrentIndex(1)

    # --------------------------------------------------------------- saved page

    def _populate_saved(self, candidates: list[dict]) -> None:
        self._clear_layout(self.saved_list_layout)
        if not candidates:
            self.saved_list_layout.addWidget(QLabel("No saved candidates."))
            return
        for candidate in candidates:
            self.saved_list_layout.addWidget(
                self._candidate_row(candidate, candidates, source="saved")
            )

    # -------------------------------------------------------------- manual page

    def _prefill_manual(self, row) -> None:
        kind = row["kind"]
        self.search_artist.setText(row["label_artist"] or "")
        if kind == "artist":
            self.search_title.setText("")
            self.search_title.setEnabled(False)
            self.search_title_label.setText("Title: (not used)")
        else:
            self.search_title.setText(row["label_title"] or "")
            self.search_title.setEnabled(True)
            self.search_title_label.setText(
                "Track title:" if kind == "track" else "Album title:"
            )
        self.search_status.setText("Edit fields and search MusicBrainz.")
        self.search_button.setEnabled(True)

    def on_manual_search(self) -> None:
        if self._current_row is None:
            return
        if self._worker and self._worker.isRunning():
            return
        kind = self._current_row["kind"]
        artist = self.search_artist.text().strip()
        title = self.search_title.text().strip() if kind != "artist" else ""
        if kind == "artist" and not artist:
            self.search_status.setText("Enter an artist name.")
            return
        if kind != "artist" and (not artist or not title):
            self.search_status.setText("Enter both artist and title.")
            return

        client = MBClient(
            contact=self.settings.contact,
            rate_limit=self.settings.rate_limit,
            album_entity=self.settings.album_entity,
        )
        self._search_candidates = []
        self._clear_layout(self.search_list_layout)
        self.search_button.setEnabled(False)
        self.search_status.setText("Searching MusicBrainz (respecting rate limit)…")

        worker = SearchWorker(client, kind, artist, title, self)
        self._worker = worker
        worker.succeeded.connect(self._on_search_results)
        worker.failed.connect(self._on_search_failed)
        worker.finished.connect(lambda: self.search_button.setEnabled(True))
        worker.start()

    def _on_search_results(self, candidates: list[dict]) -> None:
        self._search_candidates = candidates
        self._clear_layout(self.search_list_layout)
        if not candidates:
            self.search_status.setText("No results found.")
            return
        self.search_status.setText(f"Found {len(candidates)} candidate(s).")
        for candidate in candidates:
            self.search_list_layout.addWidget(
                self._candidate_row(candidate, candidates, source="search")
            )

    def _on_search_failed(self, traceback_text: str) -> None:
        self.search_status.setText("Search failed (see details).")
        QMessageBox.critical(self, "Search failed", traceback_text[-1200:])

    # ------------------------------------------------------------ candidate rows

    def _candidate_row(
        self, candidate: dict, candidates: list[dict], source: str
    ) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        text = (
            f"<b>{candidate.get('name', '')}</b> — {candidate.get('artist', '')} "
            f"<span style='color:gray'>(score {candidate.get('score', '')})</span><br/>"
            f"<span style='color:gray'>{candidate.get('mbid', '')}</span>"
        )
        label = QLabel(text)
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        layout.addWidget(label, 1)

        open_btn = QPushButton("Open")
        open_btn.setToolTip("Open on musicbrainz.org")
        open_btn.clicked.connect(lambda _=False, c=candidate: self._open_candidate(c))
        layout.addWidget(open_btn)

        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(
            lambda _=False, c=candidate, cs=candidates: self._apply_candidate(c, cs)
        )
        layout.addWidget(apply_btn)
        return frame

    def _open_candidate(self, candidate: dict) -> None:
        url = candidate.get("url", "")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _apply_candidate(self, candidate: dict, candidates: list[dict]) -> None:
        if self._current_row is None:
            return
        mbid = candidate.get("mbid", "")
        if not mbid:
            QMessageBox.warning(self, "No MBID", "Selected candidate has no MBID.")
            return
        row = self._current_row
        affected = self.db.apply_mbid(row["kind"], row["key"], mbid, "manual", candidates)
        self.applied_count += 1
        self.status.setText(
            f"Applied {mbid} to {affected:,} scrobble(s). "
            f"({self.applied_count} applied this session)"
        )
        self.reload()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.wait(5000)
        super().closeEvent(event)



class ExportOptionsDialog(QDialog):
    """Advanced export: check which MBIDs a scrobble must have to be included."""

    def __init__(
        self, db: Database, settings: AppSettings, dataset_id: int, parent=None
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self.dataset_id = dataset_id
        self.total = db.count_exportable(dataset_id)
        self.setWindowTitle("Advanced export")
        self.setMinimumWidth(480)

        note = QLabel(
            "Check the MBID fields a scrobble must have to be exported. All "
            "checked fields must be present on the same scrobble; leave them "
            "all unchecked to export the whole dataset."
        )
        note.setWordWrap(True)

        self.require_track = QCheckBox(_LABELS["track"])
        self.require_artist = QCheckBox(_LABELS["artist"])
        self.require_album = QCheckBox(_LABELS["album"])
        boxes = (self.require_track, self.require_artist, self.require_album)
        for box, checked in zip(
            boxes,
            (
                settings.export_require_track,
                settings.export_require_artist,
                settings.export_require_album,
            ),
        ):
            box.setChecked(checked)
            box.stateChanged.connect(lambda _state: self._update_preview())

        self.preview = QLabel()
        self.preview.setStyleSheet("padding: 6px 0;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Only export scrobbles with these MBIDs</b>"))
        layout.addWidget(note)
        layout.addSpacing(6)
        for box in boxes:
            layout.addWidget(box)
        layout.addWidget(self.preview)
        layout.addWidget(buttons)
        self._update_preview()

    def _update_preview(self) -> None:
        count = self.db.count_exportable(
            self.dataset_id,
            self.require_track.isChecked(),
            self.require_artist.isChecked(),
            self.require_album.isChecked(),
        )
        pct = (100.0 * count / self.total) if self.total else 0.0
        self.preview.setText(
            f"Scrobbles to export: <b>{count:,}</b> of {self.total:,} ({pct:.1f}%)"
        )

    def save(self) -> None:
        self.settings.export_require_track = self.require_track.isChecked()
        self.settings.export_require_artist = self.require_artist.isChecked()
        self.settings.export_require_album = self.require_album.isChecked()


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

        self.source_combo = QComboBox()
        for mode, label in SOURCE_LABELS.items():
            self.source_combo.addItem(label, mode)
        current_source = settings.resolve_source
        self.source_combo.setCurrentIndex(self.source_combo.findData(current_source))

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
        form.addRow(QLabel("<b>Resolution</b>"))
        form.addRow("Lookup source:", self.source_combo)
        form.addRow("Resolve kinds:", kinds_row)
        form.addRow(QLabel(""))
        form.addRow(QLabel("<b>MusicBrainz</b>"))
        form.addRow("Contact:", self.contact)
        form.addRow("Min. seconds per request:", self.rate)
        form.addRow("Album entity:", self.album_entity)
        form.addRow(QLabel(""))
        form.addRow(QLabel("<b>Navidrome (Subsonic API)</b>"))
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
        self.settings.resolve_source = self.source_combo.currentData()
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
        self.act_export = toolbar.addAction("Export JSON…", self.on_export)
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
        source = self.settings.resolve_source
        uses_mb = source in (SOURCE_MUSICBRAINZ, SOURCE_NAVIDROME_THEN_MUSICBRAINZ)
        uses_nd = source in (SOURCE_NAVIDROME, SOURCE_NAVIDROME_THEN_MUSICBRAINZ)

        if uses_mb and not self.settings.contact:
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
        client = (
            MBClient(
                contact=self.settings.contact,
                rate_limit=self.settings.rate_limit,
                album_entity=self.settings.album_entity,
            )
            if uses_mb
            else None
        )
        navidrome = self._make_navidrome_client() if uses_nd else None
        if uses_nd and navidrome is None:
            if source == SOURCE_NAVIDROME:
                QMessageBox.warning(
                    self,
                    "Navidrome not configured",
                    "Navidrome is the selected lookup source, but its URL/username "
                    "are missing or invalid. Configure it in Settings first.",
                )
                return
            QMessageBox.warning(
                self,
                "Navidrome not configured",
                "Navidrome is configured as the first lookup source, but its "
                "URL/username are missing or invalid. Continuing with MusicBrainz only.",
            )

        self._logged_progress_step = -1
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        kind_note = f"kinds: {', '.join(kinds)}"
        if source == SOURCE_NAVIDROME:
            mode = f"Navidrome only ({self.settings.navidrome_url}; {kind_note})"
        elif source == SOURCE_NAVIDROME_THEN_MUSICBRAINZ and navidrome is None:
            mode = f"MusicBrainz only (Navidrome unavailable; {kind_note})"
        elif source == SOURCE_NAVIDROME_THEN_MUSICBRAINZ:
            mode = (
                f"Navidrome first, then MusicBrainz ({self.settings.navidrome_url}; "
                f"{self.settings.rate_limit:.2f}s/MB request; {kind_note})"
            )
        else:
            mode = (
                f"MusicBrainz only ({self.settings.rate_limit:.2f}s/request, "
                f"album entity: {self.settings.album_entity}; {kind_note})"
            )
        self.log(f"Resolving with {mode} …")

        worker = ResolveWorker(
            self.db,
            client,
            navidrome=navidrome,
            source=source,
            kinds=kinds,
            parent=self,
        )
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
        dialog = UnresolvedCandidatesDialog(self.db, self.settings, self)
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
        options = ExportOptionsDialog(self.db, self.settings, self.dataset_id, self)
        if options.exec() != QDialog.Accepted:
            return
        require_track = options.require_track.isChecked()
        require_artist = options.require_artist.isChecked()
        require_album = options.require_album.isChecked()
        options.save()

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
        required = {
            "track": require_track,
            "artist": require_artist,
            "album": require_album,
        }
        if any(required.values()):
            labels = ", ".join(label for kind, label in _LABELS.items() if required[kind])
            self.log(f"Advanced export filter: {labels} must be present")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)

        worker = ExportWorker(
            self.db,
            path,
            self.dataset_id,
            require_track=require_track,
            require_artist=require_artist,
            require_album=require_album,
            parent=self,
        )
        self.worker = worker
        worker.progress.connect(self._on_generic_progress)
        worker.succeeded.connect(self._on_export_done)
        worker.failed.connect(self._on_worker_failed)
        worker.started.connect(lambda: self._enable_actions())
        worker.finished.connect(self._on_worker_finished)
        worker.start()

    def _on_export_done(self, result: dict) -> None:
        validation = result.get("validation", {})
        required = result.get("required", {})
        required_labels = [label for kind, label in _LABELS.items() if required.get(kind)]
        filter_note = f" [filter: {', '.join(required_labels)}]" if required_labels else ""
        self.log(
            f"Exported {result['tracks']:,} scrobbles / {validation.get('pages', 0):,} pages "
            f"to {result['path']}{filter_note} (schema check passed)"
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
