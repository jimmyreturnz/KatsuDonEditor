# Taiko Fancy Arranger: Full Project Context Transfer

## Purpose

This document transfers the current known project context for **Taiko Fancy Arranger** into a new chat. It should be used together with a freshly packed copy of the repository. The packed repository is the source of truth for exact file contents, current behavior, and Git state.

The next assistant must first inspect the packed repository, compare it with this document, identify mismatches and risks, and present a concise implementation plan before making broad changes.

This revision explicitly documents the Japanese-localization regression, its root causes, and the non-negotiable approach required to finish localization safely.

---

## 1. Project identity

- **Name:** Taiko Fancy Arranger
- **Author:** jimmyreturnz
- **Repository history:** The GitHub repository may still use the historical name KatsuDonEditor. Preserve current URLs and Git conventions unless an intentional migration is approved.
- **Application type:** Windows desktop application written primarily in Python and PySide6, packaged with PyInstaller.
- **Audience:** osu!taiko players and mappers creating visual or storyboard-like note arrangements without manually positioning every note.

### Core product idea

The application loads an osu!taiko difficulty, displays notes on a gameplay timeline, allows selection, previews transformations in the osu! playfield, commits transformations to an editing session, and exports or applies modified coordinates.

The application must preserve playability and unrelated map structure while enabling creative visual placement.

---

## 2. Project-owner preferences and non-negotiable rules

### Communication

Use a casual, direct, collaborative tone. The project owner is technically advanced and comfortable with Python, PySide6, Git/GitHub, PyInstaller, release workflows, security review, OCR, and industrial automation.

### Preserve working behavior

This is the highest-priority implementation rule.

- Do not break working features while adding or repairing another feature.
- Do not replace broad sections of working code for a narrow fix.
- Do not repeatedly apply mutation scripts to an already patched source file.
- Do not assume an old snippet matches the current repository.
- Inspect the current packed code before editing.
- Prefer a complete reviewed source change over a chain of search-and-replace patchers.
- Add regression tests for behavior that has already broken once.

### Discuss architecture before coding

For broad changes, first inspect the code and present a file-by-file plan. If requirements are ambiguous, discuss the design before implementation.

### Avoid unnecessary complexity

- Do not add files without architectural justification.
- Do not perform unrelated refactors during a focused feature.
- Do not broadly redesign the UI during a feature fix.
- Preserve stable configuration keys and transformation IDs.
- Keep visible labels separate from internal logic identifiers.

### Output preferences

When implementation is requested, the project owner generally prefers:

- A complete modified source ZIP
- Clear changed-file list
- Exact behavior-impact notes
- Short, copy-pasteable validation commands
- No claim of success unless the result has actually been compiled or tested

### Unicode safety

English, Japanese, and Thai text must remain intact. Use explicit UTF-8 reads and writes for programmatic text changes. Avoid PowerShell text-rewrite pipelines that can corrupt Unicode.

---

## 3. Current desktop product capabilities

The packed codebase is authoritative, but the expected baseline includes the following.

### 3.1 Map and project discovery

- Open an `.osu` difficulty
- Detect other `.osu` difficulties in the same song folder
- Switch between available difficulties
- Read the audio filename
- Parse hit objects
- Parse inherited and uninherited timing points
- Read Kiai sections
- Read bookmarks
- Read PreviewTime
- Read and display the beatmap background

### 3.2 Audio and playback

- Load beatmap audio
- Play and pause
- Seek through the track
- Display moving current time
- Playback-speed controls at 25%, 50%, 75%, and 100%
- Compact pink playback controls near the timing/timeline area

### 3.3 Gameplay timeline

- Taiko gameplay-style note display
- Current-time cursor
- Timing and snap lines
- Drag selection across a time range
- Full-map selection
- Zooming
- Playback and seek synchronization

### 3.4 Dedicated full-song timing bar

The timing overview is separate from the density chart.

Expected timing-bar indicators:

- White horizontal center line
- Orange Kiai ranges
- Green inherited timing points
- Red uninherited timing points
- Yellow marker when inherited and uninherited points overlap
- Blue bookmark markers
- Yellow PreviewTime marker
- Current playback position
- Visible gameplay viewport

