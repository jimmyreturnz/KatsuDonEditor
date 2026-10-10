"""App themes (theme.py), and the SV graph's labels staying inside the view."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from ui import gui
from ui import theme
from ui.settings_dialog import SettingsDialog

_APP: QApplication | None = None

# What a theme must never touch: they mean something in the chart.
CHART_COLOURS = ("#e54c2e", "#438dab", "#fbb706", "#738098", "#f4f7fb", "#ffffff")


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ThemeMapTests(unittest.TestCase):
    def tearDown(self) -> None:
        theme.install(theme.AS_WRITTEN)

    def test_tests_run_on_the_colours_as_written(self) -> None:
        self.assertEqual(theme.active(), theme.AS_WRITTEN)
        self.assertEqual(theme.css("color: #191f29;"), "color: #191f29;")

    def test_the_default_grounds_on_1f1e33_and_keeps_its_pink(self) -> None:
        table = theme.build_map(theme.DEFAULT)
        self.assertEqual(table["#191f29"], "#1f1e33")
        for pink in ("#f3a6bd", "#ff66aa", "#ff9dcc", "#f7bfd0", "#b0587f"):
            self.assertNotIn(pink, table)

    def test_old_theme_names_still_load(self) -> None:
        theme.install("kiai")
        self.assertEqual(theme.active(), "gold")
        theme.install("pink")
        self.assertEqual(theme.active(), "osu")

    def test_no_theme_output_is_another_key(self) -> None:
        # Or a colour passing through css() twice would move twice.
        for name in theme.THEMES:
            table = theme.build_map(name)
            self.assertFalse([v for k, v in table.items() if v in table and v != k], name)

    def test_chart_colours_are_left_alone(self) -> None:
        for name in theme.THEMES:
            table = theme.build_map(name)
            for colour in CHART_COLOURS:
                self.assertNotIn(colour, table, (name, colour))

    def test_stylesheets_are_recoloured_once_installed(self) -> None:
        theme.install("taiko")
        button = QPushButton()
        button.setStyleSheet("QPushButton { background: #f3a6bd; border: 1px solid #FF66AA55; color: #e54c2e; }")
        self.assertEqual(
            button.styleSheet(),
            "QPushButton { background: #d4432a; border: 1px solid #5aa7c755; color: #e54c2e; }")
        self.assertEqual(theme.color("#ff66aa").name(), "#5aa7c7")
        # Integers too: the song list's selected-row tint is written that way
        # and stayed pink in every theme.
        tint = theme.rgba(255, 102, 170, 46)
        self.assertEqual((tint.name(), tint.alpha()), ("#5aa7c7", 46))

    def test_switch_art_follows_the_theme(self) -> None:
        # The sheet names the art as written; css() swaps in the theme's copy,
        # so the kept source can be translated again for the next theme.
        import re
        from pathlib import Path
        from ui import config_sheet
        written = config_sheet.ui_asset("switch-on.svg")
        self.assertTrue(written.endswith("assets/ui/switch-on.svg"))
        theme.install("taiko")
        path = re.search(r"url\((.+?)\)", theme.css(f"image: url({written});")).group(1)
        themed = Path(path).read_text(encoding="utf-8")
        self.assertIn("#5aa7c7", themed)
        self.assertNotIn("#ff66aa", themed)

    def test_mockup_grounds_are_used_exactly(self) -> None:
        self.assertEqual(theme.build_map("lantern")["#191f29"], "#1d1110")
        self.assertEqual(theme.build_map("taiko")["#3a4554"], "#34445a")

    def test_settings_dialog_offers_every_theme(self) -> None:
        settings = gui.SettingsManager()
        dialog = SettingsDialog(settings, gui.ShortcutRegistry(settings))
        names = [dialog.theme_combo.itemData(i) for i in range(dialog.theme_combo.count())]
        self.assertEqual(names, list(theme.THEMES))
        self.assertEqual(dialog.theme_combo.currentData(), theme.DEFAULT)
        dialog.deleteLater()


class LiveSwitchTests(unittest.TestCase):
    """A theme changes with the window built (owner, 2026-10-08: no restart)."""

    def tearDown(self) -> None:
        theme.switch(theme.AS_WRITTEN)

    def test_every_sheet_follows_a_switch(self) -> None:
        # Built in one theme and switched to another, as a real session is:
        # built as written, a sheet re-set from its own translated text
        # (`_apply_selected_toggle_style`) looked like a source and passed.
        theme.switch("osu")
        window = gui.MainWindow()
        window._show_page(gui.PAGE_EDITOR)
        window._apply_selected_toggle_style()
        pill = window._mod_buttons[0]["HD"]
        osu_pill = pill.styleSheet()
        theme.switch("taiko")
        window.retheme()
        osu, taiko = theme.build_map("osu"), theme.build_map("taiko")
        # A colour only the osu! theme produces, anywhere, is a leak.
        stale = {value for value in osu.values() if value not in taiko.values()}
        leaks = []
        for widget in [QApplication.instance(), *QApplication.allWidgets()]:
            sheet = widget.styleSheet().lower()
            leaks += [(type(widget).__name__, colour) for colour in stale if colour in sheet]
        self.assertEqual(leaks, [])
        # The switch art, swapped for the new theme's copy.
        self.assertIn("/taiko/switch-on.svg", window.styleSheet().replace("\\", "/"))
        # Built from a computed colour, so retheme() has to redo it.
        self.assertNotEqual(pill.styleSheet(), osu_pill)
        self.assertEqual(pill.styleSheet(), theme.css(gui.mod_pill_style("HD")))
        window.close()
        window.deleteLater()

    def test_a_switch_back_restores_the_sheet_as_written(self) -> None:
        button = QPushButton()
        button.setStyleSheet("QPushButton { background: #f3a6bd; }")
        theme.switch("taiko")
        self.assertEqual(button.styleSheet(), "QPushButton { background: #d4432a; }")
        theme.switch(theme.AS_WRITTEN)
        self.assertEqual(button.styleSheet(), "QPushButton { background: #f3a6bd; }")


class ChromeTests(unittest.TestCase):
    def test_only_a_word_too_wide_gets_break_points(self) -> None:
        from PySide6.QtGui import QFont, QFontMetrics
        metrics = QFontMetrics(QFont())
        width = metrics.horizontalAdvance("Inner Oni")
        self.assertEqual(gui.breakable_words("Inner Oni", metrics, width), "Inner Oni")
        long = gui.breakable_words("worldwidesuperstar x", metrics, width)
        self.assertIn(gui.ZERO_WIDTH_SPACE, long)
        self.assertTrue(long.endswith(" x"))
        self.assertEqual(long.replace(gui.ZERO_WIDTH_SPACE, ""), "worldwidesuperstar x")

    def test_control_strip_paints_its_own_ground(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QHBoxLayout
        holder = gui.opaque_strip(QHBoxLayout())
        self.assertTrue(holder.testAttribute(Qt.WA_StyledBackground))
        self.assertNotEqual(holder.property("seeThrough"), True)


class TimingBarLengthTests(unittest.TestCase):
    def test_an_edit_does_not_squeeze_the_bar_to_the_last_note(self) -> None:
        # can you hear my voice: last object 170343ms, decoded track 170793ms.
        # Each edit set the first and the info timer the second, so a green
        # line drag rescaled the bar on every step.
        bar = gui.TimingOverviewBar()
        bar.set_duration(170793)
        bar.apply_document_data((170343, [], [], [], None, []))
        self.assertEqual(bar.duration_ms, 170793)
        bar.set_duration(170793)
        self.assertEqual(bar.duration_ms, 170793)
        # And a document longer than its audio still fits on the bar.
        bar.apply_document_data((180000, [], [], [], None, []))
        self.assertEqual(bar.duration_ms, 180000)


class SvLabelTests(unittest.TestCase):
    def test_label_drops_below_a_point_at_the_top(self) -> None:
        # A point on a 7px top margin (a gimmick band at 100% volume) with an
        # 11px ascent: above it the text would start at y=-8.
        self.assertGreaterEqual(gui.SVEditorView._label_baseline(7.0, 11) - 11, 0)
        # With room, it stays above the point, as before.
        self.assertEqual(gui.SVEditorView._label_baseline(60.0, 11), 56.0)


if __name__ == "__main__":
    unittest.main()
