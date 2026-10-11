"""The music transport: decode to PCM ourselves, and drive the output device.

Replaces `QMediaPlayer` for song playback. The reason is not preference, it is
that everything making osu!'s editor accurate lives below the level Qt's player
exposes, and none of it can be reached from up there:

- **The clock is a sample cursor.** `TrackBass.updateCurrentTime` asks BASS for
  a byte position and converts it. `QAudioSink.processedUSecs()` is the same
  thing -- measured, not assumed: writing into a 250ms sink buffer keeps the
  written cursor exactly 250ms ahead of `processedUSecs`, so what it reports is
  audio actually played out, not audio handed over. `QMediaPlayer.position()`
  is a signal in whole milliseconds arriving whenever the backend feels like it.
- **Slowing down preserves pitch.** osu!'s editor slows down with
  `AdjustableProperty.Tempo` (BASS_FX time-stretch) at exactly the rates this
  app offers -- 0.25, 0.5, 0.75, 1.0 -- not with a frequency change. Qt's
  `setPlaybackRate` is a plain resample: measured on the FFmpeg backend, a
  0.25x request delivers 0.261s of source per second of wall clock, which is a
  turntable slowed down, pitch and all. So the stretch is ours to do, and
  `TimeStretcher` below is the WSOLA that does it.
- **A rate change is a live attribute, not a restart.** osu! sets a tempo value
  on a running stream. `QMediaPlayer.setPlaybackRate` stalls the backend:
  measured on Windows Media Foundation, 114ms of song time vanishes across a
  0.25x -> 1.0x switch and 67ms at 1.0x -> 0.75x. Here the rate is a number the
  stretcher reads on its next grain, so there is nothing to stall.
- **Output latency is ours to choose.** The sink's buffer is 250ms by default
  and settable; `QMediaPlayer` never exposed it.

It also removes the Ogg problem outright. `QAudioDecoder` decodes through Qt's
FFmpeg decoder whatever the platform backend is, so there is no format the app
must fall back for, and no deprecated Windows backend to depend on.

The engine runs on its own thread. The stretch costs about 15% of a core at
0.25x (`tools/measure_stretch_quality.py`) -- affordable, but nowhere near
affordable on the thread that has 8.33ms to paint a frame in.

Four clocks, and which one is in charge
---------------------------------------

Most of the timing bugs this file has had were two of these being treated as
one, so they are named here.

1. **Chart time** -- the milliseconds in the `.osu`: note times, timing points,
   SV points, everything the editor draws and writes. Integers as the file
   gives them, and **nothing here ever changes one**. `frame_for_ms` is the
   only conversion out of it.

2. **Source time** -- the position in the decoded PCM, in frames. The same
   thing as chart time to within the audio's own offset, and the one the notes
   are placed against (`HitsoundMixer.mix`), because it is the only clock that
   says where a *sample of music* is rather than where the playhead thinks it
   is. Integer frames throughout.

3. **Device output time** -- when a sample reaches the ear. Sink buffer
   (`SINK_BUFFER_MS`, 80ms), driver, hardware, and on Bluetooth a great deal
   more. **Not knowable from in here**, only measurable: `offset_calibration`
   asks the user to tap along and writes `audio/output_offset_ms`, and gui.py
   subtracts it from what it draws.

4. **UI time** -- what the views are drawn against. gui.py's port of osu!'s
   `InterpolatingFramedClock` advances this once per rendered frame and eases
   it toward clock 2, so a frame arriving late does not make the playhead jump.

**Clock 2 is authoritative.** `_position_ms` reads `QAudioSink.processedUSecs`
-- the play cursor, measured, not the write cursor -- and maps it back through
`_segments` and `grain_offset_ms`. Clock 4 follows it and never leads it; clock
3 is a constant the user calibrates; clock 1 is data.

The one thing that is *not* derived from clock 4 any more is sound. Notes used
to be fired from the render loop, which made them a function of when a frame
happened to run; they are mixed against clock 2 now, so a dropped frame cannot
move a hitsound.
"""
from __future__ import annotations

import array
# ponytail: `audioop` is deprecated and gone in Python 3.13. It is here for one
# thing -- `add` is the only saturating 16-bit mix in the standard library, and
# `mul` the only gain, both in C -- and the frozen build is 3.11. When it does
# go, `HitsoundMixer._sound` is the only caller: a plain loop over `array`
# slices costs about ten times as much per sample but runs over one grain's
# 882 frames, so measure `tools/measure_slow_rate_playback.py` rather than
# assume it is too slow.
import audioop
import math
from bisect import bisect_left
from operator import mul

from PySide6.QtCore import (
    QElapsedTimer, QMetaObject, QObject, Qt, QThread, QTimer, QUrl, Signal, Slot,
)
from PySide6.QtMultimedia import (
    QAudioDecoder, QAudioFormat, QAudioSink, QMediaDevices, QMediaPlayer,
)

# One format everywhere: the decoder is told to convert to it, so nothing
# downstream has to care what the file was.
SAMPLE_RATE = 44100
CHANNELS = 2

# How much audio sits between `write()` and the speakers. Qt's default is 250ms,
# a quarter second of latency nothing in the editor could compensate for.
#
# The floor is what one grain costs to build. `_fill` builds a grain inside a
# single pump tick -- p99 20ms, max 29ms at SoundTouch's search width -- and the
# device gets nothing while it does, so the buffer has to outlast that plus the
# tick itself. At 40ms it did not quite: measured 5-7 runs of true silence per
# 8s at 0.25x and 0.75x, which is a dropout, not a metric. 80ms leaves a 2x
# margin and takes it back to the one drain every run has at startup; 120ms
# measured no better.
#
# It costs command latency and nothing else -- `_position_ms` reads the *play*
# cursor, so the reported song time is unaffected by how much is queued ahead of
# it.
SINK_BUFFER_MS = 80
PUMP_INTERVAL_MS = 10

# A single seek rebuilds the sink (`_land_seek`) in ~10-40ms, measured
# (`tools/measure_seek.py`'s `_open_sink` timings). A *drag* fires one seek per
# mouse-move, faster than that -- `tools/measure_scrub.py` measured 549ms of
# the device getting nothing at all across a ~1s burst 8ms apart, because each
# seek tore the previous rebuild down before it produced a single sample.
# `_Engine.seek` coalesces any seek arriving within this many ms of the last
# landed one; comfortably above the worst rebuild cost measured so a burst
# never outruns its own landing.
SEEK_COALESCE_MS = 50

# How long a fade-in runs on every (re)opened sink -- the first play and every
# seek while playing alike. A seek's restart cuts the old stream with nothing
# of its own (`seek`'s docstring: no way to discard the buffered old position
# but a restart) and the new one starts at whatever sample the target happens
# to land on -- measured at 25% of full scale for a mid-song seek
# (`tools/measure_seek.py`), which is a click, not a timing stutter. Long
# enough to flatten that; short enough that a fade is not what it sounds like.
FADE_FRAMES = int(SAMPLE_RATE * 0.005)  # 5ms

