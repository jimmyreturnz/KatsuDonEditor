from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import hypot
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QImageReader

ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_DECODED_PIXELS = 16_000_000
PROCESS_WIDTH = 1024
PROCESS_HEIGHT = 768
PLAYFIELD_WIDTH = 512.0
PLAYFIELD_HEIGHT = 384.0


@dataclass(frozen=True, slots=True)
class TraceOptions:
    mode: str = "dark"
    threshold: int = 128
    minimum_component: int = 12
    simplify_distance: float = 2.0
    invert: bool = False


def load_image(path: str | Path) -> QImage:
    source = Path(path).resolve()
    if source.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError(f"Unsupported image type: {source.suffix}")
    if not source.is_file():
        raise FileNotFoundError(source)
    reader = QImageReader(str(source))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() * size.height() > MAX_DECODED_PIXELS:
        raise ValueError("Image is too large to trace safely")
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString() or "Image could not be decoded")
    if image.width() * image.height() > MAX_DECODED_PIXELS:
        raise ValueError("Image is too large to trace safely")
    image = image.convertToFormat(QImage.Format_RGBA8888)
    if image.width() > PROCESS_WIDTH or image.height() > PROCESS_HEIGHT:
        image = image.scaled(
            PROCESS_WIDTH,
            PROCESS_HEIGHT,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
    return image


def _mask(image: QImage, options: TraceOptions) -> list[bytearray]:
    width, height = image.width(), image.height()
    output = [bytearray(width) for _ in range(height)]
    threshold = max(0, min(255, int(options.threshold)))
    mode = options.mode if options.mode in {"alpha", "dark", "light"} else "dark"
    for y in range(height):
        row = output[y]
        for x in range(width):
            color = image.pixelColor(x, y)
            if mode == "alpha":
                active = color.alpha() >= threshold
            else:
                luminance = round(
                    0.2126 * color.red()
                    + 0.7152 * color.green()
                    + 0.0722 * color.blue()
                )
                active = luminance <= threshold if mode == "dark" else luminance >= threshold
            row[x] = int(not active if options.invert else active)
    return output


def _boundary_edges(mask: list[bytearray]):
    """Create clockwise pixel-boundary edges in O(pixel count)."""
    height = len(mask)
    width = len(mask[0]) if height else 0
    edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for y in range(height):
        row = mask[y]
        for x in range(width):
            if not row[x]:
                continue
            if y == 0 or not mask[y - 1][x]:
                edges.append(((x, y), (x + 1, y)))
            if x == width - 1 or not row[x + 1]:
                edges.append(((x + 1, y), (x + 1, y + 1)))
            if y == height - 1 or not mask[y + 1][x]:
                edges.append(((x + 1, y + 1), (x, y + 1)))
            if x == 0 or not row[x - 1]:
                edges.append(((x, y + 1), (x, y)))
    return edges


def _turn_priority(previous, current, candidate):
    incoming = (current[0] - previous[0], current[1] - previous[1])
    outgoing = (candidate[0] - current[0], candidate[1] - current[1])
    cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
    dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
    # Clockwise/right turn, straight, left turn, reverse.
    if cross > 0:
        return 0
    if cross == 0 and dot > 0:
        return 1
    if cross < 0:
        return 2
    return 3


def _trace_loops(edges) -> list[list[tuple[float, float]]]:
    """Stitch directed boundary edges into separate loops in O(edge count)."""
    outgoing = defaultdict(list)
    unused = set(edges)
    for start, end in edges:
        outgoing[start].append(end)
    for values in outgoing.values():
        values.sort()

    loops: list[list[tuple[float, float]]] = []
    while unused:
        start_edge = min(unused)
        start, current = start_edge
        unused.remove(start_edge)
        loop = [start, current]
        previous = start
        guard = 0
        while current != start and guard <= len(edges):
            choices = [candidate for candidate in outgoing.get(current, ()) if (current, candidate) in unused]
            if not choices:
                break
            candidate = min(choices, key=lambda point: (_turn_priority(previous, current, point), point))
            unused.remove((current, candidate))
            previous, current = current, candidate
            loop.append(current)
            guard += 1
        if len(loop) >= 4:
            loops.append([(float(x), float(y)) for x, y in loop])
    return loops


def _simplify(points: list[tuple[float, float]], minimum_distance: float):
    if len(points) < 4 or minimum_distance <= 0:
        return points
    result = [points[0]]
    for point in points[1:]:
        if hypot(point[0] - result[-1][0], point[1] - result[-1][1]) >= minimum_distance:
            result.append(point)
    if result[-1] != result[0]:
        result.append(result[0])
    return result


def fit_strokes(strokes, width: float, height: float, margin: float = 12.0):
    points = [point for stroke in strokes for point in stroke]
    if not points:
        return []
    min_x, max_x = min(x for x, _ in points), max(x for x, _ in points)
    min_y, max_y = min(y for _, y in points), max(y for _, y in points)
    usable_width = max(1.0, width - margin * 2)
    usable_height = max(1.0, height - margin * 2)
    scale = min(
        usable_width / max(1.0, max_x - min_x),
        usable_height / max(1.0, max_y - min_y),
    )
    offset_x = (width - (max_x - min_x) * scale) / 2.0
    offset_y = (height - (max_y - min_y) * scale) / 2.0
    return [
        [((x - min_x) * scale + offset_x, (y - min_y) * scale + offset_y) for x, y in stroke]
        for stroke in strokes
    ]


def trace_image(path: str | Path, options: TraceOptions):
    image = load_image(path)
    mask = _mask(image, options)
    loops = _trace_loops(_boundary_edges(mask))
    minimum = max(4, int(options.minimum_component))
    loops = [loop for loop in loops if len(loop) >= minimum]
    strokes = [_simplify(loop, float(options.simplify_distance)) for loop in loops]
    strokes = [stroke for stroke in strokes if len(stroke) >= 3]
    if not strokes:
        raise ValueError("No usable outlines were detected")
    strokes.sort(key=lambda stroke: (min(y for _, y in stroke), min(x for x, _ in stroke)))
    return fit_strokes(strokes, PLAYFIELD_WIDTH, PLAYFIELD_HEIGHT, margin=12.0)