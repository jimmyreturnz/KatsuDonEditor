"""MainWindow must let go of QApplication when it closes.

`__init__` makes two registrations on the *application*, which outlives every
window: an event filter and a `focusChanged` slot. Qt dispatches every
application event through every installed filter, so a second live
registration doubles the per-event Python work and an N'th multiplies it by N.

One window per run never noticed. The suite builds one per test and noticed
badly -- building twelve in a row went 171ms -> 976ms (5.7x) and the whole run
degraded past four hours. Releasing on close made that flat (164ms -> 154ms)
*with the same number of objects still alive*, which is what proves the cost
was the fan-out rather than the memory.

These tests guard the release itself rather than the timing: a wall-clock
assertion would be flaky on a loaded machine, while "is it still registered"
is exact.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import warnings
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui import gui
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ApplicationHookReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _window(self) -> gui.MainWindow:
        window = gui.MainWindow()
        window.show()
        window._load_map_path(self.path, refresh_difficulties=True)
        return window

    def test_closing_disconnects_the_focus_slot(self):
        """Proved by attempting the disconnect again, not with a spy.

        QObject.receivers() reports 0 for Python-side connections in PySide6,
        and replacing the instance attribute does not redirect an already
        stored bound-method connection -- a spy set that way never fires
        whether or not the slot is still wired, so it would pass either way.
        PySide6's disconnect() returns True/False (and warns) rather than
        raising, so the exact signal is the return value: disconnecting a live
        slot gives True, an already-disconnected one gives False.
        """
        window = self._window()
        window.close()

        with warnings.catch_warnings():
            # PySide emits "Failed to disconnect" as a RuntimeWarning here --
            # that warning IS the expected outcome, not a problem.
            warnings.simplefilter("ignore", RuntimeWarning)
            again = QApplication.instance().focusChanged.disconnect(
                window._editor_view_focus_changed
            )

        self.assertFalse(
            again,
            "close() must already have disconnected it; a second disconnect "
            "returning True would mean the slot was still live",
        )

    def test_the_event_filter_stops_seeing_application_events(self):
        """Directly: the filter is what multiplies per-event cost by the number
        of live windows, so it has to be gone, not merely inert."""
        window = self._window()
        seen = []
        window.eventFilter = lambda obj, event: seen.append(1) or False

        probe = gui.QWidget()
        QApplication.instance().sendEvent(probe, gui.QEvent(gui.QEvent.User))
        self.assertTrue(seen, "the filter is installed while the window is open")

        window.close()
        seen.clear()
        QApplication.instance().sendEvent(probe, gui.QEvent(gui.QEvent.User))

        self.assertFalse(seen, "a closed window must not filter application events")
        probe.deleteLater()

    def test_closing_stops_the_render_loop(self):
        """A closed window that has not been collected yet must not keep its
        4ms PreciseTimer: WINMM posts every tick into the main queue, and a few
        dozen of them starved the main thread of its own queue lock."""
        window = self._window()
        self.assertTrue(window.gameplay_render_timer.isActive())
        window.close()
        self.assertFalse(window.gameplay_render_timer.isActive())
        self.assertFalse(window.info_timer.isActive())

    def test_releasing_twice_is_harmless(self):
        """closeEvent can run more than once -- a cancelled close, then a real
        one -- and removeEventFilter/disconnect must tolerate that."""
        window = self._window()
        window.close()

        window._release_application_hooks()  # must not raise

    def test_many_windows_do_not_accumulate_event_filters(self):
        """The whole point: N live registrations put N Python calls on every
        application event, which is what made the suite quadratic."""
        seen = []
        windows = []
        for _ in range(5):
            window = self._window()
            window.eventFilter = lambda obj, event: seen.append(1) or False
            windows.append(window)
        for window in windows:
            window.close()

        # Only what arrives *after* every window is closed counts: showing and
        # loading each one fires plenty of events that the spies installed
        # before it legitimately saw.
        seen.clear()
        probe = gui.QWidget()
        QApplication.instance().sendEvent(probe, gui.QEvent(gui.QEvent.User))
        probe.deleteLater()

        self.assertFalse(
            seen, "five opened-and-closed windows must leave no filter behind",
        )


class OrphanedLayoutItemTests(unittest.TestCase):
    """Building a window must not leave a layout item PySide still tracks after
    Qt has let go of it.

    PySide keeps a wrapper for every item of a layout that was filled and then
    attached (`addLayout`). Adding a widget that is already in a layout makes
    Qt free its old item without telling PySide, so that wrapper then claims
    freed memory -- and the next Qt object allocated there reached the window's
    eventFilter as a QWidgetItem, an access violation inside `show()` about one
    test_gimmick_editor run in six. The Fancy Arranger's sub-toolbar added
    Reset and Export twice. `replaceWidget`'s returned item is the one allowed
    orphan: it is detached but never freed, so nothing can be allocated over it.
    """

    @staticmethod
    def _orphans():
        import shiboken6
        from PySide6.QtWidgets import QLayout, QLayoutItem

        wrappers = shiboken6.getAllValidWrappers()
        layouts = [w for w in wrappers if isinstance(w, QLayout)]
        # indexOf compares pointers, so it can ask about a freed item safely.
        return {
            shiboken6.getCppPointer(w)[0] for w in wrappers
            if isinstance(w, QLayoutItem) and not isinstance(w, QLayout)
            and not any(layout.indexOf(w) >= 0 for layout in layouts)
        }

    def test_building_a_window_orphans_no_layout_item(self):
        import shiboken6
        from unittest import mock
        from PySide6.QtWidgets import QLayout

        detached = set()
        real_replace = QLayout.replaceWidget

        def replace(layout, *args, **kwargs):
            item = real_replace(layout, *args, **kwargs)
            if item is not None:
                detached.add(shiboken6.getCppPointer(item)[0])
            return item

        before = self._orphans()
        with mock.patch.object(QLayout, "replaceWidget", replace):
            window = gui.MainWindow()
        try:
            new = self._orphans() - before - detached
            self.assertEqual(new, set(), f"{len(new)} layout items orphaned while building the window")
        finally:
            window.close()


class AudioFileReleaseTests(ApplicationHookReleaseTests):
    """The song file must not stay locked by a closed window.

    Qt keeps the decoded source open until it is cleared, and on Windows that
    is a real file lock: the song cannot be moved or deleted underneath it.
    tearDown met the same lock from the other side, failing intermittently
    with NotADirectoryError while TemporaryDirectory raced the backend.
    """

    def test_closing_clears_the_media_source(self):
        window = self._window()
        window.player.setSource(
            gui.QUrl.fromLocalFile(str(Path(self._temp.name) / "audio.mp3"))
        )
        self.assertFalse(window.player.source().isEmpty())

        window.close()

        self.assertTrue(
            window.player.source().isEmpty(),
            "a closed window must let go of the song file",
        )


if __name__ == "__main__":
    unittest.main()
