"""NSO's desktop: the decorations a theme of window chrome draws.

Needy Streamer Overload's desktop (owner, 2026-10-11) is a pastel OS: a
pink-and-blue diagonal stripe for a wallpaper with stickers on it, and
windows on it -- here light pink, outlined and titled in heart pink. The colours are
`theme`'s, sampled from the game's own window and wallpaper art; this module
holds what is drawn rather than recoloured.

The stickers are drawn here, from bitmaps written for this app -- hearts,
stars, moons, sparkles, bows -- and not taken from the game.
"""
from __future__ import annotations

import random

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import QDockWidget, QWidget

from app.security_utils import resource_roots
from ui import theme

STRIPE_A = "#bbddff"
STRIPE_B = "#eeaaff"
STRIPE_WIDTH = 34
# A whole number of stripe periods, so the tile repeats seamlessly; large
# enough that the stickers on it do not read as a grid (at 408px they did).
TILE = STRIPE_WIDTH * 2 * 11
STICKERS = 22

# 1 = outline, 2 = fill, 3 = shine. Drawn at PIXEL px per bitmap pixel.
SPRITES = {
    "heart": (["0110110", "1221221", "1232221", "0122210", "0012100", "0001000"],
              ("#c2185b", "#ff7baf", "#ffebfa")),
    "star": (["000010000", "000121000", "000121000", "111232111", "122222221",
              "012222210", "001222100", "012212210", "121101121", "110000011"],
             ("#c2185b", "#ffd355", "#ffffff")),
    "moon": (["0011100", "0122100", "1221000", "1221000", "1221000", "0122110", "0011100"],
             ("#c2185b", "#fce2ba", "#ffffff")),
    "spark": (["00100", "00300", "13331", "00300", "00100"],
              ("#c2185b", "#ffffff", "#ffffff")),
    "bow": (["11000000011", "12100000121", "12211111221", "12223332221",
             "12211111221", "12100000121", "11000000011"],
            ("#c2185b", "#ff7baf", "#ffebfa")),
}


# Window chrome, as pink-theme keys so the theme map hands NSO its own.
OUTLINE = "#ff66aa"       # focus: NSO's heart pink
TITLE_BAR = "#2a3341"     # raised: NSO's pink
TITLE_INK = "#e8edf3"     # text: NSO's ink
BUTTON_FACE = "#11151c"   # lane step in the chrome map: NSO's palest pink


def active() -> bool:
    """Whether the theme in force draws window chrome (NSO)."""
    return theme.active() in theme.TITLE_FONTS


_title_family: dict[str, str | None] = {}


def title_font(pixel_size: int = 8) -> QFont:
    """The theme's title face (Press Start 2P for NSO), at a multiple of its
    8px grid so the pixels land on pixels."""
    name = theme.TITLE_FONTS.get(theme.active(), "")
    if name not in _title_family:
        family = None
        for root in resource_roots():
            path = root / "assets" / "fonts" / name
            if name and path.is_file():
                font_id = QFontDatabase.addApplicationFont(str(path))
                families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
                family = families[0] if families else None
                break
        _title_family[name] = family
    font = QFont(_title_family[name]) if _title_family[name] else QFont()
    font.setPixelSize(pixel_size)
    font.setStyleStrategy(QFont.NoAntialias)
    return font


