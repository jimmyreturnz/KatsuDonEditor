from __future__ import annotations

import ast
import math
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import QPointF
from PySide6.QtGui import QFont, QFontDatabase, QFontMetricsF, QRawFont, QTextLayout, QTextOption, QTransform

PLAYFIELD_WIDTH = 512
PLAYFIELD_HEIGHT = 384
Point = tuple[float, float]
PositionMap = dict[int, tuple[int, int]]


def transform(name: str, selected_note_indexes: Iterable[int], params: Mapping[str, Any] | None = None) -> PositionMap:
    """Return transformed coordinates keyed by original hit-object index."""
    name = _name(name)
    p = dict(params or {})
    indexes = _indexes(selected_note_indexes)
    if not indexes:
        return {}

    if name == "taiko":
        return _taiko(indexes, p)

    if name == "horizontal":
        rows = _pos_int(p, "line_count", 4)
        per_row = _pos_int(p, "notes_per_line", 16)
        return _chunks(indexes, rows * per_row, lambda c, _: _horizontal(c, rows, per_row, p))

    if name == "random":
        seed = int(p.get("seed", 0))
        return _chunks(indexes, _chunk_size(p), lambda c, n: _random(c, p, seed + n))

    if name == "random_walk":
        seed = int(p.get("seed", 0))
        continuous = dict(p); continuous["steps"] = len(indexes)
        return _on_path(indexes, _random_walk(continuous, seed), False)

    if name == "vertical":
        columns = _pos_int(p, "line_count", 4)
        per_column = _pos_int(p, "notes_per_line", 16)
        return _chunks(indexes, columns * per_column, lambda c, _: _vertical(c, columns, per_column, p))

    if name == "vertical_taiko":
        return _vertical_taiko(indexes, p)

    if name == "dvd_bouncing":
        seed = int(p.get("seed", 0))
        continuous = dict(p); continuous["steps"] = len(indexes)
        return _on_path(indexes, _dvd_bouncing(continuous, seed), False)

    if name == "text":
        return _chunks(indexes, _chunk_size(p), lambda chunk, _: _text(chunk, p), bool(p.get("back_and_forth", False)))

    if name == "pinwheel":
        return _chunks(indexes, _chunk_size(p), lambda chunk, number: _pinwheel(chunk, p, number), bool(p.get("back_and_forth", False)))

    if name == "equation":
        return _equation(indexes, p)

    if name == "drawn_path":
        points=[(float(point[0]),float(point[1])) for point in p.get("points",[]) if len(point)>=2]
        if len(points)<2: raise ValueError("Drawing needs at least two sampled points")
        ordered=_visual_reading_order(points,bool(p.get("reverse",False)));count=len(indexes)
        sampled=[ordered[round(i*(len(ordered)-1)/max(1,count-1))] for i in range(count)]
        return {index:(_round(x),_round(y)) for index,(x,y) in zip(indexes,sampled)}

    builders = {
        "circle": _circle,
        "ellipse": _ellipse,
        "square": _square,
        "triangle": _triangle,
        "diamond": _diamond,
        "infinity": _infinity,
        "star": _star,
        "spiral": _spiral,
        "arc": _arc,
        "straight_line": _line,
        "polyline": _polyline,
        "wave": _wave,
        "zigzag": _zigzag,
        "bezier": _bezier,
    }
    if name not in builders:
        raise ValueError(f"Unknown transformation: {name}. Available: {', '.join(available_transformations())}")

    path, closed = builders[name](p)
    if bool(p.get("reverse", False)):
        path = list(reversed(path))
    return _chunks(indexes, _chunk_size(p), lambda c, _: _on_path(c, path, closed))


def transform_groups(groups: Mapping[str, Mapping[str, Any]]) -> PositionMap:
    """Apply different transformations to explicit, non-overlapping note groups."""
    output: PositionMap = {}
    owners: dict[int, str] = {}
    for group_name, spec in groups.items():
        result = transform(
            str(spec["transformation_name"]),
            spec.get("selected_note_indexes", []),
            spec.get("params", {}),
        )
        for index, position in result.items():
            if index in output:
                raise ValueError(f"Note {index} appears in groups '{owners[index]}' and '{group_name}'")
            output[index] = position
            owners[index] = group_name
    return output


def available_transformations() -> tuple[str, ...]:
    return (
        "pinwheel", "equation", "text", "horizontal", "vertical", "taiko", "vertical_taiko", "dvd_bouncing",
        "circle", "ellipse", "square", "triangle",
        "diamond", "infinity", "star", "spiral", "arc", "straight_line",
        "polyline", "wave", "zigzag", "bezier", "random_walk",
        "drawn_path", "random",
    )


# -------------------- timing-aware taiko transformation --------------------

