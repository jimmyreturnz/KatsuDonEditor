"""Check GitHub Releases for a newer KatsuDon Editor and fetch it.

Everything above the "Qt layer" banner is plain stdlib so it can be tested
without a display or a network: the HTTP call is a single injectable `opener`
argument, and every network path returns None rather than raising into the
caller.

Updates install in place, so nobody re-downloads a ZIP by hand again:

1. download, verify the SHA-256 against the release's own SHA256SUMS.txt, and
   extract to `<app folder>/_update/` (`stage_update`);
2. the running app closes through its normal save prompt and starts the
   *staged* exe with `--apply-update <app folder> <pid>` (`launch_apply`);
3. that exe waits for the old process to exit -- Windows locks a running exe
   and its loaded DLLs -- swaps itself into the app folder, rolling back on any
   failure, and relaunches it (`run_apply_mode`);
4. the relaunched app deletes `_update/` and `_old/` (`cleanup_leftovers`).

Settings live in the registry and AppData, not the app folder, so the swap
loses nothing. Run from source, there is no build to replace: the dialog only
offers the release page.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.security_utils import resource_roots

LOGGER = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/jimmyreturnz/KatsuDonEditor/releases/latest"
RELEASE_PAGE = "https://github.com/jimmyreturnz/KatsuDonEditor/releases/latest"
PORTABLE_ASSET = "KatsuDonEditor-Windows-x64.zip"
CHECKSUM_ASSET = "SHA256SUMS.txt"
# GitHub answers an API request without a User-Agent with 403.
USER_AGENT = "KatsuDonEditor-Updater"
TIMEOUT_SECONDS = 10

SETTING_CHECK_ON_STARTUP = "updates/check_on_startup"
SETTING_SKIPPED_TAG = "updates/skipped_tag"

EXE_NAME = "KatsuDonEditor.exe"
# The exe's name before the rename (v3.5.9 and earlier). Those installs
# update from the TaikoFancyArranger-Windows-x64.zip the release still
# ships, which carries the new exe under this name too, so their shortcuts
# keep working; apply_update keeps that copy current from then on.
LEGACY_EXE_NAME = "TaikoFancyArranger.exe"
STAGING_FOLDER = "_update"
OLD_FOLDER = "_old"
FAILURE_MARKER = "_update_failed.txt"
APPLY_FLAG = "--apply-update"


# --------------------------------------------------------------------------
# Version
# --------------------------------------------------------------------------

def local_version() -> str:
    """The VERSION file bundled next to the executable, or "" if unreadable."""
    for root in resource_roots():
        candidate = root / "VERSION"
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip()
        except OSError as error:
            LOGGER.warning("Could not read %s: %s", candidate, error)
    return ""


def version_tuple(text: str) -> tuple[int, ...]:
    """Parse "v3.10.0" into (3, 10, 0). Unparseable input yields ()."""
    cleaned = str(text or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(remote: str, local: str) -> bool:
    """Numeric comparison, so 3.10.0 beats 3.9.0 where a string compare would not."""
    right, left = version_tuple(remote), version_tuple(local)
    if not right or not left:
        # An unreadable version on either side is not grounds for prompting.
        return False
    width = max(len(right), len(left))
    return right + (0,) * (width - len(right)) > left + (0,) * (width - len(left))


# --------------------------------------------------------------------------
# Release lookup
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    tag: str
    version: str
    notes: str
    page_url: str
    zip_url: str
    checksums_url: str


class UpdateError(RuntimeError):
    """A download or verification step failed in a way worth showing the user."""


def _asset_url(payload: dict, name: str) -> str:
    for asset in payload.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == name:
            return str(asset.get("browser_download_url") or "")
    return ""


def parse_release(payload: object) -> ReleaseInfo | None:
    """Turn the API payload into a ReleaseInfo, or None if it is not usable."""
    if not isinstance(payload, dict):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    if not version_tuple(tag):
        return None
    return ReleaseInfo(
        tag=tag,
        version=".".join(str(part) for part in version_tuple(tag)),
        notes=str(payload.get("body") or "").strip(),
        page_url=str(payload.get("html_url") or RELEASE_PAGE),
        zip_url=_asset_url(payload, PORTABLE_ASSET),
        checksums_url=_asset_url(payload, CHECKSUM_ASSET),
    )


def _request(url: str) -> urllib.request.Request:
    if not url.lower().startswith("https://"):
        raise UpdateError(f"Refusing a non-HTTPS update URL: {url}")
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def fetch_latest_release(opener=urllib.request.urlopen, url: str = RELEASES_API) -> ReleaseInfo | None:
    """GET the latest release. Every failure is logged and returns None.

    No network, DNS failure, HTTP 403 rate limit, truncated body, HTML error
    page instead of JSON -- an update check must never be the reason the app
    misbehaves, so all of it lands in the same quiet branch.
    """
    try:
        with opener(_request(url), timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, UpdateError) as error:
        LOGGER.info("Update check failed: %s", error)
        return None
    return parse_release(payload)


def check_for_update(current: str | None = None, opener=urllib.request.urlopen) -> ReleaseInfo | None:
    """The release to offer, or None when up to date / unreachable."""
    release = fetch_latest_release(opener)
    if release is None:
        return None
    return release if is_newer(release.tag, current if current is not None else local_version()) else None


# --------------------------------------------------------------------------
# Download and verification
# --------------------------------------------------------------------------

def expected_digest(checksums: str, filename: str) -> str | None:
    """Pull one "<hex>  <name>" line out of a SHA256SUMS.txt body."""
    for line in checksums.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-1].strip("*") == filename and len(fields[0]) == 64:
            return fields[0].lower()
    return None


def download(url: str, destination: Path, progress=None, opener=urllib.request.urlopen) -> str:
    """Stream url into destination and return the SHA-256 hex digest of what landed."""
    digest = hashlib.sha256()
    with opener(_request(url), timeout=TIMEOUT_SECONDS) as response:
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except (AttributeError, ValueError):
            total = 0
        read = 0
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                read += len(chunk)
                if progress is not None:
                    progress(int(read * 100 / total) if total else -1)
    return digest.hexdigest()


def verify_digest(actual: str, expected: str | None) -> bool:
    return bool(expected) and str(actual).lower() == str(expected).lower()


def application_folder() -> Path:
    """The portable folder the app is running from (repo root when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def can_install_in_place() -> bool:
    """Only a frozen Windows build replaces itself.

    From source, application_folder() is the repository, and writing a
    PyInstaller build over it would be the worst thing an update could do.
    """
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def stage_update(release: ReleaseInfo, progress=None, opener=urllib.request.urlopen,
                 folder: Path | None = None) -> Path:
    """Download, verify, and extract the release into `<folder>/_update/`.

    Nothing is written to `folder` unless the ZIP matches the digest published
    in the release's SHA256SUMS.txt. Staging inside the app folder keeps it on
    the same volume and proves the folder is writable before anyone is asked to
    restart.
    """
    if not release.zip_url or not release.checksums_url:
        raise UpdateError("The release does not carry the portable Windows assets.")
    folder = application_folder() if folder is None else folder
    staging = folder / STAGING_FOLDER

    with tempfile.TemporaryDirectory(prefix="tfa-update-") as work:
        archive = Path(work) / PORTABLE_ASSET
        actual = download(release.zip_url, archive, progress, opener)

        with opener(_request(release.checksums_url), timeout=TIMEOUT_SECONDS) as response:
            checksums = response.read().decode("utf-8", "replace")

        if not verify_digest(actual, expected_digest(checksums, PORTABLE_ASSET)):
            raise UpdateError("The download does not match the checksum published with the release.")

        shutil.rmtree(staging, ignore_errors=True)
        try:
            staging.mkdir(parents=True)
            # extractall sanitises absolute paths and .. members itself, so the
            # archive cannot reach outside staging.
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(staging)
        except PermissionError as error:
            shutil.rmtree(staging, ignore_errors=True)
            raise UpdateError(
                f"KatsuDon Editor cannot write to {folder}. Move the folder somewhere "
                f"you own (not Program Files) and try again."
            ) from error
    if not (staging / EXE_NAME).is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"The release archive does not contain {EXE_NAME}.")
    return staging


