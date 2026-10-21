"""The difficulty osu!stable has selected right now, read from its memory.

osu! publishes no API for it, so this does what gosumemory, tosu and
OsuMemoryDataProvider do: find the `Base` signature in the game's JIT code and
follow two pointers to its current beatmap object, whose folder and file name
are .NET strings. Windows and osu!stable only; lazer has no Songs folder to
point into. The offsets are those tools', and an osu! update can move them --
`tools/read_osu_current.py` is the check.

Read-only (`PROCESS_VM_READ`): nothing here writes to the game.
"""
from __future__ import annotations

import sys

BASE_SIGNATURE = bytes.fromhex("F8 01 74 04 83 65")
BEATMAP_FROM_BASE = -0xC
FOLDER_OFFSET = 0x78
OSU_FILE_OFFSET = 0x90


class OsuNotFound(RuntimeError):
    pass


def dotnet_string(read, address: int) -> str:
    """A .NET string object: method table, int32 length, UTF-16 chars."""
    if not address:
        return ""
    length = int.from_bytes(read(address + 4, 4), "little")
    if not 0 <= length <= 4096:
        return ""
    return read(address + 8, length * 2).decode("utf-16-le", "replace")


def pointer(read, address: int) -> int:
    # osu!stable is a 32-bit process.
    return int.from_bytes(read(address, 4), "little")


def beatmap_path(read, base: int) -> tuple[str, str]:
    """(folder, .osu file name) of the selected difficulty, from `Base`."""
    beatmap = pointer(read, pointer(read, base + BEATMAP_FROM_BASE))
    if not beatmap:
        return "", ""
    return (dotnet_string(read, pointer(read, beatmap + FOLDER_OFFSET)),
            dotnet_string(read, pointer(read, beatmap + OSU_FILE_OFFSET)))


class OsuProcess:
    """An open handle to osu!.exe with its `Base` found; the scan (about a
    second over the whole address space) is paid once per process."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OsuNotFound("Reading osu! needs Windows.")
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.ReadProcessMemory.argtypes = (
            wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t))
        kernel.VirtualQueryEx.argtypes = (
            wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
        kernel.VirtualQueryEx.restype = ctypes.c_size_t
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self.pid = _osu_pid()
        if self.pid is None:
            raise OsuNotFound("osu! is not running.")
        # PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
        self._handle = kernel.OpenProcess(0x0010 | 0x0400, False, self.pid)
        if not self._handle:
            raise OsuNotFound("osu! is running, but Windows would not let it be read.")
        self.base = self._find(BASE_SIGNATURE)
        if self.base is None:
            self.close()
            raise OsuNotFound("osu! is running, but its current map could not be found "
                              "(an osu! update may have moved it).")

    def read(self, address: int, size: int) -> bytes:
        ctypes = self._ctypes
        buffer = ctypes.create_string_buffer(size)
        done = ctypes.c_size_t()
        if not self._kernel.ReadProcessMemory(self._handle, address, buffer, size, ctypes.byref(done)):
            return b"\0" * size
        return buffer.raw[:done.value].ljust(size, b"\0")

    def alive(self) -> bool:
        from ctypes import wintypes
        code = wintypes.DWORD()
        return bool(self._kernel.GetExitCodeProcess(self._handle, self._ctypes.byref(code))) and code.value == 259

    def _find(self, signature: bytes) -> int | None:
        ctypes = self._ctypes

        class Region(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", ctypes.c_ulong), ("PartitionId", ctypes.c_ushort),
                        ("RegionSize", ctypes.c_size_t), ("State", ctypes.c_ulong),
                        ("Protect", ctypes.c_ulong), ("Type", ctypes.c_ulong)]

        region = Region()
        address = 0
        # JIT code lives in executable pages; scanning only those is the
        # difference between a second and most of a minute.
        executable = {0x10, 0x20, 0x40, 0x80}
        while address < 0x7FFF0000 and self._kernel.VirtualQueryEx(
                self._handle, address, ctypes.byref(region), ctypes.sizeof(region)):
            start, size = region.BaseAddress or 0, region.RegionSize
            if region.State == 0x1000 and region.Protect & 0xFF in executable:
                found = self.read(start, size).find(signature)
                if found >= 0:
                    return start + found
            address = start + size
        return None

    def current(self) -> tuple[str, str]:
        return beatmap_path(self.read, self.base)

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _osu_pid() -> int | None:
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    try:
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() == "osu!.exe":
                return entry.th32ProcessID
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return None


_process: OsuProcess | None = None


def current_difficulty() -> tuple[str, str]:
    """(folder under Songs, .osu file name) osu! has selected. Raises
    `OsuNotFound` with a sentence to show when it cannot say."""
    global _process
    if _process is None or not _process.alive():
        _process = OsuProcess()
    folder, name = _process.current()
    if not folder or not name:
        raise OsuNotFound("osu! has no beatmap selected.")
    return folder, name


if __name__ == "__main__":
    # A fake 32-bit heap: Base, its pointer chain, and two .NET strings.
    memory = bytearray(0x400)

    def put(at: int, value: int) -> None:
        memory[at:at + 4] = value.to_bytes(4, "little")

    def text(at: int, value: str) -> None:
        put(at + 4, len(value))
        memory[at + 8:at + 8 + 2 * len(value)] = value.encode("utf-16-le")

    base = 0x100
    put(base + BEATMAP_FROM_BASE, 0x180)
    put(0x180, 0x200)
    put(0x200 + FOLDER_OFFSET, 0x300)
    put(0x200 + OSU_FILE_OFFSET, 0x380)
    text(0x300, "123 a - b")
    text(0x380, "a - b (c) [Oni].osu")
    assert beatmap_path(lambda at, n: bytes(memory[at:at + n]), base) == ("123 a - b", "a - b (c) [Oni].osu")
    print("ok")
