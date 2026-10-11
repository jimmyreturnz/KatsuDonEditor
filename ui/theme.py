"""App colour themes, as a recolouring of the one palette the code is written in.

Every colour in the UI is a literal in the default (pink) theme -- some 300 of
them, across sixty stylesheets and the paint code. A theme is therefore not a
second set of literals but a map from those to its own colours, applied at the
two places a colour reaches Qt from Python:

- every `setStyleSheet` call, by wrapping `QWidget.setStyleSheet` and
  `QApplication.setStyleSheet` once (`install`);
- paint code, through `color()` instead of `QColor("#...")`.

So a new widget written in the pink literals is themed for free, and the pink
theme is the code exactly as written: `install` does nothing for it.

A theme changes live (`switch`, owner 2026-10-08: no restart). The wrapper
keeps each sheet as it was written (the `themeSource` property), so a switch
re-translates every sheet from that rather than from the last theme's output;
paint code reads `color()` as it paints. The few things that compute a colour
once and keep it -- a sheet built from a mixed colour, a cached layer -- are
the window's to redo after a switch (`MainWindow.retheme`).

What a theme leaves alone, deliberately: the note colours (don, kat,
drumroll), osu!'s snap tick colours, the SV layer's green, star-rating colours
and the red of destructive actions. Those mean something in the chart, not
in the chrome, and none of them is a key below.
"""
from __future__ import annotations

import colorsys
import re

from PySide6.QtGui import QColor

SETTING = "appearance/theme"
DEFAULT = "osu"
# Not offered to the user: the colours exactly as the code writes them. The
# test suite runs on this, because what it asserts are those literals -- and
# the default theme stopped being them when its ground became #1f1e33.
AS_WRITTEN = "as-written"
# Saved names from before a rename.
RENAMED = {"pink": "osu", "matsuri": "lantern", "kiai": "gold"}

# The pink palette's own roles. Each theme names a target for the anchors, and
# the colours derived from an anchor (its hover, its gradient stops, the dark
# tints behind a checked tile) follow it by the same hue turn.
_PRIMARY = "#f3a6bd"        # filled buttons: Save, current page, checked tool
_PRIMARY_FAMILY = ("#f7bfd0", "#f7bccd", "#f6b3c7", "#ee97b1")
_INK = "#17191f"            # text on a primary fill
_FOCUS = "#ff66aa"          # rims, slider fills, the focused lane
# The last three are the switch art's (assets/ui/switch-on-*.svg): hover, and
# the disabled track and knob.
_FOCUS_FAMILY = ("#2a2230", "#6b3a55", "#8a4a6a", "#b0587f", "#2a1f2a",
                 "#ff85bb", "#6b4459", "#9a8a93")
_ACCENT_TEXT = "#ff9dcc"    # pink text: values, the difficulty name
# The navy chrome, ground to text. Not #738098 (a snap tick) or #f4f7fb (the
# note outline): those belong to the chart.
_NEUTRALS = (
    "#0d1219", "#11151c", "#2b323d", "#12161d", "#151b24", "#161c25", "#171d26", "#191f29",
    "#1b212b", "#1d2431", "#1e2530", "#1f262f", "#222a36", "#252d39", "#262e3a",
    "#2a3341", "#2f3947", "#303947", "#39414d", "#3a4554", "#465164", "#4a5668",
    "#4d5664", "#56637a", "#7a8492", "#7d8794", "#aeb8c5", "#d5dce5", "#e8edf3",
)

# The navy's own steps, darkest first: lane, panel, ground, surface, raised,
# line, faint text, muted text, text. A theme that changes the ground names
# its own nine (the approved mockup's), and every other navy shade in the code
# is placed between the two steps it sits between.
_NEUTRAL_STEPS = ("#11151c", "#151b24", "#191f29", "#222a36", "#2a3341",
                  "#3a4554", "#7d8794", "#aeb8c5", "#e8edf3")

# The default (osu!) theme's ground since 2026-10-07: #1f1e33, a Camellia song title.
# The rest of the navy follows it, steps in the same order as _NEUTRAL_STEPS.
_PINK_STEPS = ("#151423", "#1a192d", "#1f1e33", "#282740", "#302e4a",
               "#403e5c", "#828198", "#b1b0c8", "#e9e8f4")

