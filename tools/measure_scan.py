"""What a song select scan costs, cold and warm, phase by phase.

    python tools/measure_scan.py <Songs folder> [--cold]

Warm is every launch after the first: the saved index is shown, then the
walk checks it. Cold (--cold, an empty cache) is the first launch and Rescan.
The phases are timed separately -- listing, stat, reading headers, the JSON
-- so the fix goes where the time is. Never writes the app's own cache.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library import song_library  # noqa: E402


def timed(label: str, run):
    began = perf_counter()
    result = run()
    print(f"  {label:<28} {perf_counter() - began:8.2f}s")
    return result


def main() -> None:
    root = Path(sys.argv[1])
    cache = song_library.Cache()
    print(f"{root}")
    if "--cold" in sys.argv:
        found = timed("cold scan", lambda: sum(1 for d in song_library.scan(root, cache) if d is not None))
        print(f"  {found} taiko difficulties, {len(cache)} files")
    else:
        # A warm cache to start from, built once and not timed.
        for _ in song_library.scan(root, cache):
            pass
    found = timed("warm scan", lambda: sum(1 for d in song_library.scan(root, cache) if d is not None))
    print(f"  {found} taiko difficulties, {len(cache)} cache entries")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cache.json"
        timed("save cache", lambda: song_library.save_cache(path, cache))
        timed("load cache", lambda: song_library.load_cache(path))
        print(f"  cache file {path.stat().st_size / 1e6:.1f} MB")
    # The pieces of the old per-file walk, to say where a warm scan's time was.
    files = timed("rglob *.osu", lambda: list(root.rglob("*.osu")))
    timed(f"stat {len(files)} files", lambda: [p.stat() for p in files])
    folders = timed("scandir Songs", lambda: [e.path for e in os.scandir(root) if e.is_dir()])
    timed(f"stat {len(folders)} folders", lambda: [Path(f).stat() for f in folders])
    sample = files[:2000]
    timed(f"read_header x{len(sample)}", lambda: [song_library.read_header(p) for p in sample])


if __name__ == "__main__":
    main()
