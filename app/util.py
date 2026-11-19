"""Small helpers shared across the application."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterator

_WS = re.compile(r"\s+")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def normalize(value: str | None) -> str:
    """Normalize a name for exact-match comparison (NFKC + casefold + whitespace)."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return _WS.sub(" ", text).strip()


def is_uuid(value: str) -> bool:
    return bool(_UUID.match(value.strip()))


def file_sha256(path: str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_json_array(path: str, chunk_size: int = 1 << 20) -> Iterator[Any]:
    """Stream the items of a top-level JSON array without loading the whole file.

    The Last.fm exports are large (100+ MB), so a plain ``json.load`` would need
    a lot of RAM.  This uses ``JSONDecoder.raw_decode`` on a sliding buffer.
    """
    decoder = json.JSONDecoder()
    with open(path, "r", encoding="utf-8-sig") as fh:
        buf = ""
        pos = 0
        started = False
        eof = False
        while True:
            if not eof:
                chunk = fh.read(chunk_size)
                if chunk:
                    buf += chunk
                else:
                    eof = True

            while pos < len(buf) and buf[pos].isspace():
                pos += 1

            if pos >= len(buf):
                if eof:
                    if not started:
                        raise ValueError("Empty file: expected a JSON array")
                    return
                continue

            if not started:
                if buf[pos] != "[":
                    raise ValueError("Expected a top-level JSON array")
                pos += 1
                started = True
                continue

            if buf[pos] == "]":
                return
            if buf[pos] == ",":
                pos += 1
                continue

            try:
                item, end = decoder.raw_decode(buf, pos)
            except ValueError:
                if eof:
                    raise
                if pos > chunk_size:
                    buf = buf[pos:]
                    pos = 0
                continue

            yield item
            pos = end
            if pos > chunk_size:
                buf = buf[pos:]
                pos = 0


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
