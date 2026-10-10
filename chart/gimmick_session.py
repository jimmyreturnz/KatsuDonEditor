"""Gimmick editor session state and the structures its tools write.

Three things live here, all of them free of Qt so they can be tested directly:

* the **pairing index** -- which difficulty a gimmick session edits, remembered
  permanently in `gimmick_index.json` beside the song library's `song_index.json`
* the **base timing snapshot** -- a copy of the document's timing taken when the
  session is created
* the **structure builders** -- the exact timing points and hit objects each
  gimmick tool emits

Why a base timing snapshot exists
---------------------------------
A gimmick fills the file with 60000 BPM lines. Every one of those restarts
measure counting and collapses the snap grid, so a chart view that derived its
grid from the document being edited would redraw itself under the user's cursor
on every placement. The snapshot is taken once, before any of that, and drives
snaps, wheel scroll and the BPM overlay for the life of the pairing. It is never
recomputed from the edited file.

The output file *is* expected to end up with shifted barline phase. Placements
deliberately write no phase-restoring lines: the snapshot keeps the editor
smooth, not the map.

SV is likewise not auto-restored. An uninherited point resets SV to 1.0x
(`osu_io.timing.sv_at`), so every structure here silently drops the green-line SV
in force at its time. Putting a green line back is the SV layers' job, and they
place it on the same millisecond as the red line by design. When they do,
**the green line must be ordered after the red one**: osu! resolves a shared
timestamp by file order, and `sorted_by_time` is a stable sort on time alone.
"""
from __future__ import annotations

import math

import json
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path

from model.hit_object import (
    HITSOUND_CLAP,
    HITSOUND_FINISH,
    TYPE_CIRCLE,
    TYPE_SLIDER,
    HitObject,
)
from osu_io.timing import (
    TimingPoint,
    active_point_at,
    active_uninherited_at,
    sv_at,
    parse_timing_line,
    serialize_timing_point,
    sorted_by_time,
)

INDEX_VERSION = 1

# Defaults for every configurable value in the gimmick toolbox.
DEFAULT_GIMMICK_BPM = 60000.0
# A Don's mirrored restore pair sits at +/-`spacing_ms`. 1 is as tight as the
# format goes, which is what the shape wants -- a don reads as one thin
# object rather than a smear.
#
# Kat used to be derived from this same number (+/-n, +/-(n+2), +/-(n+4)),
# but a mapper hand-layering several barline structures on one millisecond
# region needs Kat's three pairs free of that progression -- so Kat now
# carries its own three spacings below (`DEFAULT_KAT_SPACING1_MS` and
# friends) and `spacing_ms` narrows to meaning only "Don's spacing". The three
# Kat defaults reproduce the old derived values (1, 3, 5) exactly, so neither
# an existing map nor an existing test sees any change from this split.
DEFAULT_SPACING_MS = 1
DEFAULT_KAT_SPACING1_MS = 1
DEFAULT_KAT_SPACING2_MS = 3
DEFAULT_KAT_SPACING3_MS = 5
DEFAULT_FAKE_SLIDER_LENGTH = -0.001

# Longest `length` a drumroll can carry and still be a fake slider -- drawn,
# never hittable. The canonical form is negative (`-0.0001`), but the positive
# side of zero is the same trick: `mekurume` writes 616 of its 640 sliders as
# `0.001`, whose derived duration is 0.0045ms -- no tick, nothing to hit, only
# the head drawn. Read strictly as `< 0` those were ordinary drumrolls, so
# neither the fake slider layer nor this config would admit them.
FAKE_SLIDER_MAX_LENGTH = 0.001

# -- anti-barline ----------------------------------------------------------
# The inverse of a barline note: instead of drawing bars around a note, the
# lane is packed with bars until it reads as one solid white sheet and each
# note is a *slit* of missing bar travelling in it. `mekurume [eclosion]`
# 1:52.5-2:15.7 is the reference these four numbers were measured off.

# Wall ticks per beat. 36 over a 750ms beat is one red line every 20.8ms.
# How solid that reads is a question about the chart's scroll speed as much as
# its BPM, which is why it and the wall's own BPM below are both dials rather
# than derived: the reference section gets there with 72 lines a beat and a
# wall BPM of a third of the chart's, but that is one route to a white lane
# and not the only one.
DEFAULT_ANTI_LINES_PER_BEAT = 36
# Wall ticks omitted per note. The slit width is the note's colour: measured
# 100% consistent over the reference section at don = 2 ticks (3.7px) and
# kat = 4 (7.3px). There is nothing else to read a colour off -- the note
# itself is invisible. The defaults are the owner's narrower 1 and 2, which
# keep the same 1:2 ratio; the reference's widths are one setting away.
DEFAULT_ANTI_DON_TICKS = 1
DEFAULT_ANTI_KAT_TICKS = 2

# The wall is phased off the first note rather than started on it. A wall tick
# sharing a note's millisecond would be a second uninherited point there, and
# osu! honours only the file-order-first of those -- so either the wall line
# or the note's own squash line would silently do nothing. 2ms clears both the
# note and its restore.
ANTI_WALL_ANCHOR_OFFSET_MS = 2
# Where a note's restore line goes. The squash line at T makes the note scroll
# 392px/ms -- invisible -- and this hands the chart's own BPM back one
# millisecond later, before anything else can be drawn at that speed.
ANTI_RESTORE_OFFSET_MS = 1
# The restore line's meter. `beat_length x meter` is when it would emit its
# next barline, and that has to outlast the slit or the restore stamps a bar
# in the hole it just opened; 750 x 999 is a little over 12 minutes. It is the
# meter that is absurd and not the beat length, because the beat length is the
# chart's own and is the whole point of a restore.
ANTI_RESTORE_METER = 999

# -- Hidden anti-barline ----------------------------------------------------
#
# `mew`'s `LuzeriA - Nbt-Hwt [The Pharaoh's Curse]` 1:56.9-2:07.3 is the
# reference, and it is anti-barline built the other way round. There the wall
# is a red line per tick and the slit is ticks left out; here **one** meter-1
# red line makes osu! emit the whole wall itself, and the slit is opened by
# raising SV for a fraction of a beat after the note.
#
# The reason that works is that taiko does not integrate velocity: an object
# sits at `(its time - now) x the velocity at its own time`. So the bars drawn
# during the raised-SV window are pushed *ahead* of the ones at the base speed
# by `(excess velocity) x (how far away they still are)` -- a gap that opens as
# the section approaches from a distance and closes to nothing at the hit
# position. That is the "hidden": the colour is only readable early.
#
# Measured on the reference at SliderMultiplier 1.2: base 0.01x puts the bars
# 1.68px apart (solid under the sprite), don's +1.01% opens 3.5px per second of
# lead time and kat's +3.09% opens 10.7px -- exactly the 1:3 ratio the classic
# anti-barline gets from 2 ticks against 4.
DEFAULT_HIDDEN_HIDE_BPM = 66666.0
DEFAULT_HIDDEN_WALL_BPM = 12345.0
# Base, Don and Kat SV. The two note values are the reference's -9900 and
# -9700 against a -10000 wall, which is to say 1% and 3% above it. The
# difference lives in the sixth decimal on purpose -- see the module docstring
# in gui.py's SV_DECIMALS.
DEFAULT_HIDDEN_BASE_SV = 0.01
DEFAULT_HIDDEN_DON_SV = 100.0 / 9900.0
DEFAULT_HIDDEN_KAT_SV = 100.0 / 9700.0
# How long the raised-SV window lasts, as a division of the chart's own beat.
# The reference uses 1/8, which at 185 BPM is 40.5ms -- about eight wall bars.
DEFAULT_HIDDEN_WINDOW_DIVISOR = 8
# The wall line's meter. On-screen bar spacing is
# `100 x SliderMultiplier x 1.4 x SV x meter` and has no BPM in it at all, so
# the meter is what decides how tight the sheet is; 1 is as tight as osu! can
# draw it. Not configurable for that reason -- SV is the dial with range in it.
HIDDEN_WALL_METER = 1
# The hide line's meter. It lives for one millisecond, so this only has to
# outlast that; it matches ANTI_RESTORE_METER because it is the same trick.
HIDDEN_HIDE_METER = 999
# How long the note is hidden for. One millisecond at 66666 BPM already
# carries the note off the far end of the screen, and the wall has to come
# back before the next bar is due.
HIDDEN_WALL_OFFSET_MS = 1
DEFAULT_RED_LINE_OFFSET_MS = 0
# +2 rather than +1 so a shiny note has +1 to itself: the two structures are
# drawn objects at the same millisecond, and sharing an offset stacks them on
# top of each other -- which is exactly what makes a shiny shiny, so an
# accidental overlap is indistinguishable from the effect.
DEFAULT_FAKE_SLIDER_OFFSET_MS = 2
DEFAULT_SHINY_OFFSET_MS = 1
# How many fake sliders one shiny note stacks. A stack of translucent
# drumrolls is what reads as white; one is just a fake slider.
DEFAULT_SHINY_COUNT = 3
DEFAULT_FAKE_SLIDER_SV = 10.0
# Only ever shown, never written unless the red-line tool is switched to a
# custom BPM -- an unchecked box means the base timing's own BPM instead.
DEFAULT_RED_LINE_BPM = 180.0