def _taiko(indexes: list[int], p: dict[str, Any]) -> PositionMap:
    """Arrange notes in fixed musical-time rows.

    beats_per_line determines row membership. Note count never affects wrapping.
    Each non-empty row starts its first note at margin_x. Following notes retain
    their exact timing distance from that row's first note.
    """
    note_times = _note_times(p.get("note_times"), indexes)
    timing_mode = str(p.get("timing_mode", "map")).lower()
    anchor_mode = str(p.get("anchor_mode", "selection_start")).lower()
    beats_per_line = _pos_float(p, "beats_per_line", 2.0)
    line_count = _pos_int(p, "line_count", 8)
    margin_x = _margin(p, "margin_x", 12, PLAYFIELD_WIDTH)
    margin_y = _margin(p, "margin_y", 10, PLAYFIELD_HEIGHT)

    if anchor_mode not in {"continuous", "timing_section", "selection_start"}:
        raise ValueError(
            "anchor_mode must be continuous, timing_section, or selection_start"
        )

    sections = _timing_sections(p, timing_mode)
    if not sections:
        raise ValueError(
            "taiko requires at least one valid uninherited timing point"
        )

    if line_count == 1:
        row_y_positions = [PLAYFIELD_HEIGHT / 2]
    else:
        usable_height = PLAYFIELD_HEIGHT - 2 * margin_y
        row_y_positions = [
            margin_y + row * (usable_height / (line_count - 1))
            for row in range(line_count)
        ]

    absolute_beats = {
        index: _beat_position(note_times[index], sections, "continuous")
        for index in indexes
    }

    first_index = min(indexes, key=lambda index: note_times[index])

    if anchor_mode == "selection_start":
        anchor_beat = absolute_beats[first_index]
    elif anchor_mode == "timing_section":
        first_time = note_times[first_index]
        active_section = sections[0]
        for section in sections:
            if section["time"] <= first_time:
                active_section = section
            else:
                break
        anchor_beat = active_section["start_beat"]
    else:
        anchor_beat = 0.0

    relative_beats = {
        index: absolute_beats[index] - anchor_beat
        for index in indexes
    }

    boundary_tolerance = float(p.get("boundary_tolerance", 1e-7))
    rows: dict[int, list[tuple[int, float]]] = {}

    for index in indexes:
        relative_beat = relative_beats[index]
        quotient = relative_beat / beats_per_line
        nearest_boundary = round(quotient)

        if abs(quotient - nearest_boundary) <= boundary_tolerance:
            quotient = float(nearest_boundary)
            relative_beat = quotient * beats_per_line

        row_number = math.floor(quotient)
        rows.setdefault(row_number, []).append((index, relative_beat))

    usable_width = PLAYFIELD_WIDTH - 2 * margin_x
    output: PositionMap = {}

    for row_number in sorted(rows):
        row_notes = sorted(rows[row_number], key=lambda item: (item[1], item[0]))
        first_note_beat = row_notes[0][1]

        for index, relative_beat in row_notes:
            beat_distance = relative_beat - first_note_beat
            x = margin_x + beat_distance / beats_per_line * usable_width
            x = min(max(x, margin_x), PLAYFIELD_WIDTH - margin_x)
            y = row_y_positions[row_number % line_count]
            output[index] = (_round(x), _round(y))

    return output

def _fit_positions(positions: PositionMap, margin_x: int, margin_y: int) -> PositionMap:
    if not positions:
        return {}
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    available_w = PLAYFIELD_WIDTH - 2 * margin_x
    available_h = PLAYFIELD_HEIGHT - 2 * margin_y
    scale = min(1.0, available_w / max(width, 1), available_h / max(height, 1))
    center_x = (min(xs) + max(xs)) / 2
    center_y = (min(ys) + max(ys)) / 2
    return {
        index: (
            _round(PLAYFIELD_WIDTH / 2 + (x - center_x) * scale),
            _round(PLAYFIELD_HEIGHT / 2 + (y - center_y) * scale),
        )
        for index, (x, y) in positions.items()
    }


def _vertical_taiko(indexes: list[int], p: dict[str, Any]) -> PositionMap:
    base = _taiko(indexes, p)
    rotated = {index: (PLAYFIELD_WIDTH / 2 + (y - PLAYFIELD_HEIGHT / 2), PLAYFIELD_HEIGHT / 2 - (x - PLAYFIELD_WIDTH / 2)) for index, (x, y) in base.items()}
    if str(p.get("direction", "top_to_bottom")) == "bottom_to_top":
        rotated = {index: (x, PLAYFIELD_HEIGHT - y) for index, (x, y) in rotated.items()}
    return _fit_positions(rotated, _margin(p, "margin_x", 12, PLAYFIELD_WIDTH), _margin(p, "margin_y", 10, PLAYFIELD_HEIGHT))


def _timing_sections(p: dict[str, Any], mode: str) -> list[dict[str, float]]:
    if mode == "manual":
        bpm = _pos_float(p, "manual_bpm", 120.0)
        offset = float(p.get("manual_offset_ms", 0.0))
        return [{"time": offset, "beat": 60000.0 / bpm, "start_beat": 0.0}]

    raw = p.get("timing_points")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("taiko requires params['timing_points']")

    valid: list[tuple[float, float]] = []
    for item in raw:
        time, beat_length, uninherited = _timing_point_values(item)
        if not uninherited or beat_length <= 0:
            continue
        bpm = 60000.0 / beat_length
        if mode == "filtered":
            minimum = float(p.get("min_bpm", 10.0))
            maximum = float(p.get("max_bpm", 1000.0))
            if not minimum <= bpm <= maximum:
                continue
        valid.append((time, beat_length))

    valid.sort(key=lambda item: item[0])
    if not valid:
        return []

    if mode == "base":
        selection = int(p.get("base_timing_index", 0))
        if not 0 <= selection < len(valid):
            raise ValueError("base_timing_index is outside the valid timing-point range")
        time, beat = valid[selection]
        return [{"time": time, "beat": beat, "start_beat": 0.0}]

    if mode not in {"map", "filtered"}:
        raise ValueError("timing_mode must be map, base, filtered, or manual")

    sections: list[dict[str, float]] = []
    cumulative = 0.0
    for i, (time, beat) in enumerate(valid):
        if i:
            previous = sections[-1]
            cumulative = previous["start_beat"] + (time - previous["time"]) / previous["beat"]
        sections.append({"time": time, "beat": beat, "start_beat": cumulative})
    return sections


def _beat_position(time_ms: float, sections: list[dict[str, float]], anchor_mode: str) -> float:
    section_index = 0
    for i, section in enumerate(sections):
        if section["time"] <= time_ms:
            section_index = i
        else:
            break
    section = sections[section_index]
    local = (time_ms - section["time"]) / section["beat"]
    if anchor_mode == "timing_section":
        return local
    return section["start_beat"] + local


def _note_times(raw: Any, indexes: list[int]) -> dict[int, float]:
    if isinstance(raw, Mapping):
        converted = {int(k): float(v) for k, v in raw.items()}
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        converted = {}
        for item in raw:
            if isinstance(item, Mapping):
                index = item.get("original_index", item.get("index"))
                time = item.get("time_ms", item.get("time"))
            else:
                index = getattr(item, "original_index", getattr(item, "index", None))
                time = getattr(item, "time_ms", getattr(item, "time", None))
            if index is None or time is None:
                raise ValueError("Each timed note needs original_index and time_ms")
            converted[int(index)] = float(time)
    else:
        raise ValueError("taiko requires params['note_times'] as a mapping or timed-note sequence")
    missing = [i for i in indexes if i not in converted]
    if missing:
        raise ValueError(f"Missing timestamps for note indexes: {missing[:10]}")
    return converted


