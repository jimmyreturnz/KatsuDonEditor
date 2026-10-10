"""Load the note graphics out of an osu! skin folder.

**Only the playfield.** A skin folder holds well over a hundred files --
judgement counters, the song-select menu, mode icons, the scoreboard, cursor
art -- and none of that appears in a preview that never judges a hit or shows a
score. What is loaded is the wiki's *playfield* list and nothing else:

    taikohitcircle          the note body, a white disc tinted per note
    taikohitcircleoverlay   the ring drawn over it, never tinted
    taikobigcircle          the same for a finisher
    taikobigcircleoverlay
    approachcircle          the hit-target ring, 126x126 to the note's 118x118
    taiko-roll-middle       a 1px-wide strip stretched along a drumroll body
    taiko-roll-end          the drumroll's tail cap, tinted like the body
    taiko-bar-right         1024x200, the bar itself, stretched to the width
    taiko-bar-right-glow    the same rect again, laid over it during kiai
    taiko-barline           4x175, the measure marker

Left out on purpose, though the wiki files them under the playfield:

* `taiko-slider`, the background that scrolls behind the bar: owner's call,
  2026-10-07. The bar is the lane, and the scrolling art under it read as a
  second lane behind the first;
* the hit explosions (`taiko-hit300` and friends) and `taiko-slider-fail`: a
  judgement is a thing that happens to a *player*. The autoplay below shows
  which hand hits, not how well (owner, 2026-10-10: in-app play testing
  comes later);
* `lighting`, the hit explosion's light;

Loaded for the **autoplay** only, and drawn only while one plays an editor
preview (never song select's bare stage): the input drum (`taiko-bar-left`,
`taiko-drum-inner`, `taiko-drum-outer`), where the autoplay's hands land, and
`taiko-glow`, the kiai glow behind the hit target. See AUTOPLAY_ELEMENTS.

* the mascot (`pippidon*`) is a character animation, in 6 of 39 installed
  skins, and the only element here that would need BPM-synced frames.

The tinting split is osu!'s own and is why the base files are plain white: the
base carries the note's colour (red for don, blue for kat, yellow for a
drumroll) and the overlay carries the outline that must stay the colour the
skinner drew it. Loading them the other way round produces a red ring on a
white note.

`@2x` variants are preferred wherever present -- they are the same artwork at
twice the resolution, and the preview scales everything to the lane height
anyway, so the larger source is free quality.

Anything missing falls back per element rather than per skin: a skin that ships
notes but no drumroll art gets its notes, and the built-in drawing for the
rest.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

# Element name -> whether the note's colour is multiplied into it.
#
# The overlays and the explosions are not: they carry the outline and the flash
# in whatever colour the skinner drew them. The rest are, but "tint" means
# multiply and not replace -- `taiko-roll-middle` looks like a plain white
# strip and is really black at alpha 179 along its top and bottom edges around
# a translucent white core, so replacing its colour flattened it into one slab
# while multiplying leaves the dark edges dark. See `tinted`.
SKIN_ELEMENTS = {
    "taikohitcircle": True,
    "taikohitcircleoverlay": False,
    "taikobigcircle": True,
    "taikobigcircleoverlay": False,
    "approachcircle": False,
    "taiko-roll-middle": True,
    # Tinted, like the middle it caps. The wiki calls this one "already
    # coloured", and skins do ship it yellow -- but they ship the middle
    # yellow too, and osu! tints both from the drumroll's colour. Leaving the
    # cap out of the tint put an untinted end on a tinted body.
    "taiko-roll-end": True,
    "taiko-bar-right": False,
    "taiko-bar-right-glow": False,
    "taiko-barline": False,
    # The autoplay's: the input drum and the kiai glow at the hit target. See
    # AUTOPLAY_ELEMENTS.
    "taiko-bar-left": False,
    "taiko-drum-inner": False,
    "taiko-drum-outer": False,
    # LegacyKiaiGlow tints it yellow.
    "taiko-glow": True,
}

# Drawn only while an autoplay plays the editor's preview, never on song
# select's bare stage. Not part of the taiko signature: mania skins ship the
# input drum as often as taiko ones do, and offering one for its drum alone
# would change nothing about the notes.
AUTOPLAY_ELEMENTS = frozenset(
    ("taiko-bar-left", "taiko-drum-inner", "taiko-drum-outer", "taiko-glow"))

# What makes a folder a *taiko* skin, for the menu. Every name above that is
# not taiko-specific -- `approachcircle` is an osu!standard file that almost
# every skin of any mode ships -- has to be excluded here, or the menu fills up
# with mania and standard skins that would change nothing.
TAIKO_SIGNATURE_ELEMENTS = frozenset(
    name for name in SKIN_ELEMENTS
    if name.startswith("taiko") and name not in AUTOPLAY_ELEMENTS
)

# The four taiko hitsounds, keyed the way `HitsoundPlayer` already keys them so
# a skin's samples drop straight into the pools it builds. Extensions in
# preference order: osu! accepts all three and a skin may ship any of them.
SKIN_SOUNDS = {
    "normal": "taiko-normal-hitnormal",
    "clap": "taiko-normal-hitclap",
    "finish": "taiko-normal-hitfinish",
    "whistle": "taiko-normal-hitwhistle",
}
SOUND_EXTENSIONS = (".wav", ".ogg", ".mp3")
# The song select's own sounds, by the names osu!'s skins give them. The app
# ships both in assets/se for a skin that has neither.
UI_SOUNDS = {
    "select_expand": "select-expand",
    "select_difficulty": "select-difficulty",
}

# osu! keeps skins beside the songs, and the app already knows where those are,
# so nothing new has to be asked of the user.
SKINS_DIRECTORY_NAME = "Skins"


def skins_root(songs_folder: str | Path | None) -> Path | None:
    """The Skins folder that sits beside `songs_folder`, if it is there."""
    if not songs_folder:
        return None
    candidate = Path(songs_folder).parent / SKINS_DIRECTORY_NAME
    return candidate if candidate.is_dir() else None


def available_skins(root: Path | None) -> list[str]:
    """Skin folder names under `root` that contain something worth loading.

    Filtered on content rather than listed wholesale: an osu! install
    accumulates dozens of skins and most of them are for other modes, so
    offering a mania skin that would change nothing here is a menu of
    disappointments.
    """
    if root is None:
        return []
    try:
        entries = [entry for entry in os.scandir(root) if entry.is_dir()]
    except OSError:
        return []
    names = [
        entry.name for entry in entries
        if (TAIKO_SIGNATURE_ELEMENTS & set(element_paths(Path(entry.path))))
        or sound_paths(Path(entry.path))
    ]
    return sorted(names, key=str.lower)


def sound_paths(folder: Path, names: dict[str, str] = SKIN_SOUNDS) -> dict[str, Path]:
    """The skin's taiko hitsounds, by the key `HitsoundPlayer` uses -- or,
    given `names`, any other sounds it ships (UI_SOUNDS).

    A skin that ships only samples and no art is still worth offering: plenty
    of people pick a skin for how it *sounds*.
    """
    try:
        existing = {entry.name.lower(): entry.path
                    for entry in os.scandir(folder) if entry.is_file()}
    except OSError:
        return {}
    found = {}
    for key, stem in names.items():
        for extension in SOUND_EXTENSIONS:
            entry = existing.get((stem + extension).lower())
            if entry is not None:
                found[key] = Path(entry)
                break
    return found


# What a skin with no skin.ini reads as, as osu! does; one with a skin.ini
# that names no version is 1.0.
LATEST_SKIN_VERSION = 99.0


def skin_version(folder: Path) -> float:
    """The `Version:` in the skin's skin.ini. The input drum's layout changed
    at 2.1, so the drum is the one thing here that reads it."""
    try:
        names = {entry.name.lower(): entry.path for entry in os.scandir(folder) if entry.is_file()}
    except OSError:
        return LATEST_SKIN_VERSION
    ini = names.get("skin.ini")
    if ini is None:
        return LATEST_SKIN_VERSION
    try:
        text = Path(ini).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return LATEST_SKIN_VERSION
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "version":
            value = value.strip().lower()
            if value == "latest":
                return LATEST_SKIN_VERSION
            try:
                return float(value)
            except ValueError:
                return 1.0
    return 1.0


def element_paths(folder: Path) -> dict[str, Path]:
    """Every element this folder can supply, from **one** directory listing.

    One listing, not one per element, and `os.scandir` rather than
    `Path.iterdir()`. Both mattered on a real osu! install -- 59 skin folders
    holding 33,872 files between them: listing per element cost 4546ms, one
    listing per folder brought it to 2178ms, and scandir brought that to 28ms.
    The last step is the big one because `Path.iterdir()` plus `is_file()`
    stats every entry separately, while scandir reads the type straight out of
    the directory entry.

    Names are matched case-insensitively because osu! does and skins are
    inconsistent about it -- this very folder ships `taiko-Slider@2x` beside
    `taiko-slider`. `@2x` wins where present (same artwork at twice the
    resolution, and everything gets scaled to the lane height anyway), and a
    `-0` suffix is the last resort: a skin that animates an element ships
    `name-0.png` and no `name.png`, and the first frame is a fair still of it.
    """
    try:
        existing = {entry.name.lower(): entry.path
                    for entry in os.scandir(folder) if entry.is_file()}
    except OSError:
        # A skin folder can be a broken junction or vanish mid-scan; one bad
        # one should not take the whole menu down with it.
        return {}
    found = {}
    for name in SKIN_ELEMENTS:
        for suffix in ("@2x.png", ".png", "-0@2x.png", "-0.png"):
            entry = existing.get((name + suffix).lower())
            if entry is not None:
                found[name] = Path(entry)
                break
    return found


def tinted(source: QPixmap, colour: QColor) -> QPixmap:
    """`source` with each colour channel multiplied by `colour`, alpha kept.

    **Multiply, not replace.** Sprite tinting in osu! is a multiply of the
    colour with the texture, and the difference only shows on artwork that is
    not uniformly white. Replacing flattened `taiko-roll-middle` -- black edge
    bands at alpha 179 around a translucent white core -- into one solid slab
    of the drumroll colour and lost the shape entirely. Multiplying leaves
    black black and turns the white core the colour, which is what the skinner
    drew it for. On a plain white disc the two are identical, which is why the
    notes looked right either way.

    Done per channel rather than through `QPainter`'s `CompositionMode_Multiply`
    because that blend carries a `src * (1 - dst_alpha)` term: on a
    semi-transparent black pixel it tinted the *transparency*, turning those
    black bands into (76, 60, 19) instead of leaving them black.
    """
    if source.isNull():
        return source
    image = source.toImage().convertToFormat(QImage.Format_ARGB32)
    raw = bytearray(image.bits().tobytes())
    # ARGB32 is B, G, R, A per pixel in memory on little-endian. A table per
    # channel keeps the inner loop to three lookups and no arithmetic.
    tables = [bytes((value * channel) // 255 for value in range(256))
              for channel in (colour.blue(), colour.green(), colour.red())]
    blue, green, red = tables
    for index in range(0, len(raw), 4):
        raw[index] = blue[raw[index]]
        raw[index + 1] = green[raw[index + 1]]
        raw[index + 2] = red[raw[index + 2]]
        # raw[index + 3] is alpha, deliberately untouched: it is the shape.
    result = QImage(bytes(raw), image.width(), image.height(),
                    image.bytesPerLine(), QImage.Format_ARGB32)
    # QImage does not own a buffer it was handed, and this one is a local.
    return QPixmap.fromImage(result.copy())


def silhouette(source: QPixmap, colour: QColor) -> QPixmap:
    """`source` as light in `colour`: its shape, weighted by its brightness.

    The colour is *replaced* rather than multiplied -- this is light, not a
    tint, so it must not carry the artwork's own hue.

    But the **alpha is scaled by the source's luminance**, which is what stops
    the dark parts of a note lighting up. `tinted` multiplies, so a black pixel
    stays black however it is coloured; light has to behave the same way or the
    two disagree about what the artwork is. Taking the shape alone put full
    light on the eyes and mouth a skinner drew into `taikohitcircle` -- 4% of
    that element is dark ink in one real skin -- and glowed them out.

    White artwork is unaffected, which is the common case: a plain white disc
    has luminance 255 everywhere and takes the light in full.
    """
    if source.isNull():
        return source
    image = source.toImage().convertToFormat(QImage.Format_ARGB32)
    raw = bytearray(image.bits().tobytes())
    # ARGB32 is B, G, R, A per pixel in memory on little-endian.
    blue, green, red = colour.blue(), colour.green(), colour.red()
    for index in range(0, len(raw), 4):
        # Rec.601 luma, in integers: the eye weights green far above blue, and
        # a plain average would let a saturated blue note glow like a white one.
        luma = (raw[index + 2] * 77 + raw[index + 1] * 151 + raw[index] * 28) >> 8
        raw[index] = blue
        raw[index + 1] = green
        raw[index + 2] = red
        raw[index + 3] = (raw[index + 3] * luma) // 255
    result = QImage(bytes(raw), image.width(), image.height(),
                    image.bytesPerLine(), QImage.Format_ARGB32)
    return QPixmap.fromImage(result.copy())


def _masked_by(stamp: QPixmap, overlay: QPixmap) -> QPixmap:
    """`stamp` with its alpha taken down wherever `overlay` is opaque.

    The overlay is the part of a note the skinner drew to keep its own colour
    -- the black rim, the face -- so it is exactly where light must not land.
    Done on the alpha channel rather than with a clip path because the overlay
    is antialiased: a hard clip would leave a bright fringe along the rim.
    """
    if stamp.isNull() or overlay.isNull():
        return stamp
    image = stamp.toImage().convertToFormat(QImage.Format_ARGB32)
    mask = overlay.toImage().convertToFormat(QImage.Format_ARGB32)
    raw = bytearray(image.bits().tobytes())
    mask_raw = mask.bits().tobytes()
    stride, mask_stride = image.bytesPerLine(), mask.bytesPerLine()
    width = min(image.width(), mask.width())
    for y in range(min(image.height(), mask.height())):
        row, mask_row = y * stride, y * mask_stride
        for x in range(width):
            index = row + x * 4
            # ARGB32 is B, G, R, A per pixel on little-endian.
            covered = mask_raw[mask_row + x * 4 + 3]
            if covered:
                raw[index + 3] = (raw[index + 3] * (255 - covered)) // 255
    result = QImage(bytes(raw), image.width(), image.height(),
                    image.bytesPerLine(), QImage.Format_ARGB32)
    return QPixmap.fromImage(result.copy())


class TaikoSkin:
    """The loaded elements of one skin, with the scaling the preview needs.

    Scaled pixmaps are cached per (element, colour, diameter). Rescaling a
    256x256 source for every note on every frame is the kind of per-frame work
    that redoes static work -- the shape does not change between frames, only
    which frame it is drawn on.
    """

    def __init__(self, folder: Path | None = None) -> None:
        self.name = folder.name if folder is not None else ""
        self.sounds: dict[str, Path] = {}
        self.ui_sounds: dict[str, Path] = {}
        self._sources: dict[str, QPixmap] = {}
        self._tinted: dict[tuple, QPixmap] = {}
        self._scaled: dict[tuple, QPixmap] = {}
        self._flash: dict[tuple, QPixmap] = {}
        self._stretched: dict[tuple, QPixmap] = {}
        self._ink: dict[str, float] = {}
        # Source pixels per osu! unit: 2 for an `@2x` file. The ratio Qt keeps
        # is thrown away below, and the input drum is the one element drawn at
        # its own size rather than one the caller picks.
        self._density: dict[str, float] = {}
        self._natural: dict[tuple, QPixmap] = {}
        self.version = skin_version(folder) if folder is not None else LATEST_SKIN_VERSION
        if folder is None or not folder.is_dir():
            return
        for element, path in element_paths(folder).items():
            pixmap = QPixmap(str(path))
            self._density[element] = 2.0 if "@2x" in path.name.lower() else 1.0
            if not pixmap.isNull():
                # Qt stamps devicePixelRatio 2.0 on anything named `@2x`, which
                # halves it again at every `drawPixmap(point, ...)`. Every size
                # here is decided by the caller from the playfield's own units,
                # so the ratio is not information -- and a skin that ships some
                # elements at @2x and some not (riun's approachcircle) would
                # otherwise draw half of its playfield at half scale.
                pixmap.setDevicePixelRatio(1.0)
                self._sources[element] = pixmap
        self.sounds = sound_paths(folder)
        self.ui_sounds = sound_paths(folder, UI_SOUNDS)

    def __bool__(self) -> bool:
        return bool(self._sources) or bool(self.sounds)

    def has(self, element: str) -> bool:
        return element in self._sources

    def scaled(self, element: str, diameter: int, colour: QColor | None = None) -> QPixmap | None:
        """`element` at `diameter` pixels tall, tinted if it takes a colour."""
        if element not in self._sources or diameter <= 0:
            return None
        key = (element, diameter, colour.rgba() if colour is not None else 0)
        cached = self._scaled.get(key)
        if cached is not None:
            return cached
        source = self._sources[element]
        if SKIN_ELEMENTS.get(element) and colour is not None:
            # Tint the full-size artwork once per colour and scale from that.
            # Tinting inside the size cache instead would redo the per-pixel
            # multiply for every distinct lane height the view is ever given.
            tint_key = (element, colour.rgba())
            source = self._tinted.get(tint_key)
            if source is None:
                source = tinted(self._sources[element], colour)
                self._tinted[tint_key] = source
        # Height is the bound: the lane height decides how big a note is, and
        # a skinner may draw a non-square element (taiko-roll-end is 64x128).
        # The width is floored at one pixel rather than left to scaledToHeight,
        # which rounds it: `taiko-roll-middle` is a 1px (2px at @2x) column, so
        # at any note under 128px tall its width came out 0, the pixmap came
        # back null, and every real drumroll lost its whole track -- head and
        # cap with nothing between them. Callers stretch it to length anyway.
        width = max(1, round(source.width() * diameter / max(1, source.height())))
        result = source.scaled(width, diameter, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        # ponytail: unbounded cache, but it is keyed on a handful of diameters
        # and three colours, and every entry is one the view is actively using.
        self._scaled[key] = result
        return result

    def flash(self, element: str, diameter: int, colour: QColor) -> QPixmap | None:
        """`element` at `diameter` tall, filled flat with `colour`.

        The kiai pulse: laid additively over the note it belongs to, so the
        light takes the artwork's own outline instead of a plain disc drawn
        around it. See `silhouette`.

        **Masked by the element's overlay where it has one.** The base carries
        the note's colour and the overlay carries everything the skinner drew
        to stay its own colour -- the black rim, the face. Light belongs on the
        part that takes a colour and nowhere else; laid over the whole disc it
        washed the black out along with the red.
        """
        if element not in self._sources or diameter <= 0:
            return None
        key = (element, diameter, colour.rgb())
        cached = self._flash.get(key)
        if cached is None:
            stamp = silhouette(self._sources[element], colour).scaledToHeight(
                diameter, Qt.SmoothTransformation)
            overlay = self._sources.get(element + "overlay")
            if overlay is not None:
                stamp = _masked_by(stamp, overlay.scaledToHeight(
                    stamp.height(), Qt.SmoothTransformation))
            self._flash[key] = cached = stamp
        return cached

    def natural(self, element: str, px_per_unit: float, mirrored: bool = False,
                colour: QColor | None = None) -> QPixmap | None:
        """`element` at the skin's own size, one source pixel (two at @2x)
        per osu! unit, `px_per_unit` screen pixels to the unit. Mirrored
        left-right if asked: the input drum draws one half from the other.
        Tinted by `colour` if the element takes one."""
        source = self._sources.get(element)
        if source is None:
            return None
        scale = px_per_unit / self._density.get(element, 1.0)
        width, height = round(source.width() * scale), round(source.height() * scale)
        if width <= 0 or height <= 0:
            return None
        tint = colour if SKIN_ELEMENTS.get(element) and colour is not None else None
        key = (element, width, height, mirrored, tint.rgba() if tint is not None else 0)
        cached = self._natural.get(key)
        if cached is None:
            if tint is not None:
                source = tinted(source, tint)
            image = source.toImage().scaled(
                width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            if mirrored:
                image = image.flipped(Qt.Orientation.Horizontal)
            self._natural[key] = cached = QPixmap.fromImage(image)
        return cached

    def stretched(self, element: str, width: int, height: int) -> QPixmap | None:
        """`element` scaled to exactly `width` x `height`, aspect ignored.

        For the playfield panels, which osu! stretches rather than fits:
        `taiko-bar-right` is 1024x200 art "stretched to fit screen width", and
        `taiko-roll-middle` is a 1px column. Cached because the alternative is
        a smooth rescale of a 1024-wide source on every frame, and the size
        only changes when the window does.
        """
        if element not in self._sources or width <= 0 or height <= 0:
            return None
        key = (element, width, height)
        cached = self._stretched.get(key)
        if cached is None:
            cached = self._sources[element].scaled(
                width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            self._stretched[key] = cached
        return cached

    def ink_height(self, element: str) -> float:
        """How much of `element`'s canvas its visible artwork fills, 0..1.

        Skins pad their elements differently -- in one real skin
        `taikohitcircle` fills 0.83 of its box while `taiko-roll-end` fills
        1.00 -- so scaling both to the same canvas height draws the cap about
        17% larger than the note it caps. Callers that need two elements to
        *look* the same size divide by this.

        Read from the alpha bytes rather than pixel by pixel, and cached: this
        is a whole-image scan and the answer never changes.
        """
        cached = self._ink.get(element)
        if cached is not None:
            return cached
        source = self._sources.get(element)
        if source is None:
            return 1.0
        image = source.toImage().convertToFormat(QImage.Format_ARGB32)
        raw = image.bits().tobytes()
        stride = image.bytesPerLine()
        top, bottom = None, None
        for y in range(image.height()):
            row = raw[y * stride + 3: y * stride + image.width() * 4: 4]
            if max(row) > 8:
                bottom = y
                if top is None:
                    top = y
        ratio = 1.0 if top is None else (bottom - top + 1) / image.height()
        self._ink[element] = ratio
        return ratio

    def size(self, element: str) -> tuple[int, int] | None:
        """The element's own pixel size, for callers that keep its aspect."""
        source = self._sources.get(element)
        return None if source is None else (source.width(), source.height())

    def clear_cache(self) -> None:
        self._scaled.clear()
        self._tinted.clear()
        self._flash.clear()
        self._stretched.clear()
