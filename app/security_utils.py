import sys
from pathlib import Path
MAX_OSU_BYTES=32*1024*1024
MAX_IMAGE_BYTES=64*1024*1024
MAX_AUDIO_BYTES=1024*1024*1024
MAX_IMAGE_PIXELS=80_000_000
MAX_EQUATION_CHARS=512
MAX_AST_NODES=256
MAX_AST_DEPTH=32
MAX_UNDO_STATES=50
IMAGE_SUFFIXES={".jpg",".jpeg",".png",".webp",".bmp"}
AUDIO_SUFFIXES={".mp3",".ogg",".wav",".flac",".m4a",".aac"}
class UnsafeInputError(ValueError): pass
def read_limited(path,limit):
 p=Path(path)
 if p.stat().st_size>limit: raise UnsafeInputError("File is too large")
 with p.open("rb") as f: data=f.read(limit+1)
 if len(data)>limit: raise UnsafeInputError("File is too large")
 return data
def resolve_child_asset(folder,name,*,suffixes,max_bytes):
 base=Path(folder).resolve(strict=True); raw=Path(str(name).strip().strip(chr(34)))
 if raw.is_absolute(): raise UnsafeInputError("Absolute asset paths are not permitted")
 p=(base/raw).resolve(strict=True)
 try:p.relative_to(base)
 except ValueError as e: raise UnsafeInputError("Asset path escapes the beatmap folder") from e
 if p.suffix.lower() not in suffixes or not p.is_file() or p.stat().st_size>max_bytes: raise UnsafeInputError("Unsupported or oversized asset")
 return p


def resource_roots() -> list[Path]:
    """Where bundled assets may be read from: the PyInstaller bundle, then the
    source tree. Never the working directory, so a file planted where the app
    is started from cannot stand in for one of ours. The one copy -- the
    package move had to fix three of them."""
    roots = [Path(sys._MEIPASS).resolve()] if hasattr(sys, "_MEIPASS") else []
    source = Path(__file__).resolve().parents[1]
    if source not in roots:
        roots.append(source)
    return roots
