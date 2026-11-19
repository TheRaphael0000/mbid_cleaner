# MBID Cleaner

A small PySide6 desktop app that imports a Last.fm `recenttracks` JSON export into
SQLite, fills missing track / artist / album MBIDs via the MusicBrainz API, lets
you review or manually edit them, and exports the same JSON schema with the MBIDs
completed.

## Install

```powershell
python -m pip install -r requirements.txt
```

## Run

```powershell
python main.py
```

## Workflow

1. **Import JSON** — select `recenttracks-*.json`. Import streams the file (no
   huge memory spike) and is idempotent: re-importing the same file reuses the
   existing dataset. MBIDs already present in the export are used to seed the
   lookup cache, so identical scrobbles never need an API call.
2. **Settings** — optionally set a contact email (MusicBrainz requires clients to
   identify themselves), the request interval (min. 1.05 s by default), which
   kinds to resolve (**Tracks / Artists / Albums**), and whether album lookups
   use `release` (default, matches Last.fm) or `release-group`. You can also
   enable a **Navidrome (Subsonic API)** instance to be queried first for MBIDs.
3. **Start resolving** — the worker resolves each *unique* missing key (artist
   name, artist+track, artist+album), once ever, cached in SQLite. If Navidrome
   is enabled it is tried first; on an exact match the MBID is applied with
   source `navidrome`, otherwise the lookup falls back to MusicBrainz. Only
   exact normalized name + credited-artist matches are applied automatically.
   Progress is committed per lookup, so **Stop** / a crash / a restart can
   always resume. Per-query status is shown in the log; pause and ETA are shown
   in the toolbar/stats.
4. **Edit** — filter the table ("Missing only", text search), right-click a row
   to set an MBID manually, pick one of the candidates saved from a previous
   search, or run a **live MusicBrainz search** with custom artist/title and
   apply the chosen result. Use **Review unresolved** in the toolbar to browse
   all lookups that had no exact match and apply saved candidates in one place.
   A manual edit is applied to every identical scrobble.
5. **Export JSON** — writes a new file with the original page structure and all
   fields preserved, with MBIDs filled. The result is validated against the
   expected schema before the app reports success.

## Files

- `mbid_cleaner.sqlite3` — the database (created automatically next to `main.py`).
- `app/db.py` — schema, import/export, dedupe and edits.
- `app/mb.py` — rate-limited MusicBrainz client and exact-match logic.
- `app/resolver.py` — resumable query engine.
- `app/ui.py` — PySide6 GUI.

## Notes

- MusicBrainz allows about **1 request/second**; the full example export needs
  ~24k unique lookups (several hours). The cache/dedupe and resume behaviour are
  what make this practical — leave it running overnight if needed.
- Nothing is fuzzy-matched: if no exact match is found, the entry is marked
  "unresolved" with its candidates saved for manual review.
