"""Kontrol console proses claude.exe: kirim ketikan/tombol dan baca isi layar (Windows, ctypes).

Harus dijalankan di sesi login yang sama dengan console target (bukan session 0), sebagai user biasa.
Cara kerja: FreeConsole -> AttachConsole(pid) -> CONIN$/CONOUT$ -> WriteConsoleInputW / ReadConsoleOutputCharacterW.
Attach bersifat global per proses, jadi semua operasi diserialkan dengan satu lock.
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_LOCK = threading.Lock()

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3
INVALID_HANDLE = wt.HANDLE(-1).value
KEY_EVENT = 0x0001
ENHANCED_KEY, LEFT_CTRL, SHIFT, LEFT_ALT = 0x0100, 0x0008, 0x0010, 0x0002


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wt.WORD),
                ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wt.BOOL), ("wRepeatCount", wt.WORD), ("wVirtualKeyCode", wt.WORD),
                ("wVirtualScanCode", wt.WORD), ("uChar", wt.WCHAR), ("dwControlKeyState", wt.DWORD)]


class _EV(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", _EV)]


k32.AttachConsole.argtypes = [wt.DWORD]
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k32.CreateFileW.restype = wt.HANDLE
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.WriteConsoleInputW.argtypes = [wt.HANDLE, ctypes.POINTER(INPUT_RECORD), wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.GetConsoleScreenBufferInfo.argtypes = [wt.HANDLE, ctypes.POINTER(CSBI)]
k32.ReadConsoleOutputCharacterW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, COORD, ctypes.POINTER(wt.DWORD)]


class ConsoleError(RuntimeError):
    pass


class _Attached:
    """Context manager: attach ke console proses `pid`, sediakan handle input/output."""

    def __init__(self, pid):
        self.pid = int(pid)

    def __enter__(self):
        _LOCK.acquire()
        k32.FreeConsole()
        if not k32.AttachConsole(self.pid):
            err = ctypes.get_last_error()
            _LOCK.release()
            raise ConsoleError(f"AttachConsole({self.pid}) gagal (error {err}): proses tidak punya console di sesi ini")
        self.hin = k32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        self.hout = k32.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        if self.hin == INVALID_HANDLE or self.hout == INVALID_HANDLE:
            self.__exit__(None, None, None)
            raise ConsoleError("gagal membuka CONIN$/CONOUT$")
        return self

    def __exit__(self, *a):
        for h in (getattr(self, "hin", None), getattr(self, "hout", None)):
            if h and h != INVALID_HANDLE:
                k32.CloseHandle(h)
        k32.FreeConsole()
        _LOCK.release()
        return False


# ---- nama tombol -> (virtual key, karakter, flag) ----
KEYS = {
    "enter": (0x0D, "\r", 0), "esc": (0x1B, "\x1b", 0), "tab": (0x09, "\t", 0), "shift+tab": (0x09, "\0", SHIFT),
    "backspace": (0x08, "\x08", 0), "delete": (0x2E, "\0", ENHANCED_KEY), "space": (0x20, " ", 0),
    "up": (0x26, "\0", ENHANCED_KEY), "down": (0x28, "\0", ENHANCED_KEY),
    "left": (0x25, "\0", ENHANCED_KEY), "right": (0x27, "\0", ENHANCED_KEY),
    "home": (0x24, "\0", ENHANCED_KEY), "end": (0x23, "\0", ENHANCED_KEY),
    "pageup": (0x21, "\0", ENHANCED_KEY), "pagedown": (0x22, "\0", ENHANCED_KEY),
    "ctrl+c": (0x43, "\x03", LEFT_CTRL), "ctrl+o": (0x4F, "\x0f", LEFT_CTRL), "ctrl+l": (0x4C, "\x0c", LEFT_CTRL),
    "ctrl+r": (0x52, "\x12", LEFT_CTRL), "ctrl+u": (0x55, "\x15", LEFT_CTRL), "ctrl+t": (0x54, "\x14", LEFT_CTRL),
    "alt+enter": (0x0D, "\r", LEFT_ALT),
}


def _rec(vk, ch, flags, down):
    r = INPUT_RECORD()
    r.EventType = KEY_EVENT
    k = r.Event.KeyEvent
    k.bKeyDown = 1 if down else 0
    k.wRepeatCount = 1
    k.wVirtualKeyCode = vk
    k.wVirtualScanCode = 0
    k.uChar = ch
    k.dwControlKeyState = flags
    return r


def _write(h, recs):
    arr = (INPUT_RECORD * len(recs))(*recs)
    n = wt.DWORD(0)
    if not k32.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(n)):
        raise ConsoleError(f"WriteConsoleInputW gagal (error {ctypes.get_last_error()})")
    return n.value


def _char_records(text):
    recs = []
    for ch in text:
        if ch == "\n":
            vk, c, fl = KEYS["enter"]
            recs += [_rec(vk, c, fl, True), _rec(vk, c, fl, False)]
            continue
        recs += [_rec(0, ch, 0, True), _rec(0, ch, 0, False)]
    return recs


def _key_records(name):
    if name in KEYS:
        vk, ch, fl = KEYS[name]
        return [_rec(vk, ch, fl, True), _rec(vk, ch, fl, False)]
    if len(name) == 1 and (name.isalnum()):
        return _char_records(name)
    raise ConsoleError(f"tombol tidak dikenal: {name!r}")


def _verbatim(text):
    """Setiap karakter apa adanya (termasuk ESC/LF/CR) sebagai key event tanpa virtual key."""
    return [r for ch in text for r in (_rec(0, ch, 0, True), _rec(0, ch, 0, False))]


def send_text(pid, text, enter=False, mode="paste", enter_delay=0.4):
    """Masukkan teks ke prompt.

    mode="paste": bracketed paste (ESC[200~ ... ESC[201~) -> baris baru tersimpan sebagai baris baru dan
        teks panjang aman. TUI memperlakukan ketikan beruntun sebagai paste, jadi Enter HARUS datang
        terpisah setelah jeda (enter_delay); kalau menempel dengan teks, Enter ditelan sebagai baris baru.
    mode="type": satu karakter per tulis dengan jeda kecil -> terbaca sebagai ketikan biasa (untuk
        input pendek satu baris). Teks berbaris-baris otomatis dialihkan ke paste.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if mode == "type" and "\n" in text:
        mode = "paste"
    total = 0
    if mode == "paste":
        recs = _verbatim("\x1b[200~" + text + "\x1b[201~")
        with _Attached(pid) as c:
            for j in range(0, len(recs), 256):
                total += _write(c.hin, recs[j:j + 256])
    else:
        with _Attached(pid) as c:
            for ch in text:
                total += _write(c.hin, _char_records(ch))
                time.sleep(0.004)
    if enter:
        time.sleep(enter_delay)
        total += send_keys(pid, ["enter"])
    return total


def send_keys(pid, names):
    total = 0
    with _Attached(pid) as c:
        for n in names:
            total += _write(c.hin, _key_records(n))
    return total


def read_screen(pid):
    """Isi jendela console yang terlihat: dict(rows=[...], cols, cursor=(x,y))."""
    with _Attached(pid) as c:
        info = CSBI()
        if not k32.GetConsoleScreenBufferInfo(c.hout, ctypes.byref(info)):
            raise ConsoleError("GetConsoleScreenBufferInfo gagal")
        w = info.srWindow
        cols = w.Right - w.Left + 1
        rows = []
        buf = ctypes.create_unicode_buffer(cols + 1)
        for y in range(w.Top, w.Bottom + 1):
            n = wt.DWORD(0)
            k32.ReadConsoleOutputCharacterW(c.hout, buf, cols, COORD(w.Left, y), ctypes.byref(n))
            rows.append(buf.value[:n.value].rstrip())
        return {"rows": rows, "cols": cols, "cursor": [info.dwCursorPosition.X - w.Left, info.dwCursorPosition.Y - w.Top]}


# ---------------- ubah ukuran console (auto-fit dari web) ----------------
k32.GetLargestConsoleWindowSize.argtypes = [wt.HANDLE]
k32.GetLargestConsoleWindowSize.restype = COORD
k32.SetConsoleScreenBufferSize.argtypes = [wt.HANDLE, COORD]
k32.SetConsoleWindowInfo.argtypes = [wt.HANDLE, wt.BOOL, ctypes.POINTER(SMALL_RECT)]


def _win_size(info):
    w = info.srWindow
    return w.Right - w.Left + 1, w.Bottom - w.Top + 1


def _wait_window(hout, cols, rows, timeout=1.5):
    """SetConsoleWindowInfo diterapkan asinkron oleh conhost; tunggu sampai ukuran window benar-benar berubah."""
    end = time.time() + timeout
    info = CSBI()
    while True:
        k32.GetConsoleScreenBufferInfo(hout, ctypes.byref(info))
        if _win_size(info) == (cols, rows):
            return info
        if time.time() > end:
            return None
        time.sleep(0.02)


def resize(pid, cols, rows):
    """Ubah ukuran jendela console ke cols x rows (dibatasi ukuran terbesar yang muat di layar PC).
    Urutan aman: kecilkan window dulu bila perlu (dan tunggu benar-benar mengecil), ubah buffer (lebar = lebar
    window, tinggi tidak dikurangi agar scrollback aman), lalu set window. Hasil: ukuran aktual + ukuran sebelumnya."""
    with _Attached(pid) as c:
        info = CSBI()
        if not k32.GetConsoleScreenBufferInfo(c.hout, ctypes.byref(info)):
            raise ConsoleError("GetConsoleScreenBufferInfo gagal")
        largest = k32.GetLargestConsoleWindowSize(c.hout)
        cols = max(20, min(int(cols), largest.X or int(cols)))
        rows = max(8, min(int(rows), largest.Y or int(rows)))
        cur_w, cur_h = _win_size(info)
        before = (cur_w, cur_h)
        if (cols, rows) == before and info.dwSize.X == cols:
            return {"cols": cols, "rows": rows, "before": before, "changed": False}
        w = info.srWindow
        if cols < cur_w or rows < cur_h:        # buffer tidak boleh lebih kecil dari window -> kecilkan window dulu
            nw, nh = min(cols, cur_w), min(rows, cur_h)
            r = SMALL_RECT(w.Left, w.Top, w.Left + nw - 1, w.Top + nh - 1)
            k32.SetConsoleWindowInfo(c.hout, True, ctypes.byref(r))
            if _wait_window(c.hout, nw, nh) is None:
                raise ConsoleError("window tidak mengecil (timeout)")
        buf_h = max(info.dwSize.Y, rows)
        if not k32.SetConsoleScreenBufferSize(c.hout, COORD(cols, buf_h)):
            raise ConsoleError(f"SetConsoleScreenBufferSize gagal (error {ctypes.get_last_error()})")
        info2 = CSBI()
        k32.GetConsoleScreenBufferInfo(c.hout, ctypes.byref(info2))
        top = max(0, min(info2.srWindow.Top, info2.dwSize.Y - rows))
        cy = info2.dwCursorPosition.Y             # pastikan kursor (prompt) tetap terlihat
        if cy < top or cy > top + rows - 1:
            top = max(0, min(cy - rows + 2, info2.dwSize.Y - rows))
        r = SMALL_RECT(0, top, cols - 1, top + rows - 1)
        if not k32.SetConsoleWindowInfo(c.hout, True, ctypes.byref(r)):
            raise ConsoleError(f"SetConsoleWindowInfo gagal (error {ctypes.get_last_error()})")
        info3 = _wait_window(c.hout, cols, rows)
        if info3 is None:
            info3 = CSBI()
            k32.GetConsoleScreenBufferInfo(c.hout, ctypes.byref(info3))
        aw, ah = _win_size(info3)
        return {"cols": aw, "rows": ah, "before": before, "changed": True}