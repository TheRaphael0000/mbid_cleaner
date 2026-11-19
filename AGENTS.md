# AGENTS.md

Context and working notes for AI agents in this repository.

## Project summary

**MBID Cleaner** is a PySide6 desktop app that:
1. Imports a Last.fm `recenttracks` JSON export into a local SQLite database.
2. Fills missing MusicBrainz IDs (MBIDs) for **track, artist, and album** via the MusicBrainz web service.
3. Lets the user review/edit results and **export the same JSON schema** with MBIDs completed.

Entry point: `python main.py` (see `README.md`).

## Environment

- Windows, Python 3.13 (`python` on PATH). Node 24 / Go 1.26 / .NET 9 also present but unused.
- Dependencies: `PySide6>=6.8`, `requests>=2.32` (`requirements.txt`). Both are installed in the current environment.
- No git repo. No existing test suite beyond syntax checks.

## Repository layout

```
main.py                 # QApplication bootstrap
requirements.txt
README.md
AGENTS.md
app/
  __init__.py
  db.py                 # SQLite schema, streaming import/export, queries, edits
  mb.py                 # MusicBrainz client (rate limit, retries, parsing, exact match)
  navidrome.py          # Navidrome/Subsonic client for prioritized local MBID lookup
  resolver.py           # resumable dedupe/query/apply engine
  hierarchy.py          # derive artist/album/track MBIDs from exact matches
  sources.py            # lookup source modes (MusicBrainz / Navidrome / both)
  models.py             # QAbstractTableModel (paged, canFetchMore) over scrobbles
  workers.py            # QThread workers: ImportWorker, ResolveWorker, ExportWorker, SearchWorker
  ui.py                 # MainWindow, settings/candidate/review dialogs, actions
  settings.py           # QSettings wrapper (MB, Navidrome, paths)
  util.py               # normalize(), SHA-256, streaming JSON-array iterator, UUID check
recenttracks-theraphael0000-1794926052.json   # example export (~128.5 MiB, 1 line)
schema.json                                    # expected export schema
mbid_cleaner.sqlite3                           # created at runtime (default path)
```

## Data facts (example export)

- 837 pages, **167,358** scrobbles (single top-level JSON array of page objects).
- Each page: `{"track": [ ... ], "@attr": {"perPage","totalPages","page","user","total"}}`.
- Track fields: `artist{mbid,#text}`, `album{mbid,#text}`, `mbid`, `name`, `url`, `streamable`, `image[]`, `date{uts,#text}` (see `schema.json`).
- Missing MBIDs (scrobbles): track **60,549**, artist **54,275**, album **63,521**.
- Unique missing keys (after normalizing and reusing MBIDs already present in-file): track **13,771**, artist **3,146**, album **7,342** (~24k unique lookups total).
- Resolvable from MBIDs already inside the file: 8 track / 7,372 artist / 102 album scrobbles.

## Core design decisions

- **Import to SQLite, edit/export from DB.** Streaming import via `util.iter_json_array` (sliding-buffer `JSONDecoder.raw_decode`) to handle 100+ MB files without loading everything. Import is idempotent by file SHA-256 (`dataset.sha256 UNIQUE`).
- **Dedupe & cache:** global `lookup(kind, key)` table, one row per unique normalized key. A key already known (from file or prior query) is never queried again. Seeding on import copies existing in-file MBIDs into `lookup` and inserts pending rows for missing keys.
- **Resolution order:** (1) MBID already present for the same normalized key in the file/cache → apply offline; (2) query pending unique keys, most-frequent first, using the **Lookup source** selected in Settings (`app/sources.py`): `musicbrainz` (default), `navidrome` (Navidrome only — no MB requests), or `navidrome_then_musicbrainz` (Navidrome exact match first, MB on miss/error). Navidrome-only saves no-match rows as `not_found` with candidates and makes Navidrome errors retryable. Only kinds enabled in Settings (tracks/artists/albums) are queued and processed. Changing source does not reprocess already-`not_found` rows; use **Retry unresolved**.
- **MusicBrainz policy:** strict **1 request/second** (default 1.05s), `/ws/2/{recording,artist,release|release-group}` with `fmt=json&limit=25`, retries/backoff on 429/503/errors, proper `User-Agent` (configurable contact). Album entity defaults to `release` (toggleable to `release-group`).
- **Navidrome:** selectable lookup source (Settings: Lookup source + URL, username, password; old `navidrome/enabled` bool is migrated to `navidrome_then_musicbrainz`). Uses Subsonic token auth (`rest/search3.view`, `f=json`) and extracts `musicBrainzId`/`mbid` from artist/album/song results. Navidrome-only matching strips common variant markers (e.g. parenthesized/bracketed remaster/deluxe/live/version/edit… and trailing `feat./ft./featuring`) in search terms and comparison; only exact matches are auto-applied (source `navidrome`). In the combined mode, misses/errors fall back to MusicBrainz; in Navidrome-only mode they are saved as unresolved/retryable instead.
- **Matching:** **exact normalized name + credited-artist match only** (`util.normalize`: NFKC, casefold, whitespace collapse). No fuzzy/score-based auto-apply; ambiguous results are saved as candidates for manual review.
- **Hierarchical fill** (`app/hierarchy.py`, enabled by default via `resolve/hierarchical`): after an exact match, derive related MBIDs from data already in the result — track match → artist MBID + album MBID (only when a release/release-group title exactly matches the scrobbled album name); album match → artist MBID +, via `MBClient.album_recordings()` (one extra rate-limited request, cached per release), track MBIDs matched exactly against the scrobbled track names and credited artist. Navidrome results only contribute parent MBIDs when present as tags in the Subsonic response (no extra Subsonic calls). `db.apply_derived_mbid()` never overwrites an existing MBID (file/manual/earlier match), and derived rows are marked with source `<source>-derived`.
- **Resumability:** every lookup is committed before the next; `status='querying'` rows are reset to `pending` on startup; Stop/pause/crash can resume. Errors retry up to 5 attempts then become `error` (retryable from UI). Resolver logs each queried row and each source/query status.
- **Edits:** manual MBID entry (UUID-validated) and candidate picker; stored non-destructively in `override` and applied to all identical scrobbles sharing the key.
- **Export:** streams page-by-page from SQLite, rebuilds the exact original page schema (`@attr` + `track[]`, all original fields preserved via stored `raw_json`, MBIDs overwritten), writes to a new file, and runs a lightweight structural validation.