Timing markers must not be drawn in the density chart.

### 3.5 Density overview

The density chart is separate and includes:

- White-to-yellow note-density coloring
- Yellow at peak density
- Playback cursor
- Visible gameplay viewport indicator

### 3.6 Expected navigation controls

- Ctrl+A: select all notes when the gameplay view has focus
- Escape: clear selection
- Space: play/pause
- Mouse wheel: seek by beat snap
- Shift+wheel: seek by whole beat
- Ctrl+wheel: zoom gameplay timeline
- Alt+wheel: change beat snap
- Shift+Left/Right: move by one beat
- Alt+Left/Right: move by current snap division
- Ctrl+Left/Right: jump to previous/next bookmark
- Ctrl+Z: undo
- Ctrl+Y: redo

A previous wheel bug referenced an undefined `angle_delta`. The intended implementation uses the existing `angle = event.angleDelta()` object. Do not modify wheel behavior as part of unrelated work.

### 3.7 Transformation workspace

- All Notes mode
- Split Don/Kat mode
- Preview before apply
- Direct dragging of generated patterns
- Position X/Y controls
- Transformation-specific size, spacing, margin, direction, seed, and existing rotation controls
- Apply to selected notes
- Undo/redo for committed transformations

### 3.8 Transformation inventory

Expected current transformations include:

- Text
- Drawing, internally possibly `drawn_path`
- Mathematical Equation
- Horizontal
- Vertical
- Taiko
- Vertical Taiko
- Circle
- Ellipse
- Square
- Triangle
- Diamond
- Star
- Spiral
- Infinity
- Arc
- Straight line
- Polyline
- Wave
- Zigzag
- Bézier path
- Random
- Random Walk
- DVD Bouncing
- Pinwheel

Some support chunking, traversal, direction, seeded randomness, and independent Don/Kat configuration.

### 3.9 Text transformation

Expected behavior:

- User-entered text
- System-font selection using Qt
- Unicode support when installed fonts contain the glyphs
- Text size and margins
- Position controls
- Reading-order behavior

Visual ordering is top-to-bottom, then left-to-right within each visual row. Reverse mode changes horizontal row order to right-to-left. Do not reverse the entire point list vertically.

**Current roadmap correction:** planned new rotation work is for **Text only**. Do not introduce a shared rotation control across all transformations. Existing transformation-specific rotation behavior, such as Pinwheel rotation, must remain unchanged.

### 3.10 Drawing transformation

Drawing supports:

- Multiple independent strokes
- No fake connection between strokes
- One visual shape/point cloud for note allocation
- Top-to-bottom and row-wise ordering
- Reverse horizontal row order
- Dedicated Drawing dialog
- Undo, redo, and clear
- Drawing-local Ctrl+Z/Ctrl+Y
- Validation before accepting an empty drawing
- Stable preview-cache conversion for nested coordinates

Do not rename the `drawn_path` internal ID merely to translate the visible name.

### 3.11 Image-to-Drawing

Image tracing was implemented as an importer into the existing Drawing workflow, not as a separate transformation.

Pipeline:

```text
Local image
→ deterministic raster processing
→ boundary extraction
→ separate Drawing strokes
→ fit into 512×384 playfield
→ edit in Drawing
→ existing Drawing transformation places notes
```

Implemented direction:

- PNG, JPG, JPEG, WEBP, BMP
- Dark Lines mode as default
- Light Lines mode
- Alpha Outline mode
- Threshold
- Minimum outline filtering
- Simplification
- Invert
- Fast boundary-edge walking rather than quadratic nearest-neighbor ordering
- Processing copy may be downscaled for performance
- Final geometry is normalized to the osu! 512×384 playfield
- Disconnected components and holes remain separate strokes
- No OpenCV or machine-learning runtime required
- Local files only
- No network access or external commands

### 3.12 Equation transformation

Expected modes:

- Explicit
- Implicit
- Parametric

The evaluator is AST-based and must not be replaced with unrestricted `eval`. Existing limits on expression length, AST complexity, depth, exponent size, numeric magnitude, names, and calls must remain.