def _timing_point_values(item: Any) -> tuple[float, float, bool]:
    """Read one timing point, as a raw [TimingPoints] line or as an object.

    Both shapes are live: gui.py passes osu_io TimingPoint objects, and the
    headless API takes .osu lines.
    """
    if isinstance(item, str):
        fields = item.strip().split(",")
        if len(fields) < 7:
            raise ValueError(f"Invalid timing-point line: {item}")
        return float(fields[0]), float(fields[1]), int(fields[6]) == 1
    try:
        return float(item.time), float(item.beat_length), bool(item.uninherited)
    except AttributeError as error:
        raise ValueError(
            "Timing points need time, beat_length, and uninherited"
        ) from error


# -------------------- common chunk and geometry helpers --------------------

def _chunks(indexes, size, function, back_and_forth=False):
    output = {}
    for number, start in enumerate(range(0, len(indexes), size)):
        chunk = indexes[start:start + size]
        working = list(reversed(chunk)) if back_and_forth and number % 2 else chunk
        result = function(working, number)
        if set(result) != set(chunk):
            raise ValueError("Transformation must return one position for every selected note")
        output.update(result)
    return output


def _horizontal(indexes, rows, per_row, p):
    mx = _margin(p, "margin_x", 32, PLAYFIELD_WIDTH)
    my = _margin(p, "margin_y", 32, PLAYFIELD_HEIGHT)
    ys = [192.0] if rows == 1 else [my + i * ((384 - 2 * my) / (rows - 1)) for i in range(rows)]
    output = {}
    for row in range(rows):
        group = indexes[row * per_row:(row + 1) * per_row]
        if not group:
            break
        xs = [256.0] if len(group) == 1 else [mx + i * ((512 - 2 * mx) / (len(group) - 1)) for i in range(len(group))]
        reverse = str(p.get("direction", "left_to_right")) == "right_to_left"
        if reverse or (p.get("snake") and row % 2):
            xs.reverse()
        output.update({index: (_round(x), _round(ys[row])) for index, x in zip(group, xs)})
    return output


def _vertical(indexes, columns, per_column, p):
    mx = _margin(p, "margin_x", 32, PLAYFIELD_WIDTH)
    my = _margin(p, "margin_y", 32, PLAYFIELD_HEIGHT)
    xs = [256.0] if columns == 1 else [mx + i * ((PLAYFIELD_WIDTH - 2 * mx) / (columns - 1)) for i in range(columns)]
    reverse = str(p.get("direction", "top_to_bottom")) == "bottom_to_top"
    output = {}
    for column in range(columns):
        group = indexes[column * per_column:(column + 1) * per_column]
        if not group:
            break
        ys = [192.0] if len(group) == 1 else [my + i * ((PLAYFIELD_HEIGHT - 2 * my) / (len(group) - 1)) for i in range(len(group))]
        if reverse:
            ys.reverse()
        output.update({index: (_round(xs[column]), _round(y)) for index, y in zip(group, ys)})
    return output


def _dvd_bouncing(p, seed):
    rng = random.Random(seed)
    steps = max(2, _pos_int(p, "steps", _chunk_size(p)))
    step = _pos_float(p, "step_size", 35)
    mx = _margin(p, "margin_x", 20, PLAYFIELD_WIDTH)
    my = _margin(p, "margin_y", 20, PLAYFIELD_HEIGHT)
    x = rng.uniform(mx, PLAYFIELD_WIDTH - mx)
    y = rng.uniform(my, PLAYFIELD_HEIGHT - my)
    angle = math.radians(rng.uniform(0, 360))
    points = [(x, y)]
    for _ in range(steps - 1):
        remaining = step
        while remaining > 1e-9:
            dx, dy = math.cos(angle), math.sin(angle)
            tx = ((PLAYFIELD_WIDTH - mx - x) / dx if dx > 0 else (mx - x) / dx if dx < 0 else float("inf"))
            ty = ((PLAYFIELD_HEIGHT - my - y) / dy if dy > 0 else (my - y) / dy if dy < 0 else float("inf"))
            travel = min(remaining, tx if tx >= 0 else float("inf"), ty if ty >= 0 else float("inf"))
            x += dx * travel; y += dy * travel; remaining -= travel
            hit_x = abs(travel - tx) < 1e-7
            hit_y = abs(travel - ty) < 1e-7
            if hit_x: angle = math.pi - angle
            if hit_y: angle = -angle
            if not hit_x and not hit_y: break
        points.append((min(max(x, mx), PLAYFIELD_WIDTH - mx), min(max(y, my), PLAYFIELD_HEIGHT - my)))
    return points