class TitleBar(QWidget):
    """A dock's title as a Windose window's: lilac, outlined, the name in the
    pixel face, and two pixel buttons that float and close the dock."""

    BUTTON = 16

    def __init__(self, dock: QDockWidget) -> None:
        super().__init__(dock)
        self.dock = dock
        self.setFixedHeight(26)
        self.setMouseTracking(True)
        self._hover: str | None = None
        dock.windowTitleChanged.connect(self.update)

    def sizeHint(self) -> QSize:
        return QSize(120, 26)

    def _buttons(self) -> dict[str, QRect]:
        features = self.dock.features()
        rects = {}
        right = self.width() - 6
        for name, flag in (("close", QDockWidget.DockWidgetClosable), ("float", QDockWidget.DockWidgetFloatable)):
            if features & flag:
                rects[name] = QRect(right - self.BUTTON, (self.height() - self.BUTTON) // 2, self.BUTTON, self.BUTTON)
                right -= self.BUTTON + 4
        return rects

    def mouseMoveEvent(self, event) -> None:
        hover = next((name for name, rect in self._buttons().items() if rect.contains(event.position().toPoint())), None)
        if hover != self._hover:
            self._hover = hover
            self.update()
        # Unhandled, so a drag on the bar still moves the dock.
        event.ignore()

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        for name, rect in self._buttons().items():
            if rect.contains(event.position().toPoint()) and event.button() == Qt.LeftButton:
                if name == "close":
                    self.dock.close()
                else:
                    self.dock.setFloating(not self.dock.isFloating())
                event.accept()
                return
        event.ignore()

    def mousePressEvent(self, event) -> None:
        if any(rect.contains(event.position().toPoint()) for rect in self._buttons().values()):
            event.accept()
            return
        event.ignore()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        outline = theme.color(OUTLINE)
        painter.fillRect(self.rect(), theme.color(TITLE_BAR))
        painter.setPen(QPen(outline, 2))
        painter.drawRect(self.rect().adjusted(1, 1, -1, -1))
        heart = sprite("heart", 2)
        painter.drawPixmap(8, (self.height() - heart.height()) // 2, heart)
        painter.setFont(title_font(8))
        painter.setPen(theme.color(TITLE_INK))
        buttons = self._buttons()
        text_right = min((rect.left() for rect in buttons.values()), default=self.width()) - 6
        text_rect = QRect(8 + heart.width() + 6, 0, max(0, text_right - 8 - heart.width() - 6), self.height())
        title = painter.fontMetrics().elidedText(self.dock.windowTitle().replace("&&", "&"), Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, title)
        for name, rect in buttons.items():
            painter.fillRect(rect, theme.color(OUTLINE) if self._hover == name else theme.color(BUTTON_FACE))
            painter.setPen(QPen(outline, 2))
            painter.drawRect(rect.adjusted(1, 1, -1, -1))
            glyph = theme.color(BUTTON_FACE) if self._hover == name else outline
            c = rect.center()
            if name == "close":
                for d in range(-3, 4):
                    painter.fillRect(c.x() + d, c.y() + d, 2, 2, glyph)
                    painter.fillRect(c.x() + d, c.y() - d, 2, 2, glyph)
            else:
                painter.setPen(QPen(glyph, 2))
                painter.drawRect(QRect(c.x() - 3, c.y() - 3, 7, 7))


def dress_docks(docks) -> None:
    """Give `docks` Windose title bars under a theme with window chrome, and
    Qt's own back under any other. Run at build and on every theme switch."""
    for dock in docks:
        wanted = active()
        current = dock.titleBarWidget()
        if wanted and not isinstance(current, TitleBar):
            dock.setTitleBarWidget(TitleBar(dock))
        elif not wanted and isinstance(current, TitleBar):
            dock.setTitleBarWidget(None)
            current.deleteLater()


# Added to the dock area's sheet under window chrome: the dock's content
# outlined like a Windose window body, its title bar sitting on top.
DOCK_CHROME = (
    "QDockWidget > QWidget { border: 2px solid #ff66aa; border-top: 0; }"
)


def sprite(name: str, pixel: int) -> QPixmap:
    rows, colours = SPRITES[name]
    pixmap = QPixmap(len(rows[0]) * pixel, len(rows) * pixel)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    for y, row in enumerate(rows):
        for x, value in enumerate(row):
            if value != "0":
                painter.fillRect(x * pixel, y * pixel, pixel, pixel, QColor(colours[int(value) - 1]))
    painter.end()
    return pixmap


_tile: QPixmap | None = None


def wallpaper_tile() -> QPixmap:
    """One seamless square of the wallpaper. Built once: the colours are the
    game's and not theme keys, so no theme switch changes it."""
    global _tile
    if _tile is not None:
        return _tile
    tile = QPixmap(TILE, TILE)
    tile.fill(QColor(STRIPE_A))
    painter = QPainter(tile)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(STRIPE_B))
    # Stripes along x + y: every second band of STRIPE_WIDTH, drawn twice the
    # tile wide so the bands crossing a corner are whole.
    for start in range(-TILE, TILE * 2, STRIPE_WIDTH * 2):
        painter.drawPolygon(QPolygon([
            QPoint(start, 0), QPoint(start + STRIPE_WIDTH, 0),
            QPoint(start + STRIPE_WIDTH - TILE, TILE), QPoint(start - TILE, TILE),
        ]))
    # Stickers on a fixed seed, kept a sticker's width from the edges so none
    # is cut where the tile repeats.
    # Placed on a jittered grid rather than at random, so no two crowd each
    # other and no stretch of the floor goes bare.
    rng = random.Random(7)
    names = ("heart", "star", "moon", "spark", "bow", "heart", "star", "spark")
    side = round(STICKERS ** 0.5)
    cell = TILE // side
    for index in range(side * side):
        art = sprite(names[(index * 5) % len(names)], rng.choice((3, 3, 4, 5)))
        cx, cy = (index % side) * cell, (index // side) * cell
        x = min(TILE - art.width() - 4, max(4, cx + rng.randint(0, max(0, cell - art.width()))))
        y = min(TILE - art.height() - 4, max(4, cy + rng.randint(0, max(0, cell - art.height()))))
        painter.drawPixmap(x, y, art)
    painter.end()
    _tile = tile
    return tile


def paint_wallpaper(painter: QPainter, rect: QRect) -> None:
    painter.drawTiledPixmap(rect, wallpaper_tile(), rect.topLeft())


_tile_file: str | None = None


def wallpaper_url() -> str:
    """The tile as a file, for a stylesheet's background-image: written once
    beside the theme's recoloured SVGs (theme._themed_svg)."""
    global _tile_file
    if _tile_file is None:
        import tempfile
        target = Path(tempfile.gettempdir()) / "TaikoFancyArranger-theme" / "nso-wallpaper.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        wallpaper_tile().save(str(target), "PNG")
        _tile_file = target.as_posix()
    return _tile_file


def app_chrome() -> str:
    """The window as the desktop: the wallpaper in the gutters around the
    pages, and the header a box on it."""
    return f"""
        QWidget#appRoot {{ background-image: url({wallpaper_url()}); }}
        QFrame#appHeader {{ border: 2px solid #ff66aa; padding: 3px 6px; }}
    """


def dress_title_bar(window: QWidget) -> None:
    """The native title bar in the theme's colours (Windows 11's DWM), or
    back to the system's under a theme without window chrome. A no-op on
    any other platform or an older Windows."""
    import sys
    if sys.platform != "win32":
        return
    import ctypes
    # DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR; 0xFFFFFFFF
    # is DWMWA_COLOR_DEFAULT.
    def colorref(key: str) -> int:
        c = theme.color(key)
        return c.red() | c.green() << 8 | c.blue() << 16
    wanted = active()
    # Not even a window handle under any other theme, unless NSO set colours
    # there to take back: winId() makes the window native before it shows.
    if not wanted and not window.property("titleBarDressed"):
        return
    window.setProperty("titleBarDressed", wanted)
    for attribute, key in ((34, OUTLINE), (35, TITLE_BAR), (36, TITLE_INK)):
        value = ctypes.c_uint(colorref(key) if wanted else 0xFFFFFFFF)
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(window.winId()), attribute, ctypes.byref(value), ctypes.sizeof(value))
        except (AttributeError, OSError):
            return


def library_chrome() -> str:
    """The Songs page as the desktop: the wallpaper behind it, the plain
    containers between it and the panels let it through, and the panels
    outlined square like Windose windows. Pink-theme keys, as every sheet."""
    return f"""
        QWidget#libraryPage {{ background-image: url({wallpaper_url()}); }}
        QWidget#libraryColumn, QWidget#continueRow, QSplitter#librarySplit {{ background: transparent; }}
        QSplitter#librarySplit::handle {{ background: transparent; }}
        /* Captions and the status line sit on the wallpaper as little tags. */
        QWidget#continueRow > QLabel, QWidget#libraryColumn > QLabel, QWidget#libraryPage > QLabel {{
            background: #2a3341; border: 2px solid #ff66aa; padding: 2px 6px;
        }}
        QToolButton#folderMenu {{ background: #191f29; border: 2px solid #ff66aa; border-radius: 0; }}
        QListWidget, QFrame#songDetail, QFrame#continueCard {{
            border: 2px solid #ff66aa; border-radius: 0;
        }}
        QFrame#songDetail QListWidget {{ border: 0; }}
        QLineEdit {{ border: 2px solid #ff66aa; border-radius: 0; }}
    """


if __name__ == "__main__":
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication([])
    image = wallpaper_tile().toImage()
    # Seamless: the stripes depend on x + y only, so the pixel past the right
    # edge, (TILE, y), is the left edge's (0, y). Its diagonal neighbour on
    # this side of the seam is (TILE - 1, y + 1), the same x + y as (0, y).
    # The stickers keep 4px clear of every edge, so these are stripe pixels.
    seam = [(image.pixelColor(TILE - 1, y + 1).name(), image.pixelColor(0, y).name()) for y in range(TILE - 1)]
    assert all(a == b for a, b in seam), sum(a != b for a, b in seam)
    print("ok", TILE)
