"""Print the difficulty osu!stable has selected, once a second, and how long
finding it took. Browse song select while it runs: every change of
difficulty must print the new path, or `library/osu_memory`'s offsets have
moved with an osu! update.

    .venv/Scripts/python.exe tools/read_osu_current.py [seconds]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library import osu_memory  # noqa: E402

started = time.perf_counter()
process = osu_memory.OsuProcess()
print(f"pid {process.pid}, Base at {process.base:#x}, found in {time.perf_counter() - started:.2f}s")
last = None
for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 10):
    started = time.perf_counter()
    now = process.current()
    took = (time.perf_counter() - started) * 1000
    if now != last:
        print(f"{took:.2f}ms  {now[0]} / {now[1]}")
        last = now
    time.sleep(1)