def _text(indexes: list[int], p: dict[str, Any]) -> PositionMap:
    text = str(p.get("text", "67"))
    if not text.strip():
        raise ValueError("Text cannot be empty")

    margin_x = _margin(p, "margin_x", 20, PLAYFIELD_WIDTH)
    margin_y = _margin(p, "margin_y", 20, PLAYFIELD_HEIGHT)
    text_size = min(100.0, max(20.0, float(p.get("text_size", 90.0)))) / 100.0
    auto_arrange = bool(p.get("auto_arrange", True))

    # This is intentionally the operating-system general font. Exo 2 is not
    # involved in generated text geometry.
    requested_family=str(p.get("font_family","")).strip()
    font=QFont(requested_family) if requested_family else QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPointSizeF(160.0)
    metrics = QFontMetricsF(font)

    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(
        QTextOption.WrapAtWordBoundaryOrAnywhere
        if auto_arrange
        else QTextOption.NoWrap
    )
    layout.setTextOption(option)

    available_ratio = (
        (PLAYFIELD_WIDTH - 2 * margin_x)
        / max(1.0, PLAYFIELD_HEIGHT - 2 * margin_y)
    )
    natural_width = max(1.0, metrics.horizontalAdvance(text))
    line_height = max(1.0, metrics.height())

    if auto_arrange:
        # Estimate a balanced wrapped block whose aspect ratio approaches the
        # usable osu! playfield. QTextLayout performs the actual Unicode-safe
        # wrapping, so Japanese and Thai clusters are not split manually.
        estimated_lines = max(
            1,
            round(math.sqrt(natural_width / (line_height * available_ratio))),
        )
        line_width = max(metrics.averageCharWidth() * 2, natural_width / estimated_lines)
    else:
        line_width = 100000.0

    layout.beginLayout()
    lines = []
    y = 0.0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(line_width)
        line.setPosition(QPointF(0.0, y))
        lines.append(line)
        y += line.height()
    layout.endLayout()

    contours: list[list[Point]] = []
    missing_glyphs = False
    for line in lines:
        line_position = line.position()
        for glyph_run in line.glyphRuns():
            raw_font = glyph_run.rawFont()
            for glyph_index, position in zip(
                glyph_run.glyphIndexes(),
                glyph_run.positions(),
            ):
                if glyph_index == 0:
                    missing_glyphs = True
                    continue
                glyph_path = raw_font.pathForGlyph(glyph_index)
                if glyph_path.isEmpty():
                    continue
                transform = QTransform.fromTranslate(
                    position.x() + line_position.x(),
                    position.y() + line_position.y(),
                )
                for polygon in glyph_path.toSubpathPolygons(transform):
                    points = [(point.x(), point.y()) for point in polygon]
                    if len(points) >= 2:
                        contours.append(points)

    if missing_glyphs:
        raise ValueError(
            "One or more characters are unavailable in installed system fonts"
        )
    if not contours:
        raise ValueError("The system font produced no usable text outlines")

    contours.sort(
        key=lambda points: (
            min(y for _, y in points),
            min(x for x, _ in points),
            -_polyline_length(points),
        )
    )

    min_x = min(x for contour in contours for x, _ in contour)
    max_x = max(x for contour in contours for x, _ in contour)
    min_y = min(y for contour in contours for _, y in contour)
    max_y = max(y for contour in contours for _, y in contour)
    source_width = max(max_x - min_x, 1.0)
    source_height = max(max_y - min_y, 1.0)
    usable_width = PLAYFIELD_WIDTH - 2 * margin_x
    usable_height = PLAYFIELD_HEIGHT - 2 * margin_y

    fit_scale = min(usable_width / source_width, usable_height / source_height)
    scale = fit_scale * text_size
    rendered_width = source_width * scale
    rendered_height = source_height * scale
    offset_x = margin_x + (usable_width - rendered_width) / 2
    offset_y = margin_y + (usable_height - rendered_height) / 2

    fitted = [
        [
            (
                offset_x + (x - min_x) * scale,
                offset_y + (y - min_y) * scale,
            )
            for x, y in contour
        ]
        for contour in contours
    ]

    lengths = [_polyline_length(contour) for contour in fitted]
    total_length = sum(lengths)
    if total_length <= 0:
        raise ValueError("Text outlines have no measurable length")

    count = len(indexes)
    allocations = [0] * len(fitted)
    ranked = sorted(range(len(fitted)), key=lambda i: lengths[i], reverse=True)
    for contour_index in ranked[:min(count, len(fitted))]:
        allocations[contour_index] = 1

    remaining = count - sum(allocations)
    if remaining:
        raw = [remaining * length / total_length for length in lengths]
        for index, value in enumerate(raw):
            allocations[index] += int(value)
        leftovers = count - sum(allocations)
        fractions = sorted(
            range(len(raw)),
            key=lambda i: raw[i] - int(raw[i]),
            reverse=True,
        )
        for index in fractions[:leftovers]:
            allocations[index] += 1

    sampled: list[Point] = []
    for contour, allocation in zip(fitted, allocations):
        if allocation:
            sampled.extend(_sample_polyline(contour, allocation))
    sampled = sampled[:count]
    sampled = _visual_reading_order(sampled, bool(p.get("reverse", False)))
    return {
        index: (_round(x), _round(y))
        for index, (x, y) in zip(indexes, sampled)
    }

def _polyline_length(points: Sequence[Point]) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def _sample_polyline(points: Sequence[Point], count: int) -> list[Point]:
    if count <= 0:
        return []
    if count == 1:
        return [points[0]]
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    total = sum(lengths)
    if total <= 0:
        return [points[0]] * count
    targets = [total * i / (count - 1) for i in range(count)]
    output: list[Point] = []
    segment = 0
    passed = 0.0
    for target in targets:
        while segment < len(lengths) - 1 and passed + lengths[segment] < target:
            passed += lengths[segment]
            segment += 1
        length = lengths[segment]
        ratio = 0.0 if length == 0 else (target - passed) / length
        a, b = points[segment], points[segment + 1]
        output.append((a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio))
    return output


_MATH_FUNCTIONS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sqrt": math.sqrt, "abs": abs, "exp": math.exp,
    "ln": math.log, "log": math.log10, "log10": math.log10,
    "floor": math.floor, "ceil": math.ceil, "min": min, "max": max,
}
_MATH_CONSTANTS = {"pi": math.pi, "e": math.e}
_ALLOWED_BINARY = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b,
}
_ALLOWED_UNARY = {ast.UAdd: lambda a: a, ast.USub: lambda a: -a}
_ALLOWED_COMPARE = {
    ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
}


def _normalize_math_expression(expression: str) -> str:
    value = expression.strip().replace("^", "**").replace("π", "pi")
    value = value.replace("≤", "<=").replace("≥", ">=")
    value = re.sub(r"\|([^|]+)\|", r"abs(\1)", value)
    return value


MAX_EXPRESSION_LENGTH = 512
MAX_EXPRESSION_NODES = 128
MAX_EXPRESSION_DEPTH = 24
MAX_ABS_EXPONENT = 32.0
MAX_ABS_RESULT = 1e12


