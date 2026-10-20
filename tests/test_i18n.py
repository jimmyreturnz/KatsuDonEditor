"""Localization regression tests.

These exist because Japanese coverage broke repeatedly in a specific way: the
reviewed translations stayed in taiko_ja.ts while the source stopped looking
them up, so controls silently reverted to English with the catalog still
reporting 100% translated.

The tests therefore check both directions:

* every visible label the application can produce is present in the catalog
  and actually resolves to Japanese at runtime, and
* every stable internal identifier stays untranslated.

None of them assert on source text with string matching, so they keep working
when the surrounding code is reformatted.
"""
from __future__ import annotations

import ast
import os
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTranslator
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
TS_PATH = ROOT / "translations" / "taiko_ja.ts"

# Files whose visible strings are looked up through explicit contexts.
SOURCE_FILES = ("ui/gui.py", "ui/settings_dialog.py", "ui/image_trace_dialog.py", "app/updater.py",
                "audio/offset_calibration.py", "ui/config_sheet.py",
                "ui/dialogs.py", "ui/common.py", "ui/misc_page.py")

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class FakeSettings:
    """Stands in for SettingsManager so tests never touch real QSettings."""

    def __init__(self, language: str) -> None:
        self._language = language

    def string_value(self, key: str, default: str = "") -> str:
        return self._language if key == "language/current" else default


def load_catalog() -> dict[str, dict[str, str]]:
    """Return {context: {source: translation}} for live (non-vanished) messages."""
    catalog: dict[str, dict[str, str]] = {}
    root = ET.parse(TS_PATH).getroot()
    for context_node in root.findall("context"):
        name = context_node.findtext("name", "")
        entries: dict[str, str] = {}
        for message in context_node.findall("message"):
            node = message.find("translation")
            if node is None or node.get("type") in {"vanished", "obsolete"}:
                continue
            entries[message.findtext("source", "")] = "".join(node.itertext()).strip()
        catalog[name] = entries
    return catalog