# name -> (primary, ink, focus, accent text, the nine steps or None)
THEMES = {
    "osu": (_PRIMARY, _INK, _FOCUS, _ACCENT_TEXT, _PINK_STEPS),
    "taiko": ("#d4432a", "#ffffff", "#5aa7c7", "#ff8f72",
               ("#0d1118", "#10151d", "#141a24", "#1c2430", "#26313f",
                "#34445a", "#7b889b", "#a9b6c6", "#e8edf3")),
    # Lantern Rite: lantern red on a night lit warm by them, gold for the glow.
    "lantern": ("#d9452b", "#fff3e6", "#f0b34a", "#f5c46a",
                ("#120a0a", "#170d0c", "#1d1110", "#291817", "#352120",
                 "#523330", "#9a7d72", "#cdb6a6", "#f6ebdd")),
    "gold": ("#ff9f2e", "#1d1406", "#ff9f2e", "#ffc06b", None),
    # VS Code's Monokai: its green fills, its pink rims, its yellow strings,
    # on #272822 with #f8f8f2 text and the #75715e of a comment.
    "monokai": ("#a6e22e", "#272822", "#f92672", "#e6db74",
                ("#1e1f1c", "#22231f", "#272822", "#3e3d32", "#49483e",
                 "#5b5a4c", "#75715e", "#a59f85", "#f8f8f2")),
    # Needy Streamer Overload's desktop: light pink boxes with black text on
    # the stripe wallpaper (windose), light pink buttons that turn heart pink
    # once checked. Owner, 2026-10-12, after navy text on lilac and then
    # light pink on violet: "light pink instead of purple everywhere".
    "nso": ("#ffc6e3", "#111111", "#ff7baf", "#d6247a",
            ("#fff0f7", "#ffe4f1", "#ffeaf4", "#ffd9eb", "#ffcae3",
             "#f29cc6", "#a0708a", "#5c3a4d", "#111111")),
}

# A theme's own UI face, a file in assets/fonts (SIL OFL, licence beside it).
# NSO's is M PLUS Rounded 1c, which covers kana and kanji too. It was the
# pixel DotGothic16, which cannot be smooth under 16px and whose i and l
# were a pixel apart (owner, 2026-10-12). A "-Regular" file's "-Bold"
# sibling is registered with it. A theme not named here keeps Exo 2.
FONTS = {
    "nso": "MPLUSRounded1c-Regular.ttf",
}
# The face its window titles are set in, where a theme draws window chrome.
TITLE_FONTS = {
    "nso": "PressStart2P-Regular.ttf",
}

# Single colours a theme sets outright, after its steps and families. NSO's
# checked tile is a solid pink; followed from the focus family it kept the
# dark tint's lightness and read as black.
OVERRIDES = {
    "nso": {"#2a2230": "#ff9ccb"},
}

# Where a theme's primary fill is too light to be text, text written in it
# (`color: #f3a6bd`, `text()`) takes another colour: the difficulty name and
# the values were NSO's light pink on its light pink boxes.
TEXT_OVERRIDES = {
    "nso": {"#f3a6bd": "#d6247a"},
}
_TEXT_COLOR = re.compile(r"(?<![-\w])(color\s*:\s*)#([0-9a-fA-F]{6})(?![0-9a-fA-F])")

# What chart content is painted on: lanes, the playfield, the SV graph, the
# gameplay preview's built-in lane. A theme not named here paints its screens
# in its own steps. NSO's are the wallpaper's pastel pink (owner, 2026-10-12:
# "match with nso theme, like pink"), lightest-first like its chrome.
SCREEN_STEPS = {
    "nso": ("#ffe9f6", "#ffd9f0", "#ffe2f3", "#fbd0ea", "#f6c2e3",
            "#e59ccb", "#a0708a", "#5c3a4d", "#111111"),
}

