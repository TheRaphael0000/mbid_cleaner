"""Lookup source modes shared by settings, UI and the resolver."""

SOURCE_MUSICBRAINZ = "musicbrainz"
SOURCE_NAVIDROME = "navidrome"
SOURCE_NAVIDROME_THEN_MUSICBRAINZ = "navidrome_then_musicbrainz"

SOURCES = (SOURCE_MUSICBRAINZ, SOURCE_NAVIDROME, SOURCE_NAVIDROME_THEN_MUSICBRAINZ)

SOURCE_LABELS = {
    SOURCE_MUSICBRAINZ: "MusicBrainz only",
    SOURCE_NAVIDROME: "Navidrome only",
    SOURCE_NAVIDROME_THEN_MUSICBRAINZ: "Navidrome first, then MusicBrainz",
}
