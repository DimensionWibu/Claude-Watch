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


u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
k32.GetConsoleWindow.restype = wt.HWND


class _CFI(ctypes.Structure):
    _fields_ = [("nFont", wt.DWORD), ("dwFontSize", COORD)]


k32.GetCurrentConsoleFont.argtypes = [wt.HANDLE, wt.BOOL, ctypes.POINTER(_CFI)]
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10


def resize(pid, cols, rows):
    """Ubah ukuran jendela console ke cols x rows karakter (dibatasi ukuran terbesar yang muat di layar PC).

    SetConsoleWindowInfo hanya bisa MEMPERBESAR (memperkecil diabaikan tanpa error), jadi yang dipakai adalah
    SetWindowPos pada HWND console dengan loop koreksi: selisih sel x ukuran font = selisih piksel, lalu dibaca ulang
    karena conhost menerapkannya secara asinkron. Buffer diperbesar dulu bila perlu; lebar buffer akhirnya disamakan
    dengan window (tanpa scrollbar horizontal). Console yang tak punya jendela sungguhan (Windows Terminal/ConPTY)
    tidak bisa diubah dari sini."""
    with _Attached(pid) as c:
        info = CSBI()
        if not k32.GetConsoleScreenBufferInfo(c.hout, ctypes.byref(info)):
            raise ConsoleError("GetConsoleScreenBufferInfo gagal")
        largest = k32.GetLargestConsoleWindowSize(c.hout)
        cols = max(20, min(int(cols), largest.X or int(cols)))
        rows = max(8, min(int(rows), largest.Y or int(rows)))
        before = _win_size(info)
        if before == (cols, rows):
            return {"cols": cols, "rows": rows, "before": before, "changed": False}
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            raise ConsoleError("console tidak punya jendela sendiri (mungkin Windows Terminal): ukuran mengikuti jendela terminal itu")
        f = _CFI()
        k32.GetCurrentConsoleFont(c.hout, False, ctypes.byref(f))
        fw, fh = (f.dwFontSize.X or 8), (f.dwFontSize.Y or 16)
        if cols > info.dwSize.X or rows > info.dwSize.Y:      # window tak boleh lebih besar dari buffer
            k32.SetConsoleScreenBufferSize(c.hout, COORD(max(cols, info.dwSize.X), max(rows, info.dwSize.Y)))
        cur = before
        for _ in range(8):
            cur_info = CSBI()
            k32.GetConsoleScreenBufferInfo(c.hout, ctypes.byref(cur_info))
            cur = _win_size(cur_info)
            if cur == (cols, rows):
                break
            rc = wt.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(rc))
            w = max(1, (rc.right - rc.left) + (cols - cur[0]) * fw)
            h = max(1, (rc.bottom - rc.top) + (rows - cur[1]) * fh)
            u32.SetWindowPos(hwnd, None, 0, 0, w, h, SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.15)
        fin = CSBI()
        k32.GetConsoleScreenBufferInfo(c.hout, ctypes.byref(fin))
        cur = _win_size(fin)
        if fin.dwSize.X != cur[0]:                            # buang scrollbar horizontal
            k32.SetConsoleScreenBufferSize(c.hout, COORD(cur[0], max(fin.dwSize.Y, cur[1])))
            k32.GetConsoleScreenBufferInfo(c.hout, ctypes.byref(fin))
            cur = _win_size(fin)
        if cur == before:
            raise ConsoleError("ukuran jendela tidak berubah (jendela dimaksimalkan / dikunci?)")
        return {"cols": cur[0], "rows": cur[1], "before": before, "changed": True}