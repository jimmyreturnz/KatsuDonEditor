# Taiko Fancy Arranger — working notes

A PySide6 editor for osu!taiko charts. Run it with `python main.py` from the
repository root; every import is absolute from there (`from ui import gui`,
`from app.settings import SettingsManager`).

- `ui/`: `gui.py` (still the largest, ~14.6k lines: the editor views and
  `MainWindow`), `song_select.py` (the library page), `gameplay_view.py`
  (the preview), `dialogs.py` (every dialog `MainWindow` opens),
  `common.py` (what more than one of those needs), the Settings dialog,
  `config_sheet`, `theme`, `motion`, `smooth_scroll`, the trace dialog.
- `audio/`: `audio_engine` (the transport, see below), `offset_calibration`.
- `chart/`: `gimmick_session` builds gimmick structures, `time_axis` is the
  shared scrolling/snapping mixin, plus `transformer`, `parameters`,
  `image_to_drawing`, `autoplay`, `sv_curves`.
- `library/`: `osu_db`, `song_library`, `skin`.
- `app/`: `settings`, `i18n`, `updater`, `security_utils`.
- `osu_io/` parses and writes `.osu`, `model/` holds the undo commands,
  `patterns/` the arranger's shapes. `scripts/` has the build and run `.bat`s.

A path built from `__file__` in a package module is one level below the
root: use `parents[1]`, as `resource_roots` and `ui_asset` do.

`gui.py` imports only what it uses, so reach a name through the module that
defines it (`gameplay_view.beat_pulse`, not `gui.beat_pulse`); an import
`gui` does not use is dead, and `gui.X` for it is an `AttributeError`. A
test that *rebinds* a module name
(`gui.QMessageBox = Fake`, `patch.object(gui, "reduced_motion")`) only
reaches code that looks the name up in `gui`: stub it in the module the
code under test lives in, or a real modal opens offscreen and the run
hangs. A module-level variable another module assigns needs a setter in
its own module (`common.set_view_ground_alpha`): `global` rebinds only the
caller's copy.

## Measure first. Every time.

This is the rule the project keeps re-learning, so it goes first.

Four separate investigations here reached a confident, reasonable, **wrong**
diagnosis before anyone measured:

| The confident answer | What measurement said |
| --- | --- |
| Pure-Python time-stretch is infeasible without numpy | 0.11x realtime mono, 0.22x stereo. `sum(map(mul, ...))` over `array` slices does the multiply-add in C. |
| The test suite leaks widgets | Same object count either way. It was `QApplication.installEventFilter` never being released — event fan-out, not memory. 4 hours → 106s. |
| Slow playback needs better clock interpolation | The FFmpeg backend reports position in coarse steps. The clock was correct and starved of input. |
| WMF's rate runs 1.8% fast, calibrate it out | A fixed ~12ms offset in the harness, divided by a short window. Real error 0.04%. |
| Stutter is the big timeline views | A 28-pixel-tall overview bar cost more than the full timeline above it. |
| Six open charts cost a third of all frames | The harness was timing the first second after load, while a Python downmix held the GIL. Steady state, six charts are +1.8ms a frame and inside budget. |

So: **do not optimise, diagnose, or "fix" a performance or timing problem
until a number says which thing to touch.** Write the harness, keep it in
`tools/`, and put the numbers in the commit message. Three of the four rows
above cost real work spent on the wrong thing.

Corollary: when a measurement surprises you, check the harness before you
believe it. The 1.8% row above was a harness artifact, and the check was
simply running it over a longer window.

### Audio: we own the transport

`audio_engine.TrackPlayer` replaced `QMediaPlayer` for song playback on
2026-08-29. Read that module's docstring before touching anything audio; the
short version is that the three things making osu!'s editor accurate all live
below the level Qt's player exposes:

- the clock is a **sample cursor** (`QAudioSink.processedUSecs()`, measured to
  be the play cursor and not the write cursor), not a signal in whole ms;
- slowing down **preserves pitch** (WSOLA in `TimeStretcher`), because osu!'s
  editor uses `AdjustableProperty.Tempo` rather than `Frequency`;
- a rate change is a **live ratio the next grain reads**, so it cannot stall.
  `QMediaPlayer.setPlaybackRate` lost 114ms of song time on a 0.25x -> 1.0x
  switch, which is what "the offset moves when I click a speed button" was.