# Chart colours a light screen cannot show, only where chart content is
# painted (`screen`, `screen_rgba`). A dark lane's white barline, playhead and
# note outline become NSO's deep pink; osu!'s palest snap ticks keep their hue a
# shade deeper, so 1/6 is still yellow and 1/7 still aqua on the pink.
SCREEN_OVERRIDES = {
    "nso": {
        "#ffffff": "#c2185b", "#f4f7fb": "#c2185b", "#f2f2f2": "#c2185b",
        "#e1e6f0": "#7a1442",
        "#f2d34f": "#b88400", "#8bd1ff": "#2f86c9", "#55d6be": "#12917c",
        "#ffdc6e": "#d97a00", "#ffd400": "#b88400",
        "#5ae182": "#1f9e55", "#8cffb9": "#1f9e55", "#1c212b": "#ffd9f0",
        "#000000": "#ffd9f0",
    },
}


def _hls(hex_colour: str) -> tuple[float, float, float]:
    value = hex_colour.lstrip("#")
    return colorsys.rgb_to_hls(*(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4)))


def _hex(h: float, l: float, s: float) -> str:
    clamp = lambda x: min(1.0, max(0.0, x))
    r, g, b = colorsys.hls_to_rgb(h % 1.0, clamp(l), clamp(s))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _follow(member: str, anchor: str, target: str, shift_lightness: bool) -> str:
    """`member` moved the way `anchor` moves to `target`.

    The primary family is all light fills, so it shifts lightness with the
    anchor and a hover stays a step lighter than its button. The focus family
    mixes a bright rim with near-black tints, and moving those by the rim's
    lightness change would crush the tints to black -- they keep their own.
    """
    # The target's hue outright, not the member's own offset from pink carried
    # over: a tint 18 degrees off pink is the same pink to the eye, but 18
    # degrees off kat blue is green.
    _mh, ml, ms = _hls(member)
    _ah, al, as_ = _hls(anchor)
    th, tl, ts = _hls(target)
    lightness = ml + (tl - al) if shift_lightness else ml
    saturation = ms * (ts / as_) if as_ else ms
    return _hex(th, lightness, saturation)


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _between_steps(member: str, steps: tuple[str, ...]) -> str:
    """`member` placed among `steps` where it sits among the navy's own.

    Inside the range it is mixed between the two steps either side of it, by
    lightness; past either end it keeps its own offset from the end step. So a
    hover a shade above the surface stays a shade above the theme's surface.
    """
    level = _hls(member)[1]
    levels = [_hls(step)[1] for step in _NEUTRAL_STEPS]
    if level <= levels[0] or level >= levels[-1]:
        end = 0 if level <= levels[0] else -1
        own, base, target = _rgb(member), _rgb(_NEUTRAL_STEPS[end]), _rgb(steps[end])
        return "#%02x%02x%02x" % tuple(min(255, max(0, t + o - b)) for t, o, b in zip(target, own, base))
    upper = next(i for i, step_level in enumerate(levels) if step_level >= level)
    lower = upper - 1
    span = levels[upper] - levels[lower]
    f = 0.0 if span <= 0 else (level - levels[lower]) / span
    a, b = _rgb(steps[lower]), _rgb(steps[upper])
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * f) for x, y in zip(a, b))


def build_map(name: str, screen: bool = False) -> dict[str, str]:
    spec = THEMES.get(name)
    if not spec:
        return {}
    primary, ink, focus, accent, steps = spec
    if screen:
        steps = SCREEN_STEPS.get(name, steps)
    mapping = {}
    # An anchor a theme keeps keeps its family too: `_follow` takes the
    # target's hue outright, so following pink onto pink would still turn
    # every tint a few degrees -- which is not "unchanged".
    if (primary, ink, accent) != (_PRIMARY, _INK, _ACCENT_TEXT):
        mapping.update({_PRIMARY: primary, _INK: ink, _ACCENT_TEXT: accent})
        for member in _PRIMARY_FAMILY:
            mapping[member] = _follow(member, _PRIMARY, primary, True)
    if focus != _FOCUS:
        mapping[_FOCUS] = focus
        for member in _FOCUS_FAMILY:
            mapping[member] = _follow(member, _FOCUS, focus, False)
    if steps is not None:
        for member in _NEUTRALS:
            mapping[member] = _between_steps(member, steps)
    mapping.update(OVERRIDES.get(name, {}))
    if screen:
        mapping.update(SCREEN_OVERRIDES.get(name, {}))
    return mapping