### 3.13 Pinwheel

Expected controls include inner circle, blade count, blade curl/spread, inner/outer radius, rotation, radius growth, wander strength, and seed. Preserve current working behavior.

### 3.14 Background handling

- Beatmap background is displayed in the transformation view
- Opacity is configurable
- User can drag another local image to use as the beatmap background
- Asset resolution is constrained to safe paths and allowlisted file types

### 3.15 Export values and writer safety

The toolbar exposes AR and CS controls with 0.01 resolution. Expected defaults are AR 10.00 and CS 7.00.

Export behavior includes:

- Export separate arranged difficulty
- Apply committed changes to original
- Confirm and back up original
- Preserve unrelated `.osu` sections where possible
- Validate destination suffix
- Validate finite AR/CS range
- Preserve encoding and line endings where possible
- Write to temporary output and validate before replacement

---

## 4. Release and security context

### Release lineage

- v1.0.1 included Drawing restoration/fixes, font selection, timeline restoration, playback controls, AR/CS controls, and icon/title work.
- v1.0.2 was intended as security-hardening and stability.
- v1.0.3 became the localization/settings release direction. Japanese infrastructure and settings were added, but broad localization coverage later suffered regressions and must be stabilized before further release work.

### Windows packaging

- PyInstaller one-folder build
- Executable icon from the existing assets
- Compiled `.qm` translations must be packaged under `translations/`
- Build and dist directories are generated outputs and must not be treated as forbidden packaged runtime files
- `.pyd` files under the PyInstaller output are expected runtime dependencies
- Release-cleanliness checks should inspect tracked source, or explicitly ignore `build/` and `dist/`

### Defender and supply-chain posture

Do not:

- Tell users to disable Defender
- Recommend permanent exclusions
- Obfuscate binaries to evade detection
- Use UPX
- Claim code signing or source review proves harmlessness

Preferred controls:

- Clean tagged build
- Dependency review
- Tests
- Scan unpacked folder and ZIP
- SHA-256 checksums
- Microsoft false-positive submission when appropriate
- Authenticode signing when feasible

---

## 5. Japanese localization: current state and critical regression context

### 5.1 What was implemented

The desktop app has or is expected to have:

- `QSettings` language persistence
- English and Japanese selection
- Restart prompt after language change
- `QTranslator` installed before `MainWindow` construction
- `taiko_ja.ts` and compiled `taiko_ja.qm`
- PyInstaller packaging for `.qm`
- Settings dialog translation
- Main-window translation coverage in progress
- Dynamic transformation parameters requiring explicit extraction support

### 5.2 What went wrong

Localization fixes were repeatedly applied through incremental search-and-replace scripts against an evolving `gui.py`. This caused previously working translations to disappear when later patches changed translation lookup behavior.

Observed regressions included previously translated controls becoming English again:

- Playback Rate
- Play and Pause
- Reset applied transforms
- All Notes
- Split Don/Kat
- All tab
- Snap
- Duration and current-time information
- Timeline help text
- Position X/Y
- Traversal
- Font
- Notes per transformation
- Column/Columns
- Min/Max BPM
- Step Size
- Chunk controls
- Maximum Turn
- Drawing and Image-to-Drawing labels

There was also a runtime crash where calls to `tr_main(...)` were introduced but `tr_main` was not defined because an installer only inserted both helpers when `tr_parameter` was missing. This demonstrates why idempotent but partial source-mutating scripts are unsafe for the current codebase.

### 5.3 Root causes

#### Inconsistent Qt contexts

The same source text was looked up through different contexts over time, for example:

```python
self.tr("Play")
QCoreApplication.translate("MainWindow", "Play")
QCoreApplication.translate("Parameters", "Play")
```

A translation existing under one context does not guarantee a match under another context.

#### Dynamic labels not discoverable by `lupdate`

Transformation metadata contains labels and choices in dictionaries:

```python
{"key": "font_family", "label": "Font", ...}
{"key": "back_and_forth", "label": "Traversal", "choices": [...]}
```

