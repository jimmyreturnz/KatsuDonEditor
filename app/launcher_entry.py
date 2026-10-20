"""Put the app where the desktop's own search looks: a Start Menu shortcut on
Windows, a .desktop entry on Linux.

PATH is not that place -- Start search and app launchers read shortcuts and
desktop entries, and PATH only reaches a terminal. The release is a portable
ZIP with no installer, so the app writes its own entry on startup, and
rewrites it when the folder has moved (an extracted ZIP somewhere new).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.updater import application_folder

DISPLAY_NAME = "Taiko Fancy Arranger"
# Start search matches a shortcut's target exe name as well as the shortcut's
# own, so "TaikoFancyArranger" finds it there; Linux needs it as a keyword.
KEYWORDS = "TaikoFancyArranger;taiko;osu;"


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def windows_shortcut_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{DISPLAY_NAME}.lnk"


def linux_entry_path() -> Path:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "applications" / "taiko-fancy-arranger.desktop"


def desktop_entry(command: list[str], icon: Path | None, folder: Path) -> str:
    exec_line = " ".join('"' + part.replace('"', '\\"') + '"' for part in command)
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={DISPLAY_NAME}",
        "Comment=osu!taiko chart editor",
        f"Exec={exec_line}",
        f"Path={folder}",
        f"Keywords={KEYWORDS}",
        "Categories=AudioVideo;Audio;",
        "Terminal=false",
    ]
    if icon is not None:
        lines.append(f"Icon={icon}")
    return "\n".join(lines) + "\n"


def register(settings) -> None:
    """Write the entry if it is missing or points at another folder. Never
    raises: a launcher entry is a convenience, not a reason to fail startup."""
    try:
        folder = application_folder()
        if sys.platform == "win32":
            # Frozen only: from source the target would be python.exe, and
            # searching for the app would open a bare interpreter.
            if not getattr(sys, "frozen", False):
                return
            path, target = windows_shortcut_path(), sys.executable
        elif sys.platform.startswith("linux"):
            path = linux_entry_path()
            command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(folder / "main.py")]
            target = " ".join(command)
        else:
            return
        if path is None or (path.exists() and settings.value("launcher/target") == target):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            script = (
                f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(path))});"
                f"$s.TargetPath={_ps_quote(target)};$s.WorkingDirectory={_ps_quote(str(folder))};"
                f"$s.Description={_ps_quote(DISPLAY_NAME)};$s.Save()"
            )
            # Not waited on: PowerShell takes a few hundred ms to start, and a
            # failed write simply leaves no shortcut, so the next start retries.
            # By absolute path: a bare "powershell" is searched for in the
            # app's folder and the working directory first, where a planted
            # powershell.exe would run instead.
            powershell = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
                          / "WindowsPowerShell" / "v1.0" / "powershell.exe")
            if not powershell.is_file():
                return
            subprocess.Popen(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            icon = next((candidate for candidate in (
                folder / "assets" / "icons" / "FancyTaikoEditor_Logo.png",
                folder / "_internal" / "assets" / "icons" / "FancyTaikoEditor_Logo.png",
            ) if candidate.is_file()), None)
            path.write_text(desktop_entry(command, icon, folder), encoding="utf-8")
        settings.set_value("launcher/target", target)
    except Exception:
        pass