# Above this, an uninherited point is almost certainly a gimmick line rather
# than real timing. Used only to warn that a base snapshot looks polluted --
# nothing is filtered out on the strength of it.
DIRTY_BASE_BPM = 1000.0


# How long after a plain fake slider's own red line the chart's BPM comes
# back, when `GimmickConfig.fake_slider_restore_after` is on. One millisecond:
# the shortest gap that is still a separate timestamp, so the retiming reaches
# the slider and nothing past it.
FAKE_SLIDER_RESTORE_OFFSET_MS = 1


class GimmickConfigError(ValueError):
    """Raised for a configuration value that would produce a broken map."""


@dataclass(slots=True)
class GimmickConfig:
    """One layer's toolbox settings.

    Held per layer rather than once for the page: a barline note's red line
    spacing and a fake slider's offset are both "how many milliseconds", but
    they describe different structures and are tuned against each other (see
    `spacing_collides`), so sharing one value made changing either wrong.

    `fake_slider_length` is at or below `FAKE_SLIDER_MAX_LENGTH` on purpose --
    a slider short enough to derive no duration is what makes osu! draw the
    object without it ever being hittable, which is the whole trick. A real
    length here would silently turn every fake slider into a scoreable
    drumroll, so it is rejected rather than clamped.

    `place_notes` is the barline layer's: with it off, Don and Kat write their
    red lines and no hit object, for gimmicks that only want the barlines.
    """

    gimmick_bpm: float = DEFAULT_GIMMICK_BPM
    spacing_ms: int = DEFAULT_SPACING_MS
    # Kat's three mirrored pairs, independently configurable -- see the
    # comment above `DEFAULT_SPACING_MS`. Validated distinct in
    # `__post_init__`: two equal values would write two red lines onto one
    # millisecond, where only the file-order-first of them is meaningful, so
    # a mapper who set two of these the same would get a quietly thinner note
    # than the three spin boxes promised.
    kat_spacing1_ms: int = DEFAULT_KAT_SPACING1_MS
    kat_spacing2_ms: int = DEFAULT_KAT_SPACING2_MS
    kat_spacing3_ms: int = DEFAULT_KAT_SPACING3_MS
    fake_slider_length: float = DEFAULT_FAKE_SLIDER_LENGTH
    red_line_offset_ms: int = DEFAULT_RED_LINE_OFFSET_MS
    fake_slider_offset_ms: int = DEFAULT_FAKE_SLIDER_OFFSET_MS
    # SV written on the gimmick line of a Don/Kat fake slider. The note there is
    # already squashed by the gimmick BPM; multiplying on top of it is what
    # takes the last pixel of it off the screen, leaving the fake slider one
    # offset later as the only thing drawn. Configurable because how far the
    # note has to go depends on the chart's own scroll speed.
    fake_slider_sv: float = DEFAULT_FAKE_SLIDER_SV
    # Whether a *plain* fake slider writes a red line of its own. It has no
    # note to hide, so that line only ever applies `fake_slider_bpm_multiplier`
    # -- at the default it restates the chart's own BPM and changes nothing,
    # and the Multiple tool writes one per slider, a run of dead lines. Off
    # gives the bare drawn object and gives up the retiming with it.
    #
    # Plain fake sliders only. Don and Kat are unaffected -- their line is the
    # squash that hides the note, which is the whole gimmick. A shiny has its
    # own dial, `shiny_red_line` below, since dropping its line loses the
    # retiming and (for a shiny standing on no note of its own) the one thing
    # that told it apart from a plain fake slider -- a shiny beside a real
    # chart note keeps neither cost, since the note itself is what
    # `MainWindow._compute_shiny_times` finds first.
    #
    # Defaults on, which is what every structure written before this dial
    # existed did.
    fake_slider_red_line: bool = True
    # Whether that line is followed by a second one a millisecond later,
    # carrying the chart's own BPM back.
    #
    # Without it the line governs the rest of the map: a red line's BPM is its
    # scroll speed, so a plain fake slider retimed to hide something retimes
    # everything after it too, and the mapper's fix was to place a second line
    # by hand -- which the Red Line tool then refused, because the slider's own
    # line was already on that millisecond (osu! honours one uninherited point
    # per timestamp). Writing both as one structure is what makes the effect
    # local to the slider, and is the standard shape the owner asked for.
    #
    # Off by default: every structure written before this dial existed had no
    # restore, and turning it on for them would retime the map they were
    # tuned against.
    fake_slider_restore_after: bool = False
    # Whether a shiny writes the red line at its own note's millisecond. At
    # the default multiplier that line only restates the chart's own BPM, so
    # a run of shinies is a run of dead lines the same way a plain fake
    # slider's is -- see `fake_slider_red_line` just above, the same trade.
    #
    # Off is only free of cost when the shiny decorates a note that is
    # already in the chart: `_compute_shiny_times` checks for a note at the
    # shiny offset before it ever looks for a line there, so the note alone
    # still identifies it. A shiny placed on empty space has no note either,
    # and this was its only anchor -- turning the line off there leaves
    # nothing to tell it apart from a plain fake slider at the same offset.
    #
    # Defaults on, matching `fake_slider_red_line` and every shiny written
    # before this dial existed.
    shiny_red_line: bool = True
    place_notes: bool = True
    # Whether a barline note's bars go on both sides of it or only after it.
    # See `barline_note`: mirrored reads as one object centred on the note,
    # forward-only is the trailing style, and both are in use.
    #
    # One per kind, like the spacings above and for the same reason: Don and
    # Kat are separate structures a mapper layers on one region, and a single
    # flag forced a centred Don to come with a centred Kat. Nothing about the
    # two widths being independent made sense if their shapes were not.
    #
    # Don defaults to forward-only: a squash on the note and one restore at
    # the chart's BPM a millisecond later, which is the owner's standard Don.
    mirror_don_lines: bool = False
    mirror_kat_lines: bool = True
    # How far from its object an SV layer's green lines sit. An SV point
    # governs what comes *after* it, so a chart that wants a note drawn at a
    # new speed needs the line slightly ahead of that note -- the same reason
    # the plain SV function tool defaults to -5ms.
    #
    # It lives here, per layer, rather than only in the Generate dialog because
    # a layer owns its green lines by exact millisecond: the offset decides
    # which milliseconds those are, so placing a line by hand and generating a
    # sweep have to agree on it or one of them lands somewhere the layer cannot
    # see. 0 keeps a layer's lines on its objects, which is what the two
    # gimmick layers want -- their objects *are* timing lines.
    sv_offset_ms: int = 0
    # The plain red-line tool's BPM. None means the base timing's own BPM at
    # that millisecond -- a line that only restarts measure counting. A typed
    # value is the standalone red-line effect: the scroll speed an uninherited
    # point carries is its BPM, so this is how a single line speeds the chart
    # up or slows it down without a green line. Separate from `gimmick_bpm`,
    # which is the squash the structures are built out of and is never what a
    # standalone line wants.
    red_line_bpm: float | None = None
    # Beats per bar for the plain red-line tool's line. osu! reads meter from
    # the uninherited point in force, so a standalone line silently re-bars
    # everything after it at 4/4 unless the mapper says otherwise -- and a
    # large value is the anti-barline trick of a line that never stamps a
    # second bar.
    red_line_meter: int = 4
    # The shiny note: `shiny_count` fake sliders stacked on one millisecond,
    # `shiny_offset_ms` after the snap. Its own offset rather than the fake
    # slider's, because the two must not land on the same millisecond -- see
    # `shiny_collides`.
    shiny_offset_ms: int = DEFAULT_SHINY_OFFSET_MS
    shiny_count: int = DEFAULT_SHINY_COUNT
    # What the shiny's red line BPM is multiplied by, at the note's own
    # millisecond; 1.0 leaves it at the chart's own. A red line's BPM is also
    # its scroll speed, so this is a speed change -- and the green line that
    # always follows it divides SV by the same number, so the note ends up
    # travelling at exactly the speed it did before and only the numbers in
    # the file changed.
    shiny_bpm_multiplier: float = 1.0
    # What a fake slider's or Don/Kat's restore line BPM is multiplied by;
    # 1.0 -- the default the owner asked for -- leaves it at the chart's own.
    # Unlike the shiny multiplier there is no green line to pay it back with:
    # nothing is drawn across the retimed section for this to keep looking the
    # same speed, so it is a plain speed change to the fake slider itself.
    fake_slider_bpm_multiplier: float = 1.0
    # The BPM every *restore* line carries -- the line that gives the chart
    # its speed back one offset after a squash, and the bars a barline note
    # is drawn out of. None keeps what these structures always did: the
    # chart's own BPM at that millisecond, times whichever multiplier the
    # structure already had.
    #
    # Its own dial because `gimmick_bpm` and this are two different numbers
    # that were only reachable as one: `gimmick_bpm` is how invisible the
    # note is, and this is how fast the chart runs afterwards. A mapper who
    # wanted a slower section under the structure had to reach it through a
    # multiplier of the chart's BPM, which is not a number they can read off
    # their own timing panel. Same shape as `red_line_bpm` and
    # `anti_wall_bpm`, and for the same reason: a BPM is what the box asks
    # for.
    restore_bpm: float | None = None
    # Fake slider layer: set effects bit 3 (value 8) on the uninherited points
    # a fake slider writes, so its 60000 BPM line does not also draw a barline.
    # On by default -- a fake slider is decoration, and the bar it drew was
    # never wanted. The barline layer ignores it: bars are its whole output.
    omit_barline: bool = True
    # The anti-barline converter's four numbers -- see the constants above for
    # what each one does. Configurable rather than fixed because the wall's
    # density and the two slit widths are the whole look of the gimmick, and
    # what reads as solid depends on the chart's scroll speed as much as on
    # its BPM. Every one of them is derived against the map's own beat length,
    # so the defaults reproduce the reference section at any tempo.
    anti_lines_per_beat: int = DEFAULT_ANTI_LINES_PER_BEAT
    # BPM written on every wall line. None -- the default -- is the chart's own
    # at that millisecond, which is the line that changes nothing but where the
    # bars fall. A typed value is the whole scroll effect: an uninherited
    # point's BPM *is* its scroll speed in taiko, so a wall at a third of the
    # chart's BPM scrolls at a third of the speed and packs its bars three
    # times tighter on screen than their millisecond spacing alone would --
    # which is how the reference section gets from 10.4ms apart to 1.81px
    # apart, under the 4px barline sprite. Same field shape as `red_line_bpm`
    # and for the same reason.
    anti_wall_bpm: float | None = None
    anti_don_ticks: int = DEFAULT_ANTI_DON_TICKS
    anti_kat_ticks: int = DEFAULT_ANTI_KAT_TICKS
    # The hidden anti-barline converter's numbers -- see the constants above.
    # Two BPMs and three SVs, because unlike the classic converter this one has
    # no tick counts to encode a colour in: the slit is opened by SV alone, so
    # the three SV values *are* the gimmick and the BPMs only decide what is
    # invisible and how tight the sheet is.
    hidden_hide_bpm: float = DEFAULT_HIDDEN_HIDE_BPM
    hidden_wall_bpm: float = DEFAULT_HIDDEN_WALL_BPM
    hidden_base_sv: float = DEFAULT_HIDDEN_BASE_SV
    hidden_don_sv: float = DEFAULT_HIDDEN_DON_SV
    hidden_kat_sv: float = DEFAULT_HIDDEN_KAT_SV
    hidden_window_divisor: int = DEFAULT_HIDDEN_WINDOW_DIVISOR
    # Whether a Don/Kat structure hides the note it is drawn around. On (the
    # default, and what every one of these gimmicks does in the wild) the line
    # on the note's own millisecond carries `gimmick_bpm`, which scrolls the
    # note 392px/ms -- never on screen, so the bars or the fake slider beside
    # it are the whole object. Off, that line carries the chart's own BPM with
    # its barline detached: it changes nothing except restarting measure
    # counting, and the structure is drawn around a note you can still see.
    hide_note: bool = True
    # Barline notes only: detach the bar the squash line on the note's own
    # millisecond stamps, so the bars drawn are only the restores either side.
    # Off by default -- that centre bar is part of how the structure has always
    # looked. With `hide_note` off the line omits its barline regardless.
    omit_note_barline: bool = False

    def __post_init__(self) -> None:
        if self.fake_slider_length > FAKE_SLIDER_MAX_LENGTH:
            raise GimmickConfigError(
                f"Fake slider length must be at most {FAKE_SLIDER_MAX_LENGTH}")
        if self.gimmick_bpm <= 0:
            raise GimmickConfigError("Gimmick BPM must be positive")
        if self.spacing_ms < 1:
            raise GimmickConfigError("Don spacing must be at least 1 ms")
        if min(self.kat_spacing1_ms, self.kat_spacing2_ms, self.kat_spacing3_ms) < 1:
            raise GimmickConfigError("Kat spacing must be at least 1 ms")
        if len({self.kat_spacing1_ms, self.kat_spacing2_ms, self.kat_spacing3_ms}) != 3:
            # Two equal spacings write their red lines onto the same
            # millisecond, and only the first of the pair in file order is a
            # meaningful line -- the second is a wasted spin box value.
            raise GimmickConfigError("Kat spacings must be distinct")
        if self.fake_slider_offset_ms < 1:
            raise GimmickConfigError("Fake slider offset must be at least 1 ms")
        if self.fake_slider_sv <= 0:
            raise GimmickConfigError("Fake slider SV must be positive")
        if self.red_line_bpm is not None and self.red_line_bpm <= 0:
            raise GimmickConfigError("Red line BPM must be positive")
        if self.red_line_meter < 1:
            raise GimmickConfigError("Red line meter must be at least 1")
        if self.shiny_offset_ms < 1:
            raise GimmickConfigError("Shiny offset must be at least 1 ms")
        if self.shiny_count < 1:
            raise GimmickConfigError("Shiny note count must be at least 1")
        if self.shiny_bpm_multiplier <= 0:
            raise GimmickConfigError("Shiny BPM multiplier must be positive")
        if self.fake_slider_bpm_multiplier <= 0:
            raise GimmickConfigError("Fake slider BPM multiplier must be positive")
        if self.restore_bpm is not None and self.restore_bpm <= 0:
            raise GimmickConfigError("Red line BPM must be positive")
        if self.anti_lines_per_beat < 1:
            raise GimmickConfigError("Anti-barline density must be at least 1 line per beat")
        if self.anti_wall_bpm is not None and self.anti_wall_bpm <= 0:
            raise GimmickConfigError("Anti-barline barline BPM must be positive")
        if min(self.anti_don_ticks, self.anti_kat_ticks) < 1:
            # A slit of no ticks is a note with nothing marking it: the note
            # itself is squashed to invisibility, so the hole in the wall is
            # the only thing left to see.
            raise GimmickConfigError("Anti-barline slit width must be at least 1 tick")
        if min(self.hidden_hide_bpm, self.hidden_wall_bpm) <= 0:
            raise GimmickConfigError("Hidden anti-barline BPM must be positive")
        if min(self.hidden_base_sv, self.hidden_don_sv, self.hidden_kat_sv) <= 0:
            raise GimmickConfigError("Hidden anti-barline SV must be positive")
        if self.hidden_window_divisor < 1:
            raise GimmickConfigError("Hidden anti-barline window must be at least 1/1 beat")