# WSOLA, in frames.
#
# SEQUENCE is how much output one grain contributes and OVERLAP is how much of
# that is cross-faded with the previous grain. **SEQUENCE must be much larger
# than OVERLAP.** The first version of this had them equal, which meant every
# output sample was a blend of two different parts of the song -- comb
# filtering from end to end, measured at 0.015 tonality (a 440Hz sine came out
# with 1.5% of its energy still at 440Hz). SoundTouch, which is what BASS_FX
# runs and therefore what osu! sounds like, uses an 82ms sequence against an
# 8ms overlap, so roughly 90% of its output is untouched source.
#
# These are tuned by `tools/measure_stretch_quality.py` *and* by ear, and it
# takes both: the harness is the only thing that catches a rate nobody listened
# to, and the ear is the only thing that caught the harness scoring a broken
# grain 0.987 because every signal it owns is synthetic.
#
# SEARCH is how far the read head may slide to find a splice that lines up with
# what was already written -- the whole difference between time-stretching and
# chopping. The intuition that it must span a period of the lowest frequency
# that matters (20ms for 50Hz) is wrong, and measurably so: what the
# correlation has to align is one *overlap* of continuation, so the reach is
# bounded by the grain rather than by the bass. See the table below.
# **Neither size is upstream's any more, and that is deliberate.** SoundTouch's
# companion line asks for a 125ms sequence at 0.25x, and shipping it made slow
# playback audibly worse than the 82ms it replaced: on a real mastered track at
# 0.25x a kick drum came apart, which two harnesses full of synthetic signals
# had rated 0.987 tonality and called fine. A ladder of grain lengths played
# through the real device at 0.25x -- 53, 40, 30, 20, 10, 5ms, A-B-A-B against
# 117ms to rule out a cold device -- put the knee at 20ms, and every number
# agrees with the ear once the overlap is allowed to scale with the grain:
#
#     grain   overlap   _fill p99/max   over 10ms budget   worst err @0.25x
#     117ms     8.0ms      20 / 47ms        70 / 750             45.1ms
#      53ms     6.6ms      20 / 30ms       132 / 673             21.0ms
#      30ms     3.8ms      11 / 20ms        11 / 692             11.3ms
#      20ms     2.5ms       7 / 19ms         1 / 793              7.5ms
#      10ms     1.3ms       6 / 22ms         1 / 691              3.8ms
#
# Two things in that table are the whole reason upstream's curve is wrong here:
#
# - **A shorter grain is cheaper, not dearer.** The search correlates over the
#   *overlap*, so scaling the overlap with the sequence shrinks the inner loop
#   as well as the hop. Held at SoundTouch's fixed 8ms it does not, and then a
#   short grain really is dearer -- which is what an earlier round of this
#   measured (0.18x realtime at 117ms against 0.57x at 40ms) and drew exactly
#   the wrong conclusion from.
# - **A fixed 8ms overlap is most of a short grain.** At 10ms it is 80% of the
#   output cross-faded, which is the comb filtering this docstring opens with
#   -- measured at 0.699 tonality, a third of a 440Hz sine leaving its own
#   frequency. Below about 30ms the overlap has to come down with the grain, or
#   the grain gets blamed for the window's failure.
#
# So one length at every rate, and the eighth of it that upstream's own 8ms is
# of its 82ms nominal. There is nothing left for the rate to change: this is
# already shorter than the shortest sequence `calcSeqParameters` ever asks for.
#
# **And then SEARCH has to come down with it, which is the half that is easy to
# miss.** Upstream's 25ms reach was sized against upstream's 117ms grain -- a
# ratio of 0.21. Left at 25ms against a 20ms grain it is *wider than the whole
# grain*, and wider than the read head's own hop (`sequence * rate`: 5ms at
# 0.25x, 15ms at 0.75x), so a grain may be taken from source the previous grain
# has already emitted. That is not a subtle effect. Sweeping the rule at a
# fixed 20ms sequence, tonality (how much of a 440Hz sine is still at 440Hz;
# the source scores 1.000) and warble (the source's own floor is 0.150):
#
#     reach rule          0.25x           0.5x            0.75x          cpu
#     soundtouch 25ms     0.923 / 0.168   1.000 / 0.159   0.457 / 0.154  0.38x
#     hop (seq * rate)    0.968 / 0.164   1.000 / 0.146   0.862 / 0.156  0.07x
#     half the sequence   0.999 / 0.158   1.000 / 0.146   0.999 / 0.149  0.14x
#     the overlap only    0.997 / 0.164   0.996 / 0.158   0.997 / 0.158  0.04x
#
# Half the sequence sits on the ceiling of both metrics at every rate, takes
# the doubled-attack count at 0.5x from 12 to 2, and costs less than the 0.18x
# the 117ms geometry cost. 0.75x is the row that matters most: it is the only
# rate where upstream's reach is still nearly its full width against the short
# grain, and it is the one that collapsed -- which no amount of listening at
# 0.25x would have found.
#
# It does give up reach in absolute terms: 10ms spans a 100Hz period rather
# than the 50Hz the 20ms above was chosen for. Measured, that costs nothing,
# because what the correlation has to align is one overlap of continuation
# (2.5ms) and not a whole bass period.
SEQUENCE_MS = 20.0
SEQUENCE_FRAMES = int(SAMPLE_RATE * SEQUENCE_MS / 1000.0)   # 882
OVERLAP_FRAMES = SEQUENCE_FRAMES // 8                       # 110, 2.5ms
GRAIN_FRAMES = SEQUENCE_FRAMES + OVERLAP_FRAMES
SEARCH_FRAMES = SEQUENCE_FRAMES // 2                        # 441, 10ms

# BASS_FX takes the same shortcut behind BASS_ATTRIB_TEMPO_OPTION_USE_QUICKALGO,
# and leaves it **off** by default -- so osu! runs the full-resolution search.
SEARCH_STEP = 1
# One grain (SEQUENCE_FRAMES of output) is produced synchronously inside
# `_Engine._fill`, once every ~82ms of playback regardless of rate -- the
# grain size, not the rate, sets how often the search runs. At
# CORRELATION_STEP=1 that search cost 20-28ms against the pump's 10ms budget
# (`tools/measure_slow_rate_playback.py` at 0.25/0.5/0.75x: ~12% of `_fill()`
# calls over budget, max 28ms), which is most of SINK_BUFFER_MS's 40ms of
# slack on a single grain and was the sustained-slow-rate stutter report.
# Subsampling the correlation input -- every other frame of the dot product,
# same trick as SEARCH_STEP but on the other axis of the search -- halved that
# cost (max 15-16ms, calls over budget ~0.6%, no run since produced a real
# hardware-buffer drain) with no measured change in
# `tools/measure_stretch_quality.py`'s tonality/warble/click numbers.
CORRELATION_STEP = 2
# Fixed point for the window, so the overlap-add stays in integer arithmetic.
WINDOW_BITS = 12
WINDOW_ONE = 1 << WINDOW_BITS


def _crossfade_window(frames: int) -> array.array:
    """Linear ramp from 0 to 1 across the overlap, as `overlapStereo` is.

    Over the *overlap*, not over the grain: the fade has to complete inside the
    short blend region, and a window sized to the whole grain would leave the
    two halves summing to well under one for most of it.

    Linear rather than the raised cosine this used to run, because that is what
    SoundTouch does and there is no reason here to differ from the thing being
    matched.
    """
    return array.array("i", (
        (WINDOW_ONE * i) // frames for i in range(frames)
    ))


def _centre_bias(frames: int) -> array.array:
    """SoundTouch's `(1.0 - 0.25 * tmp * tmp)` over the seek window.

    The piece whose absence let the splice wander. Upstream scales every
    candidate's score by a parabola that peaks in the middle of the search and
    falls to 0.75 at either end, so an offset far from the read head has to be
    meaningfully better correlated to win rather than merely better. Without it
    the search takes any small improvement wherever it finds it, and the read
    head jitters -- which is what a listener calls warping.
    """
    return array.array("d", (
        1.0 - 0.25 * ((2.0 * i - frames) / frames) ** 2 for i in range(frames)
    ))


# How far ahead of the reported position the audio actually is, at each rate
# the UI offers (`gui.py`'s four speed buttons, 25/50/75/100% -- there is no
# continuous dial). Measured directly on real music, not modelled: at each
# grain, `tools/measure_search_bias.py`'s tail-matching trick recovers
# `best_offset` exactly (`_tail_mono` is a byte-for-byte copy of
# `mono[best_offset + SEQUENCE_FRAMES : best_offset + GRAIN_FRAMES]`, and real
# audio does not repeat itself at that length by chance, so searching for it
# finds `best_offset` with no assumptions). That gives the *true* content
# position at every pump-tick instant, sampled uniformly across each grain
# (not just at its edges -- sampling only at grain end measures the
# sawtooth's worst point, not its mean, which is what centring needs) and
# compared against what the naive linear-at-`rate` mapping would report.
# Three tracks of different genre (electronic, dubstep remix, vocal pop),
# stdev under 0.25ms across all of them at every rate:
#
#     rate    needed correction   old formula (half of everything)
#     0.25          10.72ms                12.50ms
#     0.50           8.71ms                10.00ms
#     0.75           7.67ms                 7.50ms
#     1.00           0.00ms (exact -- the straight-copy path, no grain, no search)
#
# The old formula assumed the splice search lands *uniformly* across its reach
# and used half of it. That assumption was never checked against real music --
# only inferred from a click train's residual bias, and the click train turns
# out to be a bad proxy: measured the same way, its own search lands at 26-28%
# of the reach, against real music's 50-58%. A quiet bed with sparse loud
# impulses does not correlate the way continuously-textured music does, so
# calibrating anything from it is the same trap that put SoundTouch's geometry
# here in the first place -- a number that looked fine on a synthetic signal
# and wasn't. This measurement uses real tracks throughout for that reason.
#
# **Two other explanations were investigated and ruled out**, worth recording
# so they are not re-suspected without a number next time:
#
# - Uncorrected physical device latency (`audio/output_offset_ms` at its
#   default of 0). Wrong direction: a constant *wall-clock* delay maps to
#   `L * rate` of *song-time* mismatch, which shrinks at slower rates -- the
#   opposite of "worse at 0.25x, decent at 1.0x".
# - `gui.py`'s interpolating clock (`_advance_interpolated_clock`) introducing
#   its own lag on top of this. Simulated exactly (constants and formula
#   copied verbatim) and driven first by a perfect ramp, then by the real,
#   grain-quantised reports above: the displayed clock tracks the engine's own
#   reported position to within simulation noise at every rate. It extrapolates
#   by the known rate every frame and only blends the *residual*, which has
#   zero steady-state lag against a signal advancing at a roughly constant
#   rate -- unlike a plain low-pass filter, which would not.
#
# The measured points do not sit on one straight line in `(1 - rate)`, so this
# is a small anchor table with linear interpolation between real, measured
# values rather than a two-parameter formula bent to fit three points it
# does not actually lie on -- which would claim a precision the measurement
# does not have. Only 0.25/0.5/0.75 are ever asked for; the interpolation
# exists so an out-of-table rate (a test, a future speed) gets something
# continuous and reasonable rather than a lookup failure.
#
# **1.5x (DT) was added 2026-09-25**, when the mods made the UI offer a rate
# above 1 for the first time. Extrapolating the line through 0.75 and 1.0 put it
# at -15.3ms, which is not a measurement. Measured instead with the same
# harness (`tools/measure_search_bias.py`, Libertas and Nbt-Hwt): the search
# lands at 51% of its reach, 225.4 frames. Averaged across a grain the offset is
# that displacement plus the sawtooth `(SEQUENCE_FRAMES - 1) / 2 * (1 - rate)`,
# and above 1x the sawtooth is negative: 225.4 - 220.25 = +5 frames, +0.12ms.
# The same arithmetic at 0.75 gives 8.3ms against the table's 7.67, so read it
# as +/-0.7ms -- nearly nothing, against the 15ms the extrapolation would have
# put on the DT playhead.
_MEASURED_OFFSET_ANCHORS_MS = (
    (0.25, 10.72), (0.5, 8.71), (0.75, 7.67), (1.0, 0.0), (1.5, 0.12),
)


