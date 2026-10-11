"""How long the app takes to open, phase by phase.

    python tools/measure_startup.py [runs]

Each run is a fresh process (an import that is already cached would be
measured as free otherwise), on the real platform with the window kept off
screen, using the real settings and library -- what a launch costs is what
the owner's library costs. Phases:

- import: `from ui import gui`, every module the app pulls in.
- window: MainWindow() -- building every page.
- shown: show() through the first processed paint.
- library: start_library() until the song list has rows, or 20s. Uses the
  real song index (main()'s organization and application names), so this is
  a second start: the cached list, then the verification walk behind it.

Run it twice: the first pass reads .pyc and Qt plugins cold.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHILD = r"""
import json, sys, time
t0 = time.perf_counter()
sys.path.insert(0, ROOT)
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication([])
t_qt = time.perf_counter()
from ui import gui
# What main() sets: without them the song index is looked for in a folder
# shared with every Python app, found empty, and the run measures a first scan.
app.setOrganizationName(gui.ORGANIZATION_NAME)
app.setApplicationName(gui.APPLICATION_NAME)
t_import = time.perf_counter()
window = gui.MainWindow()
window.resize(1920, 1080)
window.setAttribute(Qt.WA_DontShowOnScreen, True)
t_window = time.perf_counter()
window.show()
app.processEvents()
t_shown = time.perf_counter()
window.start_library()
deadline = t_shown + 20.0
rows = 0
while time.perf_counter() < deadline:
    app.processEvents()
    rows = window._library.song_list.count() if hasattr(window, "_library") else 0
    if rows:
        break
    time.sleep(0.002)
t_library = time.perf_counter()
print(json.dumps({"qt": t_qt - t0, "import": t_import - t_qt, "window": t_window - t_import,
                  "shown": t_shown - t_window, "library": t_library - t_shown, "rows": rows}))
import os
sys.stdout.flush()
os._exit(0)
"""


def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    results = []
    for _ in range(runs):
        out = subprocess.run([sys.executable, "-c", f"ROOT = {str(ROOT)!r}\n" + CHILD],
                             capture_output=True, text=True, cwd=ROOT)
        line = [l for l in out.stdout.splitlines() if l.startswith("{")]
        if not line:
            print(out.stderr[-2000:])
            raise SystemExit("run failed")
        results.append(json.loads(line[-1]))
    for key in ("qt", "import", "window", "shown", "library"):
        values = [r[key] * 1000 for r in results]
        print(f"{key:8} " + "  ".join(f"{v:7.0f}ms" for v in values))
    print("rows    ", [r["rows"] for r in results])


if __name__ == "__main__":
    main()
