# TaikoFancyEditor

If you want to ask on how to use it, suggest ideas, or report bugs, my osu name is [jimmyreturnz](https://osu.ppy.sh/users/11306153)\
my discord is also jimmyreturnz, thanks in advance!

使い方の質問やアイデアの提案などがあれば僕のosu!ネームはjimmyreturnzです
Discordもjimmyreturnzです\
よろしくお願いします！

**An osu!taiko editor with a visual pattern arranger built in, now with gimmick editor**

*日本語版は [README_JP.md](README_JP.md) をご覧ください。*

Taiko Fancy Arranger started as a tool for turning osu!taiko notes into visual patterns such as text, shapes, equations, drawings, spirals, or even an image, without placing every circle by hand. Version 2.0.0 grew it into an editor: browse your osu! Songs folder, open a difficulty, edit notes, edit scroll velocity, preview the chart the way osu! renders it, and still arrange notes into patterns when you want to. Version 3.0.0 adds a dedicated gimmick editor, for the fake sliders, barline gimmick and extreme SV effects that osu!taiko mappers build out of timing points rather than notes. 

The editor does not support editing storyboards, breaks, colours, editor bookmarks yet.

> **Current release:** v3.5.8  
> **Platform:** Windows x64  
> **Author:** [jimmyreturnz](https://osu.ppy.sh/users/11306153)

The main inspirations include Alchyr's ranked maps *13 Stairs* and *Helios*, which use unusual note placement to create visual expression. You should go check it out [here!](https://osu.ppy.sh/beatmapsets/1093671#taiko/3819326)

## Credits
- [Alchyr's TaikoEditor](https://github.com/Alchyr/TaikoEditor)
- {Mew, _gt, Alchyr}'s maps for study reference
- Many thanks to riunosk for many ideas to polish and improve the app!

---

**Song select.** Star ratings from your osu!.db, now with background and song preview!

![Song select](docs/screenshots/song-select.png)

**Editor.** You may edit multiple difficulties at once, and copy and paste from one to another.

![Editor](docs/screenshots/editor.png)

**Gameplay preview.** Sized to match the osu! playfield.

![Gameplay preview](docs/screenshots/gameplay-preview.png)

**Gimmick editor.** One layer per kind of gimmick: the fake sliders, the barlines, and each layer's SV.

![Gimmick editor](docs/screenshots/gimmick-editor.png)

**Fancy Arranger.** Transform notes into a shape, text, or an image. You can change CS and AR here.

![Fancy Arranger](docs/screenshots/fancy-arranger.png)

**Config sheets.** Every tool's options, in a redesigned sheet.

| | |
|---|---|
| ![Barline settings](docs/screenshots/config-barline.png) | ![Convert notes](docs/screenshots/convert-notes.png) |
| ![Generate SV](docs/screenshots/generate-sv.png) | ![Settings](docs/screenshots/settings.png) |

---

## What is new in 3.5.8

- **Rate Changer:** the difficulty selected in osu! at any rate from 0.5x to 2x, as new difficulties with their own MP3 or OGG, pitch kept or changed, OD scaled or typed, and every 1ms gimmick structure intact.
- **Ctrl+click** adds or removes one note from the selection.
- **The window reopens at the size it was closed at.**

Details: [`docs/releases/v3.5.8.md`](docs/releases/v3.5.8.md)


The full development log is in [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md).

---

## Windows SmartScreen notice

Taiko Fancy Arranger v3.5.8 is an unsigned Windows application. The release validates its input files, ships with automated tests, and publishes SHA-256 checksums.

Windows Defender SmartScreen may still show an "unrecognized app" warning, because a new executable has no download reputation yet.

---

## Languages

Currently supports English and Japanese. There is no Thai translation yet even though I am Thai myself 😂

Language is chosen on first start and can be changed in **Settings**. Changing it asks you to restart, because Qt does not retranslate widgets that already exist.

---

## Download and run

1. Open the repository's **Releases** page.
2. Download:

```text
TaikoFancyArranger-Windows-x64.zip
```

3. Extract the entire ZIP.
4. Run:

```text
TaikoFancyArranger.exe
```

5. That is the last time you download it by hand. When a new version is out, the app shows its patch notes on startup. Click **Update and Restart** and it installs itself into the same folder and reopens, keeping your settings and shortcuts. Keep the folder somewhere you can write to (not `Program Files`).
The release ZIP bundles Python and PySide6, so you do not need to install Python or run `pip`.

> Windows may show a reputation warning for an unsigned new application. Review the repository and release files before running the program.

---

## First start

1. **Choose a language.** English or 日本語.
2. **Choose your osu! Songs folder.** The box starts at `%LOCALAPPDATA%/osu!/Songs` when it exists. The app remembers the folder, and you can change it later from the library page.
3. **Wait for the scan.** The first scan reads every `.osu` file once and takes a while on a large collection. It runs in slices, so the window keeps responding. Later starts show the cached list before checking it. You can use the app while it scans, but the editor and osu! will not run as smoothly. Wait for the scan, or make a small folder with only the maps you want to edit.

Cancelling the folder picker leaves the library empty with a **Change folder** button.

---

## Song library

The app opens on the library page.

- Left: songs, shown as `Artist - Title · mapper · difficulty count`.
- Right: the difficulties of the selected song.
- Double-click a difficulty, or press **Edit this difficulty**, to open it in the editor.

Controls:

- **Search.** Matches artist, title, both Unicode variants, difficulty name, creator and tags at once, as osu! does.
- **Group by.** Nothing, mapper, or artist.
- **Sort.** A to Z or Z to A. This reverses the group order too.
- **Original language metadata.** Shows `ArtistUnicode` and `TitleUnicode` instead of the romanized fields, and falls back to whichever the map actually has.
- **Music player.** Previous, play and pause, stop, and next. Previous and next walk a shuffled song order that stays fixed for the session, and select that song. **Space** pauses and resumes, except while you type a search.
- **Songs folder and Reload buttons.** Click the folder to change it. **Reload** beside it has **Reload this beatmap** (`F5`, which re-reads only the selected song), Quick scan and Rescan everything.

The preview plays at 1.00x and the song's own pitch whatever mod is selected. Mods apply in the editor.

Only taiko charts are listed.

There will be a rework on the song library soon, stay tuned!

---

## Editor page

The Editor page stacks views vertically, grouped under their difficulty. Open one with **+ Add view** (view type + difficulty), or right-click the empty space below the views. Opening a difficulty automatically gives it a chart view and an SV view.

The map's own background shows faded behind the page. **Settings → Appearance → Background opacity** sets how strongly, and the page changes while you drag it. At 0% the page is plain.

Holding **Ctrl** places at 1ms precision whatever the snap divisor, with a guide line and a millisecond readout in the corner of every view.

View types:

| View | Explanation |
|---|---|
| Chart | Notes on a time axis, with a snap grid |
| SV editor | Red, green and yellow timing lines plus an effective-SV graph |
| Gameplay viewer | Read-only osu!taiko gameplay preview |
| Density | The white-to-yellow note density heatmap |

You can edit multiple difficulties at the same time as well. Ctrl+S saves every changed difficulty at once, and the Editor and the Gimmick editor each offer their own list of views to open.

### Note editing

The tool row sits at the bottom of the page and acts on the focused chart view.

```text
1  Select
2  Don
3  Kat
4  Slider
5  Spinner
6  New combo
```

- Left click places at the snapped time. A translucent ghost shows where it will land.
- Sliders and spinners are **click-dragged** to their length. Press near the right edge of an existing one to resize it, or drag its tail with the **Select** tool afterwards.
- Holding **Shift** places a finisher.
- Right click deletes the note under the cursor.
- Each millisecond holds one object, spinners excepted. Placing over a note replaces it, as a single undo step.

### SV editing

With an SV view focused, the tool row becomes:

```text
1  Select
2  Green line
3  Function
```

- Red lines are uninherited (BPM) points, green are inherited (SV), yellow means both share a millisecond.
- Click a green line to select it. Drag vertically to change its SV, or horizontally to retime it on the grid. Which axis you get depends on how close the click was to the value dot.
- Deleting green lines does not remove red lines.
- Double-clicking any timing line allows you to toggle kiai or the line's omit-barline flag.
- Dragging vertically moves the SV one `0.01` step per pixel from where you pressed.
- The graph shows **effective** SV, which is the green line's value where a green and a red line share a millisecond. Its floor is a fixed 0.1x and its ceiling scales itself. The floor will change to 0.01x.

**Function mode.** Drag a range, then choose the initial rate, final rate, position offset, whether to omit the first barline, and whether the sweep is relative to the final BPM. The seven curves are tiles, each drawn from the values you typed:

```text
linear   sin in   sin out   exp1.3   exp1.6   true exp   sin
```

The tool places points on the notes in range by default, or every N snaps. The default -5 ms offset puts the SV in force before the note it governs arrives. However many points it makes, it is one undo step.

Will eventually add an option to select exp(x>=1) SV for an experimental purpose.

### Editor shortcuts

```text
Space               Play or pause
Ctrl+Z / Ctrl+Y     Undo / redo (per difficulty)
Ctrl+C / Ctrl+V     Copy / paste notes or SV points, snapped on paste
Ctrl+S              Save every changed difficulty
Ctrl+A              Select all in the focused view
Delete              Delete the selection
Esc                 Back to the song list
1 - 6               Tools, routed to whichever tool row is showing
Mouse wheel         Seek by one snap
Shift+wheel         Seek by one beat
Ctrl+wheel          Zoom
Alt+wheel           Change beat snap
```

All of these are rebindable in **Settings**, except `Delete` and `Ctrl+A`, which belong to the focused view.

Leaving the editor with unsaved work lists which difficulties are unsaved by name and offers **Save**, **Continue without saving**, or **Cancel**. Continuing without saving keeps the edits in the session and nothing will be saved.

---

## Gimmick editor page

A "gimmick" in osu!taiko is a visual effect built out of timing points rather than notes. Examples are drumrolls of negative length that are drawn but never hittable ("fake sliders"), notes drawn out of barlines, and extreme BPM and SV values that move objects around the screen.

The gimmick page stacks six layers over one difficulty: the normal chart, a fake slider layer, a barline gimmick layer, and an SV layer for each of the three.

Entering it for a difficulty asks once whether to edit that difficulty in place or copy it into a new `[Gimmick]` difficulty, and remembers the answer permanently.\
You can also change the timing reference for a smoother scrolling experience, it is suggested to use a clean version of the chart though.\

Kiai sections show as a translucent orange band in every layer. Every timing point a gimmick tool writes carries the kiai state of the section it lands in.

Hold Ctrl over any layer to place objects 1 millisecond apart.

### Fake sliders and shiny notes

The fake slider layer places fake sliders, and "shiny" notes (several fake sliders stacked on one millisecond, which can be seen in game as a bright white glow beside a note)\
The tool classifies them by position: a fake slider at one millisecond after the note or line it hangs off is a shiny, two milliseconds after is a fake slider.\
The layer draws them on two rows, fake sliders on the ceiling and shiny below, with the snap grid down the middle, because at gimmick zoom they sit one pixel column apart.\
I hope this design would be easier for you to tell which notes are fake sliders and which notes are shiny.

You can also change the length of the fake slider by double clicking it, defaulting to -0.0010.\
This is to allow you to kind of 'encode' the fake slider; let's say you have many fake sliders in any given interval and you want each of the groups within them to have different SV behavior, you may pre-config the length before you place the fake slider and then apply SV only to fake sliders with this specific length :) 

Fake sliders with a long negative length are drawn to scale in the gameplay preview. At SliderMultiplier 2.8, a 750ms beat and 1.0x SV, a length of -100 is 140px of track.

Tools:

```text
Fake Slider
Don / Kat
Shiny
Multiple Fake Slider
Function
Convert Notes
Kiai
```

- **Multiple Fake Slider** writes a whole run of fake sliders at a set spacing in one click.
- **Function** fills a dragged range.
- **Convert Notes** turns the chart's own notes into fake slider or shiny structures.
- **Kiai** drags a range and turns every timing point inside it into one kiai section.

### Barline gimmicks

Notes drawn out of red lines. A Don is one mirrored pair of bars and a Kat is three, and you can set up each pair on its own.\
Bars can be mirrored around the note or trail it. A red line tool has its own configurable BPM, and a Function tool fills a range with red lines on a millisecond count or the beat grid, with an optional BPM ramp and a chosen SV.\
You can also do the same kind of behavior with fake sliders where you might want to apply SV to this specific BPM, or better, BPM range.

### Per-structure SV

Each of the three object layers (chart, fake slider, barline) has its own SV layer, and each owns exactly its own structures' milliseconds\
Copy and paste inside them maps by object index rather than by millisecond, so an SV shape lifted off four fake sliders lands on the next four whatever their spacing. Oscillating SV joins the seven easing curves in the generator.\
You can also change the offset of where SV is applied if you prefer, however it is suggested in gimmick editor that you should apply it directly onto where the object you want to apply the SV to is.\
As mentioned earlier, you can also apply SV speed only to fake sliders with length-specific and barline with BPM-specific.\
I believe this can simplify your gimmick beatmaps creation workflow much better, especially if you are a big fan of barline gimmick with barline to obstruct players' reading.

### Known issues (will fix soon)
- Will try to figure out an issue that when you add redline to the ms where there is already a fake slider and it would be moved to fake slider layer (this is fine and intended), but you cannot move them afterward.\
- You cannot place redline directly under fake sliders anyways, the current workaround is to place it at somewhere else and Ctrl+Drag it to the fake slider. 
I will add an option to place redline at the first fake slider at the current BPM (or custom BPM) to accommodate the behavior of you wanting the regular note to be invisible with high bpm, and reset the chart speed back to normal with current bpm at one millisecond later. I hope this would standardize the format of how gimmick charts should be created.

---

## Fancy Arranger page

The original visual arranger, unchanged in behaviour, now on its own page with its own timeline, timing bar, density chart, difficulty selector, AR and CS controls and export buttons.

### Transformations

- Text
- Drawing
- Mathematical Equation
- Horizontal
- Vertical
- Taiko
- Vertical Taiko
- Circle and ellipse
- Square, triangle, and diamond
- Star
- Spiral
- Infinity
- Arc
- Straight line
- Wave and zigzag
- Random
- Random Walk
- DVD Bouncing
- Pinwheel

Some transformations support chunking, direction controls, seeded randomness, or **Back and Forth** traversal.

Suggest other transformation if you would like to.

### Text patterns

Enter text such as:

```text
67
TAIKO
日本
ภาษาไทย
```

The Text transformation uses a selectable system font and fits the result inside the osu! playfield. Text size, margins, direction, note count, and position can be adjusted before applying the result.

Text notes are ordered from top to bottom, then horizontally within each visual row. Reverse direction keeps the top-to-bottom order and reverses only the horizontal order within each row.

You can also select fonts from your computer as well!

### Drawing patterns

Drawing accepts multiple independent strokes, treated as one visual shape rather than being connected into an artificial path. An image can be imported and traced into strokes.

Drawing notes are ordered:

1. From top to bottom
2. From left to right within each visual row

Reverse direction keeps the top-to-bottom order and changes each row to right-to-left.

Drawing-window shortcuts:

```text
Ctrl+Z    Undo the latest stroke
Ctrl+Y    Restore the latest undone stroke
```

### Mathematical equation patterns

> Warning: this is still an experimental feature, please expect some bugs.

Equation mode can generate note paths from graph outlines.

Supported modes:

- Explicit, for example `y=sin(x)`
- Implicit, for example `x^2+y^2=9`
- Parametric, for example `x(t)=cos(3*t)` and `y(t)=sin(2*t)`

Restrictions can be added to implicit or explicit expressions, for example:

```text
tan(x^2+y^2)=1{|x|<3}{|y|<3}
```

The built-in graph keyboard focuses on graph-related operations such as:

```text
sin  cos  tan
asin acos atan
sqrt abs
exp ln log
floor ceil
pi e
```

The equation renderer creates outlines only. It does not fill or color mathematical regions.

### Pinwheel patterns

Pinwheel creates a central burst with curved blades and optional seeded wander.

Its controls include:

- Inner circle
- Inner-circle note count and radius
- Number of blades
- Blade curl and spread
- Inner and outer radius
- Rotation
- Radius growth
- Wander strength and seed

### Arranger workflow

1. Drag across the timeline to select a section, or `Ctrl+A` for the whole map.
2. Choose **All Notes** or **Split Don / Kat**, then a transformation.
3. Adjust parameters, or drag the pattern directly inside the transformation view. In Split mode, dragging a Don moves the Don pattern and dragging a Kat moves the Kat pattern.
4. Press **Transform selected notes** to commit it to the session. `Ctrl+Z` / `Ctrl+Y` still apply.
5. Set **AR** and **CS** if needed. Each has a slider and a number box, from `0.00` to `10.00` in `0.01` steps. `AR 0.00` is the slowest approach rate and `CS 0.00` the biggest circle size. CS starts at the map's own value and sizes the circles in the transformation view as osu! does. AR starts at `10`.
6. **Export applied map** writes a separate arranged difficulty. **Apply all changes to original file** overwrites the loaded `.osu`, after making a backup.

The transformation pane is a preview. Nothing is written until you export or apply.

The transformation view shows the beatmap background. You can change its opacity, or drag in another image to replace it.

Notes glide to their new places when a transformation changes. **Settings → Appearance → Animate Fancy Arranger transforms** turns that off, which is lighter on a dense map.

---

## Saving and exporting

| Action | Where | What it does |
|---|---|---|
| Save | Global header | Writes every changed difficulty at once, backing each one up first |
| Export new difficulty | Global header | Writes a new difficulty file into the same song folder |
| Export applied map | Fancy Arranger | Writes an arranged difficulty to a destination you choose |
| Apply all changes to original file | Fancy Arranger | Overwrites the loaded `.osu`, after a backup |

Saving rewrites `[TimingPoints]` and `[HitObjects]` and copies every other section through unchanged. The one thing it drops is `//` comments inside those two sections.

---

## Important mapper notes

### Always keep a backup

Although the program backs up before overwriting, keep a separate copy of important beatmaps before editing.

### Test the exported map in osu!

Always open the exported difficulty in the osu! editor and verify:

- Note positions and timing
- Timing points, SV and barlines
- Hitsounds
- Approach Rate and Circle Size
- Background and difficulty name

### Visual readability depends on note count

Text, equations, drawings, and detailed shapes need enough selected notes to remain recognizable. If a pattern looks incomplete, try:

- Selecting more notes
- Increasing the number of notes per pattern
- Reducing text complexity
- Reducing the number of Pinwheel blades
- Increasing equation resolution
- Adjusting graph bounds or size

---

## Feedback and bug reports

When reporting a bug, include:

- Taiko Fancy Arranger version
- Windows version
- Which page and view (Editor chart, SV editor, Gimmick editor, Fancy Arranger, …)
- Transformation name, selected-note count and parameters, if relevant
- Exact error message or traceback
- Steps that reproduce the problem
- A minimal `.osu` example if redistribution is allowed

Do not upload copyrighted audio or private beatmap assets unless permission has been granted.

---

## Project status

Version 3.5.8 is the current public release. The project is for editing, arranging and previewing charts. Multiplayer and difficulty calculation are out of scope.

Not yet implemented, and next in line:

- Multi-difficulty editing of maps that share audio is possible today, but has not been exercised hard.

## Not guaranteed further plans

- Add rotation controls to transformations that do not support rotation yet
- Improve the usability of the Equation transformation

---

## License

Taiko Fancy Arranger is released under the MIT License. See [`LICENSE`](LICENSE) for details.

This project is a community-made tool and is not affiliated with or endorsed by osu! or peppy.

## Lastly

Thanks for reading if you made it to here, it was fun working on this project.\
Hope you could enjoy mapping better with this editor!