def grain_offset_ms(rate: float) -> float:
    """How far ahead of the reported position the audio actually is, at
    `rate`. See `_MEASURED_OFFSET_ANCHORS_MS` above for what this is and how
    it was measured -- real tracks, not a synthetic signal or a formula.

    Deliberately a function of the rate and **nothing else**, still. A
    correction that tracks each grain's own splice is more accurate on paper
    and unusable in practice: it steps at every grain boundary, and gui.py's
    interpolating clock abandons interpolation and jumps whenever the source
    disagrees by more than `POSITION_ALLOWABLE_ERROR_MS * rate` (8.3ms at
    0.25x). Measured, that was 146 snaps in 1199 ticks -- the playhead
    teleporting a dozen times a second. This one changes only when the rate
    does, which is a moment the playhead is being moved anyway, and the
    harness holds it to 0 snaps in 1199 ticks at every rate.
    """
    if rate == 1.0:
        return 0.0
    anchors = _MEASURED_OFFSET_ANCHORS_MS
    # Whichever consecutive pair brackets `rate`, or the first/last pair to
    # extrapolate outside [0.25, 1.5] -- no rate the UI offers ever needs that,
    # but a test or a future speed should get a continuous line, not a crash.
    for (r0, m0), (r1, m1) in zip(anchors, anchors[1:]):
        if rate <= r1:
            break
    return m0 + (m1 - m0) * (rate - r0) / (r1 - r0)


# Where a chart's notes sit against this app's decode of their song: the attack
# a mapper timed a note to arrives this many ms *after* the note's millisecond.
# Measured with `tools/measure_note_vs_music.py` over 60 random installed taiko
# maps, averaging the energy envelope around every note: median +22ms on MP3
# (21 self-consistent maps) and +23ms on OGG (14), so it is not a codec's
# gapless delay -- it is what charts timed in osu! carry. Inaudible at 1.0x;
# at 0.25x it is ~90ms of wall time with the hitsound first, which is what
# "the kick lands late at slow speed" was. The default for
# `audio/hitsound_offset_ms`, so a user trim still sits on top.
DEFAULT_HITSOUND_OFFSET_MS = 22

# How long a mouse-wheel seek during playback pauses, audio and playhead
# together, after it lands. Default for `playback/wheel_seek_hold_ms`; 0 is
# no pause. Being tuned by ear.
WHEEL_SEEK_HOLD_MS = 0


def frame_for_ms(time_ms: float) -> int:
    """Source frame for a chart millisecond. Halves up.

    Not `round`, which is half-to-*even* in Python: 5ms is 220.5 frames and
    rounds down to 220 while 15ms is 661.5 and rounds up to 662. Half a frame
    is 11us and nobody can hear it, but a conversion that goes two different
    ways on the same fraction is the kind of thing that costs an afternoon when
    a test disagrees with the code by one sample.

    Halves up like gui.py's `osu_round`, and deliberately not like
    `osu_snap_ms`, which truncates because osu!stable truncates a *beat*
    position. A note's millisecond is not a beat position -- it is already the
    integer the file gave us.
    """
    return math.floor(time_ms * SAMPLE_RATE / 1000.0 + 0.5)


def downmix_to_mono(source: array.array) -> array.array:
    """The correlation input, built once per track rather than per grain.

    The splice search is the entire cost of `TimeStretcher`, and searching one
    channel instead of two halves it. Averaging rather than summing keeps it in
    16-bit range so the scores cannot overflow into nonsense on loud material.

    No Python object per sample. This runs on the engine thread for every
    buffer the decoder delivers, right after a map opens, and it used to be a
    generator over every stereo frame -- 5.8 million iterations for a 131s
    track, holding the GIL in 5ms slices (`sys.getswitchinterval`), so the UI
    thread stalled for the whole decode: a third of all frames over budget for
    the first second or so of every load.

    `audioop.tomono` works on the raw bytes in C. With both factors 0.5 it
    computes `floor((l + r) * 0.5)` in double precision, which is exact for any
    two int16 samples and equals the `(l + r) // 2` this used to be --
    `tools/bench_downmix.py` checks that over a whole track, the extremes of
    the range included, before it times anything. A `map` over two builtins
    was tried first and only managed 1.6x: the loop was C, but every sample
    still became a Python int on the way through.
    """
    mono = array.array("h")
    mono.frombytes(audioop.tomono(source.tobytes(), 2, 0.5, 0.5))
    return mono


def _clip(value: int) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value