Consequences worth remembering: there is no media backend to select any more
(`QT_MEDIA_BACKEND`, the Ogg fallback and the decoder setting are all gone, and
Qt's WMF backend is deprecated as of 6.10 anyway), the whole track is decoded
into memory (~50MB for 4:43), and the engine owns a thread that `closeEvent`
must shut down.

When porting from `ppy/osu`, port the *current* revision. This project's
`InterpolatingFramedClock` came from an older one and was missing both the
`AllowableErrorMilliseconds * Rate` scaling and the `DampContinuously` drift
recovery -- two real bugs inherited from reading a stale copy.

### The harnesses

- `tools/profile_playback.py <map> [frames] [--gimmick] [--profile]` — per-frame
  render cost against the 8.33ms budget. Reports the distribution, because
  stutter is the tail, not the mean. `--playing RATE` plays the song for real
  and reports the gaps between the app's own frames; it showed the
  time-stretch thread costs no frames (2026-10-06).
- `tools/measure_audio_backend.py <backend> <audio> <rate> [seconds]` — position
  reporting granularity and rate accuracy. Use a real map's audio; FFmpeg's
  granularity turned out to be codec-dependent, so a generated WAV lied.
- `tools/measure_rate_change.py <backend> <audio> <from> <to> [seconds]` — what
  a mid-playback rate change does. Taps `QAudioBufferOutput` so "the audio went
  silent" is a number rather than a report, and reports the song time *lost* at
  the switch, which is the thing `position()` hides by looking correct again
  afterwards.
- `tools/profile_playback.py ... --gameplay --skin NAME` — the gameplay
  preview is the only view that draws the playfield, and a run without one open
  measures none of it. The full skinned playfield costs 0.11ms a frame over the
  built-in lane (1.80ms -> 1.91ms at 1882x170), which is the stretch and
  silhouette caches doing their job.
- `tools/bench_timestretch.py [rate] [seconds]` — whether the WSOLA inner loop
  keeps up. It prints the distinct splice offsets actually chosen, because a
  search that quietly degenerates looks fast for the wrong reason. **Its grain
  sizes are its own**, not `audio_engine`'s, so it measures a different amount
  of work per second of output than the real code does.
- `tools/measure_stretch_timing.py [rate ...]` — whether a transient comes out
  where the playhead says it does, which is the whole reason an editor slows
  down. The `snaps` column must be 0: a correction can place every transient
  perfectly and still arrive as a step at each grain boundary, which on screen
  is the playhead teleporting. **Its own signal is a click train and that is
  not neutral** -- its search lands at 26-28% of the reach against real
  music's 50-58%, measured, so calibrating anything from its residual bias
  is the same trap that put a click-detector-tuned geometry here once already.
  Use it for snaps and relative before/after comparisons; use
  `measure_search_bias.py` below for the number that goes into
  `grain_offset_ms`.
- `tools/measure_search_bias.py <real audio> [audio ...] [rate ...]` — where
  the WSOLA splice search actually lands, and what correction that needs, on
  real tracks. Recovers `best_offset` with no assumptions and no production
  code changed: `_tail_mono` is a byte-for-byte copy of a slice of the source,
  and real audio does not repeat itself at that length by chance, so searching
  for it finds exactly where a grain came from.
- `tools/measure_stretch_quality.py <real map audio> [rate]` — tonality,
  warble, clicks and cost. **Needs a real track**; every signal it generates
  itself is synthetic, and that is exactly how a geometry that took a kick
  drum apart scored 0.987 and shipped.
- `tools/measure_wheel_seek.py ... --rate R` plays at a speed button's rate;
  `tools/measure_play_start.py <map> [rate ...] [--editor] [--gameplay]` is
  the wait after Play and the longest stall once moving. "Scrolling while
  playing freezes the playhead for about a second, worse slowed down" was
  **a stale parked seek landing after a newer one** (2026-10-09): a notch
  inside `SEEK_COALESCE_MS` parked, the next one landed immediately, and the
  parked one's timer then put the audio back up to a notch. The playhead held
  at the newer target under the no-backwards clamp until the music caught up
  -- gap / rate, hence slower is longer: 774ms at 0.5x, 300-400ms at 1.0x.
  `_land_seek` now cancels whatever is parked; 15 traced runs, worst 22ms.
- `tools/measure_song_switch.py [switches] [--seed N]` — what moving to
  another song in song select costs the event loop, phase by phase, with the
  worst gap of each switch put down to what ran inside it and a no-switch
  floor to compare against. **Run it twice**: the first pass reads every
  `.osu` cold, which is the real case when browsing, and hashing for the
  staleness check was 62ms of a switch cold against 0.6ms warm. It found
  the stutter before the chart preview loads: 99ms median, 208ms worst on
  the GUI thread, now 16ms / 27ms against a 9.6ms floor (2026-10-09), by
  moving the hashing, the background decode and the `.osu` parse onto
  `BackgroundJobs`. Qt's image decode was measured to release the GIL (0.7ms
  worst main-thread stall during 1.3s of decoding); a worker that merely
  *overlaps* a gap is not its cause, which the attribution once got wrong.