def _validate_expression_tree(tree: ast.AST, variables: set[str]) -> None:
    nodes=list(ast.walk(tree))
    if len(nodes)>MAX_EXPRESSION_NODES:
        raise ValueError("Equation is too complex")
    def visit(node: ast.AST, depth: int=0) -> None:
        if depth>MAX_EXPRESSION_DEPTH:
            raise ValueError("Equation nesting is too deep")
        allowed=(ast.Expression,ast.Constant,ast.Name,ast.BinOp,ast.UnaryOp,ast.Call,ast.Compare,ast.BoolOp,
                 ast.Add,ast.Sub,ast.Mult,ast.Div,ast.Mod,ast.Pow,ast.UAdd,ast.USub,
                 ast.Lt,ast.LtE,ast.Gt,ast.GtE,ast.Eq,ast.And,ast.Or,ast.Load)
        if not isinstance(node,allowed):
            raise ValueError("Unsupported equation syntax")
        if isinstance(node,ast.Name) and node.id not in variables and node.id not in _MATH_CONSTANTS and node.id not in _MATH_FUNCTIONS:
            raise ValueError(f"Unknown name: {node.id}")
        if isinstance(node,ast.Call):
            if not isinstance(node.func,ast.Name) or node.func.id not in _MATH_FUNCTIONS or node.keywords:
                raise ValueError("Unsupported function call")
        for child in ast.iter_child_nodes(node):visit(child,depth+1)
    visit(tree)


def _bounded_number(value: Any) -> float:
    result=float(value)
    if not math.isfinite(result) or abs(result)>MAX_ABS_RESULT:
        raise ValueError("Equation result is outside the supported range")
    return result


