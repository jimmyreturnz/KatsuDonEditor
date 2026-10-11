from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTranslator

from app.security_utils import resource_roots
from app.settings import SettingsManager

_TRANSLATOR: QTranslator | None = None


def translation_file(language_code: str, suffix: str) -> Path | None:
    filename = f"taiko_{language_code}.{suffix}"
    for root in resource_roots():
        candidate = root / "translations" / filename
        if candidate.is_file():
            return candidate
    return None


class TsTranslator(QTranslator):
    """Safe source-tree fallback when a compiled QM catalog is unavailable.

    Release builds should still ship the compiled QM file. This fallback keeps
    localization functional in recovered/source distributions that contain the
    reviewed TS catalog but not its generated binary.
    """

    def __init__(self, ts_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._messages: dict[tuple[str, str], str] = {}
        root = ET.parse(ts_path).getroot()
        for context_node in root.findall("context"):
            context = context_node.findtext("name", default="")
            if not context:
                continue
            for message in context_node.findall("message"):
                source = message.findtext("source", default="")
                translation_node = message.find("translation")
                if not source or translation_node is None:
                    continue
                if translation_node.get("type") in {"unfinished", "vanished", "obsolete"}:
                    continue
                translated = "".join(translation_node.itertext()).strip()
                if translated:
                    self._messages[(context, source)] = translated

    def isEmpty(self) -> bool:  # noqa: N802 - Qt virtual method name
        return not self._messages

    def translate(self, context, source_text, disambiguation=None, n=-1):
        del disambiguation, n
        return self._messages.get((str(context), str(source_text)), "")


def install_translator(app, settings: SettingsManager | None = None) -> str:
    """Install the persisted translator before UI construction.

    Prefer a compiled QM catalog. If the recovered source package has only the
    TS catalog, install the deterministic TS fallback instead of silently
    returning to English.
    """
    global _TRANSLATOR
    settings = settings or SettingsManager()
    language = settings.string_value("language/current", "en")
    if language not in {"en", "ja"}:
        language = "en"
    if _TRANSLATOR is not None:
        app.removeTranslator(_TRANSLATOR)
        _TRANSLATOR = None
    if language == "en":
        return "en"

    qm_path = translation_file(language, "qm")
    if qm_path is not None:
        translator = QTranslator(app)
        if translator.load(str(qm_path)):
            app.installTranslator(translator)
            _TRANSLATOR = translator
            return language

    ts_path = translation_file(language, "ts")
    if ts_path is not None:
        try:
            translator = TsTranslator(ts_path, app)
        except (ET.ParseError, OSError):
            return "en"
        if not translator.isEmpty():
            app.installTranslator(translator)
            _TRANSLATOR = translator
            return language

    return "en"


def tr(context: str, text: str) -> str:
    return QCoreApplication.translate(context, text)