def spacing_collides(barline: GimmickConfig, fake_slider: GimmickConfig) -> bool:
    """Whether any of the barline layer's spacings lands on the fake slider's
    own offset.

    A barline note puts restore lines at +/- `spacing_ms` (Don) and +/- each
    of `kat_spacing1_ms`/`kat_spacing2_ms`/`kat_spacing3_ms` (Kat); a fake
    slider puts its restore line at +`fake_slider_offset_ms`. Any one of those
    four values equalling the fake slider's offset means the two structures
    fight for the same millisecond whenever they are placed on the same snap,
    and only one uninherited point per millisecond is meaningful. Reported as a
    caution rather than enforced -- it is only wrong where they actually
    overlap, which the editor cannot know in advance.
    """
    offset = fake_slider.fake_slider_offset_ms
    return offset in (
        barline.spacing_ms,
        barline.kat_spacing1_ms,
        barline.kat_spacing2_ms,
        barline.kat_spacing3_ms,
    )


def shiny_collides(config: GimmickConfig) -> bool:
    """Whether the shiny offset lands on the fake slider's own offset.

    Both are drawn objects placed one offset after the snap, so equal offsets
    put them on the same millisecond -- and a stack of fake sliders on one
    millisecond, and which of the two an object is is read off exactly that
    distance. Equal offsets leave the editor no way to tell them apart.
    Reported as a caution rather than enforced, like `spacing_collides`.
    """
    return config.shiny_offset_ms == config.fake_slider_offset_ms