def _compile_safe_expression(expression: str, variables: set[str]):
    normalized=_normalize_math_expression(expression)
    if len(normalized)>MAX_EXPRESSION_LENGTH:
        raise ValueError("Equation is too long")
    tree = ast.parse(normalized, mode="eval")
    _validate_expression_tree(tree,variables)
    def evaluate(node, env):
        if isinstance(node, ast.Expression): return evaluate(node.body, env)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)): return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in env: return env[node.id]
            if node.id in _MATH_CONSTANTS: return _MATH_CONSTANTS[node.id]
            raise ValueError(f"Unknown name: {node.id}")
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINARY:
            left=evaluate(node.left,env);right=evaluate(node.right,env)
            if isinstance(node.op,ast.Pow) and abs(float(right))>MAX_ABS_EXPONENT:
                raise ValueError("Exponent is outside the supported range")
            return _bounded_number(_ALLOWED_BINARY[type(node.op)](left,right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            return _ALLOWED_UNARY[type(node.op)](evaluate(node.operand, env))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _MATH_FUNCTIONS:
            return _bounded_number(_MATH_FUNCTIONS[node.func.id](*[evaluate(arg, env) for arg in node.args]))
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _ALLOWED_COMPARE:
            return _ALLOWED_COMPARE[type(node.ops[0])](evaluate(node.left, env), evaluate(node.comparators[0], env))
        if isinstance(node, ast.BoolOp):
            values = [bool(evaluate(v, env)) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        raise ValueError("Unsupported equation syntax")
    return lambda **env: evaluate(tree, env)


def _split_restrictions(expression: str):
    restrictions = re.findall(r"\{([^{}]+)\}", expression)
    core = re.sub(r"\{[^{}]+\}", "", expression).strip()
    return core, restrictions


def _equation(indexes: list[int], p: dict[str, Any]) -> PositionMap:
    mode = str(p.get("equation_mode", "implicit"))
    x_min, x_max = float(p.get("x_min", -5)), float(p.get("x_max", 5))
    y_min, y_max = float(p.get("y_min", -3.75)), float(p.get("y_max", 3.75))
    if x_max <= x_min or y_max <= y_min:
        raise ValueError("Maximum graph bounds must exceed minimum bounds")
    mx = _margin(p, "margin_x", 20, PLAYFIELD_WIDTH)
    my = _margin(p, "margin_y", 20, PLAYFIELD_HEIGHT)

    if mode == "parametric":
        fx = _compile_safe_expression(str(p.get("x_expression", "cos(3*t)")), {"t"})
        fy = _compile_safe_expression(str(p.get("y_expression", "sin(2*t)")), {"t"})
        t_min, t_max = float(p.get("t_min", 0)), float(p.get("t_max", 2 * math.pi))
        samples = max(64, int(p.get("resolution", 160)) * 2)
        math_paths, current = [], []
        for i in range(samples):
            tv = t_min + (t_max - t_min) * i / (samples - 1)
            try:
                x, y = float(fx(t=tv)), float(fy(t=tv))
                if not math.isfinite(x) or not math.isfinite(y): raise ValueError
                current.append((x, y))
            except (ValueError, ZeroDivisionError, OverflowError):
                if len(current) >= 2: math_paths.append(current)
                current = []
        if len(current) >= 2: math_paths.append(current)
    elif mode == "explicit":
        core, restrictions = _split_restrictions(str(p.get("equation", "y=sin(x)")))
        rhs = core.split("=", 1)[1] if "=" in core else core
        fy = _compile_safe_expression(rhs, {"x", "y"})
        rules = [_compile_safe_expression(r, {"x", "y"}) for r in restrictions]
        samples = max(64, int(p.get("resolution", 160)) * 2)
        math_paths, current = [], []
        for i in range(samples):
            x = x_min + (x_max - x_min) * i / (samples - 1)
            try:
                y = float(fy(x=x, y=0))
                if not math.isfinite(y) or not all(rule(x=x, y=y) for rule in rules): raise ValueError
                current.append((x, y))
            except (ValueError, ZeroDivisionError, OverflowError):
                if len(current) >= 2: math_paths.append(current)
                current = []
        if len(current) >= 2: math_paths.append(current)
    else:
        core, restrictions = _split_restrictions(str(p.get("equation", "x^2+y^2=9")))
        if "=" not in core: raise ValueError("Implicit equation requires =")
        left, right = core.split("=", 1)
        fl = _compile_safe_expression(left, {"x", "y"})
        fr = _compile_safe_expression(right, {"x", "y"})
        rules = [_compile_safe_expression(r, {"x", "y"}) for r in restrictions]
        n = max(32, min(384, int(p.get("resolution", 160))))
        values = []
        for j in range(n + 1):
            y = y_min + (y_max - y_min) * j / n
            row = []
            for i in range(n + 1):
                x = x_min + (x_max - x_min) * i / n
                try:
                    if not all(rule(x=x, y=y) for rule in rules): raise ValueError
                    value = float(fl(x=x, y=y) - fr(x=x, y=y))
                    if not math.isfinite(value) or abs(value) > 1e6: raise ValueError
                    row.append(value)
                except (ValueError, ZeroDivisionError, OverflowError): row.append(None)
            values.append(row)
        math_paths = _marching_square_paths(values, x_min, x_max, y_min, y_max)

    if not math_paths:
        raise ValueError("No graph outline exists in the selected viewport")
    graph_size = min(3.0, max(0.1, float(p.get("graph_size", 100.0)) / 100.0))
    graph_center_x = (x_min + x_max) / 2.0
    graph_center_y = (y_min + y_max) / 2.0
    math_paths = [
        [
            (
                graph_center_x + (x - graph_center_x) * graph_size,
                graph_center_y + (y - graph_center_y) * graph_size,
            )
            for x, y in path
        ]
        for path in math_paths
    ]

    mapped = []
    for path in math_paths:
        mapped.append([
            (mx + (x - x_min) / (x_max - x_min) * (PLAYFIELD_WIDTH - 2 * mx),
             my + (1 - (y - y_min) / (y_max - y_min)) * (PLAYFIELD_HEIGHT - 2 * my))
            for x, y in path if x_min <= x <= x_max and y_min <= y <= y_max
        ])
    mapped = [p for p in mapped if len(p) >= 2]
    mapped.sort(key=_polyline_length, reverse=True)
    total = sum(_polyline_length(path) for path in mapped)
    allocations = [max(1, round(len(indexes) * _polyline_length(path) / total)) for path in mapped]
    while sum(allocations) > len(indexes):
        i = max(range(len(allocations)), key=lambda k: allocations[k])
        if allocations[i] > 1: allocations[i] -= 1
        else: break
    while sum(allocations) < len(indexes):
        allocations[sum(allocations) % len(allocations)] += 1
    points = []
    for path, count in zip(mapped, allocations): points.extend(_sample_polyline(path, count))
    if bool(p.get("back_and_forth", False)) and len(points) > 1:
        size = _chunk_size(p); reordered = []
        for number, start in enumerate(range(0, len(points), size)):
            part = points[start:start+size]
            reordered.extend(reversed(part) if number % 2 else part)
        points = reordered
    return {index: (_round(x), _round(y)) for index, (x, y) in zip(indexes, points)}


def _marching_square_paths(values, x_min, x_max, y_min, y_max):
    rows, cols = len(values) - 1, len(values[0]) - 1
    segments = []
    edge_pairs = {1:[(3,2)],2:[(2,1)],3:[(3,1)],4:[(0,1)],5:[(0,3),(2,1)],6:[(0,2)],7:[(0,3)],8:[(0,3)],9:[(0,2)],10:[(0,1),(3,2)],11:[(0,1)],12:[(3,1)],13:[(2,1)],14:[(3,2)]}
    def interp(a, b, va, vb):
        ratio = .5 if va == vb else va / (va - vb)
        return (a[0] + (b[0]-a[0])*ratio, a[1] + (b[1]-a[1])*ratio)
    for j in range(rows):
        for i in range(cols):
            v=[values[j+1][i],values[j+1][i+1],values[j][i+1],values[j][i]]
            if any(x is None for x in v): continue
            case=sum((1<<k) for k,x in enumerate(v) if x>=0)
            if case in (0,15): continue
            x0=x_min+(x_max-x_min)*i/cols; x1=x_min+(x_max-x_min)*(i+1)/cols
            y0=y_min+(y_max-y_min)*j/rows; y1=y_min+(y_max-y_min)*(j+1)/rows
            c=[(x0,y1),(x1,y1),(x1,y0),(x0,y0)]
            edges=[interp(c[0],c[1],v[0],v[1]),interp(c[1],c[2],v[1],v[2]),interp(c[2],c[3],v[2],v[3]),interp(c[3],c[0],v[3],v[0])]
            for a,b in edge_pairs.get(case,[]): segments.append((edges[a],edges[b]))
    paths=[]
    tolerance=(x_max-x_min)/max(cols,1)*1.5
    while segments:
        a,b=segments.pop(); path=[a,b]; changed=True
        while changed:
            changed=False
            for k,(c,d) in enumerate(segments):
                if math.dist(path[-1],c)<=tolerance: path.append(d)
                elif math.dist(path[-1],d)<=tolerance: path.append(c)
                elif math.dist(path[0],d)<=tolerance: path.insert(0,c)
                elif math.dist(path[0],c)<=tolerance: path.insert(0,d)
                else: continue
                segments.pop(k); changed=True; break
        if len(path)>=2: paths.append(path)
    return paths


def _pinwheel(indexes: list[int], p: dict[str, Any], chunk_number: int = 0) -> PositionMap:
    center_x = float(p.get("center_x", 256.0))
    center_y = float(p.get("center_y", 192.0))
    num_blades = max(1, int(p.get("num_blades", 6)))
    blade_curl = float(p.get("blade_curl", 0.8))
    rotation = math.radians(float(p.get("rotation_offset_deg", 0.0)))
    spread = math.radians(float(p.get("blade_spread_deg", 360.0 / num_blades)))
    inner_radius = max(0.0, float(p.get("inner_radius", 18.0)))
    outer_radius = max(inner_radius, float(p.get("outer_radius", 170.0)))
    wander_strength = max(0.0, float(p.get("wander_strength", 8.0)))
    seed = int(p.get("wander_seed", 12345)) + chunk_number * 1000003
    curve = str(p.get("radius_growth_curve", "ease_out"))

    inner_circle_enabled = bool(p.get("inner_circle_enabled", True))
    requested_circle_notes = max(1, int(p.get("inner_circle_notes", 24)))
    inner_circle_radius = max(1.0, float(p.get("inner_circle_radius", inner_radius)))
    circle_count = min(len(indexes), requested_circle_notes) if inner_circle_enabled else 0
    circle_indexes = indexes[:circle_count]
    blade_indexes = indexes[circle_count:]

    output: PositionMap = {}

    # Closed inner ring. The final point approaches the first point without
    # duplicating it, preserving one unique coordinate assignment per note.
    for order, index in enumerate(circle_indexes):
        angle = rotation + math.tau * order / max(1, circle_count)
        x = center_x + inner_circle_radius * math.cos(angle)
        y = center_y + inner_circle_radius * math.sin(angle)
        output[index] = (
            _round(min(PLAYFIELD_WIDTH, max(0.0, x))),
            _round(min(PLAYFIELD_HEIGHT, max(0.0, y))),
        )

    # Remaining notes form the organic curved blades from the outside edge of
    # the center ring. If the ring is larger than Inner Radius, blades begin at
    # the ring radius so they do not visibly start inside it.
    blade_start_radius = max(inner_radius, inner_circle_radius if circle_count else inner_radius)
    blade_notes = [[] for _ in range(num_blades)]
    for order, index in enumerate(blade_indexes):
        blade_notes[order % num_blades].append(index)

    for blade_index, blade in enumerate(blade_notes):
        blade_length = len(blade)
        if not blade_length:
            continue
        previous_wander = 0.0
        for position_in_blade, index in enumerate(blade):
            progress = position_in_blade / max(1, blade_length - 1)
            if curve == "ease_out":
                growth = 1.0 - (1.0 - progress) ** 2
            elif curve == "ease_in":
                growth = progress ** 2
            else:
                growth = progress
            radius = blade_start_radius + (outer_radius - blade_start_radius) * growth
            theta = blade_index * spread + rotation + blade_curl * progress * math.tau
            rng = random.Random(seed + index * 7919 + blade_index * 104729)
            target_wander = rng.uniform(-wander_strength, wander_strength)
            previous_wander = previous_wander * 0.55 + target_wander * 0.45
            x = center_x + radius * math.cos(theta) - previous_wander * math.sin(theta)
            y = center_y + radius * math.sin(theta) + previous_wander * math.cos(theta)
            output[index] = (
                _round(min(PLAYFIELD_WIDTH, max(0.0, x))),
                _round(min(PLAYFIELD_HEIGHT, max(0.0, y))),
            )

    return output

def _on_path(indexes, path, closed):
    sampled = _sample(path, len(indexes), closed)
    return {i: (_round(x), _round(y)) for i, (x, y) in zip(indexes, sampled)}


def _sample(path, count, closed):
    points = list(path)
    if not points:
        raise ValueError("Path cannot be empty")
    if count == 1:
        return [points[0]]
    if len(points) == 1:
        return [points[0]] * count
    work = points + [points[0]] if closed and points[-1] != points[0] else points
    lengths = [math.dist(a, b) for a, b in zip(work, work[1:])]
    total = sum(lengths)
    if total == 0:
        return [work[0]] * count
    targets = [total * i / (count if closed else count - 1) for i in range(count)]
    output, segment, passed = [], 0, 0.0
    for target in targets:
        while segment < len(lengths) - 1 and passed + lengths[segment] < target:
            passed += lengths[segment]
            segment += 1
        length = lengths[segment]
        ratio = 0.0 if length == 0 else (target - passed) / length
        a, b = work[segment], work[segment + 1]
        output.append((a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio))
    return output


def _circle(p):
    r = _pos_float(p, "radius", 150)
    return _parametric(p, lambda t: (r * math.cos(t), r * math.sin(t))), True


def _ellipse(p):
    rx = _pos_float(p, "radius_x", float(p.get("width", 360)) / 2)
    ry = _pos_float(p, "radius_y", float(p.get("height", 240)) / 2)
    return _parametric(p, lambda t: (rx * math.cos(t), ry * math.sin(t))), True


def _square(p):
    h = _pos_float(p, "side_length", 280) / 2
    return _place([(-h, -h), (h, -h), (h, h), (-h, h)], p), True


def _triangle(p):
    r = _pos_float(p, "radius", 160)
    return _place(_box_centred([(r * math.cos(-math.pi / 2 + 2 * math.pi * i / 3), r * math.sin(-math.pi / 2 + 2 * math.pi * i / 3)) for i in range(3)]), p), True


def _diamond(p):
    w, h = _pos_float(p, "width", 320) / 2, _pos_float(p, "height", 260) / 2
    return _place([(0, -h), (w, 0), (0, h), (-w, 0)], p), True


def _infinity(p):
    w, h = _pos_float(p, "width", 380), _pos_float(p, "height", 220)
    return _parametric(p, lambda t: (w / 2 * math.sin(t), h / 2 * math.sin(t) * math.cos(t))), True


def _star(p):
    count, outer, inner = _pos_int(p, "points", 5), _pos_float(p, "outer_radius", 170), _pos_float(p, "inner_radius", 75)
    vertices = []
    for i in range(count * 2):
        angle, radius = -math.pi / 2 + i * math.pi / count, outer if i % 2 == 0 else inner
        vertices.append((radius * math.cos(angle), radius * math.sin(angle)))
    return _place(_box_centred(vertices), p), True


def _spiral(p):
    turns, start, end = _pos_float(p, "turns", 2.5), max(0.0, float(p.get("start_radius", 0))), _pos_float(p, "end_radius", 170)
    n = _samples(p)
    points = []
    for i in range(n):
        u = i / (n - 1); r = start + (end - start) * u; a = 2 * math.pi * turns * u
        points.append((r * math.cos(a), r * math.sin(a)))
    return _place(points, p), False


def _arc(p):
    rx = _pos_float(p, "radius_x", float(p.get("radius", 170)))
    ry = _pos_float(p, "radius_y", float(p.get("radius", 170)))
    start = math.radians(float(p.get("start_angle_deg", -180)))
    end = start + math.radians(float(p.get("sweep_angle_deg", 180)))
    return _parametric(p, lambda t: (rx * math.cos(t), ry * math.sin(t)), start, end), False


def _line(p):
    if "start" in p and "end" in p:
        return [_point(p["start"]), _point(p["end"])], False
    length = _pos_float(p, "length", 400)
    return _place([(-length / 2, 0), (length / 2, 0)], p), False


def _polyline(p):
    points = _points(p.get("points"))
    if len(points) < 2:
        raise ValueError("polyline requires at least two points")
    return points, bool(p.get("closed", False))


def _wave(p):
    width, amp, cycles, n = _pos_float(p, "width", 440), _pos_float(p, "amplitude", 100), _pos_float(p, "cycles", 2), _samples(p)
    phase = math.radians(float(p.get("phase_deg", 0)))
    return _place([(-width / 2 + width * i / (n - 1), amp * math.sin(2 * math.pi * cycles * i / (n - 1) + phase)) for i in range(n)], p), False


def _zigzag(p):
    width, height, segments = _pos_float(p, "width", 440), _pos_float(p, "height", 240), _pos_int(p, "segments", 8)
    return _place([(-width / 2 + width * i / segments, -height / 2 if i % 2 == 0 else height / 2) for i in range(segments + 1)], p), False


def _bezier(p):
    controls = _points(p.get("control_points", [(40, 300), (160, 40), (352, 344), (472, 84)]))
    if len(controls) not in (3, 4):
        raise ValueError("bezier requires 3 or 4 control points")
    return [_bezier_point(controls, i / (_samples(p) - 1)) for i in range(_samples(p))], False


def _drawn(p):
    points = _points(p.get("points"))
    if len(points) < 2:
        raise ValueError("drawn_path requires at least two points")
    return points, bool(p.get("closed", False))


def _random(indexes, p, seed):
    rng = random.Random(seed); mx = _margin(p, "margin_x", 20, 512); my = _margin(p, "margin_y", 20, 384)
    return {i: (rng.randint(mx, 512 - mx), rng.randint(my, 384 - my)) for i in indexes}


def _random_walk(p, seed):
    rng = random.Random(seed); steps = max(2, _pos_int(p, "steps", _chunk_size(p))); size = _pos_float(p, "step_size", 35)
    turn = math.radians(_pos_float(p, "max_turn_deg", 55)); mx = _margin(p, "margin_x", 20, 512); my = _margin(p, "margin_y", 20, 384)
    x, y = _center(p); angle = math.radians(float(p.get("start_angle_deg", rng.uniform(0, 360)))); points = [(x, y)]
    for _ in range(steps - 1):
        angle += rng.uniform(-turn, turn); x += math.cos(angle) * size; y += math.sin(angle) * size
        if x < mx or x > 512 - mx: angle = math.pi - angle; x = min(max(x, mx), 512 - mx)
        if y < my or y > 384 - my: angle = -angle; y = min(max(y, my), 384 - my)
        points.append((x, y))
    return points


def _parametric(p, function, start=0.0, end=2 * math.pi):
    n = _samples(p)
    return _place([function(start + (end - start) * i / (n - 1)) for i in range(n)], p)


def _box_centred(points):
    """`points` shifted so their bounding box is centred on the origin.

    A triangle or star drawn on its circumcircle with a point straight up is
    centred on that circle, not on itself: the triangle's apex reached 160 above
    the centre and its base only 80 below, so a default triangle sat 40
    osu!pixels high of the Center it was given. The centre a mapper places is
    the middle of the shape they see, so that is what it now means.
    """
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mid_x, mid_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return [(x - mid_x, y - mid_y) for x, y in points]


def _place(points, p):
    cx, cy = _center(p); angle = math.radians(float(p.get("rotation_deg", 0))); c, s = math.cos(angle), math.sin(angle)
    return [(cx + x * c - y * s, cy + x * s + y * c) for x, y in points]


def _bezier_point(points, t):
    work = list(points)
    while len(work) > 1:
        work = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1]) for a, b in zip(work, work[1:])]
    return work[0]