class HitsoundMixer:
    """Note samples, placed in the output at the source frame they belong to.

    **Anchored to the source, not to the reported position.** The old path fired
    a `QSoundEffect` when the interpolated playhead crossed a note, which put
    every note one rendered frame of quantisation plus that path's own
    uncalibrated device latency away from the music -- and on top of both, the
    whole of `grain_offset_ms`'s residual, because the playhead is a linear map
    over content that arrives a grain at a time. Mixed in here instead, a note
    lands on its own millisecond of the song to the sample at every rate, and
    inherits the music's latency because it *is* the music's buffer.

    Deliberately free of Qt and of any I/O, like `TimeStretcher`: it takes
    already-decoded samples and already-sorted times, so every placement rule
    below is testable without an audio device.
    """

    def __init__(self) -> None:
        self.enabled = True
        # The user's own hitsound level, applied at mix time rather than baked
        # into the schedule, so the settings slider does not need a reschedule.
        self.volume = 1.0
        # The music level, applied here rather than on the sink -- see `mix`.
        self.music_gain = 1.0
        # Source frame per note, ascending, with the key and the section volume
        # `hitsound_schedule` resolved for it.
        self._frames: list[int] = []
        self._keys: list[str] = []
        self._gains: list[float] = []
        self._samples: dict[str, bytes] = {}
        # Sounding notes: [pcm, byte offset reached, gain]. Bounded by
        # MAX_VOICES.
        self._voices: list[list] = []
        # Highest source frame already considered. See `mix`: this is the whole
        # of the dedupe, and without it a note plays 1/rate times.
        self._voiced_through = -1

    # A stack of notes on one millisecond is a real thing in this editor (a
    # shiny note is a pile of objects), and a barline gimmick can put thousands
    # of lines in a second. The bound is the pool size the QSoundEffect path
    # used, which was measured against a 200 BPM 1/4 stream -- around twenty
    # samples overlap there, and nobody can hear the twenty-first.
    MAX_VOICES = 24

    def set_samples(self, samples: dict) -> None:
        """`{key: decoded 16-bit stereo PCM at SAMPLE_RATE}`.

        Sounding voices are left alone: they are already-mixed bytes from the
        old sample and cutting them off mid-hit to change skins would be more
        audible than letting them finish.
        """
        self._samples = dict(samples)

    def set_schedule(self, frames, keys, gains) -> None:
        """Three parallel lists, ascending by frame.

        Taken pre-sorted because `hitsound_schedule` in gui.py already sorts
        them to resolve each note's section volume in one forward merge, and
        sorting again per edit on a gimmick difficulty is a second pass over
        thousands of objects for nothing.
        """
        self._frames, self._keys, self._gains = frames, keys, gains

    def reset(self, source_frame: float) -> None:
        """Every seek and every stop. Drops the sounding voices and moves the
        dedupe watermark absolutely, so a backwards seek replays the notes it
        lands before rather than skipping them for having been played once."""
        self._voices = []
        self._voiced_through = int(source_frame) - 1

    def resync(self, source_frame: float) -> None:
        """Rebase the dedupe watermark to `source_frame` when the *schedule*
        changes rather than the playhead -- an edit, or a hitsound-offset
        change -- without touching sounding voices, which `reset` is for.

        `_voiced_through` is an absolute source frame, but it only means
        anything relative to the schedule that produced it: it says how far
        *through that schedule* playback has gone. Replacing the schedule
        without this left the old watermark in force against the new frame
        numbers, which is wrong in both directions. Shift the offset by -220ms
        while playing and a note whose new frame lands behind the watermark is
        silently skipped forever; a note that had already played, whose new
        frame lands ahead of a watermark that had already passed it, fires
        again later -- disconnected from wherever the playhead actually is,
        which is exactly "I hear a note but it is not the note I am at".
        Reproduced and confirmed both ways before this existed.

        Rebasing to *now* makes the new schedule authoritative from this
        instant on: everything at or after the current position is unplayed
        under it, everything before is treated as already handled, regardless
        of what the old watermark was.
        """
        self._voiced_through = int(source_frame) - 1

    def mix(self, out: array.array, source_start: int, frames: int,
            source_frames: int | None = None, rate: float = 1.0,
            covers: tuple[int, int] | None = None) -> None:
        """Mix into `out`, which is `frames` frames of source from
        `source_start`.

        Or, with `rate`, `frames` frames of *resampled* output covering
        `source_frames` of source (NC/DC): a note at source frame `s` then
        starts at output frame `(s - source_start) / rate`, and plays at its own
        speed from there -- the song changes pitch, the notes do not.

        Called once per grain, from `TimeStretcher._produce`, with the grain's
        *real* source position -- wherever the splice search landed, not where
        the read head nominally is.

        **The music level is applied here too**, which is not where it used to
        be. `QAudioSink.setVolume` attenuates everything written to it, so with
        the notes in the same buffer it scaled them as well -- and, worse, it
        happens *after* the sum, so it cannot make room for anything. Measured
        on a 0dBFS master with the default 65% music and 70% hitsounds, moving
        the attenuation to here takes saturated samples from 71 per note to
        9.6: the music has to be turned down before the notes are added, or the
        addition is the thing that clips. It also gives the two volume sliders
        back their independence, which they had when hitsounds left through
        `QSoundEffect` and briefly lost when they did not.

        The cost is that a volume change is heard a grain plus the sink buffer
        later (about 100ms) rather than instantly. Nobody can hear the
        difference on a slider drag, and the alternative is clipping.

        **The watermark is the dedupe, and it is not an optimisation.** At rate
        < 1 the read head advances `sequence * rate` while each grain covers
        `sequence`, so consecutive grains overlap in source and a note's frame
        falls inside roughly 1/rate of them -- four grains at 0.25x. Voicing it
        every time plays every note four times, 5ms apart, which is a flam and
        not a hit.

        **`covers` is the source range the grain answers for**, when that is
        wider than what it contains. At rate > 1 (DT) the read head advances
        `sequence * rate` while a grain holds `sequence`, so the source between
        grains is never in any output -- and neither were the notes in it, a
        third of them at 1.5x. The search can also start a grain past the read
        head at any rate. A note there has no music of its own to sit on, so
        it goes on the nearest edge of this grain, which is the join its
        skipped source collapsed into.
        """
        if self.music_gain != 1.0:
            # Before the notes, and over the music alone: that ordering is the
            # whole point of doing it here.
            scaled = array.array("h")
            scaled.frombytes(audioop.mul(out.tobytes(), 2, self.music_gain))
            out[:] = scaled
        if self._voices:
            self._sound(out, frames)
        begin = source_start
        end = source_start + (frames if source_frames is None else source_frames)
        if covers is not None:
            begin, end = min(begin, covers[0]), max(end, covers[1])
        if not self.enabled or not self._samples:
            # Still advance: a note passed over while hitsounds are off has
            # been passed over, and turning them on mid-playback should start
            # from the playhead rather than replay the section behind it.
            self._voiced_through = max(self._voiced_through, end - 1)
            return
        index = bisect_left(self._frames, max(begin, self._voiced_through + 1))
        while index < len(self._frames) and self._frames[index] < end:
            pcm = self._samples.get(self._keys[index])
            gain = self._gains[index] * self.volume
            # Zero is a volume mappers set deliberately (see the Kiai and Sound
            # Volume layer), so it is obeyed rather than floored.
            if pcm and gain > 0.0 and len(self._voices) < self.MAX_VOICES:
                offset = max(0, min(frames - 1, round(
                    (self._frames[index] - source_start) / rate)))
                self._voices.append([pcm, 0, gain])
                self._sound(out, frames, start_frame=offset,
                            voice=self._voices[-1])
            index += 1
        self._voiced_through = max(self._voiced_through, end - 1)

    def _sound(self, out: array.array, frames: int, start_frame: int = 0,
               voice=None) -> None:
        """Add one voice (or every continuing voice) into `out`.

        `audioop.add` saturates at 16-bit, and that saturation *is* the mixing
        policy: osu! sums its hitsounds over the track the same way, and a note
        quietly ducking the music would be a worse surprise than a loud stack
        clipping. Measured on a near-0dBFS master, the stretch output saturates
        at 0.68x the rate the source does, so there is headroom for the notes
        in practice rather than in theory.
        """
        voices = [voice] if voice is not None else list(self._voices)
        for entry in voices:
            pcm, position, gain = entry
            take = min(frames - start_frame,
                       (len(pcm) - position) // (CHANNELS * 2))
            if take <= 0:
                if voice is None:
                    self._voices.remove(entry)
                continue
            segment = pcm[position:position + take * CHANNELS * 2]
            if gain != 1.0:
                segment = audioop.mul(segment, 2, gain)
            low = start_frame * CHANNELS
            high = low + take * CHANNELS
            mixed = array.array("h")
            mixed.frombytes(audioop.add(out[low:high].tobytes(), segment, 2))
            out[low:high] = mixed
            entry[1] = position + take * CHANNELS * 2
            if entry[1] >= len(pcm) and voice is None:
                self._voices.remove(entry)


class TimeStretcher:
    """Pitch-preserving time-stretch: WSOLA over interleaved 16-bit stereo.

    Deliberately free of Qt and of any I/O, so the interesting half is testable
    without an audio device.
    """

    def __init__(self, mixer: "HitsoundMixer | None" = None) -> None:
        # The notes, if anyone wants them. A collaborator rather than something
        # this class does itself, because where a grain came from is the only
        # thing the placement needs and this is the only place that knows it.
        self.mixer = mixer
        self._window = _crossfade_window(OVERLAP_FRAMES)
        # Rebuilt whenever the search width changes, which is whenever the rate
        # does; one array per rate, not per grain.
        self._bias = array.array("d")
        # Nightcore/Daycore: resample instead of stretch, so pitch follows the
        # rate (osu!'s AdjustableProperty.Frequency, where DT/HT are Tempo).
        self.pitch = False
        self.reset(0.0)

    def reset(self, source_frame: float) -> None:
        """Move the read head, dropping any overlap or pending output carried
        across the jump.

        Every seek and every stop comes through here. Keeping the tail would
        splice the end of the old position onto the start of the new one, and
        keeping the pending buffer would play a fragment of the old position
        first -- both audible at exactly the moment the user asked to be
        somewhere else.
        """
        self._read = float(source_frame)
        if self.mixer is not None:
            self.mixer.reset(source_frame)
        self._tail = array.array("h", bytes(2 * CHANNELS * OVERLAP_FRAMES))
        self._tail_mono = array.array("h", bytes(2 * OVERLAP_FRAMES))
        self._pending = bytearray()
        self._pending_rate = 1.0
        # audioop.ratecv's interpolation state, carried grain to grain so the
        # joins are seamless. A jump anywhere starts it afresh.
        self._ratecv_state = None

    @property
    def source_frame(self) -> float:
        """Where in the source the *next returned* sample comes from.

        Not where the grain generator has reached: a whole grain is 82ms of
        output, and reporting the generator's position would put the playhead
        that far ahead of the audio. The pending buffer was produced at one
        rate, because a rate change only takes effect on the next grain.
        """
        pending_frames = len(self._pending) // (CHANNELS * 2)
        return self._read - pending_frames * self._pending_rate

    def pull(self, source: array.array, mono: array.array, frames: int,
             rate: float) -> bytes:
        """Return up to `frames` output frames, consuming `frames * rate`.

        Grains are generated whole and held in `_pending`, so the caller may
        ask for any size it likes -- the sink asks for whatever space it has
        free, which is far less than one grain.
        """
        wanted = frames * CHANNELS * 2
        while len(self._pending) < wanted:
            if not self._produce(source, mono, rate):
                break
        block = bytes(self._pending[:wanted])
        del self._pending[:wanted]
        return block

    def ensure_grain(self, source: array.array, mono: array.array,
                     rate: float) -> None:
        """Keep a whole grain buffered ahead of the device.

        The search costs a few ms per grain and a grain is built inside one
        10ms pump tick with only `SINK_BUFFER_MS` of slack, so built on demand
        it lands exactly when the sink has run dry. Called *before* `pull` in
        `_fill`, which then always finds more than a tick's worth waiting and
        never has to produce -- so at most one grain is ever built per tick,
        and there is a further 20ms of output between the search and silence.

        20ms of slack is thinner than the 117ms the old geometry left, and it
        is still the right trade: the same measurement that shortened the grain
        took `_fill` calls over the 10ms budget from 132/673 to 1/793, because
        the correlation window came down with it. The buffer absorbs a stall;
        what it cannot absorb is one every other tick.

        One grain, not two: `_pending` is also how long a rate change takes to
        be heard, and two grains of it is a quarter second of the old speed
        after the button says otherwise.
        """
        if len(self._pending) < SEQUENCE_FRAMES * CHANNELS * 2:
            self._produce(source, mono, rate)

    def _produce(self, source: array.array, mono: array.array, rate: float) -> bool:
        """Append one grain's worth of output. False when the source runs out."""
        available = len(mono)
        start = int(self._read)
        if self.pitch and rate != 1.0:
            return self._produce_resampled(source, start, available, rate)
        if rate == 1.0:
            # Nothing to stretch: hand the decoded samples straight through, so
            # the rate the editor spends nearly all its time at has no splices
            # to colour it and costs nothing to play.
            end = min(available, start + SEQUENCE_FRAMES)
            if end <= start:
                return False
            out = source[start * CHANNELS:end * CHANNELS]
            if self.mixer is not None:
                self.mixer.mix(out, start, end - start)
            self._pending += out.tobytes()
            self._pending_rate = 1.0
            self._read = float(end)
            return True

        # Every size is a function of the rate, as `calcSeqParameters` makes
        # them: a 125ms grain and a 25ms search at 0.25x, tightening toward
        # 100ms and 18ms as the rate comes back to 1.
        grain_len = GRAIN_FRAMES
        sequence = SEQUENCE_FRAMES
        reach = SEARCH_FRAMES
        # Forward only, as `seekBestOverlapPositionFull` scans [0, seekLength).
        low = start
        high = min(available - grain_len, start + reach)
        if high <= low:
            return False
        # Which offset best continues the tail already written? Correlating
        # keeps waveform periods lined up across the splice; without it the
        # overlap cancels itself and the result warbles. `sum(map(mul, ...))`
        # runs the multiply-add in C, which is what makes this affordable in
        # Python at all -- an explicit loop is about ten times slower.
        #
        # Two things upstream does that a plain dot product does not, and both
        # matter more than any amount of tuning around them:
        #
        # - **Normalised** by the candidate's own energy (`calcCrossCorr`
        #   returns `corr / sqrt(norm)`). Unnormalised, a loud candidate outbids
        #   a well-matching one, so the splice is pulled onto every drum hit.
        # - **Centre-biased** (`_centre_bias`), so an offset far from the read
        #   head must be meaningfully better rather than merely better. This is
        #   what keeps the read head still; without it the search accepts any
        #   small improvement anywhere in its reach and the result warps.
        tail_mono = self._tail_mono[::CORRELATION_STEP]
        span = OVERLAP_FRAMES
        if len(self._bias) != high - low:
            self._bias = _centre_bias(max(1, high - low))
        bias = self._bias
        best_offset, best_score = low, None
        for candidate in range(low, high, SEARCH_STEP):
            window = mono[candidate:candidate + span:CORRELATION_STEP]
            energy = sum(map(mul, window, window))
            if energy <= 0:
                continue
            score = sum(map(mul, tail_mono, window)) / math.sqrt(energy)
            score = (score + 0.1) * bias[candidate - low]
            if best_score is None or score > best_score:
                best_score, best_offset = score, candidate

        grain = source[best_offset * CHANNELS:(best_offset + grain_len) * CHANNELS]
        out = array.array("h", bytes(2 * CHANNELS * sequence))
        window = self._window
        tail = self._tail
        # Cross-fade only the overlap; the rest of the sequence is the source
        # untouched, which is where the quality comes from.
        for i in range(OVERLAP_FRAMES):
            weight = window[i]
            inverse = WINDOW_ONE - weight
            left = i * CHANNELS
            out[left] = _clip(
                (grain[left] * weight + tail[left] * inverse) >> WINDOW_BITS)
            out[left + 1] = _clip(
                (grain[left + 1] * weight + tail[left + 1] * inverse) >> WINDOW_BITS)
        out[OVERLAP_FRAMES * CHANNELS:] = grain[OVERLAP_FRAMES * CHANNELS:
                                                sequence * CHANNELS]
        # The tail is what naturally follows what was just emitted, so the next
        # grain is looked for against a real continuation of the song.
        self._tail = grain[sequence * CHANNELS:]
        self._tail_mono = mono[best_offset + sequence:best_offset + grain_len]
        # After the tail is taken, and into `out` rather than `grain`: a note
        # mixed before that would be carried into the next grain's crossfade
        # and into what the correlation searches against, so the search would
        # start matching hitsounds instead of music.
        # The read head advances by sequence * rate: that ratio, and only it, is
        # what makes the output 1/rate times longer than the input. The search
        # moves where a grain is taken from, never how far the head advances,
        # which is why WSOLA changes duration without changing the mapping from
        # output time back to source time.
        self._read += sequence * rate
        if self.mixer is not None:
            # Up to where the next grain's search starts, so no note falls
            # between two grains -- see `HitsoundMixer.mix`.
            self.mixer.mix(out, best_offset, sequence,
                           covers=(start, int(self._read)))
        self._pending += out.tobytes()
        self._pending_rate = rate
        return True

    def _produce_resampled(self, source: array.array, start: int, available: int,
                           rate: float) -> bool:
        """One grain's worth of the source played faster or slower *as a
        record would be*: the song's pitch moves with the rate. No search and
        no splice, so no `grain_offset_ms` either -- output frame i is source
        frame `start + i * rate` exactly.

        The notes are mixed into the *output*, after the resample, at the
        output frame their source frame maps to, so they keep their own pitch.
        Owner's call, 2026-09-26, after hearing both: only the song changes.
        (They were mixed into the source first, which pitched them with it --
        lazer's `ModRateAdjust.ApplyToSample` does that for every rate mod.)
        `audioop.ratecv` does the interpolation in C; a Python loop over every
        sample would be the cost the WSOLA search already was.
        """
        frames = min(available - start, max(1, round(SEQUENCE_FRAMES * rate)))
        if frames <= 0:
            return False
        chunk = source[start * CHANNELS:(start + frames) * CHANNELS]
        # ratecv takes the ratio as two integers; the rates on offer are 0.75
        # and 1.5, and anything else is still close to exact at /1000.
        in_rate = round(rate * 1000)
        resampled, self._ratecv_state = audioop.ratecv(
            chunk.tobytes(), 2, CHANNELS, in_rate, 1000, self._ratecv_state)
        out = array.array("h")
        out.frombytes(resampled)
        if self.mixer is not None and out:
            self.mixer.mix(out, start, len(out) // CHANNELS, source_frames=frames, rate=rate)
        self._pending += out.tobytes()
        self._pending_rate = rate
        self._read = float(start + frames)
        return True



class _Engine(QObject):
    """Decoder, sink and pump. Lives on the audio thread and owns every Qt audio
    object here, because a QAudioSink may only be used from the thread that made
    it.

    Everything is a slot: the facade below talks to it exclusively through
    queued signals, which is why no lock appears anywhere in this file.
    """

    position_changed = Signal(float)
    duration_changed = Signal(int)
    state_changed = Signal(object)
    error_occurred = Signal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self._format = QAudioFormat()
        self._format.setSampleRate(SAMPLE_RATE)
        self._format.setChannelCount(CHANNELS)
        self._format.setSampleFormat(QAudioFormat.Int16)
        self._decoder = None
        self._sink = None
        self._device = None
        self._pump = None
        self._mixer = HitsoundMixer()
        self._stretcher = TimeStretcher(self._mixer)
        # Not optional for the engine: the music level is applied inside the
        # mixer, so the engine always has one even with no chart loaded. The
        # harnesses construct a bare `TimeStretcher`, which then applies no
        # gain at all -- which is what a measurement wants.
        # {key: decoded PCM}, and the decoders building it. Held on the engine
        # rather than in the mixer because decoding is Qt's and the mixer is
        # deliberately free of it.
        self._samples: dict[str, bytes] = {}
        self._sample_decoders: list = []
        self._hitsound_offset_ms = 0.0
        # A wheel seek's pause: ms asked for by the next landing, and the
        # silent frames still to write for the landing that took it.
        self._next_hold_ms = 0.0
        self._hold_frames = 0
        # ponytail: the whole track is decoded into memory -- 50MB for a 4:43
        # song, measured. Streaming would halve that at the cost of making every
        # backwards seek a re-decode, and backwards seeks are what an editor does.
        self._pcm = array.array("h")
        self._mono = array.array("h")
        self._rate = 1.0
        self._volume = 1.0
        self._written = 0
        self._segments = []
        self._fade_remaining = 0
        self._state = QMediaPlayer.StoppedState
        self._duration_ms = 0
        # Seek coalescing (SEEK_COALESCE_MS): the timer that lands a parked
        # target, the target itself, and when the last real rebuild landed.
        self._seek_timer = None
        self._pending_seek_ms = None
        self._pending_seek_wall = 0
        self._last_seek_wall = None
        self._last_seek_target = None
        self._seek_clock = QElapsedTimer()
        self._seek_clock.start()

    # -- loading --------------------------------------------------------------

    @Slot()
    def release(self) -> None:
        """Let go of the device and the file, on the audio thread.

        Called blocking from `TrackPlayer.shutdown` before the thread's event
        loop is asked to quit, because a queued stop would simply never run --
        and until the decoder lets go, the source file stays open. On Windows
        that is a live lock: the dialog that generates a click track into a
        temporary directory could not delete it afterwards.
        """
        self.stop()
        self._release_decoder()
        for decoder in self._sample_decoders:
            decoder.stop()
            decoder.deleteLater()
        self._sample_decoders = []

    def _release_decoder(self) -> None:
        """Stop the decoder and unhook it before dropping the reference.

        Its signals are queued, so a `bufferReady` emitted just before the
        object goes away is still in the queue when the slot runs -- and
        `self._decoder.read()` on a deleted C++ object raises. Disconnecting
        first is what makes that impossible rather than unlikely.
        """
        if self._decoder is None:
            return
        decoder, self._decoder = self._decoder, None
        try:
            decoder.bufferReady.disconnect(self._take_buffer)
            decoder.error.disconnect(self._decode_failed)
            decoder.durationChanged.disconnect(self._decoder_duration)
        except (RuntimeError, TypeError):
            # Already disconnected, or the C++ side is gone.
            pass
        decoder.stop()
        decoder.deleteLater()

    @Slot(str)
    def load(self, path: str) -> None:
        self.stop()
        self._release_decoder()
        self._pcm = array.array("h")
        self._mono = array.array("h")
        self._duration_ms = 0
        self._stretcher.reset(0.0)
        self.duration_changed.emit(0)
        if not path:
            return
        # Parented, so its lifetime is the engine's rather than whatever the
        # Python reference happens to be doing.
        self._decoder = QAudioDecoder(self)
        self._decoder.setAudioFormat(self._format)
        self._decoder.bufferReady.connect(self._take_buffer)
        self._decoder.error.connect(self._decode_failed)
        self._decoder.durationChanged.connect(self._decoder_duration)
        self._decoder.setSource(QUrl.fromLocalFile(path))
        self._decoder.start()

    @Slot()
    def _take_buffer(self) -> None:
        if self._decoder is None:
            return
        buffer = self._decoder.read()
        if not buffer.isValid():
            return
        block = array.array("h")
        block.frombytes(bytes(buffer.constData()))
        self._pcm.extend(block)
        # Extended as the track arrives rather than built in one pass at the
        # end, so playback can start before the decode has finished.
        self._mono.extend(downmix_to_mono(block))
        decoded_ms = int(len(self._mono) / SAMPLE_RATE * 1000)
        if decoded_ms > self._duration_ms:
            self._duration_ms = decoded_ms
            self.duration_changed.emit(decoded_ms)

    @Slot(int)
    def _decoder_duration(self, duration_ms: int) -> None:
        if duration_ms > self._duration_ms:
            self._duration_ms = duration_ms
            self.duration_changed.emit(duration_ms)

    @Slot(object)
    def _decode_failed(self, _error) -> None:
        message = self._decoder.errorString() if self._decoder is not None else ""
        self.error_occurred.emit(QMediaPlayer.FormatError, message)

    # -- transport ------------------------------------------------------------

    @Slot()
    def play(self) -> None:
        if self._state == QMediaPlayer.PlayingState:
            return
        if self._sink is None:
            self._open_sink()
        self._set_state(QMediaPlayer.PlayingState)

    @Slot()
    def pause(self) -> None:
        if self._state != QMediaPlayer.PlayingState:
            return
        # The sink is left running with nothing fed to it rather than
        # suspended: suspend/resume is where a device-level stall would come
        # back in, and an idle sink costs nothing.
        self._set_state(QMediaPlayer.PausedState)

    @Slot()
    def stop(self) -> None:
        if self._pump is not None:
            self._pump.stop()
            self._pump = None
        if self._sink is not None:
            self._sink.stop()
            self._sink = None
        self._device = None
        self._written = 0
        self._segments = []
        # A seek parked by SEEK_COALESCE_MS is a promise to land later; stop()
        # (and load(), which calls it first) means that later never comes --
        # otherwise a stale target could land against a sink or track that has
        # since moved on.
        if self._seek_timer is not None:
            self._seek_timer.stop()
        self._pending_seek_ms = None
        self._last_seek_target = None
        self._set_state(QMediaPlayer.StoppedState)

    def _open_sink(self) -> None:
        song_ms = self._stretcher.source_frame / SAMPLE_RATE * 1000.0
        self._sink = QAudioSink(QMediaDevices.defaultAudioOutput(), self._format)
        self._sink.setBufferSize(
            int(SINK_BUFFER_MS / 1000.0 * SAMPLE_RATE) * CHANNELS * 2)
        # Unity, deliberately: the music level is applied in the buffer by
        # `HitsoundMixer.mix`, where it can still make headroom for the notes.
        self._device = self._sink.start()
        self._written = 0
        # Song time starts after the hold's silence, which `_fill` writes first.
        self._segments = [(self._hold_frames, song_ms, self._rate)]
        self._fade_remaining = FADE_FRAMES
        self._pump = QTimer(self)
        self._pump.setTimerType(Qt.PreciseTimer)
        self._pump.timeout.connect(self._fill)
        self._pump.start(PUMP_INTERVAL_MS)

    @Slot(float)
    def seek(self, position_ms: float) -> None:
        """Move the playhead, landing the rebuild immediately unless one just
        happened.

        A single click lands with no added latency. A *drag* is a
        `seek_requested` per mouse-move (gui.py's `mouseMoveEvent` on the
        timeline bar) -- `tools/measure_scrub.py` fired 20 of these 15ms
        apart and measured 341ms of the device getting nothing at all, because
        each one tore the sink down before the previous rebuild (`_open_sink`,
        itself ~10-40ms) had produced a single sample; an 8ms-apart burst
        measured 549ms silent out of about a second. `_land_seek` is the
        actual rebuild; a seek arriving inside `SEEK_COALESCE_MS` of the last
        one only parks its target and (re)arms a trailing timer, so a burst
        collapses into the one rebuild the burst ends on, and the sink already
        playing keeps playing right up to that landing instead of going quiet.
        """
        self.position_changed.emit(position_ms)
        if self._sink is not None and self._state == QMediaPlayer.PlayingState:
            now = self._seek_clock.elapsed()
            if self._last_seek_wall is not None and now - self._last_seek_wall < SEEK_COALESCE_MS:
                if position_ms == self._last_seek_target:
                    # A burst that never moves is not a drag, it is one click:
                    # the overview bars emit on press and again on release at
                    # the same pixel. Parking that duplicate landed a *second*
                    # teardown 50ms after the first -- the hiccup heard just
                    # after every click. There is nowhere new to go, so there
                    # is nothing to do.
                    #
                    # Bounded by the burst window on purpose: a deliberate seek
                    # back to the same millisecond a second later is a real
                    # request, because playback has moved on since.
                    return
                self._pending_seek_ms = position_ms
                # When it was asked for: the window starts moving from the
                # target now, the audio only when this lands.
                self._pending_seek_wall = now
                if self._seek_timer is None:
                    self._seek_timer = QTimer(self)
                    self._seek_timer.setSingleShot(True)
                    self._seek_timer.timeout.connect(self._land_pending_seek)
                self._seek_timer.start(SEEK_COALESCE_MS)
                return
            self._last_seek_wall = now
        self._land_seek(position_ms)

    @Slot()
    def _land_pending_seek(self) -> None:
        if self._pending_seek_ms is None:
            return
        position_ms, self._pending_seek_ms = self._pending_seek_ms, None
        self._last_seek_wall = self._seek_clock.elapsed()
        # The playhead has been running from the target (after its own hold)
        # since the request, up to SEEK_COALESCE_MS ago. Landing the audio on
        # the bare target started it that far behind, and gui.py's
        # no-backwards clamp froze the playhead until it caught up --
        # `tools/measure_wheel_seek.py --burst` measured ~60ms of freeze after
        # a fast spin, ~250ms with a 15ms hold. Land where the playhead is.
        elapsed = max(0.0, float(self._last_seek_wall - self._pending_seek_wall))
        hold = self._next_hold_ms
        position_ms += max(0.0, elapsed - hold) * self._rate
        self._next_hold_ms = max(0.0, hold - elapsed)
        self._land_seek(position_ms)

    def _land_seek(self, position_ms: float) -> None:
        # Whatever lands now supersedes a seek still parked: left armed, its
        # timer fired after this one and landed the *older* target -- the audio
        # jumped back up to a notch while the playhead held at the newer one
        # (gui.py's no-backwards clamp) until the music caught up. Measured at
        # 0.5x (scratch trace through `tools/measure_wheel_seek.py`'s notch): a
        # seek parked at 932ms, a newer one landed at 963ms, the stale one at
        # 991ms, 774ms frozen; the catch-up is gap / rate, so slower is longer.
        self._pending_seek_ms = None
        if self._seek_timer is not None:
            self._seek_timer.stop()
        self._last_seek_target = position_ms
        frame = max(0.0, position_ms) / 1000.0 * SAMPLE_RATE
        self._stretcher.reset(frame)
        if self._sink is None:
            return
        # The sink is holding up to SINK_BUFFER_MS of the *old* position and
        # offers no way to discard it but a restart. Playing it out would be the
        # seek audibly arriving late.
        playing = self._state == QMediaPlayer.PlayingState
        self._pump.stop()
        self._sink.stop()
        self._sink = None
        self._pump = None
        self._device = None
        # Consumed by this landing whether or not it plays, so a hold never
        # leaks into a later play() or an unrelated seek.
        hold_ms, self._next_hold_ms = self._next_hold_ms, 0.0
        self._hold_frames = int(hold_ms / 1000.0 * SAMPLE_RATE) if playing else 0
        if playing:
            self._open_sink()

    @Slot(bool)
    def set_pitch(self, pitch: bool) -> None:
        """Resample (pitch follows the rate) rather than stretch. Read by the
        stretcher on its next grain, like a rate change."""
        self._stretcher.pitch = bool(pitch)

    @Slot(float)
    def set_rate(self, rate: float) -> None:
        rate = float(rate)
        if rate == self._rate:
            return
        self._rate = rate
        if self._sink is not None:
            # No flush and no restart: the stretcher reads the new ratio on its
            # next grain, so audio already queued plays out at the old rate and
            # everything after it at the new one. The segment records where the
            # changeover lands in the output, which is what keeps _position_ms
            # honest across it -- without it the in-flight audio would be
            # measured at the wrong rate, which is the 114ms error that made
            # QMediaPlayer's own speed buttons move the offset.
            self._segments.append(
                (self._written,
                 self._stretcher.source_frame / SAMPLE_RATE * 1000.0,
                 rate))

    @Slot(float)
    def set_volume(self, volume: float) -> None:
        """The music level, which is applied in the buffer and not on the sink.

        See `HitsoundMixer.mix`: the sink attenuates everything written to it,
        including the notes, and it does so after they have been summed -- so
        the one thing it cannot do is leave room for them.
        """
        self._volume = max(0.0, min(1.0, float(volume)))
        self._mixer.music_gain = self._volume

    def _set_state(self, state) -> None:
        if state != QMediaPlayer.PlayingState:
            self._hold_frames = 0
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)


    # -- hitsounds ------------------------------------------------------------

    @Slot(object)
    def set_hitsound_samples(self, paths) -> None:
        """Decode each note sample once, through the decoder the song uses.

        That conversion is the whole reason not to read WAV headers here: the
        decoder is handed the engine's own format, so a skin's 48kHz mono .ogg
        arrives as 44100 stereo like everything else, and `.wav`/`.ogg`/`.mp3`
        all work because Qt's FFmpeg decoder handles all three (which is what
        `skin.SOUND_EXTENSIONS` allows).

        Asynchronous, and deliberately not waited for: a note whose sample has
        not arrived yet is simply not voiced, the same graceful nothing the
        `QSoundEffect` pools gave while they were loading.
        """
        for decoder in self._sample_decoders:
            decoder.stop()
            decoder.deleteLater()
        self._sample_decoders = []
        self._samples = {}
        self._mixer.set_samples({})
        for key, path in dict(paths).items():
            chunks = bytearray()
            decoder = QAudioDecoder(self)
            decoder.setAudioFormat(self._format)
            decoder.bufferReady.connect(
                lambda d=decoder, c=chunks: self._take_sample_buffer(d, c))
            decoder.finished.connect(
                lambda k=key, c=chunks: self._sample_decoded(k, c))
            decoder.setSource(QUrl.fromLocalFile(str(path)))
            decoder.start()
            self._sample_decoders.append(decoder)

    def _take_sample_buffer(self, decoder, chunks: bytearray) -> None:
        buffer = decoder.read()
        if buffer.isValid():
            chunks.extend(bytes(buffer.constData()))

    def _sample_decoded(self, key: str, chunks: bytearray) -> None:
        """Published as a whole new dict rather than mutated in place, so the
        mixer never reads a half-built one between grains."""
        self._samples[key] = bytes(chunks)
        self._mixer.set_samples(self._samples)

    @Slot(object, object, object)
    def set_hitsound_schedule(self, times_ms, keys, volumes) -> None:
        """The three parallel lists `gui.hitsound_schedule` produces, converted
        to source frames once here rather than per grain.

        Resyncs the mixer's watermark to *now* after replacing the schedule --
        see `HitsoundMixer.resync`. Every caller of this (an edit during
        playback, a hitsound-offset change) rewrites which absolute source
        frame each note lives at, and the watermark means nothing without
        that rebase.
        """
        self._mixer.set_schedule(
            [frame_for_ms(time_ms + self._hitsound_offset_ms)
             for time_ms in times_ms],
            list(keys), list(volumes))
        self._mixer.resync(self._stretcher.source_frame)

    @Slot(float)
    def set_hitsound_volume(self, volume: float) -> None:
        self._mixer.volume = max(0.0, min(1.0, float(volume)))

    @Slot(bool)
    def set_hitsounds_enabled(self, enabled: bool) -> None:
        self._mixer.enabled = bool(enabled)

    @Slot(float)
    def set_hitsound_offset_ms(self, offset_ms: float) -> None:
        """Held here rather than applied at mix time because it shifts the
        *schedule*, and the schedule is integers in source frames. Changing it
        rebuilds nothing on its own -- the next `set_hitsound_schedule` picks
        it up, and gui.py sends one whenever a setting changes."""
        self._hitsound_offset_ms = float(offset_ms)

    @Slot(float)
    def set_seek_hold_ms(self, hold_ms: float) -> None:
        self._next_hold_ms = max(0.0, float(hold_ms))

    # -- the pump -------------------------------------------------------------

    @Slot()
    def _fill(self) -> None:
        if self._sink is None or self._device is None:
            return
        if self._state == QMediaPlayer.PlayingState and self._hold_frames > 0:
            frames = min(self._hold_frames, self._sink.bytesFree() // (CHANNELS * 2))
            if frames > 0:
                self._device.write(bytes(frames * CHANNELS * 2))
                self._written += frames
                self._hold_frames -= frames
        elif self._state == QMediaPlayer.PlayingState:
            # Before the pull, so the pull never has to run the search itself.
            self._stretcher.ensure_grain(self._pcm, self._mono, self._rate)
            free_frames = self._sink.bytesFree() // (CHANNELS * 2)
            if free_frames > 0:
                block = self._stretcher.pull(
                    self._pcm, self._mono, free_frames, self._rate)
                if block:
                    if self._fade_remaining > 0:
                        block = self._fade_in(block)
                    self._device.write(block)
                    self._written += len(block) // (CHANNELS * 2)
                elif self._decoder is None or not self._decoder.isDecoding():
                    self.stop()
                    return
        if self._pending_seek_ms is not None:
            # The sink is still playing the position this seek is leaving.
            # Reporting it pulled gui.py's clock back to the old spot, where
            # its no-backwards clamp froze the playhead until the seek landed:
            # `tools/measure_wheel_seek.py` measured 110 frozen frames of 359
            # with a notch every 25ms, all of them during a parked seek. The
            # window already holds the target and extrapolates from it.
            return
        self.position_changed.emit(self._position_ms())

    def _fade_in(self, block: bytes) -> bytes:
        """Ramp the leading frames up from silence -- what softens the splice
        a sink restart otherwise leaves at full volume (see `FADE_FRAMES`).

        A raised cosine rather than a straight ramp for the same reason
        `_crossfade_window` is one: gentler at the very start, which is
        where a residual click would otherwise hide.
        """
        samples = array.array("h")
        samples.frombytes(block)
        frames = len(samples) // CHANNELS
        ramp = min(self._fade_remaining, frames)
        done = FADE_FRAMES - self._fade_remaining
        for i in range(ramp):
            gain = 0.5 - 0.5 * math.cos(math.pi * (done + i) / FADE_FRAMES)
            for channel in range(CHANNELS):
                index = i * CHANNELS + channel
                samples[index] = int(samples[index] * gain)
        self._fade_remaining -= ramp
        return samples.tobytes()

    def _position_ms(self) -> float:
        """Song time of the sample leaving the device right now.

        `processedUSecs` is the play cursor, not the write cursor -- measured:
        with a 250ms buffer kept full, the written cursor stays exactly 250ms
        ahead of it. That makes this the sample-accurate equivalent of the byte
        position osu! reads out of BASS, rather than an estimate of one.
        """
        if self._sink is None:
            return self._stretcher.source_frame / SAMPLE_RATE * 1000.0
        played = self._sink.processedUSecs() / 1_000_000.0 * SAMPLE_RATE
        start_frame, song_ms, rate = self._segments[0]
        for segment in self._segments:
            if segment[0] <= played:
                start_frame, song_ms, rate = segment
            else:
                break
        # A segment the play cursor has left can never be needed again.
        while len(self._segments) > 1 and self._segments[1][0] <= played:
            self._segments.pop(0)
        # Clamped: during a seek hold the play cursor is still in the silence
        # before the first segment, and that is not song time.
        return (song_ms + max(0.0, played - start_frame) / SAMPLE_RATE * 1000.0 * rate
                + (0.0 if self._stretcher.pitch else grain_offset_ms(rate)))


class TrackPlayer(QObject):
    """`QMediaPlayer`'s shape over the engine, so the window keeps its API.

    Only the members gui.py actually uses are here, and the enums are
    QMediaPlayer's own, so every `== QMediaPlayer.PlayingState` comparison in
    the window keeps working untouched.
    """

    positionChanged = Signal(float)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)
    errorOccurred = Signal(object, str)

    _load = Signal(str)
    _play = Signal()
    _pause = Signal()
    _stop = Signal()
    _seek = Signal(float)
    _seek_hold = Signal(float)
    _rate = Signal(float)
    _pitch = Signal(bool)
    _volume = Signal(float)
    _hitsound_schedule = Signal(object, object, object)
    _hitsound_samples = Signal(object)
    _hitsound_volume = Signal(float)
    _hitsounds_enabled = Signal(bool)
    _hitsound_offset = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._thread = QThread()
        self._thread.setObjectName("audio-engine")
        self._engine = _Engine()
        self._engine.moveToThread(self._thread)
        self._thread.finished.connect(self._engine.deleteLater)

        self._load.connect(self._engine.load)
        self._play.connect(self._engine.play)
        self._pause.connect(self._engine.pause)
        self._stop.connect(self._engine.stop)
        self._seek.connect(self._engine.seek)
        self._seek_hold.connect(self._engine.set_seek_hold_ms)
        self._rate.connect(self._engine.set_rate)
        self._pitch.connect(self._engine.set_pitch)
        self._volume.connect(self._engine.set_volume)
        self._hitsound_schedule.connect(self._engine.set_hitsound_schedule)
        self._hitsound_samples.connect(self._engine.set_hitsound_samples)
        self._hitsound_volume.connect(self._engine.set_hitsound_volume)
        self._hitsounds_enabled.connect(self._engine.set_hitsounds_enabled)
        self._hitsound_offset.connect(self._engine.set_hitsound_offset_ms)

        self._engine.position_changed.connect(self._on_position)
        self._engine.duration_changed.connect(self._on_duration)
        self._engine.state_changed.connect(self._on_state)
        self._engine.error_occurred.connect(self.errorOccurred)

        self._position_ms = 0.0
        self._duration_ms = 0
        self._state = QMediaPlayer.StoppedState
        self._playback_rate = 1.0
        self._source = ""
        self._thread.start()

    # -- what the window reads (cached: the engine is on another thread) ------

    @Slot(float)
    def _on_position(self, position_ms: float) -> None:
        self._position_ms = position_ms
        self.positionChanged.emit(position_ms)

    @Slot(int)
    def _on_duration(self, duration_ms: int) -> None:
        self._duration_ms = duration_ms
        self.durationChanged.emit(duration_ms)

    @Slot(object)
    def _on_state(self, state) -> None:
        self._state = state
        self.playbackStateChanged.emit(state)

    def position(self) -> float:
        return self._position_ms

    def duration(self) -> int:
        return self._duration_ms

    def playbackState(self):
        return self._state

    def playbackRate(self) -> float:
        return self._playback_rate

    # -- what the window commands --------------------------------------------

    def setSource(self, url) -> None:
        path = url.toLocalFile() if isinstance(url, QUrl) else str(url)
        self._source = path
        self._position_ms = 0.0
        self._duration_ms = 0
        self._load.emit(path)

    def source(self) -> QUrl:
        return QUrl.fromLocalFile(self._source) if self._source else QUrl()

    def play(self) -> None:
        # Mirrored here rather than waited for: the window reads playbackState
        # on the very next line in places, and a queued round trip to the audio
        # thread will not have happened yet.
        self._state = QMediaPlayer.PlayingState
        self._play.emit()

    def pause(self) -> None:
        self._state = QMediaPlayer.PausedState
        self._pause.emit()

    def stop(self) -> None:
        self._state = QMediaPlayer.StoppedState
        self._stop.emit()

    def setPosition(self, position_ms) -> None:
        self._position_ms = float(position_ms)
        self._seek.emit(float(position_ms))

    def set_seek_hold_ms(self, hold_ms) -> None:
        """Silence before the *next* landed seek's audio. Call before
        `setPosition`: the two signals are queued in order."""
        self._seek_hold.emit(float(hold_ms))

    def setPlaybackRate(self, rate) -> None:
        self._playback_rate = float(rate)
        self._rate.emit(float(rate))

    def set_pitch_shift(self, pitch: bool) -> None:
        """NC/DC: the next rate change resamples instead of stretching. Call
        before `setPlaybackRate`; the two signals are queued in order."""
        self._pitch.emit(bool(pitch))

    def setVolume(self, volume) -> None:
        self._volume.emit(float(volume))

    # -- hitsounds, which leave through this player and not beside it ---------
    #
    # snake_case rather than camelCase on purpose: everything above is
    # `QMediaPlayer`'s shape so the window's calls keep working, and none of
    # this is `QMediaPlayer`'s.

    def set_hitsound_schedule(self, times_ms, keys, volumes) -> None:
        self._hitsound_schedule.emit(list(times_ms), list(keys), list(volumes))

    def set_hitsound_samples(self, paths) -> None:
        self._hitsound_samples.emit(dict(paths))

    def set_hitsound_volume(self, volume) -> None:
        self._hitsound_volume.emit(float(volume))

    def set_hitsounds_enabled(self, enabled) -> None:
        self._hitsounds_enabled.emit(bool(enabled))

    def set_hitsound_offset_ms(self, offset_ms) -> None:
        """Takes effect on the next schedule, which is what the offset shifts.
        gui.py sends both together."""
        self._hitsound_offset.emit(float(offset_ms))

    def shutdown(self) -> None:
        """Stop the audio thread. Called from the window's closeEvent: an engine
        still pumping into a sink while Qt tears the process down is a teardown
        crash waiting to happen.
        """
        if not self._thread.isRunning():
            return
        # Blocking, not queued: `quit()` stops the event loop, so anything
        # merely posted to it would never be delivered and the device and file
        # would stay open for the life of the process.
        QMetaObject.invokeMethod(self._engine, "release", Qt.BlockingQueuedConnection)
        self._thread.quit()
        self._thread.wait(2000)
