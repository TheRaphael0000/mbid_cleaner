# MBID Cleaner

A small PySide6 desktop app that imports a Last.fm `recenttracks` JSON export into
SQLite, fills missing track / artist / album MBIDs from **MusicBrainz** and/or a
local **Navidrome** library, lets you review or manually edit them, and exports
the same JSON schema with the MBIDs completed.

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
2. **Settings** — choose a **Lookup source**:
   - **MusicBrainz only** (default) — no Navidrome requests.
   - **Navidrome only** — no MusicBrainz requests; needs a configured Subsonic
     URL, username and password.
   - **Navidrome first, then MusicBrainz** — try the local library, fall back to
     MusicBrainz when there is no exact match.

   Also set a contact email (MusicBrainz requires clients to identify themselves),
   the request interval (min. 1.05 s by default), which kinds to resolve
   (**Tracks / Artists / Albums**), and whether album lookups use `release`
   (default, matches Last.fm) or `release-group`. Navidrome matching uses relaxed,
   Navidrome-only cleanup (e.g. strips `(Remaster)`/deluxe/version tags and
   `feat./ft.` credits) for search and comparison; MusicBrainz matching remains
   strict. Existing "prioritize Navidrome" settings are migrated automatically.
3. **Start resolving** — the worker resolves each *unique* missing key (artist
   name, artist+track, artist+album), once ever, cached in SQLite. With
   **Navidrome only**, no exact match is recorded as unresolved (candidates
   saved) and Navidrome errors are retryable; MusicBrainz is never contacted.
   With **Navidrome first, then MusicBrainz**, a miss or Navidrome error falls
   back to MusicBrainz. Only exact normalized name + credited-artist matches are
   applied automatically.
   Progress is committed per lookup, so **Stop** / a crash / a restart can
   always resume. Per-query status is shown in the log; pause and ETA are shown
   in the toolbar/stats.
4. **Edit** — filter the table ("Missing only", text search), right-click a row
   to set an MBID manually, pick one of the candidates saved from a previous
   search, or run a **live MusicBrainz search** with custom artist/title and
   apply the chosen result. Use **Review unresolved** in the toolbar to browse
   all lookups that had no exact match and apply saved candidates in one place.
   A manual edit is applied to every identical scrobble.
5. **Export JSON…** — opens *Advanced export*, where checkboxes let you require
   track, artist and/or album MBIDs. Only scrobbles with **all** checked fields
   are written (leave all unchecked for the full dataset); a live count previews
   the selection and your choice is remembered. The new file keeps the original
   page structure and all fields with MBIDs filled, and is validated against the
   expected schema before the app reports success.

## Files

- `mbid_cleaner.sqlite3` — the database (created automatically next to `main.py`).
- `app/db.py` — schema, import/export, dedupe and edits.
- `app/mb.py` — rate-limited MusicBrainz client and exact-match logic.
- `app/resolver.py` — resumable query engine.
- `app/sources.py` — lookup source modes.
- `app/ui.py` — PySide6 GUI.

## Notes

- MusicBrainz allows about **1 request/second**; the full example export needs
  ~24k unique lookups (several hours). The cache/dedupe and resume behaviour are
  what make this practical — leave it running overnight if needed.
- Changing the lookup source does not automatically reprocess keys already
  marked unresolved; use **Retry unresolved** in the toolbar to queue them again.
- Nothing is fuzzy-matched: if no exact match is found, the entry is marked
  "unresolved" with its candidates saved for manual review.