_HEX = re.compile(r"#([0-9a-fA-F]{6})(?=[0-9a-fA-F]{2}\b|\b)")
_FOCUS_RGBA = re.compile(r"rgba\(\s*255\s*,\s*102\s*,\s*170\s*,")
# An SVG a sheet draws: its colours are in the file, so the file is swapped
# for the theme's copy (svg_asset) as the sheet is translated. Done here, not
# by whoever builds the sheet, so the kept source names the as-written file
# and every later theme gets its own copy.
_SVG_URL = re.compile(r"url\(\s*([\"']?)([^)\"']+\.svg)\1\s*\)", re.IGNORECASE)
_svg_copies: dict[tuple[str, str], str] = {}

_active = DEFAULT
_map: dict[str, str] = {}
_screen_map: dict[str, str] = {}
_focus_rgb = "255, 102, 170"


def css(text: str) -> str:
    """`text` with every pink-theme colour in it swapped for the active theme's."""
    if not _map or not text:
        return text
    text = _SVG_URL.sub(lambda m: f"url({m.group(1)}{svg_asset(m.group(2))}{m.group(1)})", text)
    overrides = TEXT_OVERRIDES.get(_active)
    if overrides:
        text = _TEXT_COLOR.sub(lambda m: m.group(1) + overrides.get("#" + m.group(2).lower(), "#" + m.group(2)), text)
    text = _HEX.sub(lambda m: _map.get("#" + m.group(1).lower(), m.group(0)), text)
    return _FOCUS_RGBA.sub(f"rgba({_focus_rgb},", text)


def text(value) -> QColor:
    """`color()` for text drawn in code (TEXT_OVERRIDES)."""
    if isinstance(value, str):
        value = TEXT_OVERRIDES.get(_active, {}).get(value.lower(), value)
    return color(value)


def color(value) -> QColor:
    """QColor of a pink-theme colour, in the active theme."""
    if isinstance(value, str) and _map:
        value = _map.get(value.lower(), value)
    return QColor(value)


def has_screens() -> bool:
    """Whether the active theme paints chart content darker than its chrome."""
    return _active in SCREEN_STEPS


def screen(value) -> QColor:
    """`color()` for paint that draws chart content (lanes, the playfield,
    the SV graph): the theme's SCREEN_STEPS where it has them."""
    if isinstance(value, str) and _screen_map:
        value = _screen_map.get(value.lower(), value)
    return QColor(value)


def screen_rgba(r: int, g: int, b: int, a: int = 255) -> QColor:
    """`screen()` for a colour written as integers, keeping its alpha: the
    chart's translucent whites, QColor(255, 255, 255, 70) and the like."""
    colour = screen("#%02x%02x%02x" % (r, g, b))
    colour.setAlpha(a)
    return colour


def rgba(r: int, g: int, b: int, a: int = 255) -> QColor:
    """`color()` for a pink-theme colour written as integers, keeping its alpha.

    The song list's selected-row tint was QColor(255, 102, 170, 46): no hex
    for the remap to see, so it stayed pink in every theme.
    """
    colour = color("#%02x%02x%02x" % (r, g, b))
    colour.setAlpha(a)
    return colour


def svg_asset(path: str) -> str:
    """`path`, or a recoloured copy of it when it is an SVG carrying a
    pink-theme colour.

    The switches, the hovered spin-box chevrons and the like are drawn by SVG
    files referenced from stylesheets as url(), so the colour is in the file
    and neither css() nor color() ever sees it -- the toggles stayed pink in
    every theme. The copy is written once per theme beside the user's temp
    files and reused.
    """
    if not _map or not path.lower().endswith(".svg"):
        return path
    key = (_active, path)
    if key not in _svg_copies:
        _svg_copies[key] = _themed_svg(path)
    return _svg_copies[key]


def _themed_svg(path: str) -> str:
    import tempfile
    from pathlib import Path
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return path
    themed = css(text)
    if themed == text:
        return path
    target = Path(tempfile.gettempdir()) / "TaikoFancyArranger-theme" / _active / source.name
    try:
        if not target.is_file() or target.read_text(encoding="utf-8") != themed:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(themed, encoding="utf-8")
    except OSError:
        return path
    return target.as_posix()