- `tools/measure_ui_freeze.py <map.osu> [--runs N] [--profile map|settings|theme]`
  -- the freeze on opening a map, Settings, or switching theme, each up to
  the end of the paint it causes. Opening a map parsed every difficulty in
  the folder for the dropdown's names: 360ms -> 76ms repeat, 626ms -> 325ms
  cold (2026-10-11). Settings (~260ms) and a theme switch (~230ms) are now
  Qt building and restyling widgets: setting the dialog's sheet after its
  widgets made it 180 -> 285ms, and the order sheets are re-set in on a
  theme switch changes nothing. Nor does the sheet's size (MainWindow's 8KB
  restyles in 110ms, an empty sheet in 90ms): the cost is per widget, and a
  switch restyles each about twice, because every one of the ~100 themed
  sheets repolishes its whole subtree under MainWindow's own pass. With
  Settings open that is 669ms (main 282, the dialog's own 145). Hoisting
  the big nested sheets' colours into MainWindow's would save ~35%, at the
  price of Qt's own-sheet-wins precedence; the owner chose to leave it
  (2026-10-11). **Process deleteLater** in a harness
  (`sendPostedEvents(None, QEvent.DeferredDelete)`): `processEvents` never
  does, so closed dialogs lived on and inflated every theme switch to 1.4s.
- `tools/play_with_hitsounds.py <map.osu> [rate] [seconds] [start_ms]` — the
  whole path, audibly: parse, schedule, decode, stretch, mix, sink. `voiced`
  and `offered` must be equal, or the 1/rate dedupe has leaked and every note
  is playing two to four times.

Two traps both harnesses were caught by, worth repeating:

- **Size the window realistically.** The offscreen platform's default is
  758×180. Paint cost scales with pixels, so measuring there understated
  everything by the ratio of the areas. `window.resize(1920, 1080)`.
- **Drive the path the app actually drives.** `repaint()` per widget charges
  each one its own compositing pass; the app calls `update()` and gets one
  coalesced pass. The difference was 2x.

## Performance shapes that keep recurring

Found by measurement, listed so the next one is recognised faster:

- **Per-frame work that redoes static work.** `TimingOverviewBar` repainted
  ~1150 marker lines every frame for content that never moves. The fix is a
  `QPixmap` static layer plus the few things that do move — `DensityOverview`
  has the reference implementation.
- **`setPen` per primitive.** Grouping draws by colour turns N pen changes
  into one per colour.
- **Binary search inside a forward-only walk.** `_draw_snap_grid` and
  `_draw_sv_curve` both searched the whole timing list per iteration while
  their cursor only moved forward. Hold the section until the walk leaves it.
- **`active_uninherited_at` is O(inherited points skipped).** It steps back one
  point at a time. On a gimmick difficulty that is thousands of steps per
  lookup. Feed it a pre-filtered list (`TimeAxisMixin.snap_beat_points`) rather
  than filtering per call — filtering per call is the cost the backward walk
  exists to avoid.
- **The skinned path returning before it used an argument.**
  `draw_note_sprite` bakes the outline pen into its cached sprite, but the
  skin branch draws the artwork and returns without ever touching it -- so a
  selected note lost its outline entirely the moment a skin was loaded. It now
  strokes the pen over the art, gated on `selected` because a skin's circle has
  its own rim and ringing every note draws a selection that is not there.
- **A hit area that is not the thing drawn.** `_note_near_x` reached a flat
  20px while `TimelineGameplay.note_radii()` draws up to 31 (42 for a
  finisher), so the outer two thirds of every note looked draggable and was
  not. Both read the same method now, as `note_center_y()` is the one place for
  the vertical.
- **Two hit tests, deliberately different.** `_note_near_x` ignores y off a
  split layer -- a right click anywhere in the band deletes what is in that
  column -- while `_press_is_on_note_body` needs both axes. Starting a *move*
  is the one thing that has to be strict: on a normal chart every column has a
  note in it, so a press meant to begin a rubber-band selection would grab one.
  A press on the circle moves it selected or not; off the circle,
  `move_requires_selection` still governs, which leaves the height above and
  below the row for selecting.
- **A grid finer than its own pen is a fill.** `_draw_snap_grid`'s floor was
  one tick per *pixel*, which on nbt-hwt's 12345 BPM anti-barline wall is 1422
  ticks 1.15px apart -- 15.3ms of a 16.3ms chart paint, against an 8.33ms
  budget, to draw a solid rectangle one line at a time. The bound is the widest
  tick pen plus a background pixel (`_min_tick_spacing_px`), and a section too
  fine for it is drawn at a coarser *divisor of* the setting rather than
  dropped: 15.3ms -> 4.3ms. Batching the lines by pen was worth half the raster
  call and nothing else -- the frame was in the walk, not the pen changes.
- **A pixmap at a fractional x is drawn at a whole one.** Qt's raster
  engine snaps every unscaled pixmap draw -- QPointF, a same-size QRectF, a
  near-identity transform, SmoothPixmapTransform on or off, all measured -- so
  a note scrolling 1.4px a frame stepped 1, 1, 2, 1, 2 and juddered.
  `draw_pixmap_subpixel` caches quarter-pixel-shifted copies (a Plus blend of
  the sprite and itself one pixel over) and blits those; anything that
  scrolls goes through it. `tests/test_subpixel_draw.py` holds the motion.
- **Frame cadence is the display's, not a constant.** The owner's monitor is
  99.93Hz; a fixed 120fps put 1.2 frames on every refresh, so one refresh in
  five showed a double step -- a 20Hz judder that paint speed cannot touch.
  `display_frame_interval_ns(screen)` is the interval, re-read when the
  window changes screen; the song select preview gates its 4ms poll on it.
- **A full GC pass is a dropped frame.** With the library loaded the heap
  is ~80k objects, and `gc.collect(2)` over it is 12-13ms against an 8.33ms
  frame, at moments nothing chose (2 of them in one 5s scroll run).
  `main()` freezes everything built at startup; the same pass is then 0.0ms.
  Measure with a `gc.callbacks` timer around the harness, not by guessing.
- **Batching draw calls does not help antialiased lines.** Tried on the SV
  bands (2026-10-11): 18 drawLines a paint into one drawLines, and the curve
  into one drawPolyline. The raster work is per pixel, not per call -- the
  batched lines cost the same 0.2s, and the polyline twice that (it pays for
  joins). Reverted. A Gimmick-page scroll repaints six layers of chart + SV
  bands at ~0.6ms each, ~7ms a notch, which is the real limit there.

- **Application-wide event filters.** Qt runs every event in the process
  through every installed filter. Bail out on `event.type()` in one lookup,
  and release the filter in `closeEvent` (`_release_application_hooks`).

### The gameplay preview's numbers come from the ruleset

`GameplayViewerView` exists to answer "what will this look like in game", so
its constants are `ppy/osu`'s and not chosen by eye. Read them from
`osu.Game.Rulesets.Taiko` rather than re-deriving:

- `TaikoHitObject.DEFAULT_SIZE = 0.475f` — note diameter as a fraction of the
  playfield height (`TaikoPlayfield.BASE_HEIGHT = 200`). This was
  `min(30.0, height * 0.19)`, which also stopped growing once the view was
  tall enough.
- `TaikoStrongableHitObject.STRONG_SCALE = 1 / 0.65f` — a finisher is 1.538x a
  normal note, not 1.4x.
- Scroll distance is `100 * SliderMultiplier * VELOCITY_MULTIPLIER(1.4) *
  ScrollSpeed / beatLength` (`DrumRoll.cs`, `TaikoBeatmapConverter.cs`). The
  map's SliderMultiplier was missing here entirely, so two maps that scroll
  three times apart previewed identically.
- `CirclePiece` flashes kiai at `kiai_flash_opacity = 0.15f` with
  `BlendingParameters.Additive`. Additive is the half that matters: as plain
  alpha it lays a film over the note instead of lighting it, and every note in
  a kiai section drifted toward the same pale orange. The flash is also a
  **child of the circle piece**, so it inherits its masking — drawn as a plain
  ellipse it lit the whole bounding box including the transparent rim, which
  reads as a glow radiating from behind the note rather than the note pulsing.
  `skin.silhouette` is that mask: the artwork's own alpha, filled flat with the
  flash colour. It pulses once per **1/1 beat**, phased from the start of the
  kiai section rather than from whatever timing point governs the playhead.

### The playfield is the skin's, and 200 is the unit

`GameplayViewerView` draws the wiki's playfield list — the bar, its kiai glow,
the scrolling background, the barline, the hit target, `lighting`. The **input
drum** (`taiko-bar-left`, `taiko-drum-inner`, `taiko-drum-outer`) and
`taiko-glow` are the autoplay's, below, and never drawn without one. `TaikoPlayfield
.BASE_HEIGHT` is 200 and **every** playfield element in the wiki is sized
against that same 200 (bar 1024x200, barline 4x175, background 776x162), so the
view's height *is* the 200 and each constant is the wiki's number over it. That is also what keeps `TAIKO_NOTE_SIZE` and the panels
from disagreeing about scale.

Three things that cost real time here:

- **Qt stamps `devicePixelRatio = 2.0` on any `@2x` file**, and every derived
  pixmap inherits it, so `drawPixmap(point, ...)` draws it at half size. A skin
  that ships some elements at `@2x` and some not (riun's `approachcircle`)
  therefore drew half its playfield at half scale. `TaikoSkin` normalises the
  ratio to 1 on load: the caller decides every size from the units above, so
  the ratio is not information. Note that `drawPixmap(QRectF, ...)` is immune,
  which is why the notes always looked right and the panels did not.
- **`lighting` goes over the bar, not behind it.** The wiki says behind; in
  practice most skins' `taiko-bar-right` is fully opaque (riun's is a flat
  `#0a0a0a`), so behind it the light never reaches the eye.
- **The built-in white wash is a stand-in, not an addition.** It is skipped
  when the skin supplies `lighting` or `taiko-bar-right-glow`, or a skinned
  lane is washed twice.

Deliberately not loaded, each because it depicts something this preview is not
doing: the **hit explosions** (`taiko-hit300*`) and `taiko-slider-fail` — a
judgement happens to a player, and nobody is playing, so a 300 burst would be
inventing a judgement the autoplay does not make; `sliderscorepoint` — drumroll ticks need `SliderTickRate`, which the
parser does not read; and `pippidon*` — a mascot, in 6 of 39 installed skins,
and the only element needing BPM-synced frames.

### Autoplay: the owner's hands, not osu!'s

The editor strips' **Autoplay** menu (owner, 2026-10-10) plays the preview's
input drum in a playstyle -- kddk full alt, semi alt, single tap, roll, ddkk --
with a binding (1234, 1324, 4231) and a dominant hand -- kddk full alt until
the user picks otherwise; Off is saved like any choice. `autoplay.py` assigns
the halves; its rules were taken down from the owner typing how *they* play
interview strings in 1234, and `tests/test_autoplay.py` holds those answers
verbatim. Change a rule only by asking for another string -- semi alt in
particular is "80% full alt, 20% single tap, no big rule", so it is a stable
hash of the note time, not a derivation.

- **Never on song select** (`plain_stage`), and nothing is judged: no hit
  explosions, no flying notes. In-app play testing is a later, bigger thing.
- Rests and breaks are measured against the map's **main** beat
  (`most_common_beat_length`): under a gimmick's 60000 BPM line every gap is a
  rest.
- **A half is lit for 150ms, not 230.** `LegacyHalfDrum` is
  `FadeTo(1, 80).Delay(100).FadeOut(50)`, and osu-framework's `Delay` counts
  from the start of the sequence, not the end of the fade before it. Read as
  three consecutive steps, every hit held visibly too long (owner, 2026-10-10).
- **A real drumroll is heard**: `hitsound_schedule` voices a don on every
  tick (`drumroll_tick_times`, 1/4 beat), the same ticks the autoplay hits. A
  fake slider has no duration, so no ticks, so it stays silent.
- The hit target holds `taikobigcircle` at 0.22 alpha (`TaikoLegacyHitTarget`),
  under the ring, everywhere but the bare stage.
- Layout and timings are ppy/osu's current `LegacyInputDrum` / `LegacyKiaiGlow`.
  The rim art is drawn **mirrored on the left** -- skinners draw it for the
  right half -- and skins before 2.1 (or with a skin.ini naming no version)
  use stable's offsets. `TaikoSkin.natural` is the one place an element is
  drawn at its own size, so it is the one place that needs to know a file was
  `@2x`.

### A drumroll is three pieces, and the wiki gives their origins

`taikohitcircle` (the head, in the drumroll colour) + `taiko-roll-middle` +
`taiko-roll-end`, in the gameplay preview **and** the editor timeline — one
object, so one drawing of it.

- `taiko-roll-middle` is **1px wide, origin TopLeft**: it is the track, meant
  to be *stretched* across the body rather than tiled.
- `taiko-roll-end` is **64x128, origin TopLeft**. TopLeft is the part that is
  easy to get wrong: the cap butts onto the far end of the track and reaches
  *past* it by its own width, mirroring the head reaching back past the start
  by its radius. Centred on the end, a roll comes up half a radius short.
- Both tint **multiplicatively** from the drumroll colour, like the head. They
  look like plain art and are not; see `skin.tinted`.

**A negative length is the same three pieces, with the track running the other
way.** Head, `taiko-roll-middle` from the object's x extending to its *right*
by the magnitude of the negative length, and `taiko-roll-end` butted onto the
far end of that.

**Only the head is taken away at the hit position.** The track and its cap pass
straight through and travel on out to the left, so a fake slider outlives its
own millisecond -- which means it cannot be culled on where its head is, and
its forward extent has to widen `_max_extend_ms` like any other body. Culled on
the head, a long one vanished while a thousand pixels of it were still on
screen.

**The two ends of the range look like different objects and are not.** A `-800`
track is over a thousand pixels of the same yellow at the same height, so the
head sits at its left edge and reads as part of the bar — which is why a long
one looks headless. The canonical `-0.0001` collapses the track to nothing and
leaves the head as the whole object. Dropping the head on the evidence of a
long one made every short fake slider invisible, and short is most of them.

The note colours are `DON_COLOR` `#e54c2e`, `KAT_COLOR` `#438dab` and
`DRUMROLL_COLOR` `#fbb706`, one constant each because the two views had
drifted to different values for the same objects.

**A circle disappears when it reaches the hit position** (`_has_been_hit`),
because that is what being hit looks like; a drumroll travels straight through
(its body is still being hit as it crosses) and a spinner stays in place.

**A kiai section too short to finish the pulse it started finishes it — under
60 BPM only.** The flash repeats every 1/1 beat from the section start; cutting
it at the section end left it black mid-fade. `_unfinished_pulse_anchor` keeps
the anchor alive until the pulse in progress runs out, so the shortest possible
kiai shows one whole beat of light and a section ending on a beat is unchanged.
Gated at `KIAI_PULSE_CARRY_MIN_BEAT_MS` (1000ms): above 60 BPM the beat is short
enough that a section ending inside one simply stops, which is what the end of a
chorus should look like, and carrying it there holds light past a kiai the map
has finished with.

### The Kiai and Sound Volume layer has no horizontal axis to edit

Its lines belong to the milliseconds that put them there — a barline, a note, a
kiai edge — so dragging one sideways moves a section boundary out from under
whatever generated it. `_drag_axis_for_click` returns `"value"` there
unconditionally, dot or no dot, red line or green; double click types a volume
rather than opening `TimingLineDialog`, which carries a Time field.

The number it writes is `TimingPoint.volume`, which is osu!'s **hitsound**
volume for the section — so it has to reach the samples or the layer is a graph
of nothing. `hitsound_schedule` resolves it per note in one forward merge over
the sorted points (`active_point_at` walks backwards a point at a time, which on
a gimmick difficulty is thousands of steps a lookup), and it rides the schedule
into `HitsoundMixer`, which applies it per voice. 0 is silent and is obeyed:
mappers set it deliberately.

**A fake slider is a drumroll too short to derive a duration** — canonically
`256,192,54692,2,12,L|624:192,643,-0.0001`, but the positive side of zero is
the same trick and is what maps in the wild mostly use: `mekurume` writes 616
of its 640 sliders as `0.001`, whose duration works out at 0.0045ms — no tick,
nothing hittable, only the head drawn. `gimmick_session.FAKE_SLIDER_MAX_LENGTH`
(0.001) is the one threshold, shared by `MainWindow.is_fake_slider` and
`GimmickConfig`, so what the toolbox can write is exactly what the layer will
admit. Read as `< 0` it missed 616 of that map's 640.

The preview draws the track **to scale**: at SliderMultiplier 2.8, a 750ms beat
and 1.0x SV, `-0.001` is 0.001px, `-1` is 1.4px, `-100` is 140px and `-800` is
1120px. Do not pad it to visibility — the head is what is meant to be seen at
that end of the range.

**The sign is not the boundary the renderer once made it.** `+0.001` derives a
duration of +0.0027ms and `-0.001` derives -0.0027ms; `_compute_end_time` splits
on `duration > 0`, so the positive one is drawn as a real roll (head + cap) and
the negative one as a fake slider. At these magnitudes the sign is a rounding
artefact, not a difference a player can see, so the two paths have to agree
about the head.

It appears at **full opacity, like any other object** — no fade-in.

**A fake slider takes its own kiai stamp**, and that is the only thing that
compounds. A shiny note is a stack of them under one note, each stamping the
same millisecond, so the note above reads brighter the deeper the pile with
nothing counting it. Dropping the stamp on the grounds that a fake slider has
no head to light broke the shiny — it is per *object*, not per drawn circle.
Outside a chorus a pile is flat drumroll yellow however deep, because a stack
of opaque objects reads as one.

Its `length` is the one number no drag can reach (its end precedes its start,
so it has no right edge to pull), so **double-clicking one types it**. That
outranks the red line under it, which on a plain fake slider is the same
millisecond — the line's BPM is 60000 by construction and the length is what
gets tuned.

### Anti-barline: the lane is white and the notes are the gaps

`mekurume [eclosion]` 1:52.5–2:15.7 is the reference. Barlines are packed until
they read as a solid sheet, and each note is a *slit* of missing barline
travelling in it — a black line on white, the exact inverse of a barline.
Per note, at `T`:

```
T,   1,   4,   1,0,60,1,8      # 60000 BPM: the note scrolls 392px/ms, invisible
T+1, 750, 999, 1,0,60,1,8      # back to the real BPM; meter 999 = never fires again
```

Both carry omit-first-barline, and that is the whole subtlety: each is a red
line and would otherwise stamp a barline in the middle of its own slit. The
second's `beat_length x meter` has to outlast the hole, which is what the
absurd meter is for — not the beat length.

The wall is one red line every `750/72` = 10.417ms (`t,2250,4,1,0,100,1,0`),
anchored at first note + 2ms. `2250 x 4` = 9000ms, so each emits exactly one
barline; `2250` = 3x the real 750 also divides scroll speed by three, which is
what packs the lines to 1.81px apart — under the 4px barline sprite, hence
solid. Slit width is the count of wall ticks omitted, and it **encodes the
colour**: don = 2 ticks (3.7px), kat = 4 (7.3px), measured 100% consistent over
the section. It is `barline_note` in negative. Finishers get no slit; they are
the six downbeats, marked by a fake slider instead.

`gimmick_session.anti_barline` writes it, from the barline layer's Anti-barline
tool over a dragged range. Nothing is hard-coded at 750: the wall is `beat /
anti_lines_per_beat` and the slit widths are counts of ticks, so every dial
holds at any tempo. All of them sit in the barline Config.

**The wall's BPM is the dial, not a ratio.** `anti_wall_bpm` defaults to None,
meaning the chart's own — the line that changes nothing but where the bars
fall. A value below it is the whole scroll effect, because an uninherited
point's BPM *is* its scroll speed: the reference's `2250` against a `750` beat
is a third of the BPM, so a third of the speed, so the same lines a third as
far apart. It was briefly exposed as that ratio (`anti_slowdown = 3.0`) and
that is not a thing the file format has — a mapper reading their own timing
panel sees a BPM, so the box asks for one. Reaching the reference section is
then `anti_lines_per_beat = 72` and a wall BPM of a third of the chart's; the
defaults (36, chart BPM) are a plainer wall to start from.

**The wall fills the dragged range and reaches one slit past the notes at
either end of it.** A note is a *hole in a sheet*, so it needs sheet on both
sides. Anchored at the first note -- which is what the reference literally does
-- that note is the leading edge of the sheet rather than anything travelling
in it, and the last note loses its trailing bars the same way, because a drag
over a run of notes naturally starts and ends on one. The bound is the edge
note's own slit width (half of it for the hole, as much again for the bars the
hole is in) rather than a number of milliseconds, so it scales with the wall.
The grid is still phased at first note + 2ms and now runs out from that anchor
in both directions, so the phase is the same either side of it.

