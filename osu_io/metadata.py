"""Reading and rewriting `Key:Value` lines in a document's passthrough sections.

The writer keeps every line it does not generate exactly as it was read, so
[Metadata], [General] and [Editor] edits are edits to `document.lines` itself:
a key already there is rewritten in place (its own line ending kept), a missing
one goes at the end of its section, and a missing section is added before
[Metadata] -- where osu! itself puts [Editor].
"""
from __future__ import annotations

# The beatmapset's own fields, the same in every difficulty. Version is the
# difficulty's name, and BeatmapID/BeatmapSetID belong to the website.
SET_KEYS = ("Title", "TitleUnicode", "Artist", "ArtistUnicode", "Creator", "Source", "Tags")
# osu!'s submission limit, spaces included (owner, 2026-10-18).
TAGS_LIMIT = 1000


def _ending(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""


def _header(line: str) -> str | None:
    stripped = line.strip()
    return stripped[1:-1] if stripped.startswith("[") and stripped.endswith("]") else None


def read_keys(lines: list[str], section: str) -> dict[str, str]:
    values: dict[str, str] = {}
    current = None
    for line in lines:
        name = _header(line)
        if name is not None:
            current = name
            continue
        if current == section and ":" in line:
            key, value = line.rstrip("\r\n").split(":", 1)
            values.setdefault(key.strip(), value.strip())
    return values


def with_keys(lines: list[str], section: str, values: dict[str, str], ending: str = "\r\n") -> list[str]:
    """A copy of `lines` with `values` written into `section`."""
    lines = list(lines)
    pending = dict(values)
    start = end = None
    current = None
    for index, line in enumerate(lines):
        name = _header(line)
        if name is not None:
            if current == section:
                end = index
            current = name
            if name == section:
                start = index
            continue
        if current == section and ":" in line:
            key = line.split(":", 1)[0].strip()
            if key in pending:
                lines[index] = f"{key}:{pending.pop(key)}{_ending(line) or ending}"
    if not pending:
        return lines
    if start is None:
        at = next((i for i, line in enumerate(lines) if _header(line) == "Metadata"), len(lines))
        block = [f"[{section}]{ending}", *(f"{k}:{v}{ending}" for k, v in pending.items()), ending]
        return lines[:at] + block + lines[at:]
    end = len(lines) if end is None else end
    # Before the blank lines separating this section from the next.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return lines[:end] + [f"{k}:{v}{ending}" for k, v in pending.items()] + lines[end:]


if __name__ == "__main__":
    sample = ["[General]\r\n", "PreviewTime: 100\r\n", "\r\n", "[Metadata]\r\n", "Title:a\r\n", "\r\n"]
    out = with_keys(sample, "Metadata", {"Title": "b", "Tags": "x y"})
    assert out[4] == "Title:b\r\n" and out[5] == "Tags:x y\r\n" and out[6] == "\r\n", out
    out = with_keys(out, "Editor", {"Bookmarks": "1,2"})
    assert read_keys(out, "Editor") == {"Bookmarks": "1,2"} and _header(out[3]) == "Editor", out
    assert read_keys(out, "General") == {"PreviewTime": "100"}
    print("ok")