def _indexes(values):
    output = [int(v) for v in values]
    if len(output) != len(set(output)): raise ValueError("selected_note_indexes must not contain duplicates")
    if any(v < 0 for v in output): raise ValueError("selected_note_indexes must be non-negative")
    return sorted(output)


def _name(value):
    result = value.strip().lower().replace(" ", "_").replace("-", "_")
    return {"line": "straight_line", "bezier_curve": "bezier", "scatter": "random"}.get(result, result)


def _chunk_size(p): return _pos_int(p, "chunk_size", 36)
def _samples(p): return max(16, _pos_int(p, "path_samples", 256))
def _center(p): return float(p.get("center_x", 256)), float(p.get("center_y", 192))
def _pos_int(p, key, default):
    value = int(p.get(key, default))
    if value < 1: raise ValueError(f"{key} must be at least 1")
    return value

def _pos_float(p, key, default):
    value = float(p.get(key, default))
    if value <= 0: raise ValueError(f"{key} must be greater than 0")
    return value

def _margin(p, key, default, extent):
    value = int(p.get(key, default))
    if not 0 <= value < extent / 2: raise ValueError(f"{key} must satisfy 0 <= {key} < {extent // 2}")
    return value

def _point(value):
    if not isinstance(value, Sequence) or len(value) != 2: raise ValueError("A point needs x and y")
    return float(value[0]), float(value[1])
def _points(values): return [] if values is None else [_point(v) for v in values]
def _round(value): return int(math.floor(value + 0.5))


def _visual_reading_order(points, reverse_horizontal=False, row_band=8.0):
    rows={}
    for x,y in points: rows.setdefault(round(float(y)/max(1.0,row_band)),[]).append((float(x),float(y)))
    ordered=[]
    for row in sorted(rows): ordered.extend(sorted(rows[row],key=lambda point:point[0],reverse=reverse_horizontal))
    return ordered