def _detached(arguments: list[str], cwd: Path) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(arguments, cwd=str(cwd), creationflags=flags, close_fds=True)


def launch_apply(staging: Path, folder: Path | None = None) -> None:
    """Start the staged build to copy itself over `folder` once we have exited.

    The new exe is the helper because Windows will not let this process
    overwrite its own exe or the DLLs it has loaded, and a dropped .bat or
    PowerShell script is exactly what antivirus heuristics look for.
    """
    folder = application_folder() if folder is None else folder
    _detached([str(staging / EXE_NAME), APPLY_FLAG, str(folder), str(os.getpid())], folder)


def wait_for_exit(pid: int, timeout: float = 30.0) -> bool:
    """True once `pid` has exited (or never existed), False on timeout."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Without restype a 64-bit HANDLE is truncated to a C int.
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        kernel32.CloseHandle(handle)


def _retry(action, *arguments, attempts: int = 25, delay: float = 0.2):
    """Antivirus scanners and the exiting process hold files for a moment."""
    for attempt in range(attempts):
        try:
            return action(*arguments)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def apply_update(staging: Path, folder: Path, pid: int, wait=wait_for_exit) -> None:
    """Swap every top-level entry of `staging` into `folder`, or change nothing.

    The old entries are renamed into `<folder>/_old/` first (cheap: same
    volume), then the new ones are copied in -- copied, not moved, because the
    staged exe doing this is running out of `staging`. Any failure puts the old
    entries back, so the user always ends up with a working copy.
    """
    if not wait(pid):
        raise UpdateError("KatsuDon Editor did not close, so the update was not installed.")
    old = folder / OLD_FOLDER
    shutil.rmtree(old, ignore_errors=True)
    old.mkdir()
    touched: list[str] = []
    try:
        for name in sorted(entry.name for entry in staging.iterdir()):
            source, target = staging / name, folder / name
            touched.append(name)
            if target.exists():
                _retry(os.replace, target, old / name)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        legacy = folder / LEGACY_EXE_NAME
        if legacy.exists() and not (staging / LEGACY_EXE_NAME).exists():
            # Left behind by the bridge, and what the old Start Menu shortcut
            # and any pinned taskbar icon still start: a stale one would run
            # the previous version's code.
            touched.append(LEGACY_EXE_NAME)
            _retry(os.replace, legacy, old / LEGACY_EXE_NAME)
            shutil.copy2(staging / EXE_NAME, legacy)
    except OSError:
        # Only what was reached: an entry not yet processed is still the old
        # one, and deleting it here would be the update destroying the app.
        for name in touched:
            target = folder / name
            if (old / name).exists():
                _remove(target)
                os.replace(old / name, target)
            elif target.exists():
                _remove(target)
        raise


def run_apply_mode(argv: list[str]) -> bool:
    """Handle `--apply-update <folder> <pid>`. False when argv is a normal launch.

    Whatever happens, the app in `folder` is started again -- the new version
    on success, the untouched old one after a rollback, with the reason left in
    a marker file for that launch to show.
    """
    if APPLY_FLAG not in argv:
        return False
    index = argv.index(APPLY_FLAG)
    folder = Path(argv[index + 1])
    try:
        apply_update(Path(sys.executable).resolve().parent, folder, int(argv[index + 2]))
    except (UpdateError, OSError, ValueError) as error:
        LOGGER.warning("Update could not be applied: %s", error)
        try:
            (folder / FAILURE_MARKER).write_text(str(error), encoding="utf-8")
        except OSError:
            pass
    _detached([str(folder / EXE_NAME)], folder)
    return True


def cleanup_leftovers(folder: Path | None = None) -> str:
    """Remove what an update left behind; return its failure message, if any.

    Errors are ignored: the helper that just relaunched us may still be
    exiting out of `_update`, and the next launch tries again.
    """
    folder = application_folder() if folder is None else folder
    if folder.name == STAGING_FOLDER:
        # Someone started the staged exe by hand; do not delete it from under itself.
        return ""
    shutil.rmtree(folder / STAGING_FOLDER, ignore_errors=True)
    shutil.rmtree(folder / OLD_FOLDER, ignore_errors=True)
    marker = folder / FAILURE_MARKER
    try:
        message = marker.read_text(encoding="utf-8")
        marker.unlink()
    except OSError:
        return ""
    return message


# --------------------------------------------------------------------------
# Qt layer
# --------------------------------------------------------------------------

from PySide6.QtCore import QThread, QUrl, Qt, Signal  # noqa: E402
from PySide6.QtGui import QDesktopServices  # noqa: E402
from ui.smooth_scroll import smooth  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from app.i18n import tr  # noqa: E402


class CheckThread(QThread):
    """Runs the network check off the GUI thread so startup never waits on it.

    `reachable` is separate from the release, because "you are up to date" and
    "GitHub could not be reached" are the same None to check_for_update and
    must not be the same message to the user.
    """

    result = Signal(object, bool)  # ReleaseInfo | None, reachable

    def run(self) -> None:  # noqa: D102
        release = fetch_latest_release()
        if release is None:
            self.result.emit(None, False)
            return
        self.result.emit(release if is_newer(release.tag, local_version()) else None, True)


class DownloadThread(QThread):
    """Download + checksum + stage, off the GUI thread."""

    progressed = Signal(int)
    finished_with = Signal(object, str)  # Path | None, error message

    def __init__(self, release: ReleaseInfo, parent=None) -> None:
        super().__init__(parent)
        self._release = release

    def run(self) -> None:  # noqa: D102
        try:
            self.finished_with.emit(stage_update(self._release, self.progressed.emit), "")
        except (UpdateError, urllib.error.URLError, OSError, zipfile.BadZipFile) as error:
            LOGGER.warning("Update download failed: %s", error)
            self.finished_with.emit(None, str(error))


class UpdateDialog(QDialog):
    """Offers the new release: install it in place, read it online, or skip it.

    `restart(staging)` is the window's: it asks about unsaved work, hands over
    to the staged build and quits, returning False if the user stayed. Without
    one -- or run from source -- there is nothing to install over, so only the
    release page is offered.
    """

    # QDialog.Rejected is 0 and Accepted is 1; a third code says "skip this
    # version" without a fourth signal.
    SKIPPED = 2

    def __init__(self, release: ReleaseInfo, current: str, parent=None, restart=None) -> None:
        super().__init__(parent)
        self._release = release
        self._restart = restart if can_install_in_place() else None
        self._staging: Path | None = None
        self._download: DownloadThread | None = None
        self.setWindowTitle(tr("MainWindow", "Update available"))
        # main() sets the app-wide window icon from application_icon(); reusing
        # it here avoids importing gui, which imports this module.
        icon = QApplication.windowIcon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(520, 420)

        layout = QVBoxLayout(self)
        headline = QLabel(tr("MainWindow", "KatsuDon Editor {0} is available.").format(release.version))
        headline.setStyleSheet("font-weight: 600;")
        layout.addWidget(headline)
        layout.addWidget(QLabel(tr("MainWindow", "You are running version {0}.").format(current or "?")))

        notes = QTextEdit()
        notes.setReadOnly(True)
        smooth(notes)
        notes.setPlainText(release.notes or tr("MainWindow", "No release notes were published."))
        layout.addWidget(notes, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        self.skip_button = QPushButton(tr("MainWindow", "Skip This Version"))
        self.skip_button.clicked.connect(lambda: self.done(self.SKIPPED))
        buttons.addWidget(self.skip_button)
        buttons.addStretch(1)
        self.page_button = QPushButton(tr("MainWindow", "Open Release Page"))
        self.page_button.clicked.connect(self._open_release_page)
        buttons.addWidget(self.page_button)
        self.later_button = QPushButton(tr("MainWindow", "Later"))
        self.later_button.clicked.connect(self.reject)
        buttons.addWidget(self.later_button)
        self.download_button = QPushButton(tr("MainWindow", "Update and Restart"))
        self.download_button.setDefault(True)
        self.download_button.clicked.connect(self._start_download)
        self.download_button.setEnabled(bool(release.zip_url and release.checksums_url))
        self.download_button.setVisible(self._restart is not None)
        buttons.addWidget(self.download_button)
        layout.addLayout(buttons)
        if self._restart is None:
            layout.insertWidget(layout.count() - 1, QLabel(tr(
                "MainWindow", "Running from source: update with git pull, or get the build from the release page.")))

    def _open_release_page(self) -> None:
        # html_url comes straight from the API response; hand the OS only https.
        if self._release.page_url.lower().startswith("https://"):
            QDesktopServices.openUrl(QUrl(self._release.page_url))

    def skipped(self) -> bool:
        return self.result() == self.SKIPPED

    def reject(self) -> None:
        """Refuse Esc / the window's X while a download is in flight.

        exec() returning drops the last reference to this dialog, which would
        take the still-running DownloadThread down with it. QDialog's own
        closeEvent routes here too, so this one override covers both.
        """
        if self._download is not None and self._download.isRunning():
            return
        super().reject()

    def _start_download(self) -> None:
        if self._staging is not None:
            # Already staged; the user stayed for unsaved work and is retrying.
            self._hand_over()
            return
        self.download_button.setEnabled(False)
        self.skip_button.setEnabled(False)
        self.later_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self._download = DownloadThread(self._release, self)
        self._download.progressed.connect(self._on_progress)
        self._download.finished_with.connect(self._on_finished)
        self._download.start()

    def _on_progress(self, percent: int) -> None:
        if percent < 0:
            self.progress.setRange(0, 0)
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(percent)

    def _on_finished(self, folder: object, error: str) -> None:
        self.progress.setVisible(False)
        self.skip_button.setEnabled(True)
        self.later_button.setEnabled(True)
        if folder is None:
            self.download_button.setEnabled(True)
            QMessageBox.warning(
                self,
                tr("MainWindow", "Update failed"),
                tr("MainWindow", "The update could not be installed. Your current copy was not changed.")
                + "\n\n"
                + str(error),
            )
            return
        self._staging = Path(folder)
        self._hand_over()

    def _hand_over(self) -> None:
        if self._restart(self._staging):
            self.accept()
            return
        # The user chose to stay for unsaved work. The build stays staged, so
        # the button now restarts without downloading again.
        self.download_button.setEnabled(True)


def present_update(release: ReleaseInfo, settings, parent=None, restart=None) -> None:
    """Show the offer and remember a skipped tag so it does not nag every launch."""
    dialog = UpdateDialog(release, local_version(), parent, restart)
    dialog.exec()
    if dialog.skipped():
        settings.set_value(SETTING_SKIPPED_TAG, release.tag)
        settings.sync()


def _alive(widget) -> bool:
    """False once Qt has deleted the C++ side of a widget out from under us."""
    try:
        widget.isVisible()
    except RuntimeError:
        return False
    return True


def check_now(parent, settings, restart=None) -> CheckThread:
    """Manual check: always reports a result, and ignores a previously skipped tag.

    The thread is parented to the application, not to `parent`: the Settings
    dialog that starts the check can be closed while the request is in flight,
    and Qt destroys a still-running QThread along with its parent.
    """
    parent.setCursor(Qt.BusyCursor)

    def finished(release: object, reachable: bool) -> None:
        owner = parent if _alive(parent) else QApplication.activeWindow()
        if owner is parent:
            parent.unsetCursor()
        if isinstance(release, ReleaseInfo):
            present_update(release, settings, owner, restart)
        elif reachable:
            QMessageBox.information(
                owner,
                tr("MainWindow", "Check for updates"),
                tr("MainWindow", "You are running the latest version ({0}).").format(local_version() or "?"),
            )
        else:
            QMessageBox.information(
                owner,
                tr("MainWindow", "Check for updates"),
                tr("MainWindow", "Could not reach GitHub to check for updates. Please try again later."),
            )

    thread = CheckThread(QApplication.instance())
    thread.result.connect(finished)
    thread.start()
    return thread