# Which structure an object is, is read off *where* it sits: a fake slider
# layer object one `shiny_offset_ms` after its gimmick line is a shiny note, one
# `fake_slider_offset_ms` after it is a fake slider. Not off how many objects
# share the millisecond -- a plain fake slider is routinely stacked as well, to
# brighten it, so the count says nothing about which of the two it is. This is
# also why the two offsets must differ; see `shiny_collides`.


# -- base timing -----------------------------------------------------------


def snapshot_timing(points: list[TimingPoint]) -> list[TimingPoint]:
    """Detached copy of `points`, safe to keep while the document is edited."""
    return [point.copy() for point in points]


def looks_gimmicked(points: list[TimingPoint], threshold: float = DIRTY_BASE_BPM) -> bool:
    """Whether a snapshot appears to already contain gimmick lines.

    "Use this one" on an already-gimmicked difficulty captures that mess as its
    base timing, permanently. The editor warns on this and continues; there is
    no filtering, because a genuine 1200 BPM stream is indistinguishable from a
    gimmick line without knowing the mapper's intent.
    """
    return any(point.bpm is not None and point.bpm > threshold for point in points)


def base_bpm_at(base_timing: list[TimingPoint], time_ms: float) -> float:
    """BPM of the base snapshot at `time_ms`, for the restore lines."""
    point = active_uninherited_at(base_timing, time_ms)
    return point.bpm or 120.0


# -- pairing index ---------------------------------------------------------


def restore_bpm_at(
    base_timing: list[TimingPoint],
    time_ms: float,
    config: GimmickConfig,
    multiplier: float = 1.0,
) -> float:
    """The BPM a structure's restore line carries.

    One place, because a barline note's bars and a fake slider's restore are
    the same line doing the same job, and they disagreed about where their BPM
    came from: the fake slider had a multiplier of the chart's BPM and the
    barline note had no dial at all. `config.restore_bpm` is the typed value
    the owner asked for; None is what both did before it existed.
    """
    if config.restore_bpm is not None:
        return config.restore_bpm
    return base_bpm_at(base_timing, time_ms) * multiplier


@dataclass(slots=True)
class GimmickPairing:
    """One remembered session: which file it edits, and its base timing.

    `reference` is the difficulty the base timing was read out of. It is asked
    for once, on entry, and remembered with the pairing, because a gimmick
    difficulty is the worst possible source for its own grid: by the second
    placement it is full of 60000 BPM lines, and every one of them restarts
    measure counting. A clean sibling difficulty keeps saying where the beats
    are however far the gimmick file drifts from them. It is kept only so the
    editor can say which file it is honouring -- the snapshot itself is stored,
    not re-read, so editing the reference later does not silently move the grid
    under an in-progress gimmick.
    """

    target: Path
    base_timing: list[TimingPoint] = field(default_factory=list)
    reference: Path | None = None