Three more things that are not obvious from the description:

- **Only plain circles convert.** A finisher, a drumroll and a spinner in the
  range are left exactly as they are. The mapper knows which notes are the
  downbeats, and a body outlasting a slit has nothing to be a slit of.
- **Wall ticks are whole milliseconds off a fractional accumulator.** 750/72 is
  10.4167, so the ticks alternate 10 and 11 apart rather than drifting; a line
  is claimed by `round(point.time)` everywhere else in the editor, and two
  ticks that round together would be a second uninherited point on one
  millisecond, where only the first ever counted.
- **A green line on every wall line, and none on a note's own two.** Layer 6
  owns every red line's millisecond already, but what it *draws* is green
  lines — so a wall without them was a thousand barlines the layer listed and
  could not show, drag or sweep. Same handle rule `_gimmick_commands` applies
  to every other structure. It carries the chart's own SV rather than a flat
  1.0x, so a wall drawn across a section the map had sped up does not silently
  flatten it. The note's squash and restore are deliberately bare: both carry
  omit-first-barline and draw no bar, so neither is the wall's speed.

### Hidden anti-barline: the wall is osu!'s, and the slit is SV

`mew`'s `LuzeriA - Nbt-Hwt [The Pharaoh's Curse]` 1:56.9-2:07.3 is the
reference, and it is the anti-barline above built inside out. There the wall is
a red line per bar and the slit is bars left out; here **one** meter-1 red line
makes osu! emit the whole sheet, and the slit is opened by raising SV for a
fraction of a beat. Four lines per note against hundreds.
`gimmick_session.hidden_anti_barline` writes it, from Convert Notes. Per plain circle at `T`:

```
T,     0.90000900009, 999, 1,0,75, 1, 8   # 66666 BPM, omit barline -- the note, gone
T+1,   4.8602673147,  1,   1,0,75, 1, 0   # 12345 BPM, meter 1 -- the sheet, resumed
T+1,   -9900 | -9700, 4,   1,0,75, 0, 0   # don | kat: the slit opens
T+1/8, -10000,        4,   1,0,75, 0, 0   # and closes again
```

**Taiko does not integrate velocity.** An object sits at `(its time - now) x
the velocity at its own time`, so bars drawn during the raised-SV window are
pushed *ahead* of the base-speed ones by `excess velocity x how far away they
still are`. That is the whole gimmick, and the three things that follow from it
are all counter-intuitive:

- **The 1ms hole contributes almost nothing.** At SliderMultiplier 1.2 it is
  0.35px against a 1.68px bar spacing. The hide line is there to make the *note*
  invisible (66666 BPM carries it off the screen in under a frame), not to open
  the slit.
- **The slit is a percentage, not a pixel count.** don's `-9900` is 1.01% above
  the `-10000` wall and opens 3.5px per second of lead; kat's `-9700` is 3.09%
  and opens 10.7px. Exactly the 1:3 the classic converter gets from 2 ticks
  against 4 -- and `mekurume`'s fixed-width slits do not change with distance
  while these do.
- **It closes at the hit position**, which is what "hidden" means: the colour is
  only readable while the note is still far away.

Bar spacing on screen is `100 x SliderMultiplier x 1.4 x SV x meter` and has no
BPM in it at all, which is why `HIDDEN_WALL_METER` is 1 and not configurable --
SV is the dial with range in it. **The wall must be closed**: its last line has
meter 1 and no end of its own, so without a closing red line the sheet runs to
the map's next one, which on cleanly-timed chart is the rest of the song. It is
placed on the chart's own next barline after the range, so the bars resume in
phase.

### Keep SV: the chart's speed survives a conversion

Every structure is red lines, and a red line resets SV to 1.0x. Keep SV
(Convert Notes > Barline notes, per selection) and the barline layer's
**Experimental Conversion** menu (whole map, barline notes or anti-barline,
owner 2026-10-09) write the chart's own speed back after every one of them,
read from `sv_curves.SvModel`:

- **A green line belongs to the next note**, provided no note sits between
  them: mappers offset a note's line a few ms back so the note reads it, and
  never past the previous note. Original red lines split everything -- nothing
  is interpolated across an SV reset.
- **Runs are Generate SV's own curves**, detected by fitting `sv_ease`
  (moved to `sv_curves`, the very function that wrote them) by note time:
  linear, Sin In/Out, Sin, Exp 1.3/1.6, True Exp, and a fitted Exp x. A run
  is found by its monotonic reach, tried from the far end inward -- grown one
  point at a time it broke every Sin sweep into pieces, since the start of a
  Sin sweep is not a Sin sweep between its own ends.
- **At every note, the exact value osu! reads there.** A green line usually
  governs several notes; interpolating anchor to anchor sped up the ones in
  between (up to 7.7x off, measured on installed maps). Between two notes the
  value ramps along the curve **only if a green line sits in that gap**, the
  owner's "never interpolate where there is no green line".
- **Barline keeps a lone jump a step; anti-barline ramps it.** The wall fills
  every gap, and a step in it is a tear in the sheet.
- **A barline note is one object**: every line of its structure takes the
  note's own value (`keep_sv(..., at_note=)`). Read per line, a Kat's mirrored
  bars (before the note) kept the speed from before a jump and came apart
  from the rest -- Don never showed it, its bars default to after the note.
- **A kat is whistle *or* clap.** `_without_redundant_note` tested the clap
  bit alone, so converting replaced every whistle kat with a new clap kat at
  the playfield centre: 713 notes of a real map lost their hitsound and their
  Fancy Arranger position.
- **Speed**: the exponent of a typed Exp x is solved (`ln u / ln t`), not
  searched, and a candidate curve stops at its first miss; `anti_barline`
  looks BPM up in the red lines alone. Whole map, Darling Game Over Love Ura
  Oni: barline 131s -> 7.7s, anti-barline 22.5s -> 3.9s.
- **Under an anti-barline wall the chart's green lines go**, or each would hold
  its step until the next tick. A line that changes kiai or volume stays on its
  millisecond with its SV rewritten to the curve. Measured over ten installed
  maps: every note's SV identical to the original, whole-map detection
  0.01-7s.

### Convert Notes is the one way in, and its numbers are per call

Both gimmick layers route Convert Notes through `ConvertNotesDialog`. The fake
slider layer has one structure to draw and asks only for its numbers; the
barline layer has three -- **Barline notes**, **Anti-barline**, **Hidden
anti-barline** -- and asks which first. Anti-barline had a fourth button in the
barline tool row until then; a second way in that could only ever use the
layer's saved numbers was the worse one, so it is gone.

The dialog opens on the layer's saved Config and its edits are **not written
back** (`dataclasses.replace` on a copy). A section whose slits want to be
wider than the last one's is the normal case, not a reason to re-save the
layer -- and the click tools go on using the Config as saved.

**`hide_note` is the option every Don/Kat structure shares.** These gimmicks
work by putting a 60000 BPM line on the note's own millisecond: the note then
scrolls 392px/ms, which is to say it is never on screen, and the bars or the
fake slider beside it are the whole object. Off, that line carries the chart's
*own* BPM with **omit-first-barline** -- it then changes nothing but where
measure counting restarts, so the note stays visible inside its structure, and
the line does not stamp a bar over the note it was just told to show.

Two things that do not survive being made visible, and one that does:

- **The fake slider's `fake_slider_sv` goes.** That green line exists to take
  an *already squashed* note the rest of the way off screen; left in, it flings
  the visible note off the screen the red line was just told to keep it on. The
  green line itself stays, restating the speed in force -- it is the handle the
  layer's SV band is made of, exactly as for a shiny.
- **The barline note's bar goes** (its line is detached), but the restores
  either side still draw theirs, so the structure keeps its shape.
- The two anti-barline converters have no such option and must not grow one:
  the note being invisible *is* the gimmick there -- a visible note sitting in
  its own slit is not a slit.

### A delete is not a drag run backwards

`_expand_move` grows whatever was grabbed into the whole structure around it,
and both the drag and the delete go through it. They part company on one thing:
a gimmick is drawn **around a note that was already in the chart**
(`_without_redundant_note` is what stops it writing a second one), so the note
has to travel with the structure on a drag and must **not** go with it on a
delete. Removing a barline Don took the don with it; removing an anti-barline
wall line took whatever note its squash line was hiding.

`_delete_gimmick_objects` therefore keeps only the notes that were actually
selected, plus the gimmick's own drawn objects -- fake sliders, which exist for
no other reason. A structure that did have to write its own note leaves that
note behind, which is one Delete in layer 1 rather than lost music. Deleting a
note that *was* selected still takes its structure with it, which is the case
layer 1 is for.

The fake slider layer's own red lines are the one delete that does not expand:
a right click *on the line* (off the slider's circle, which is tried first)
goes to `_delete_timing_lines` and leaves the slider standing. A right click on
the slider still takes both. The line is still not draggable or selectable
there -- `timing_delete_enabled`, not `timing_edit_enabled`.

## Things that are true and non-obvious

- **Line endings are mixed, and `core.autocrlf=true`.** `ui/gui.py` used to be
  LF and is now uniformly CRLF; `audio/offset_calibration.py` and
  `tests/test_offset_calibration.py` are LF. A scripted patch must **detect**
  the file's own newline rather than assume either — read bytes, normalise to
  `\n`, patch, write back in the newline the file arrived with. Several edits
  have silently failed their own assertion because of this — and one went out
  inside a commit whose message described the change it had not made. If a
  patch script asserts, fix it before committing.
- **Every `tr()` literal needs a catalog entry** in `translations/taiko_ja.ts`
  with a non-empty translation, or `tests/test_i18n.py` fails. After editing
  the `.ts`, run `tools/compile_translations.bat`. The `.qm` is generated and
  not tracked.
- **A checked pink button is wider than the same button unchecked.**
  `QPushButton:checked` is `border: 1px solid` where the base rule is
  `border: 0`, and `font-weight: 700` where the base is 600 -- but under a
  stylesheet `QWidget::font()` still reports 600, so `sizeHint()` is short on
  both counts and the *selected* button in every tool row clipped the last
  letter of its own label. `button_chrome_width` measures the checked state as
  well as the current one and `button_text_width` measures a bold copy of the
  font; between them, anything sized through `fit_button_width` /
  `equalize_button_widths` is safe. `widths=False` is not "leave it to
  sizeHint" for the same reason -- it floors each button at its own measured
  label. `tools/check_button_widths.py` is the harness, and it has to run on
  the **real** platform: the offscreen plugin ships no fonts, every glyph
  measures as an identical tofu box, and it reports nothing.
- **Write colours in the pink palette; `theme.py` recolours them.** A theme
  is a map from the pink literals to its own, applied by a wrapped
  `QWidget.setStyleSheet` and by `theme.color()` in paint code -- so a new
  stylesheet is themed for free, but a new `QColor("#...")` of a chrome
  colour must be `theme.color("#...")`. Chart colours (notes, snap ticks, SV
  green) are deliberately not keys. A theme switches live (`theme.switch` +
  `MainWindow.retheme`): every sheet is re-translated from the source the
  wrapper keeps (`themeSource`), so **never re-set a sheet from
  `styleSheet()`** -- that is the translation, and it pins the colours to
  whichever theme was active. Append to `property(theme.SOURCE_PROPERTY)`.
  A colour computed once and kept (a mixed colour in a sheet, a cached
  layer) must be redone in `retheme`. The test suite is pinned to pink in
  `tests/__init__.py`.
- **SV is typed to 8 decimals and shown at 2.** `SV_DECIMALS` is the spin-box
  precision, not the label's: under a 60000 BPM red line the SV that moves a
  note a visible distance differs from its neighbour in the seventh decimal, so
  the boxes have to reach it -- and the graph still labels at 2dp, because it is
  read at a glance and 8 decimals on every point is a smear. The `.osu` writer
  is full-precision either way.
- **A snapped millisecond goes down, not to the nearest.** osu!stable casts a
  fractional beat position to an int, and C# truncates. Measured over 25
  installed maps: on a clean single-BPM map every circle sits in `(-1, 0]` of
  its exact beat position and **never above it** -- a flat spread with nothing
  positive, which cannot be rounding (`Hiyashi 2014`, a 315.789ms beat: 114
  notes a full 0.9ms below their own gridline). Rounding put 100 of that
  difficulty's 203 snapped notes one millisecond above what osu! wrote for the
  same snap, which is what "the snaps are one off" was. `osu_snap_ms` is that
  truncation and everything committed to a *beat* goes through it -- gridline,
  wheel seek, placement. `osu_round` (nearest, halves up) is left for the one
  thing that is not a beat position: the millisecond under the cursor for Ctrl
  placement.
- **Tests: run per file in parallel**, not `unittest discover`. Discover in one
  process still runs for tens of minutes. Three at a time, not six: six made
  the owner's machine lag while he was using it (2026-10-08).

  ```
  ls tests/test_*.py | sed 's#/#.#;s#\.py$##' \
    | QT_QPA_PLATFORM=offscreen xargs -P 3 -I{} .venv/Scripts/python.exe -m unittest {}
  ```

- **A test that passes can still prove nothing.** `QObject.receivers()` reports
  0 for Python-side connections, and replacing an instance attribute does not
  redirect an already-stored bound-method connection, so a spy set that way
  never fires either way. `tests/test_window_lifecycle.py` documents both.

**Unowned SV is barline SV.** A green line on no note, no fake slider and no
red line was owned by no gimmick SV layer and so drawn in none of them, while
the Kiai and Sound Volume layer (which owns every millisecond) listed it
happily. `_sv_layer_times` gives those orphans to `sv_barline` — display and
paste only, not `_sv_layer_object_times`, which is what Generate sweeps: an
orphan is a line, not an object.

## Conventions

- Comments explain **why**, and name the failure the code prevents. Match the
  density of the surrounding code.
- New timing points inherit the state in force at their time —
  `gimmick_session.carry_active_state` does kiai and volume for anything
  generated. Paste is the exception: it carries the clipboard's values.
- Non-trivial logic leaves one runnable check behind. Prefer a real regression
  test over a comment claiming it works.
- No new dependencies.
