from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QDoubleSpinBox,
    QVBoxLayout, QWidget,
)

from ui import theme
from chart.image_to_drawing import TraceOptions, fit_strokes, trace_image


MODE_HELP = {
    "dark": "Dark Lines: for black or dark artwork on a white/light background. Best default for sketches, scanned line art, and most logos.",
    "light": "Light Lines: for white or bright artwork on a black/dark background, such as glowing symbols or inverted line art.",
    "alpha": "Alpha Outline: for transparent PNGs. Traces the boundary between visible and transparent pixels, ideal for silhouettes, icons, and exported logos.",
}


class TracePreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.strokes = []
        self.setMinimumSize(620, 360)

    def set_strokes(self, strokes):
        self.strokes = strokes
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.color("#11151c"))
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(theme.color("#f3a6bd"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        for stroke in fit_strokes(self.strokes, self.width(), self.height(), 14):
            for first, second in zip(stroke, stroke[1:]):
                painter.drawLine(QPointF(*first), QPointF(*second))


class ImageTraceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Import Image as Drawing"))
        self.resize(780, 700)
        self.source_path = None
        self.accepted_strokes = []
        root = QVBoxLayout(self)
        chooser = QHBoxLayout()
        self.path_label = QLabel(self.tr("No image selected"))
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        choose_button = QPushButton(self.tr("Choose Image..."))
        choose_button.clicked.connect(self.choose_image)
        chooser.addWidget(self.path_label, 1)
        chooser.addWidget(choose_button)
        root.addLayout(chooser)
        self.preview = TracePreview()
        root.addWidget(self.preview, 1)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem(self.tr("Dark Lines"), "dark")
        self.mode.addItem(self.tr("Light Lines"), "light")
        self.mode.addItem(self.tr("Alpha Outline"), "alpha")
        self.mode_help = QLabel()
        self.mode_help.setWordWrap(True)
        self.mode_help.setStyleSheet("color: #aeb8c5; padding: 4px 0;")
        self.threshold = QSpinBox(); self.threshold.setRange(0, 255); self.threshold.setValue(128)
        self.minimum = QSpinBox(); self.minimum.setRange(4, 100000); self.minimum.setValue(12)
        self.simplify = QDoubleSpinBox(); self.simplify.setRange(0.0, 50.0); self.simplify.setValue(2.0); self.simplify.setSingleStep(0.5)
        self.invert = QCheckBox(self.tr("Invert"))
        form.addRow(self.tr("Trace Mode"), self.mode)
        form.addRow("", self.mode_help)
        form.addRow(self.tr("Threshold"), self.threshold)
        form.addRow(self.tr("Minimum Outline Length"), self.minimum)
        form.addRow(self.tr("Simplification"), self.simplify)
        form.addRow("", self.invert)
        root.addLayout(form)
        settings_help = QLabel(self.tr(
            "Threshold controls which pixels count as lines. Minimum Outline Length removes tiny noise loops. "
            "Simplification reduces point count after tracing; increase it for faster Drawing previews and cleaner outlines."
        ))
        settings_help.setWordWrap(True)
        settings_help.setStyleSheet("color: #aeb8c5; padding: 4px 0;")
        root.addWidget(settings_help)
        refresh = QPushButton(self.tr("Refresh Preview"))
        refresh.clicked.connect(self.refresh_preview)
        root.addWidget(refresh)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText(self.tr("Import into Drawing"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()

    def _mode_changed(self):
        mode = str(self.mode.currentData() or "dark")
        self.mode_help.setText(self.tr(MODE_HELP[mode]))
        self.threshold.setEnabled(True)

    def choose_image(self):
        filename, _ = QFileDialog.getOpenFileName(self, self.tr("Choose Image"), "", self.tr("Images (*.png *.jpg *.jpeg *.webp *.bmp)"))
        if filename:
            self.source_path = Path(filename)
            self.path_label.setText(self.source_path.name)
            self.refresh_preview()

    def options(self):
        return TraceOptions(self.mode.currentData(), self.threshold.value(), self.minimum.value(), self.simplify.value(), self.invert.isChecked())

    def refresh_preview(self):
        if self.source_path is None:
            return
        try:
            self.accepted_strokes = trace_image(self.source_path, self.options())
        except Exception as error:
            self.accepted_strokes = []
            self.preview.set_strokes([])
            QMessageBox.warning(self, self.tr("Image tracing failed"), str(error))
            return
        self.preview.set_strokes(self.accepted_strokes)

    def accept(self):
        if not self.accepted_strokes:
            self.refresh_preview()
        if not self.accepted_strokes:
            return
        super().accept()