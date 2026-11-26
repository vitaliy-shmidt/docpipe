"""PDF names <-> JSON text for the forms contract (inspect and fill).

A PDF name is a byte sequence (ISO 32000-1, 7.3.5), not text. Appearance-state
names of checkboxes and radio buttons (the keys of `/AP /N`, used by inspect as
`options[].value` / `export_value` and by fill to set `/AS`) are often plain
ASCII or UTF-8, but real-world forms also contain single-byte encodings, e.g.
`/Wei#DF` (0xDF = "ß" in Latin-1/Windows-1252). pikepdf hands such keys out as
`str` with lone surrogates ("\\udcdf"), which cannot be encoded as UTF-8 JSON
and cannot be turned back into a `pikepdf.Name`.

Presentation (deterministic and reversible, i.e. distinct names never share a
value):

- a name that is valid UTF-8 and contains no `#` followed by two hex digits is
  returned unchanged (as before - umlauts, spaces and a lone `#` included);
- every other name is returned in escaped form: each byte that is not part of
  valid UTF-8 and every `#` is written as `#XX` (two upper-case hex digits,
  the PDF name escape syntax), all other characters stay as they are.
  Example: bytes `Wei\\xdf` -> "Wei#DF"; a literal name `Wei#DF` -> "Wei#23DF".

An escaped value always contains `#XX` and an unchanged value never does, so
`name_bytes_from_text` can always tell which form it was given and recovers the
exact original bytes.
"""

from __future__ import annotations

import re

import pikepdf

_HEX_ESCAPE = re.compile(r"#[0-9A-Fa-f]{2}")
_HEX_ESCAPE_BYTES = re.compile(rb"#([0-9A-Fa-f]{2})")


def name_bytes(value) -> bytes:
    """The raw bytes of a PDF name, without the leading slash.

    Accepts a dictionary key as pikepdf returns it (`str`, invalid bytes as
    surrogate escapes) or a `pikepdf.Name` object (whose `str()` raises for
    such names).
    """
    if isinstance(value, str):
        raw = value.encode("utf-8", "surrogateescape")
    else:
        raw = _HEX_ESCAPE_BYTES.sub(lambda m: bytes([int(m.group(1), 16)]), value.unparse())
    return raw[1:] if raw.startswith(b"/") else raw


def name_text(value) -> str:
    """JSON-safe, reversible text of a PDF name (see module docstring)."""
    raw = name_bytes(value)
    text = raw.decode("utf-8", "surrogateescape")
    if not _needs_escaping(text):
        return text
    out = []
    for char in text:
        code = ord(char)
        if char == "#":
            out.append("#23")
        elif 0xDC80 <= code <= 0xDCFF:  # surrogate escape of a byte that is not valid UTF-8
            out.append(f"#{code - 0xDC00:02X}")
        else:
            out.append(char)
    return "".join(out)


def name_label(value) -> str:
    """Human-readable label for a name without its own label (display only, never an identity).

    Escaped names are decoded as Windows-1252 when that is possible (`Wei#DF` -> "Weiß"),
    otherwise the reversible text is used.
    """
    raw = name_bytes(value)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        return name_text(value)


def name_bytes_from_text(text: str) -> bytes | None:
    """Inverse of `name_text`; None if `text` is not a value `name_text` can produce."""
    if _HEX_ESCAPE.search(text) is None:
        if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
            return None
        return text.encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(text):
        char = text[i]
        if char == "#":
            pair = text[i + 1:i + 3]
            if len(pair) != 2 or not all(c in "0123456789ABCDEF" for c in pair):
                return None  # escaped form: every '#' starts an upper-case #XX escape
            out.append(int(pair, 16))
            i += 3
            continue
        if 0xD800 <= ord(char) <= 0xDFFF:
            return None
        out += char.encode("utf-8")
        i += 1
    raw = bytes(out)
    return raw if name_text("/" + raw.decode("utf-8", "surrogateescape")) == text else None


def make_name(raw: bytes) -> pikepdf.Name:
    """A `pikepdf.Name` for arbitrary bytes (`pikepdf.Name()` only accepts valid text)."""
    return pikepdf.Object.parse(b"/" + b"".join(b"#%02X" % b for b in raw))


def _needs_escaping(text: str) -> bool:
    return _HEX_ESCAPE.search(text) is not None or any(0xDC80 <= ord(c) <= 0xDCFF for c in text)


def json_safe(value, replaced: list[int] | None = None):
    """Copy of a JSON-ready structure in which every string is encodable as UTF-8.

    Names are made safe by `name_text`; this is the last line of defence for
    text (field names, labels, page text) that might still carry a lone
    surrogate. Such characters are replaced by U+FFFD and counted in
    `replaced[0]` so the caller can report it - never silently for names.
    """
    if replaced is None:
        replaced = [0]
    if isinstance(value, str):
        bad = sum(1 for c in value if 0xD800 <= ord(c) <= 0xDFFF)
        if not bad:
            return value
        replaced[0] += bad
        return "".join("�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in value)
    if isinstance(value, dict):
        return {json_safe(k, replaced): json_safe(v, replaced) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v, replaced) for v in value]
    if isinstance(value, tuple):
        return tuple(json_safe(v, replaced) for v in value)
    return value
