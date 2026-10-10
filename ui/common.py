"""Helpers and constants shared by the main window and the modules split out of it."""
from __future__ import annotations
import sys
from pathlib import Path
from PySide6.QtCore import QRect
from PySide6.QtGui import QFont, QFontMetrics, QIcon
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton


def resource_roots() -> list[Path]:
    """Return only trusted application roots, never the process working directory."""
    roots=[]
    if hasattr(sys,"_MEIPASS"):
        roots.append(Path(sys._MEIPASS).resolve())
    roots.append(Path(__file__).resolve().parents[1])
    unique=[]
    for root in roots:
        if root not in unique:unique.append(root)
    return unique


def application_icon() -> QIcon:
    relative_candidates=(
        Path("assets/icons/FancyTaikoEditor_Logo.ico"),
        Path("assets/icons/FancyTaikoEditor_Logo.png"),
        Path("assets/FancyTaikoEditor_Logo.ico"),
        Path("FancyTaikoEditor_Logo.ico"),
        Path("icon.ico"),
    )
    for root in resource_roots():
        for relative in relative_candidates:
            candidate=root/relative
            if candidate.is_file():
                icon=QIcon(str(candidate))
                if not icon.isNull():return icon
    return QIcon()


def button_chrome_width(button: QPushButton) -> int:
    """Pixels `button` spends on padding and border before any text at all.

    Measured, not assumed: the gap between a rect and the contents rect the
    button's *own current style* hands back for it, which is where the window
    stylesheet's `padding: 8px 16px` and any per-button override of it
    actually show up. Measured against a rect widened by the label, because
    the buttons this exists for are exactly the ones currently too narrow to
    hold their own padding, and a style clamps the contents rect at zero
    rather than reporting the overflow.

    **In the checked state as well as the current one**, which is the half of
    the clipping report that survived every previous fix. `QPushButton:checked`
    is `border: 1px solid` against the base rule's `border: 0`, so checking a
    button costs it two pixels of chrome that were never in the width it was
    pinned to -- and a tool row's selected button is the one every gimmick
    layer is looking at. `button_text_width` already measured the checked
    *font*; this is the other thing that rule changes.
    """
    button.ensurePolished()
    option = QStyleOptionButton()
    option.initFrom(button)
    text_width = button.fontMetrics().horizontalAdvance(button.text())
    height = max(1, option.rect.height())
    width = option.rect.width() + text_width
    states = [option.state]
    if button.isCheckable():
        states.append(option.state | QStyle.StateFlag.State_On)
    widest = 0
    for state in states:
        option.state = state
        option.rect = QRect(0, 0, width, height)
        contents = button.style().subElementRect(QStyle.SE_PushButtonContents, option, button)
        widest = max(widest, width - contents.width())
    return max(0, widest)


def button_text_width(button: QPushButton) -> int:
    """The width `button` needs before its own body starts covering its label.

    This is the fix for the typed widths that used to clip. A 28px "+" under
    the app-wide 16px of padding either side has -4px left for the glyph, so
    Qt drew the pink body straight over both ends of it -- the "buttons eat
    their own text at the left and right" report -- and every attempt to nudge
    the number was a guess at a total the stylesheet already decides. Both
    halves are read back instead: the label from the button's font metrics,
    the padding and border from its style. A longer translation, a bigger font
    or a different padding each widen the button by exactly their cost.

    sizeHint() is still the floor, so an icon, a menu indicator or a style
    minimum is never sized away.

    A second cause layers on top of the first for any checkable button: the
    `QPushButton:checked` rule bumps `font-weight` to 700, but this function
    was measuring with the button's own (unchecked, lighter) font -- so the
    width was pinned before the button ever got heavier, and checking it
    clipped the label again. The advance is now measured with the current
    font AND a bold copy of it, and the wider of the two wins.
    """
    metrics_width = button.fontMetrics().horizontalAdvance(button.text())
    bold_width = 0
    if button.isCheckable():  # only a checked button is drawn bold
        bold_font = QFont(button.font())
        bold_font.setWeight(QFont.Weight.Bold)
        bold_width = QFontMetrics(bold_font).horizontalAdvance(button.text())
    return max(
        button.sizeHint().width(),
        max(metrics_width, bold_width) + button_chrome_width(button),
    )


def equalize_button_widths(buttons, heights: bool = False, widths: bool = True) -> None:
    """Give every button the width of the widest one.

    Polished first: before the style has been applied a button reports the
    unpadded hint, and the widest label is exactly the one that then no longer
    fits. The width itself comes from `button_text_width`, which measures the
    label and the real padding separately rather than trusting that hint.

    `heights` does the same for the tallest, and is how the tool rows are sized:
    a typed height clipped descenders once the window stylesheet's padding was
    applied, and the hint is the one number that already accounts for the font,
    the padding and the border together.

    `widths=False` keeps every button at its own natural width and equalizes
    only the height. Uniform width is worth having while a row is a handful of
    short labels, and unaffordable once it is not: the gimmick fake slider row
    reached ten buttons, and giving all of them the width of "Multiple Fake
    Slider" -- across all six layers, since they are sized as one set -- came
    to 2798px, which is wider than the monitor. A ragged row that fits beats a
    tidy one you cannot reach the end of.
    """
    for button in buttons:
        button.ensurePolished()
    hints = [button.sizeHint() for button in buttons]
    # button_text_width rather than the hint: the hint is what let the widest
    # label clip, since it is the one that then has no padding left over.
    width = max((button_text_width(button) for button in buttons), default=0)
    height = max((hint.height() for hint in hints), default=0)
    for button in buttons:
        if widths:
            button.setFixedWidth(width)
        else:
            # Not "leave it to sizeHint". Qt measures that hint in the font the
            # button reports, and under a stylesheet a *checked* button is
            # painted at font-weight 700 while `font()` still says 600 -- so
            # the hint is a couple of pixels short of what the selected button
            # in every tool row is actually drawn with, and it clipped the last
            # letter of its own label. A floor rather than a fixed width, so a
            # ragged row still is one.
            button.setMinimumWidth(button_text_width(button))
        if heights:
            button.setFixedHeight(height)


# Kiai flash. Notes in a kiai section brighten on every 1/1 beat and fade out
# across it. Stacked objects -- a fake slider dropped on top of a note -- each
# paint their own overlay, so a stack reads brighter than a lone note without
# anything having to count them. Drumroll yellow rather than white: the shine
# mappers stack fake sliders for is that yellow showing through.
# The note colours, and the ones every note part is tinted from. One place
# each, because the editor timeline and the gameplay preview draw the same
# objects and had drifted to four slightly different values between them.
DON_COLOR = (229, 76, 46)


KAT_COLOR = (67, 141, 171)


# The drumroll yellow, and the colour every drumroll part is tinted from --
# head, `taiko-roll-middle` and `taiko-roll-end` alike. One constant because
# the editor timeline and the gameplay preview draw the same object and had
# drifted to two slightly different yellows.
DRUMROLL_COLOR = (251, 183, 6)


def format_time(time_ms: int) -> str:
    value = max(0, int(time_ms))
    return f"{value // 60000:02d}.{(value % 60000) // 1000:02d}.{value % 1000:03d}"
