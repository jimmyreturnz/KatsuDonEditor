from dataclasses import dataclass, field
from pathlib import Path

from model.hit_object import HitObject
from osu_io.timing import TimingPoint, parse_timing_line


@dataclass(slots=True)
class OsuDocument:
    source_path: Path
    lines: list[str]
    encoding: str
    version: str
    audio_filename: str
    hit_objects: list[HitObject]
    # Added for the editor work. All default, so the positional construction
    # below and any existing caller keep working unchanged.
    timing_points: list[TimingPoint] = field(default_factory=list)
    # {section name: (header line index, one past the last body line)}. Lets the
    # writer splice a section without offset arithmetic over the whole file.
    section_spans: dict[str, tuple[int, int]] = field(default_factory=dict)
    line_ending: str = "\r\n"
    format_version: int = 14
    # [Difficulty] SliderMultiplier. Defaulted to osu!'s own default so the
    # positional construction below and every existing caller keep working,
    # but read off the file when it is there: it scales every slider's
    # length-to-duration conversion, so guessing it draws drumroll ends at
    # the wrong time on every map that does not happen to use 1.4.
    slider_multiplier: float = 1.4


def _decode_file(raw):
    if raw.startswith(b"\xef\xbb\xbf"): return raw.decode("utf-8-sig"),"utf-8-sig"
    try:return raw.decode("utf-8"),"utf-8"
    except UnicodeDecodeError:return raw.decode("cp1252"),"cp1252"


def _dominant_ending(lines):
    crlf = sum(1 for line in lines if line.endswith("\r\n"))
    lf = sum(1 for line in lines if line.endswith("\n") and not line.endswith("\r\n"))
    return "\n" if lf > crlf else "\r\n"


def _format_version(lines):
    for line in lines[:1]:
        text = line.strip().lower()
        marker = "osu file format v"
        if marker in text:
            try:
                return int(text.split(marker, 1)[1])
            except ValueError:
                break
    return 14


def read_version(path):
    """The difficulty name, read the way parse_osu reads it, without parsing
    the notes and timing behind it.

    The difficulty dropdown wants one line per file and was parsing every
    difficulty in the folder for it: 17 files, 0.88s of a 1.1s map open
    (measured 2026-10-11). The whole file is still decoded, because whether
    it is UTF-8 or cp1252 is decided over all of it, and a label must not
    come out differently from the one parse_osu would give.
    """
    text,_=_decode_file(Path(path).read_bytes()); section=""; version=""
    for line in text.splitlines():
        stripped=line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section=="Metadata": break
            section=stripped[1:-1]; continue
        if section=="Metadata" and line.startswith("Version:"): version=line.split(":",1)[1].strip()
    return version


def parse_osu(path):
    source_path=Path(path).resolve(); text,encoding=_decode_file(source_path.read_bytes()); lines=text.splitlines(keepends=True)
    section=version=audio_filename=""; hit_objects=[]; timing_points=[]; slider_multiplier=1.4
    section_spans={}; open_section=None; open_index=0
    for line_index,line in enumerate(lines):
        content=line.rstrip("\r\n"); stripped=content.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if open_section is not None:
                section_spans[open_section]=(open_index,line_index)
            section=stripped[1:-1]; open_section=section; open_index=line_index
            continue
        if not stripped or stripped.startswith("//"): continue
        if section=="General" and content.startswith("AudioFilename:"): audio_filename=content.split(":",1)[1].strip(); continue
        if section=="Metadata" and content.startswith("Version:"): version=content.split(":",1)[1].strip(); continue
        if section=="Difficulty" and content.startswith("SliderMultiplier"):
            try: slider_multiplier=float(content.split(":",1)[1].strip())
            except (IndexError,ValueError): pass
            continue
        if section=="TimingPoints":
            point=parse_timing_line(line,line_index)
            if point is not None: timing_points.append(point)
            continue
        if section!="HitObjects": continue
        note=HitObject.from_line(line,len(hit_objects),line_index)
        if note is not None: hit_objects.append(note)
    if open_section is not None:
        section_spans[open_section]=(open_index,len(lines))
    # Sorted once here so every later lookup can binary-search instead of
    # scanning or re-sorting. Stable, so points sharing a timestamp keep the
    # file order osu! resolves them by.
    timing_points.sort(key=lambda point: point.time)
    return OsuDocument(
        source_path,lines,encoding,version,audio_filename,hit_objects,
        timing_points,section_spans,_dominant_ending(lines),_format_version(lines),
        slider_multiplier,
    )