## SQLite schema (high level)

- `dataset(id, file_name, sha256, user, page_count, track_count, imported_at)`
- `page(id, dataset_id, idx, attr_json)` — preserves per-page `@attr`.
- `scrobble(...)` — per-track row with `raw_json`, normalized keys (`track_key/artist_key/album_key`, composite `track_lookup_key`/`album_lookup_key` as `artist\x1f...`), current MBIDs, `source_*`, `page_id`, `pos`, `uts`.
- `lookup(id, kind['track'|'artist'|'album'], key UNIQUE, status['pending'|'found'|'not_found'|'error'|'querying'], mbid, candidates_json, attempts, ...)` — dedupe/cache/resume state.
- `override(id, scrobble_id, field, mbid, note, created_at)` — manual edits.
- WAL mode, indexes on keys/status/dataset ordering.

## UI / behavior notes

- `ScrobbleModel` is a paged table model (`canFetchMore`/`fetchMore`, 500-row pages) with filter (text + "missing only") and sorting to stay responsive on 167k rows.
- All long work runs in `QThread` workers (`app/workers.py`) emitting progress to the UI; the resolver throttles globally at ~1 rps (MusicBrainz), so a full unresolved pass on the example file is on the order of hours but is fully resumable. Per-query log lines show the current row and each source/result status (including Navidrome-then-MusicBrainz when enabled).
- Stats strip shows totals, missing counts, queue/unresolved/error/cached counts and ETA. Right-click rows for manual set, review of saved candidates, or a live **Search MusicBrainz** query (prefilled, editable artist/title) whose selected result is applied to all identical scrobbles via `lookup`. Toolbar **Review unresolved** opens a window to browse `not_found` lookups (with saved candidates) and apply a candidate; its **Hide entries with no usable candidates** checkbox (on by default) hides lookups whose saved candidates all lack an MBID (and entries with no candidates), while mixed lists show only the MBID-bearing candidates and put MBID-less-only entries on the manual search page. The **Discard — no matching candidate** button marks the selected lookup `status='discarded'` (candidates kept, confirmed via dialog); it then leaves `unresolved_lookups()` and is not affected by **Retry unresolved**. **Show discarded** includes those rows and turns the button into **Restore to unresolved** (`db.restore_lookup()`), so discards are reversible. `stats()` exposes a `discarded` count shown in the queue strip when non-zero. **Export JSON…** opens the Advanced export dialog with checkboxes requiring track/artist/album MBIDs (all checked must be present; live count preview; choices remembered). Manual/live searches and review actions are separate from the resolver worker and should not be run concurrently with auto-resolving to respect the 1 req/s policy. Settings configure MusicBrainz, the lookup source (MusicBrainz only / Navidrome only / Navidrome first then MusicBrainz), hierarchical fill and the Navidrome connection.

## Commands

```powershell
python -m pip install -r requirements.txt
python main.py
python -m compileall app main.py   # syntax check
```

## Gotchas / future work

- `db.py` streaming import uses `INSERT OR IGNORE` semantics for lookups; scrobbles are inserted fresh per dataset import. A same-file re-import returns the existing dataset (no duplicate rows).
- Candidate review applies the chosen MBID to all scrobbles sharing the lookup key (by design) and marks source as `manual`.
- No automated tests yet; `schema.json` can be used to validate export structure. Be careful when changing export to keep field names and `@attr`/`track` layout identical to the source.
- MusicBrainz rate limits and ToS apply; keep requests at ~1/s and set a contact in Settings.