def literal_lookups() -> set[tuple[str, str]]:
    """Collect (context, source) pairs from literal translation calls in source.

    Handles both tr("Context", "text") and self.tr("text"), where the latter's
    context is the enclosing class name, matching Qt's own behavior.
    """
    found: set[tuple[str, str]] = set()
    for filename in SOURCE_FILES:
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
        enclosing: dict[ast.AST, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in ast.walk(node):
                    enclosing.setdefault(child, node.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_self_tr = isinstance(func, ast.Attribute) and func.attr == "tr"
            is_plain_tr = isinstance(func, ast.Name) and func.id == "tr"
            if not (is_self_tr or is_plain_tr):
                continue
            args = node.args
            if is_plain_tr and len(args) >= 2:
                if isinstance(args[0], ast.Constant) and isinstance(args[1], ast.Constant):
                    found.add((args[0].value, args[1].value))
            elif is_self_tr and len(args) == 1 and isinstance(args[0], ast.Constant):
                context = enclosing.get(node)
                if context:
                    found.add((context, args[0].value))
    return found


def parameter_labels() -> tuple[set[str], set[str]]:
    """Return (labels, choice labels) across every live parameter definition.

    Importing gui applies the definitions it injects into PARAMETERS, so this
    covers both parameters.py originals and gui.py additions such as Font and
    Notes per Drawing.
    """
    from ui import gui  # noqa: F401  (import for its PARAMETERS side effects)
    from chart.parameters import PARAMETERS

    labels: set[str] = set()
    choices: set[str] = set()
    for definitions in PARAMETERS.values():
        for definition in definitions:
            labels.add(str(definition["label"]))
            for label, _value in definition.get("choices", []):
                choices.add(str(label))
    return labels, choices


class CatalogIntegrityTests(unittest.TestCase):
    def test_every_live_message_is_translated(self):
        empty = [
            (context, source)
            for context, entries in load_catalog().items()
            for source, value in entries.items()
            if not value
        ]
        self.assertEqual(empty, [], f"untranslated catalog entries: {empty}")

    def test_contexts_are_limited_to_real_runtime_owners(self):
        allowed = {
            "MainWindow",
            "Parameters",
            "Transformations",
            "DrawingDialog",
            "ImageTraceDialog",
            "SettingsDialog",
            "OffsetCalibrationDialog",
        }
        self.assertLessEqual(set(load_catalog()), allowed)

    def test_compiled_catalog_matches_the_source_catalog(self):
        """Compare content rather than timestamps, which git checkouts destroy."""
        qm = ROOT / "translations" / "taiko_ja.qm"
        self.assertTrue(qm.is_file(), "taiko_ja.qm missing; run tools/compile_translations.bat")

        translator = QTranslator()
        self.assertTrue(translator.load(str(qm)), "taiko_ja.qm failed to load")

        stale = [
            (context, source)
            for context, entries in load_catalog().items()
            for source, value in entries.items()
            if translator.translate(context, source) != value
        ]
        self.assertEqual(stale, [], f"taiko_ja.qm is out of date for: {stale}")


class CoverageTests(unittest.TestCase):
    """Guards against the exact regression: source and catalog drifting apart."""

    def test_every_literal_lookup_exists_in_the_catalog(self):
        catalog = load_catalog()
        missing = [
            (context, source)
            for context, source in sorted(literal_lookups())
            if source not in catalog.get(context, {})
        ]
        self.assertEqual(missing, [], f"tr() calls with no catalog entry: {missing}")

    def test_every_parameter_label_is_translated(self):
        entries = load_catalog()["Parameters"]
        labels, choices = parameter_labels()
        missing = sorted(label for label in labels | choices if not entries.get(label))
        self.assertEqual(missing, [], f"parameter labels missing Japanese: {missing}")

    def test_every_transformation_name_is_translated(self):
        from ui import gui

        entries = load_catalog()["Transformations"]
        missing = sorted(
            label for label in gui.TRANSFORMATION_LABELS.values() if not entries.get(label)
        )
        self.assertEqual(missing, [], f"transformation names missing Japanese: {missing}")

    def test_every_gui_transformation_has_an_explicit_label(self):
        from ui import gui

        missing = [
            name for name in gui.GUI_TRANSFORMATIONS if name not in gui.TRANSFORMATION_LABELS
        ]
        self.assertEqual(missing, [], f"transformations falling back to .title(): {missing}")


class JapaneseLookupTests(unittest.TestCase):
    """Spot checks for controls that have regressed to English before."""

    def setUp(self):
        from app import i18n

        self.i18n = i18n
        self.assertEqual(i18n.install_translator(_APP, FakeSettings("ja")), "ja")

    def tearDown(self):
        self.i18n.install_translator(_APP, FakeSettings("en"))

    def test_known_translations(self):
        from app.i18n import tr

        expected = {
            ("MainWindow", "Play"): "再生",
            ("MainWindow", "Pause"): "一時停止",
            ("MainWindow", "Playback Rate"): "再生速度",
            ("MainWindow", "All"): "すべて",
            ("MainWindow", "Snap"): "スナップ",
        }
        for (context, source), value in expected.items():
            with self.subTest(source=source):
                self.assertEqual(tr(context, source), value)

    def test_previously_regressed_controls_are_not_english(self):
        from app.i18n import tr

        cases = [
            ("MainWindow", "Reset Applied Transforms"),
            ("MainWindow", "All Notes"),
            ("MainWindow", "Split Don / Kat"),
            ("MainWindow", "Duration"),
            ("MainWindow", "Now"),
            # Parameters defined in parameters.py.
            ("Parameters", "Position X"),
            ("Parameters", "Position Y"),
            ("Parameters", "Traversal"),
            # Parameters injected by gui.py.
            ("Parameters", "Font"),
            ("Parameters", "Notes per Drawing"),
            # Dialogs.
            ("DrawingDialog", "Drawing"),
            ("ImageTraceDialog", "Trace Mode"),
            ("SettingsDialog", "Settings"),
        ]
        for context, source in cases:
            with self.subTest(context=context, source=source):
                translated = tr(context, source)
                self.assertTrue(translated, f"{source} resolved to nothing")
                self.assertNotEqual(translated, source, f"{source} is still English")

    def test_untranslated_source_falls_back_to_english(self):
        from app.i18n import tr

        self.assertEqual(
            tr("MainWindow", "String that is deliberately absent"),
            "String that is deliberately absent",
        )


class TranslatorInstallationTests(unittest.TestCase):
    def tearDown(self):
        from app import i18n

        i18n.install_translator(_APP, FakeSettings("en"))

    def test_invalid_stored_language_falls_back_to_english(self):
        from app import i18n

        self.assertEqual(i18n.install_translator(_APP, FakeSettings("klingon")), "en")

    def test_missing_catalog_files_do_not_crash(self):
        from app import i18n

        original = i18n.translation_file
        i18n.translation_file = lambda language_code, suffix: None
        try:
            self.assertEqual(i18n.install_translator(_APP, FakeSettings("ja")), "en")
        finally:
            i18n.translation_file = original

    def test_english_installs_no_translator(self):
        from app import i18n

        self.assertEqual(i18n.install_translator(_APP, FakeSettings("en")), "en")


class StableIdentifierTests(unittest.TestCase):
    """Internal identifiers must never follow the display language."""

    def tearDown(self):
        from app import i18n

        i18n.install_translator(_APP, FakeSettings("en"))

    def _build_window(self, language: str):
        from app import i18n

        i18n.install_translator(_APP, FakeSettings(language))
        from ui import gui
        from app import settings

        # Test settings are seeded from the real ones, and a real session
        # leaves its Fancy layout there -- one with the Transform dock closed
        # hid the Swap button this test looks for. Start from the default.
        settings.SettingsManager().set_value("fancy/dock_state", "")
        window = gui.MainWindow()
        # Closed after the test: a window still open at exit keeps the audio
        # thread alive into interpreter shutdown, which exits 127 after OK.
        self.addCleanup(window.close)
        window.show()
        return window

    def test_mode_identity_is_language_independent(self):
        for language in ("en", "ja"):
            with self.subTest(language=language):
                window = self._build_window(language)
                # swap_don_kat_button lives on the Fancy Arranger page; a
                # descendant of a page that isn't current reports
                # isVisible() False regardless of its own setVisible calls.
                window.page_stack.setCurrentWidget(window.fancy_arranger_page)
                combo = window.mode_combo
                data = [combo.itemData(i) for i in range(combo.count())]
                self.assertEqual(data, ["all", "split"])

                combo.setCurrentIndex(0)
                self.assertFalse(window._is_split_mode())
                self.assertEqual(window.control_tabs.count(), 1)

                combo.setCurrentIndex(1)
                self.assertTrue(window._is_split_mode())
                self.assertEqual(window.control_tabs.count(), 2)
                self.assertTrue(window.swap_don_kat_button.isVisible())

    def test_transformation_ids_are_language_independent(self):
        identifiers = {}
        for language in ("en", "ja"):
            window = self._build_window(language)
            combo = window.control_tabs.widget(0).combo
            identifiers[language] = [combo.itemData(i) for i in range(combo.count())]
        self.assertEqual(identifiers["en"], identifiers["ja"])
        self.assertIn("drawn_path", identifiers["ja"])

    def test_japanese_window_shows_translated_labels(self):
        window = self._build_window("ja")
        self.assertEqual(window.play_button.text(), "再生")
        self.assertIn("曲の長さ", window.timeline_info.text())
        self.assertEqual(window.mode_combo.itemText(0), "すべてのノーツ")
        self.assertEqual(window.control_tabs.tabText(0), "すべて")


if __name__ == "__main__":
    unittest.main()