Runtime calls such as `translate(context, definition["label"])` can translate a known entry, but `lupdate` cannot infer all possible dictionary values unless they are explicitly marked or generated into a catalog source.

#### Live parameters exist in more than one file

Some parameters come from `gui_draft.py`, while others are injected in `gui.py`, including Drawing parameters and Font. Scanning only one file misses visible labels.

#### Incorrect coverage-gate design

One attempted generator copied every discovered string into every translation context. This created a Cartesian-product explosion and hundreds of false missing entries.

Examples of false positives that must not be treated as visible translatable labels:

- Internal values: `en`, `ja`, `dark`, `light`, `alpha`
- Symbols: `+`, `-`, `▶`, `❚❚`, `⌨`, `⚙`
- Numeric placeholder: `00:00:000`
- Mathematical notation: `x(t)`, `y(t)`

For `QComboBox.addItem(visible_label, internal_value)`, only the first argument is visible and translatable. The second argument is stable data and must remain unchanged.

#### Blind source rewriting

Repeated exact-string patching failed when formatting or localization had already changed. Partial patch runs modified some sections and stopped before completing others, leaving inconsistent states.

### 5.4 Non-negotiable localization architecture

The next assistant must not apply another broad translation patch script before inspecting the current packed code.

Required architecture:

1. **One stable translation lookup policy per UI area.**
2. **Stable internal identifiers remain untranslated.**
3. **Visible labels are translated at display time only.**
4. **Dynamic parameter labels and choice labels are explicitly collected from all live definition sources.**
5. **The catalog generator is context-specific, not all-strings-to-all-contexts.**
6. **Existing reviewed translations must be preserved when regenerating catalogs.**
7. **Coverage auditing must understand Qt APIs and ignore internal data arguments.**
8. **No patch chain. Commit final source changes directly and review the diff.**
9. **Add regression tests for every previously broken critical control.**
10. **Release builds fail closed when required visible strings are unfinished.**

### 5.5 Recommended contexts

Use only contexts that correspond to real runtime ownership, such as:

- `MainWindow`
- `Parameters`
- `DrawingDialog`
- `ImageTraceDialog`
- `SettingsDialog`
- `Transformations`, if transformation names use a dedicated context

Do not register every string under every context.

### 5.6 Required visible-string coverage

#### Main window and toolbar

- Open `.osu`
- Settings
- Difficulty
- Play
- Pause
- Reset applied transforms
- Export applied map
- Playback Rate
- AR/CS labels and tooltips
- Background Opacity
- Transformation mode
- All Notes
- Split Don/Kat
- Swap Don/Kat
- Transform selected notes
- Apply all changes to original file
- Beat snap

#### Timeline

- Duration
- Now, replacing the old visible wording Position for song time
- Snap
- Wheel seek help
- Shift+wheel help
- Ctrl+wheel zoom help
- Timeline tooltips and status text

#### Transformation controls

- Transformation display names
- All/Don/Kat tabs
- None
- Position X/Y
- Font
- Traversal
- Direction
- All `Notes per ...` labels
- Column/Columns
- Rows
- Min/Max BPM
- Step Size
- Chunk fields
- Maximum Turn
- Seeds
- Margins
- Angles
- Equation parameters
- Every visible choice label

#### Dialogs and messages

- Settings dialog
- Drawing dialog
- Image-to-Drawing dialog
- File dialogs
- Warnings
- Errors
- Status messages
- Tooltips
- Export/overwrite confirmations

### 5.7 Required regression tests

At minimum, test Japanese lookup for:

- Play → 再生
- Pause → 一時停止
- Playback Rate → 再生速度
- Reset applied transforms
- All Notes
- Split Don/Kat
- All → すべて
- Snap → スナップ
- Duration
- Now
- Position X/Y
- Font
- Traversal
- Notes per Drawing
- At least one parameter from `gui_draft.py`
- At least one parameter injected in `gui.py`
- Drawing dialog text
- Image tracing mode text
- Settings dialog text

Also test:

- Invalid stored language falls back to English
- Missing translation falls back to source English
- Missing `.qm` does not crash
- Both translation helper functions exist before `MainWindow` construction
- Compiled `.qm` loads from source and PyInstaller one-folder output

### 5.8 Translation-update command requirements

A translation-update command should:

1. Extract literal static strings from the real source.
2. Collect dynamic labels/choices from all parameter-definition locations.
3. Update `taiko_ja.ts` without discarding reviewed translations.
4. Report only genuine unfinished visible strings.
5. Compile `taiko_ja.qm` only after required translations are complete.
6. Run the context-aware coverage audit.
7. Return nonzero on genuine localization failure.

The audit must not rewrite application source.

### 5.9 Recovery rule

If the current packed code still contains partially applied translation scripts or duplicated helpers:

- Preserve the current state with `git diff`.
- Compare against the last known-good commit.
- Remove duplicate or dead helper definitions deliberately.
- Consolidate runtime translation wiring in reviewed source.
- Do not rerun old patch scripts.
- Validate application startup before further translation work.

---

## 6. Settings and shortcut infrastructure

Expected persistent service:

```python
QSettings("jimmyreturnz", "TaikoFancyArranger")
```

Stable keys may include language, overwrite confirmation, and configurable shortcuts.

Settings UI direction:

- Plain, native, functional layout
- General, Language, Shortcuts, Advanced pages
- Apply, OK, Cancel, Restore Defaults behavior
- Duplicate-shortcut detection
- Text controls retain standard editing shortcuts
- Drawing-local undo/redo remains local
- Only expose options that actually work

Language changes currently require restart.

---

## 7. Current priority roadmap

### Immediate stabilization

1. Inspect the current packed desktop repository.
2. Establish a clean, launching source state.
3. Remove or quarantine obsolete translation mutation scripts.
4. Consolidate translation runtime helpers and contexts.
5. Build a context-aware dynamic-string catalog.
6. Complete reviewed Japanese translations.
7. Add localization regression tests.
8. Verify source launch.
9. Verify clean PyInstaller one-folder build.
10. Confirm Japanese in packaged runtime.
11. Re-run existing transformation, playback, timeline, Drawing, image tracing, writer, and security tests.

Do not start another major feature until this baseline is stable.

### Next feature: Text-only rotation

Add rotation only to Text.

Suggested control:

- Range: -180.00° to 180.00°
- Step: 0.10°
- Default: 0.00°
- Rotate complete generated text around visual bounds center
- Apply Position X/Y after rotation
- 0° must reproduce existing output
- Split Don/Kat may hold independent Text rotations through existing separate parameter state

Do not add a global shared rotation stage to every transformation.

### Text layout follow-up

- Whole Text mode
- Per-character or grapheme-aware mode
- Character spacing
- Preserve Unicode clusters and combining marks
- No independent per-character rotation in the first version

### Canvas interaction

After Text work:

- Box-select visible notes on the playfield
- Shift-drag add to selection
- Ctrl-click toggle note
- Drag selected group in X/Y
- Keep timeline time movement separate from canvas position movement
- Preserve current transform workflow

### Single-chart editor

After geometry interaction is stable:

- Add/delete notes
- Change Don/Kat/Big Don/Big Kat
- Move notes in time
- Copy/paste with relative timing
- Snap-based editing
- Rebuild `[HitObjects]`
- Unified chronological undo/redo
- Save as new difficulty
- Apply to original with backup

### Multi-difficulty workspace

Only after single-chart editing and serialization are stable:

- Shared song folder/audio/playback
- Independent note state, selection, dirty state, undo history, and export target per difficulty
- Safe switching without edit loss or cross-chart leakage

### SV roadmap

1. SV/inherited timing-point data model
2. Preview
3. Command/undo system
4. Valid serialization
5. Configurable effect functions
6. Barline, slider, shiny, reverse-barline, and experimental gimmick tools

Do not mutate raw timing lines independently in each gimmick tool.

### Lower-priority tier

- Equation usability improvements
- Broad UI redesign/workspace cleanup
- Secure updater
- Full-alt transformation exploration

---

## 8. Testing and release gates

### Unit tests

Maintain and expand:

- Transformation tests
- Writer tests
- Security tests
- Settings tests
- Shortcut-conflict tests
- Translation-context and key-coverage tests
- Image tracing tests
- Future Text rotation geometry tests
- Future chart serialization tests

### Desktop smoke tests

Before release:

- Open and switch difficulty
- Audio playback
- Play/Pause labels and behavior
- Playback speeds
- Wheel, Shift+wheel, Ctrl+wheel, Alt+wheel
- Timeline current time, duration, snap, selection
- Timing overview and density chart separation
- Bookmarks and PreviewTime
- All Notes and Split Don/Kat
- Transformation selection and parameters
- Text font and Unicode
- Drawing multi-stroke and local undo/redo
- Image-to-Drawing import and fast tracing
- Equation transformation
- AR/CS
- Main undo/redo
- Export
- Apply with backup
- Long metadata elision
- English and Japanese full UI pass

### Packaged tests

- Build from clean tracked source
- Confirm `.qm` is included
- Launch packaged EXE
- Confirm selected language persists
- Verify source and packaged translation lookup are identical
- Scan unpacked folder and ZIP
- Verify icon, README, LICENSE, VERSION, and checksums
- Do not remove required `.pyd` or Qt runtime files from `dist`

---

## 9. Git and repository hygiene

- Work on focused feature branches
- Stage explicit files
- Do not commit `build/`, `dist/`, release archives, backups, caches, generated reports, or patch artifacts
- Preserve source assets and PyInstaller spec
- Use anchored conflict-marker detection
- Use Python UTF-8 file edits for Markdown and translation catalogs
- Push main successfully before tagging
- Compare tag content with main
- Do not rewrite public tags casually

Recommended localization branch name:

```text
feature/localization-stabilization
```

---

## 10. Instructions for the next assistant

The next assistant must:

1. Read this document and inspect the packed repository.
2. Treat packed current files as authoritative.
3. Report the current translation architecture and every translation helper/context in use.
4. Identify partial patch artifacts, duplicated helpers, and dynamic label sources.
5. Compare current startup behavior against the last known-good state.
6. Present a file-by-file localization stabilization plan.
7. Avoid another blind search-and-replace patcher.
8. Preserve playback, timeline, wheel behavior, transformations, Drawing, Image-to-Drawing, Equation security, writer behavior, and packaging.
9. Implement final reviewed source changes directly.
10. Add regression tests before declaring localization complete.
11. Return a complete reviewed desktop source ZIP when implementation is requested.

### Recommended opening request for the next chat

```text
Read the attached updated full-project context transfer and the packed current desktop codebase.
Do not code yet.

First inspect the repository and report:
1. Current startup and i18n architecture
2. All translation helpers and Qt contexts currently used
3. All dynamic labels and choice sources in gui.py and gui_draft.py
4. Any partial patch artifacts, duplicate helpers, or catalog-generation problems
5. A file-by-file plan to stabilize complete English/Japanese localization without changing working behavior
6. Regression tests that will prevent Play, Pause, Playback Rate, Reset applied transforms, All Notes, Split Don/Kat, Snap, timeline labels, and transformation parameters from becoming untranslated again

Preserve all desktop behavior. Rotation work is Text-only. Do not modify wheel handling. Do not use translated display text as an internal identifier. Do not use an all-strings-to-all-contexts catalog generator.
```

---

## 11. Final project intent

Taiko Fancy Arranger is evolving incrementally from a visual transformation utility into a broader osu!taiko creative editor.

Current progression:

```text
Stable desktop arranger
→ reliable settings and complete Japanese localization
→ Image-to-Drawing stabilization
→ Text-only rotation and text layout improvements
→ direct canvas interaction
→ single-chart editing
→ multi-difficulty project editing
→ SV model and preview
→ individual SV gimmick tools
```

Core principles:

- Preserve playability
- Protect source maps
- Preview before commit
- Keep undo/redo reliable
- Preserve working features
- Use stable internal identifiers
- Keep localization maintainable and regression-tested
- Maintain clean and transparent releases
- Avoid overengineering
- Keep the software understandable for mappers