def load_index(path: Path) -> dict[str, GimmickPairing]:
    """Read the pairing index, or start empty when missing or unusable.

    Same shape and failure behaviour as `song_library.load_cache`: a corrupt or
    version-mismatched file is treated as no file at all, because the worst case
    is being asked the setup question once more.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("version") != INDEX_VERSION:
        return {}
    pairings: dict[str, GimmickPairing] = {}
    for source, entry in payload.get("pairings", {}).items():
        try:
            target = Path(entry["target"])
        except (KeyError, TypeError):
            continue
        points = [parse_timing_line(line) for line in entry.get("base", [])]
        reference = entry.get("reference")
        pairings[source] = GimmickPairing(
            target,
            [p for p in points if p is not None],
            Path(reference) if reference else None,
        )
    return pairings


def save_index(path: Path, pairings: dict[str, GimmickPairing]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": INDEX_VERSION,
        "pairings": {
            source: {
                "target": str(pairing.target),
                "reference": str(pairing.reference) if pairing.reference else None,
                # Stored as the file's own text, so a snapshot round-trips
                # through the same parser the map does.
                "base": [serialize_timing_point(point, "") for point in pairing.base_timing],
            }
            for source, pairing in pairings.items()
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def index_key(source: Path) -> str:
    """Absolute path of the difficulty the user entered the tab from.

    Moving the Songs folder loses every pairing, which costs one re-answer of
    the setup dialog and a fresh base snapshot.
    """
    return str(Path(source).resolve())


def difficulty_setting(document, key: str, default: float) -> float:
    """Read one [Difficulty] value out of a document's raw lines.

    `write_osu` always rewrites ApproachRate and CircleSize from its arguments,
    so copying a difficulty faithfully means handing it back the values the
    source already had rather than letting the export defaults through.
    """
    section = ""
    for line in document.lines:
        stripped = line.rstrip("\r\n").strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section == "Difficulty" and stripped.startswith(f"{key}:"):
            try:
                return float(stripped.split(":", 1)[1].strip())
            except ValueError:
                return default
    return default


def gimmick_path_for(source: Path) -> Path:
    """`... [Oni].osu` -> `... [Oni] [Gimmick].osu`, in the same folder."""
    return Path(source).with_name(f"{Path(source).stem} [Gimmick].osu")


def gimmick_version_for(version: str) -> str:
    return f"{version} [Gimmick]"


# -- structure builders ----------------------------------------------------
#
# Each returns (timing points, hit objects) for one placement. The caller wraps
# both lists in a single CompositeCommand so a placement is one undo step.

# The hitsound the *real* note a placement writes carries. Don and Kat mean an
# actual don and an actual kat here; what the structure drawn beside that note
# does to say which of the two it is differs per layer -- a fake slider changes
# size (see `_SLIDER_HITSOUND`), a barline note changes how many bars it draws.
_KIND_HITSOUND = {"regular": 0, "don": 0, "kat": HITSOUND_CLAP}

# ...and the hitsound the *fake slider* carries. A drumroll has no don/kat of
# its own -- it is yellow whatever is under it -- so the only thing it can say
# about which button the structure means is its size, and the finisher bit is
# what makes it the big one. Kat is the big fake slider; don is the small one.
_SLIDER_HITSOUND = {"regular": 0, "don": 0, "kat": HITSOUND_FINISH}


def _circle(time_ms: int, hit_sound: int, x: int = 256, y: int = 192) -> HitObject:
    """A plain, hittable taiko note."""
    return HitObject(
        x=x, y=y, time=time_ms, type=TYPE_CIRCLE, hit_sound=hit_sound,
        hit_sample="0:0:0:0:",
    )


def fake_slider(
    time_ms: int,
    base_timing: list[TimingPoint],
    config: GimmickConfig,
    *,
    kind: str = "regular",
    x: int = 256,
    y: int = 192,
    shiny: bool = False,
    copies: int | None = None,
    big: bool = False,
) -> tuple[list[TimingPoint], list[HitObject]]:
    """A drumroll of negative length -- what draws without ever being hittable.

    Three structures share this builder, because everything about the drawn
    object -- its shape, its offsets, which kind gets the finisher bit -- is
    identical between them, and only the timing points that shape it differ.
    Which one is built is read off `kind` and `shiny`:

    * **plain fake slider** (`kind="regular"`, not shiny) -- no note, so
      nothing to hide. One red line, on the slider itself, `fake_slider_offset_ms`
      after the snap and retimed by `fake_slider_bpm_multiplier`. There is no
      squash: a squash exists to hide a hittable note, and this structure does
      not have one.
    * **shiny** (`shiny=True`) -- a glow around a note that *stays visible*, so
      squashing it is not an option: that would delete the very note the glow
      decorates. Its red line sits on the snap itself, retimed by
      `shiny_bpm_multiplier`, and that is the whole structure: no green line.
      Written only when `shiny_red_line` is on -- see that field for what
      dropping it costs. `shiny_count` sliders, the stack that reads as
      white, sit `shiny_offset_ms` later, beside the note rather than on it.
      With a Don/Kat `kind` a real, hittable note is written on the snap as
      well, undisturbed by any of this.
    * **Don / Kat, not shiny** -- the one case that deliberately hides its
      note. A gimmick-BPM line squashes the snap to nothing; an SV line on top
      of it (`fake_slider_sv`) -- the uninherited point has just reset SV to
      1.0x, so this is a green line stacked after the red one -- takes the
      already-squashed note the rest of the way off screen; and the chart's
      own BPM comes back `fake_slider_offset_ms` later (retimed by
      `fake_slider_bpm_multiplier`), which is exactly where the fake slider --
      small for Don, big (finisher bit) for Kat -- is the only thing left to
      look at.

    **No structure here carries the chart's own SV.** The only green line left
    is the Don/Kat `fake_slider_sv`, which is the gimmick's own number and
    exists to finish taking an already-squashed note off screen -- so it is
    written only when `hide_note` is on, and dropped with the squash when it is
    off. A fake slider is decoration: inheriting the speed of whichever section
    it was dropped in made identical structures scroll differently.

    Wherever there is a green line, it is always written *after* the red line
    sharing its millisecond: osu! resolves a shared timestamp by file order,
    and an uninherited point resets SV to 1.0x, so the other way round would
    silently cancel it.

    `big` is Shift-placement, exactly the finisher bit a Shift-clicked Don or
    Kat gets in a normal chart -- here it makes the drawn object the big one.
    Only `kind="regular"` (the plain fake slider and the shiny both) honours
    it: a Don or Kat already spends that bit saying which of the two it is
    (see `_SLIDER_HITSOUND`), so its size is not free to mean anything else.

    `copies` overrides the slider count for one call; otherwise it is
    `shiny_count` for a shiny and 1 otherwise. The note, where there is one, is
    what the player hits; the fake slider is the decoration next to it, which
    is why it cannot share the note's millisecond -- two hit objects on one
    timestamp leaves one of them permanently invisible.

    None of this takes the red-line placement offset -- that belongs to the
    plain red-line tool alone.
    """
    if kind not in _KIND_HITSOUND:
        raise GimmickConfigError(f"Unknown fake slider kind: {kind!r}")
    time_ms = int(time_ms)
    if copies is None:
        copies = config.shiny_count if shiny else 1
    copies = max(1, int(copies))
    omit = config.omit_barline

    if shiny:
        # The note the glow decorates stays on the snap and stays visible, so
        # the red line governing it sits there too rather than on an offset --
        # there is nothing to squash. The stack that makes the glow read as
        # white is a separate, later object; see `shiny_offset_ms`.
        slider_at = time_ms + config.shiny_offset_ms
        # No green line. This used to restate the chart's SV divided by the
        # multiplier -- both a correction for the retiming and the handle the
        # SV layer was made of -- but the chart's speed has no business
        # reaching a shiny: the same structure scrolled differently depending
        # on which section it was dropped in, which is not what "shiny" is.
        # The retiming compensation goes with it, deliberately.
        if config.shiny_red_line:
            # A shiny's line sits on its own note rather than restoring
            # anything, so `restore_bpm` deliberately does not reach it.
            red_bpm = base_bpm_at(base_timing, time_ms) * config.shiny_bpm_multiplier
            points = [TimingPoint.uninherited_at(time_ms, red_bpm, omit_first_barline=omit)]
        else:
            points = []
    elif kind == "regular":
        # No note, no squash: the drawn object alone, one offset after the
        # snap, at the chart's own BPM -- and whether it gets a line at all is
        # the mapper's call (`fake_slider_red_line`).
        #
        # There is nothing to hide here, so that line only ever applied
        # `fake_slider_bpm_multiplier`: at the default it re-states the chart's
        # own BPM and changes nothing, and a run of them (the Multiple tool)
        # writes one dead line per slider restating a speed already in force.
        # Off is therefore the cheaper structure and on is the one that can be
        # retimed, which is why it is a dial rather than a decision made here.
        #
        # Don and Kat are never gated -- the squash *is* the gimmick. A shiny
        # has its own `shiny_red_line` dial rather than this one, because a
        # standalone shiny (no note of its own) loses its only identifying
        # mark when that line goes; see `shiny_red_line`.
        slider_at = time_ms + config.fake_slider_offset_ms
        if config.fake_slider_red_line:
            bpm = restore_bpm_at(
                base_timing, slider_at, config, config.fake_slider_bpm_multiplier)
            points = [TimingPoint.uninherited_at(slider_at, bpm, omit_first_barline=omit)]
            # ...and the chart's own speed back one millisecond later, so the
            # line above governs the slider rather than the rest of the map.
            # See `fake_slider_restore_after`.
            if config.fake_slider_restore_after:
                points.append(TimingPoint.uninherited_at(
                    slider_at + FAKE_SLIDER_RESTORE_OFFSET_MS,
                    base_bpm_at(base_timing, slider_at + FAKE_SLIDER_RESTORE_OFFSET_MS),
                    omit_first_barline=omit,
                ))
        else:
            points = []
    else:
        slider_at = time_ms + config.fake_slider_offset_ms
        restore_bpm = restore_bpm_at(
            base_timing, slider_at, config, config.fake_slider_bpm_multiplier)
        # `hide_note` off keeps the note on screen, and both halves of the
        # squash have to go for that: the chart's own BPM in place of the
        # gimmick one, and the chart's own SV in place of `fake_slider_sv` --
        # that green line exists to take an already-squashed note the rest of
        # the way off screen, so left in it would fling the visible note off
        # the screen the red line was just told to keep it on. The green line
        # itself stays, restating the speed in force: it is the handle this
        # layer's SV band is made of, exactly as for a shiny.
        points = [
            TimingPoint.uninherited_at(
                time_ms,
                config.gimmick_bpm if config.hide_note
                else base_bpm_at(base_timing, time_ms),
                omit_first_barline=omit or not config.hide_note,
            ),
            # Only `fake_slider_sv`, and only when it has a squashed note to
            # finish taking off screen. With `hide_note` off this restated the
            # chart's own SV, which is the one thing this layer must not carry:
            # a fake slider is decoration, and inheriting the speed of whatever
            # section it landed in made identical structures scroll
            # differently. `fake_slider_sv` is the gimmick's own number and
            # stays -- without it the hidden note is not hidden.
            *(
                [TimingPoint.inherited_at(time_ms, config.fake_slider_sv)]
                if config.hide_note else []
            ),
            TimingPoint.uninherited_at(slider_at, restore_bpm, omit_first_barline=omit),
        ]

    slider_hitsound = _SLIDER_HITSOUND[kind]
    if big and kind == "regular":
        slider_hitsound |= HITSOUND_FINISH

    notes = [] if kind == "regular" else [_circle(time_ms, _KIND_HITSOUND[kind])]
    notes.extend(
        HitObject(
            x=x, y=y, time=slider_at, type=TYPE_SLIDER, hit_sound=slider_hitsound,
            extras=(f"L|{x + 100}:{y}", "1", format_length(config.fake_slider_length)),
            hit_sample="0:0:0:0:",
        )
        for _ in range(copies)
    )
    return points, notes


def format_length(length: float) -> str:
    """Keep -1 as "-1" rather than "-1.0"; osu! reads both, mappers read one."""
    return str(int(length)) if length == int(length) else repr(float(length))


def barline_note(
    time_ms: int,
    base_timing: list[TimingPoint],
    config: GimmickConfig,
    *,
    kind: str = "don",
) -> tuple[list[TimingPoint], list[HitObject]]:
    """A note drawn out of barlines: one gimmick BPM line plus mirrored restores.

    Every uninherited point emits a barline at its own time, so the count of
    restore lines is the count of bars drawn. Don is a single pair, at
    +/-`spacing_ms`; kat is three pairs, at +/-`kat_spacing1_ms`,
    +/-`kat_spacing2_ms` and +/-`kat_spacing3_ms`, independently of each other
    and of Don's own spacing -- which is what makes kat read as the wider note,
    and what lets a mapper hand-layer several structures on one millisecond
    region without a shared "n" forcing their widths apart in lockstep. Whether
    each kind is mirrored at all is `mirror_don_lines` / `mirror_kat_lines`,
    separately for the same reason.

    With `place_notes` on (the default) a real, hittable note goes on the snap
    as well, so the drawn bars are something the player actually plays. Off, the
    bars are all that is written -- for gimmicks that use them as scenery.

    The gimmick line always lands exactly on `time_ms` -- the placement offset
    does not apply here.
    """
    if kind == "don":
        spacings = (config.spacing_ms,)
        mirrored = config.mirror_don_lines
    elif kind == "kat":
        spacings = (
            config.kat_spacing1_ms, config.kat_spacing2_ms, config.kat_spacing3_ms,
        )
        mirrored = config.mirror_kat_lines
    else:
        raise GimmickConfigError(f"Unknown barline note kind: {kind!r}")

    # Mirrored, bars either side of the note read as one object centred on
    # it. Forward only -- a squash on the note and a single restore one
    # millisecond later -- is what several hand-made maps use, and is Don's
    # default; Kat defaults to mirrored. Per kind, so a centred Don can sit
    # beside a trailing Kat.
    offsets = (
        [-value for value in spacings] + list(spacings) if mirrored
        else list(spacings)
    )

    time_ms = int(time_ms)
    # `hide_note` off swaps the squash for a line that does nothing but restart
    # measure counting -- the chart's own BPM, and its own barline detached so
    # it does not stamp a bar in the middle of the note it is now showing. The
    # restores either side still draw the bars, so the structure is the same
    # shape with a visible note inside it.
    points = [
        TimingPoint.uninherited_at(
            time_ms, config.gimmick_bpm, omit_first_barline=config.omit_note_barline,
        ) if config.hide_note
        else TimingPoint.uninherited_at(
            time_ms, base_bpm_at(base_timing, time_ms), omit_first_barline=True,
        )
    ]
    for offset in offsets:
        at = time_ms + offset
        points.append(
            TimingPoint.uninherited_at(at, restore_bpm_at(base_timing, at, config)))
    points.sort(key=lambda point: point.time)
    notes = [_circle(time_ms, _KIND_HITSOUND[kind])] if config.place_notes else []
    return points, notes


def keep_sv(points: list[TimingPoint], model, at_note: float | None = None) -> list[TimingPoint]:
    """A green line after every red line in `points`, at the chart's SV there.

    Every structure is red lines, and each resets SV to 1.0x for whatever
    follows it -- bars, the note, the wall. This writes the chart's own speed
    back on the same millisecond, read from `model` (`sv_curves.SvModel`):
    exactly what osu! read at a note, the chart's curve between notes where
    the chart changed speed. Placed straight after its red line, since osu!
    resolves a shared millisecond by file order. A green line the builder
    already put on that millisecond is replaced rather than stacked.

    `at_note`: the structure is one object drawn around that note, so every
    line of it takes the *note's* value. Read per line, a barline note right
    after a break wrote its leading bars (T-5..T-1) at the speed before the
    break and the rest at the note's, and the bars came apart (owner,
    2026-10-09, Darling Game Over Love at 50841ms). Its first element is in
    effect where the ramp toward the note ends.
    """
    reds = [point for point in points if point.uninherited]
    taken = {round(point.time) for point in reds}
    kept = [point for point in points
            if point.uninherited or round(point.time) not in taken]
    result: list[TimingPoint] = []
    for point in sorted_by_time(kept):
        result.append(point)
        if point.uninherited:
            result.append(TimingPoint.inherited_at(
                point.time, model.value_at(point.time if at_note is None else at_note)))
    return result


def anti_barline(
    notes: list[HitObject],
    start_ms: float,
    end_ms: float,
    base_timing: list[TimingPoint],
    config: GimmickConfig,
    model=None,
) -> list[TimingPoint]:
    """`barline_note` in negative: a solid wall of bars with the notes as gaps.

    A barline note draws bars *around* a note. This packs the lane with bars
    until it reads as one white sheet and takes bars *away* where each note is,
    so every note is a black slit travelling in the sheet -- the exact inverse
    of a barline. `mekurume [eclosion]` 1:52.5-2:15.7 is the reference.

    Two structures, and they only work together:

    * **The wall.** One uninherited point every `beat / anti_lines_per_beat`,
      each carrying `anti_wall_bpm` -- or the chart's own where that is None.
      A BPM below the chart's is what packs the bars tighter than their
      millisecond spacing alone would: an uninherited point's BPM is its scroll
      speed, so a third of it draws the same lines a third as far apart. It
      also has to keep `beat_length x meter` longer than the tick spacing, or
      a wall line emits a *second* barline before the next one arrives -- which
      the chart's own BPM and anything slower does with room to spare.
    * **The notes.** Per note, a squash line on its own millisecond and a
      restore one after it. The squash makes the note scroll 392px/ms, which is
      to say invisible; the restore hands the chart's BPM back with a meter
      long enough that it never emits a bar of its own. **Both carry
      omit-first-barline**, and that is the whole subtlety -- each is a red
      line, and without the flag each would stamp a bar in the middle of the
      slit it exists to open. With `hide_note` off there is no squash and so
      nothing to restore: one line, the chart's own BPM, same flag and same
      meter, and the note travels through its own slit in plain sight.

    The slit itself is the wall ticks nearest the note, left out. Its width is
    the note's *colour* (`anti_don_ticks` / `anti_kat_ticks`), because the note
    is invisible and there is nothing else to read one off. Centred on the
    note, since the note's own two lines sit in the middle of the hole.

    Only plain circles are converted. A finisher, a drumroll and a spinner are
    all left exactly as they are: a finisher is marked some other way (the
    reference section uses a fake slider on the six downbeats), and a body that
    lasts longer than a slit has nothing to be a slit of.

    Returns timing points only. The notes are the chart's own and are not
    rewritten -- being converted here changes nothing about what is played.

    With `model` (`sv_curves.SvModel`, the experimental conversion) every
    line's speed is the chart's own curve rather than its step: the wall's
    green lines follow the sweep between notes, and the note's own lines get
    one too, so a visible note keeps the speed it had (`keep_sv`).
    """
    eligible = sorted(
        (note for note in notes if note.is_circle and not note.is_finisher),
        key=lambda note: note.time,
    )
    if not eligible:
        return []
    # The red lines alone, looked up per wall tick: `active_uninherited_at`
    # steps back one point at a time, and over the full snapshot that was
    # every green line between a tick and its red line -- 6.2s of a 22s
    # whole-map conversion (Darling Game Over Love, 2026-10-09).
    full_timing = base_timing
    base_timing = [point for point in sorted_by_time(base_timing) if point.uninherited] or base_timing

    def step_at(time_ms: float) -> float:
        """Tick spacing at `time_ms`. Read per tick rather than once for the
        range: the spacing is a fraction of a beat, so a BPM change inside the
        range has to move the wall with it or the sheet thins out over the
        second half of the section."""
        return 60000.0 / base_bpm_at(base_timing, time_ms) / config.anti_lines_per_beat

    def slit_of(note) -> int:
        return config.anti_kat_ticks if note.is_kat else config.anti_don_ticks

    def reach_of(note) -> int:
        """Ticks the wall runs past an edge note. The slit fills nearer side
        first, so one side gives up at most half of it rounded up, and one bar
        has to be left beyond that. From 2 ticks up the slit's own width
        already covers both; a 1-tick slit reached one tick, the slit took it,
        and the last note had no sheet after it."""
        slit = slit_of(note)
        return max(slit, -(-slit // 2) + 1)

    # The wall fills the dragged range, and reaches past the notes at either
    # end of it by one slit's width more. A note is a *hole in a sheet*, so it
    # needs sheet on both sides of it: anchored at the first note the wall
    # began where that note is and the first note was the leading edge of the
    # sheet rather than anything travelling in it -- and the last note lost its
    # trailing bars the same way, since a drag naturally ends on it. Its own
    # slit width is the bound because that is exactly what has to fit: half of
    # it for the hole, and as much again beyond for the bars the hole is in.
    first, last = eligible[0], eligible[-1]
    wall_start = max(0.0, min(start_ms, first.time - reach_of(first) * step_at(first.time)))
    wall_end = max(end_ms, last.time + reach_of(last) * step_at(last.time))

    # Phased off the first note, never started on it -- see
    # ANTI_WALL_ANCHOR_OFFSET_MS. The grid runs out from that anchor in both
    # directions so the phase is the same either side of it.
    anchor = float(round(first.time) + ANTI_WALL_ANCHOR_OFFSET_MS)
    positions = []
    at = anchor
    while at >= wall_start:
        positions.append(at)
        at -= step_at(at)
    positions.reverse()
    at = anchor + step_at(anchor)
    while at <= wall_end:
        positions.append(at)
        at += step_at(at)

    ticks: list[int] = []
    for at in positions:
        # Whole milliseconds, off a fractional accumulator: the spacing is
        # rarely an integer (750/36 is 20.833) and rounding the running total
        # rather than the step keeps the wall from drifting off it. Rounded
        # because a line is claimed by its millisecond everywhere else in the
        # editor -- a layer owns `round(point.time)` -- and because two ticks
        # can round together once the density is high enough to put them under
        # a millisecond apart, where only the first would have counted anyway.
        tick = round(at)
        if not ticks or tick != ticks[-1]:
            ticks.append(tick)
    if not ticks:
        return []

    dropped: set[int] = set()
    for note in eligible:
        wanted = config.anti_kat_ticks if note.is_kat else config.anti_don_ticks
        # Outward from the note, nearer side first, so the hole stays centred
        # on it whether the count is odd or even.
        after = bisect_left(ticks, note.time)
        before = after - 1
        for _ in range(wanted):
            if before >= 0 and (
                after >= len(ticks)
                or note.time - ticks[before] <= ticks[after] - note.time
            ):
                dropped.add(before)
                before -= 1
            elif after < len(ticks):
                dropped.add(after)
                after += 1
            else:
                break

    points: list[TimingPoint] = []
    for index, at in enumerate(ticks):
        if index in dropped:
            continue
        points.append(TimingPoint.uninherited_at(
            at,
            config.anti_wall_bpm if config.anti_wall_bpm is not None
            else base_bpm_at(base_timing, at),
        ))
        # A green line on every wall line, straight after it -- never before,
        # since osu! resolves a shared timestamp by file order and the red one
        # has just reset SV to 1.0x.
        #
        # Written even where it restates the 1.0x the red line has already
        # forced, because it is the handle the SV (barlines) layer is made of:
        # that layer matches its green lines by exact millisecond, so a wall
        # with none had nothing in it to show, drag or sweep -- a thousand
        # barlines whose speed could not be touched. Same rule
        # `_gimmick_commands` applies to every other structure.
        #
        # And it carries the chart's own SV rather than 1.0x, so a wall drawn
        # across a section the map had sped up does not silently flatten it.
        points.append(TimingPoint.inherited_at(
            at, model.value_at(at) if model is not None else sv_at(full_timing, at)))
    for note in eligible:
        time_ms = round(note.time)
        if not config.hide_note:
            # One line where there were two. The pair exists because the squash
            # takes the chart's BPM away and something has to hand it back; a
            # line that never took it away has nothing to restore, so this is
            # the restore alone -- the chart's own BPM, its barline detached so
            # it stamps none in the slit, and a meter long enough that it never
            # emits one later either.
            points.append(TimingPoint.uninherited_at(
                time_ms, base_bpm_at(base_timing, time_ms),
                meter=ANTI_RESTORE_METER, omit_first_barline=True,
            ))
            continue
        restore_at = time_ms + ANTI_RESTORE_OFFSET_MS
        points.append(TimingPoint.uninherited_at(
            time_ms, config.gimmick_bpm, omit_first_barline=True,
        ))
        points.append(TimingPoint.uninherited_at(
            restore_at, base_bpm_at(base_timing, restore_at),
            meter=ANTI_RESTORE_METER, omit_first_barline=True,
        ))
    if model is not None:
        # The objects the wall leaves alone -- finishers, drumrolls, spinners
        # -- have no line of their own, so each would read the wall's ramp
        # at its millisecond rather than its own speed (measured: 0.0085x off
        # on an installed map). A green line on it says its own value.
        converted = {round(note.time) for note in eligible}
        for note in notes:
            at = round(note.time)
            if start_ms <= note.time <= end_ms and at not in converted:
                points.append(TimingPoint.inherited_at(at, model.value_at(at)))
        return keep_sv(points, model)
    points.sort(key=lambda point: point.time)
    return points


def hidden_anti_barline(
    notes: list[HitObject],
    start_ms: float,
    end_ms: float,
    base_timing: list[TimingPoint],
    config: GimmickConfig,
) -> list[TimingPoint]:
    """`anti_barline` with the wall drawn by osu! and the slit cut with SV.

    The classic converter writes a red line per bar and takes bars away. This
    writes **one** meter-1 red line and lets osu! emit every bar of the wall
    from it, then opens each note's slit by raising SV for a fraction of a beat
    after the note -- see the constants above for why a velocity change is a
    gap. Four lines per note against the other one's hundreds, and the slit
    grows with distance instead of being a fixed number of pixels.

    Per plain circle at `T`:

        T,   hide BPM,  meter 999, omit-first-barline   # the note, gone
        T+1, wall BPM,  meter 1                         # the sheet, resumed
        T+1, SV = Don's or Kat's                        # the slit opens
        T+1/n beat, SV = the base                       # and closes again

    Both the hide line and the wall line are red lines and would each stamp a
    bar of their own; the hide line carries omit-first-barline for that reason
    and the wall line deliberately does not -- its bar is the first bar of the
    resumed sheet.

    Only plain circles are converted, the same rule and for the same reasons as
    `anti_barline`: a finisher is marked some other way and a body outlasting
    its own slit has nothing to be a slit of.

    Returns timing points only -- the notes are the chart's own and still play
    exactly as they did.
    """
    eligible = sorted(
        (note for note in notes if note.is_circle and not note.is_finisher),
        key=lambda note: note.time,
    )
    if not eligible:
        return []

    def beat_at(time_ms: float) -> float:
        return 60000.0 / base_bpm_at(base_timing, time_ms)

    first, last = eligible[0], eligible[-1]
    # A note is a hole in a sheet, so it needs sheet in front of it: a drag
    # naturally starts on the first note, and anchored there that note would be
    # the leading edge rather than anything travelling in the wall. One beat is
    # what the reference section uses.
    wall_start = round(min(start_ms, first.time - beat_at(first.time)))

    points: list[TimingPoint] = [
        TimingPoint.uninherited_at(
            wall_start, config.hidden_wall_bpm, meter=HIDDEN_WALL_METER,
        ),
        TimingPoint.inherited_at(wall_start, config.hidden_base_sv),
    ]
    for note in eligible:
        time_ms = round(note.time)
        wall_at = time_ms + HIDDEN_WALL_OFFSET_MS
        # Clamped past the wall line rather than allowed to land on it: at a
        # high enough BPM 1/n of a beat is under a millisecond, and a restore
        # sharing the wall line's timestamp would close the slit before it
        # opened -- the later line in file order wins.
        restore_at = max(
            wall_at + 1, round(time_ms + beat_at(time_ms) / config.hidden_window_divisor)
        )
        # `hide_note` off swaps the hide BPM for the chart's own. Everything
        # else is unchanged, slit included: the red line still resets SV to
        # 1.0x for its millisecond, so the note travels at the chart's normal
        # speed while the sheet around it crawls at the wall SV -- visible, and
        # moving against the bars rather than with them.
        points.append(TimingPoint.uninherited_at(
            time_ms,
            config.hidden_hide_bpm if config.hide_note
            else base_bpm_at(base_timing, time_ms),
            meter=HIDDEN_HIDE_METER, omit_first_barline=True,
        ))
        points.append(TimingPoint.uninherited_at(
            wall_at, config.hidden_wall_bpm, meter=HIDDEN_WALL_METER,
        ))
        points.append(TimingPoint.inherited_at(
            wall_at, config.hidden_kat_sv if note.is_kat else config.hidden_don_sv,
        ))
        points.append(TimingPoint.inherited_at(restore_at, config.hidden_base_sv))

    # Closing the wall is not optional. Its last line has meter 1 and no end of
    # its own, so without this the sheet runs on to the map's next red line --
    # which on a chart with clean timing is the rest of the song. Placed on the
    # chart's own next barline after the range so the bars resume in phase, and
    # carrying that section's real BPM and meter.
    wall_end = max(end_ms, last.time + beat_at(last.time))
    closing = active_uninherited_at(base_timing, wall_end)
    measure = closing.beat_length * max(1, closing.meter)
    at = closing.time
    if measure > 0:
        at += math.ceil((wall_end - closing.time) / measure) * measure
    points.append(TimingPoint.uninherited_at(
        max(round(at), points[-1].time + 1),
        60000.0 / closing.beat_length,
        meter=max(1, closing.meter),
    ))
    points.sort(key=lambda point: point.time)
    return points


def sv_restore_point(
    points: list[TimingPoint],
    time_ms: float,
    restore_at: float,
) -> TimingPoint:
    """Green line re-stating the SV in force at `time_ms`, at `restore_at`.

    A gimmick's uninherited lines reset SV to 1.0x, which silently drops the
    chart's scroll speed for everything after them. This hands that speed back.

    Written even when the chart was already at 1.0x by its own timing. The line
    changes nothing on its own then -- the uninherited point it follows has just
    said 1.0x -- but it is the handle the SV layer for that structure is made of:
    a layer matches its green lines by exact millisecond, so with nothing there
    it has no line to show, drag or generate from. Writing one that agrees with
    the timing beats making the user place one by hand before they can edit it.

    **The fake slider layer no longer asks for one.** It carries no SV from the
    chart in any form, so `_gimmick_commands` skips this call for it -- which
    does cost layer 5 the handles described above, deliberately. Everything
    still calling this (the barline structures, the plain red line tool) is
    unaffected.
    """
    active = active_point_at(sorted_by_time(points), time_ms)
    if active is None:
        return TimingPoint.inherited_at(restore_at, 1.0)
    return TimingPoint.inherited_at(
        restore_at,
        1.0 if active.uninherited else active.sv_multiplier,
        template=active,
    )


def carry_active_state(points: list[TimingPoint], document_points: list[TimingPoint]) -> list[TimingPoint]:
    """Carry the map's state at each point's time onto that point.

    Two fields live on the timing point rather than on a span, so whichever
    point is active at a millisecond decides them for everything after it until
    the next one:

    * **Kiai.** Every line a gimmick writes inside a kiai section has to say
      so, or the first one silently ends the section -- which is what made a
      gimmick placed in a chorus switch the chorus off.
    * **Hitsound volume.** A generated line carries `TimingPoint`'s default of
      100% unless told otherwise, so a structure placed inside a quiet section
      used to slam it back to full volume for the rest of that section.

    Applied to the built points rather than passed into each builder: it is the
    same rule for red lines, green lines and every structure, and the state it
    reads is the live document's, which the builders deliberately do not see.
    """
    ordered = sorted_by_time(document_points)
    for point in points:
        active = active_point_at(ordered, point.time)
        if active is None:
            continue
        if active.kiai:
            point.set_kiai(True)
        point.volume = active.volume
    return points


def red_line(
    time_ms: int,
    bpm: float,
    config: GimmickConfig,
) -> tuple[list[TimingPoint], list[HitObject]]:
    """The plain red-line tool: one uninherited point at snap + offset.

    This is the only tool the placement offset applies to. Nudging it off the
    snap is how a gimmick avoids sharing a millisecond with the real chart's own
    timing line, since only one uninherited point per millisecond is meaningful.

    `bpm` is the base timing's own BPM at that millisecond -- what the caller
    reads when the layer is configured to leave it alone. `red_line_bpm`
    overrides it: an uninherited point's BPM *is* its scroll speed, so a typed
    value is a standalone speed change with no green line involved.
    `red_line_meter` is where its bars fall, since osu! takes meter from the
    uninherited point in force and a line written at 4/4 re-bars the rest of
    the chart whether the mapper meant it to or not.
    """
    at = int(time_ms) + config.red_line_offset_ms
    return [TimingPoint.uninherited_at(
        at, config.red_line_bpm or bpm, meter=config.red_line_meter
    )], []


# -- oscillating SV --------------------------------------------------------


def oscillating_series(
    base: float,
    difference: float,
    count: int,
    ease,
    *,
    per_pair: bool = False,
) -> list[float]:
    """SV values fanning out from `base`, alternating side.

    Index 0 sits on the base and `difference` is always the *total* amplitude
    reached by the last point. What the caller chooses is how fast the fan
    opens, and both modes are available on every growth function:

    * **per point** (default) -- the amplitude advances on every point, so the
      two sides peak at different widths::

          base 1.40, difference 0.12, exp1.6, 7 points ->
          1.400  1.407  1.379  1.440  1.337  1.490  1.280

    * **per pair** -- it advances once per up/down pair, so each pair is
      symmetric about the base::

          base 1.00, difference 0.06, linear, 7 points ->
          1.00  1.02  0.98  1.04  0.96  1.06  0.94

    Linear is usually configured as a per-step value rather than a total.
    Converting that to a total is the dialog's job, so this stays one formula.

    `ease` is `gui.sv_ease` bound to a growth function; passed in rather than
    imported so this module stays free of the GUI.
    """
    if count <= 0:
        return []
    if count == 1:
        return [base]
    levels = count // 2 if per_pair else count - 1
    values = []
    for index in range(count):
        level = (index + 1) // 2 if per_pair else index
        amplitude = difference * ease(level / levels)
        sign = 1 if index % 2 else -1
        values.append(base + sign * amplitude)
    values[0] = base
    return values