def apply_palette(app) -> None:
    """Give natively drawn controls the theme's focus colour.

    Anything without a stylesheet of its own -- the Settings sliders, selected
    text, a selected list row -- is filled from the palette's Highlight and
    Accent, which Qt takes from the *Windows* accent colour. On a pink accent
    that read as the app's pink in every theme; on any other it was neither.
    """
    from PySide6.QtGui import QPalette
    palette = app.palette()
    focus = color(_FOCUS)
    for role in (QPalette.Highlight, QPalette.Accent):
        palette.setColor(role, focus)
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)


def active() -> str:
    return _active


def install(name: str) -> None:
    """Make `name` the theme for this process. Before any widget is built."""
    global _active, _map, _screen_map, _focus_rgb
    name = RENAMED.get(name, name)
    _active = name if name in THEMES or name == AS_WRITTEN else DEFAULT
    _map = build_map(_active)
    _screen_map = build_map(_active, screen=True) if _active in SCREEN_STEPS else _map
    focus = QColor(_map.get(_FOCUS, _FOCUS))
    _focus_rgb = f"{focus.red()}, {focus.green()}, {focus.blue()}"
    from PySide6.QtWidgets import QApplication, QWidget
    for cls in (QWidget, QApplication):
        original = getattr(cls, "_unthemed_setStyleSheet", None) or cls.setStyleSheet
        cls._unthemed_setStyleSheet = original
        cls.setStyleSheet = _keeping_source(original)


def _keeping_source(original):
    def set_style_sheet(self, text):
        # The sheet as written, for `switch` to translate again.
        self.setProperty(SOURCE_PROPERTY, text)
        original(self, css(text))
    return set_style_sheet


SOURCE_PROPERTY = "themeSource"


def switch(name: str) -> None:
    """Make `name` the theme now, with the window already built: every sheet
    is translated again from what was written, and the palette follows.

    What paints itself reads `color()` as it paints and needs only a repaint;
    what computed a colour once and kept it is the caller's to redo
    (`MainWindow.retheme`).
    """
    install(name)
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance()
    if app is None:
        return
    source = app.property(SOURCE_PROPERTY)
    if source is not None:
        QApplication._unthemed_setStyleSheet(app, css(source))
    for widget in app.allWidgets():
        source = widget.property(SOURCE_PROPERTY)
        if source is None:
            continue
        # Most sheets name no themed colour; setting one anyway re-polishes
        # the widget and everything under it for nothing.
        themed = css(source)
        if themed != widget.styleSheet():
            QWidget._unthemed_setStyleSheet(widget, themed)
    apply_palette(app)


def _load() -> None:
    # Through SettingsManager rather than a QSettings of its own, so the test
    # suite's private settings copy (tests/__init__.py) is the one read.
    from app.settings import SettingsManager
    install(SettingsManager().string_value(SETTING, DEFAULT))


_load()


if __name__ == "__main__":
    for theme_name in THEMES:
        for table in (build_map(theme_name), build_map(theme_name, screen=True)):
            # A theme's output must never be another key, or a colour that passes
            # through css() twice (a constant baked into a stylesheet) moves twice.
            clashes = [v for k, v in table.items() if v in table and v != k]
            assert not clashes, (theme_name, clashes)
    install("nso")
    assert screen("#151b24").name() == "#ffd9f0", "NSO's lanes are its pastel pink"
    assert screen("#ffffff").name() == "#c2185b" and color("#ffffff").name() == "#ffffff",         "white lines turn deep pink on the lanes only"
    assert css("a{color: #f3a6bd; background-color: #f3a6bd}") == "a{color: #d6247a; background-color: #ffc6e3}",         css("a{color: #f3a6bd; background-color: #f3a6bd}")
    install("taiko")
    assert css("a{color:#F3A6BD; b: #ff66aa55; c: rgba(255,102,170,13); d: #e54c2e}") == \
        "a{color:#d4432a; b: #5aa7c755; c: rgba(90, 167, 199,13); d: #e54c2e}", css("a{color:#F3A6BD; b: #ff66aa55; c: rgba(255,102,170,13); d: #e54c2e}")
    install("osu")
    assert css("#f3a6bd") == "#f3a6bd" and css("#191f29") == "#1f1e33"
    print("ok", {name: len(build_map(name)) for name in THEMES})
