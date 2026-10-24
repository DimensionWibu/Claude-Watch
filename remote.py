"""Mode remote: kontrol sesi Claude Code yang sedang berjalan dari web.

Alur: web -> server.py (SYSTEM, memvalidasi auth + memetakan sessionId ke pid sah dari registry
~/.claude/sessions) -> agent.py (proses user, 127.0.0.1) -> AttachConsole ke claude.exe.
Kontrol sesi = bisa menyuruh Claude menjalankan apa pun di PC ini. TANPA login (atas permintaan user):
siapa pun yang bisa menjangkau port web bisa mengontrol. Yang dijaga:
 - target hanya pid yang terdaftar & terverifikasi (procStart cocok) di registry Claude,
 - semua aksi dicatat ke remote-audit.log.
"""
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request

import settings

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_HOME = settings.claude_home()
SESS_DIR = os.path.join(CLAUDE_HOME, "sessions")
AGENT_KEY_FILE = os.path.join(HERE, "agent.key")
AUDIT_FILE = os.path.join(HERE, "remote-audit.log")
AGENT_PORT = int(os.environ.get("CLAUDE_WATCH_AGENT_PORT", "64001"))
AGENT_VERSION = 6
MAX_MSG = 8000
SID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wt.HANDLE
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
_k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]


class RemoteError(RuntimeError):
    pass


def audit(ip, action, sid, detail=""):
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ip={ip} {action} sid={sid} {detail}\n")
    except OSError:
        pass


# ---------------- registry sesi live ----------------
def _proc_info(pid):
    h = _k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, k, u = (wt.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        buf = ctypes.create_unicode_buffer(520)
        n = wt.DWORD(520)
        _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        return (c.dwHighDateTime << 32) | c.dwLowDateTime, buf.value
    finally:
        _k32.CloseHandle(h)


_live_cache = (0.0, {})
_live_lock = threading.Lock()


def live_sessions(max_age=1.0):
    """sessionId -> info untuk proses claude yang benar-benar hidup."""
    global _live_cache
    now = time.time()
    with _live_lock:
        if now - _live_cache[0] < max_age:
            return _live_cache[1]
        out = {}
        for f in glob.glob(os.path.join(SESS_DIR, "*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    j = json.load(fh)
                pid = int(j["pid"])
                pi = _proc_info(pid)
                if not pi or str(pi[0]) != str(j.get("procStart")) or "claude" not in os.path.basename(pi[1]).lower():
                    continue
                sid = j["sessionId"]
                info = {
                    "pid": pid, "sid": sid, "status": j.get("status"), "statusUpdatedAt": (j.get("statusUpdatedAt") or 0) / 1000.0,
                    "updatedAt": (j.get("updatedAt") or 0) / 1000.0, "kind": j.get("kind"), "entrypoint": j.get("entrypoint"),
                    "name": j.get("name"), "cwd": j.get("cwd"), "startedAt": (j.get("startedAt") or 0) / 1000.0,
                    "controllable": j.get("entrypoint") == "cli",
                }
                if sid not in out or info["updatedAt"] > out[sid]["updatedAt"]:
                    out[sid] = info
            except (OSError, ValueError, KeyError):
                continue
        _live_cache = (now, out)
        return out


def live_public(sid):
    i = live_sessions().get(sid)
    if not i:
        return None
    return {k: i[k] for k in ("pid", "status", "statusUpdatedAt", "kind", "entrypoint", "name", "controllable", "startedAt", "cwd")}


# ---------------- agent ----------------
_agent_lock = threading.Lock()


def _agent_key():
    try:
        with open(AGENT_KEY_FILE, "r", encoding="ascii") as f:
            k = f.read().strip()
            if k:
                return k
    except OSError:
        pass
    k = secrets.token_hex(16)
    with open(AGENT_KEY_FILE, "w", encoding="ascii") as f:
        f.write(k + "\n")
    return k


def _agent_http(method, path, body=None, timeout=6):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{AGENT_PORT}{path}", data=data, method=method,
                                 headers={"X-Agent-Key": _agent_key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"agent HTTP {e.code}"}


_agent_ok_until = 0.0


def ensure_agent():
    global _agent_ok_until
    if time.time() < _agent_ok_until:          # sehat baru saja dicek: lewati (ketikan langsung sering)
        return
    with _agent_lock:
        try:
            h = _agent_http("GET", "/health", timeout=1.5)
            if h.get("ok") and h.get("version", 0) >= AGENT_VERSION:
                _agent_ok_until = time.time() + 5
                return
            if h.get("ok"):                      # versi lama: matikan, nanti dijalankan ulang
                try:
                    _agent_http("POST", "/shutdown", {}, timeout=2)
                except Exception:
                    pass
                time.sleep(0.8)
        except (urllib.error.URLError, OSError, ValueError):
            pass
        import spawn
        _agent_key()
        spawn.spawn_agent(os.path.join(HERE, "agent.py"), HERE)
        for _ in range(30):
            time.sleep(0.25)
            try:
                h = _agent_http("GET", "/health", timeout=1.0)
                if h.get("ok"):
                    return
            except (urllib.error.URLError, OSError, ValueError):
                continue
        raise RemoteError("agent kontrol gagal start di sesi login user")


def _resolve(sid):
    if not SID_RE.match(sid or ""):
        raise RemoteError("session id tidak valid")
    info = live_sessions(max_age=0.3).get(sid)
    if not info:
        raise RemoteError("sesi ini tidak sedang berjalan (tidak ada proses claude hidup). Pakai Resume/Fork dulu.")
    if not info["controllable"]:
        raise RemoteError(f"sesi ini berjalan lewat '{info.get('entrypoint')}', bukan terminal CLI, jadi tidak bisa dikontrol dari sini")
    return info


def _clean_text(text):
    text = str(text).replace("\t", "    ")
    return "".join(ch for ch in text if ch == "\n" or ch >= " " and ch != "\x7f")


_live_audit = {}


def send_message(sid, text, ip, enter=True, mode="paste", live=False):
    """live=True: ketikan keyboard langsung (spasi/potongan teks boleh, isi TIDAK dicatat, audit dibatasi)."""
    text = _clean_text(text)
    if not live:
        text = text.strip("\n")
        if not text.strip():
            raise RemoteError("pesan kosong")
    elif not text:
        raise RemoteError("teks kosong")
    if len(text) > MAX_MSG:
        raise RemoteError(f"pesan terlalu panjang (maks {MAX_MSG} karakter)")
    info = _resolve(sid)
    ensure_agent()
    mode = mode if mode in ("paste", "type") else "paste"
    if not live:
        audit(ip, "send", sid, f"pid={info['pid']} len={len(text)} enter={enter} mode={mode}")
    elif time.time() - _live_audit.get(sid, 0) > 10:      # ketikan langsung: cukup satu catatan per 10 dtk
        _live_audit[sid] = time.time()
        audit(ip, "live-type", sid, f"pid={info['pid']} (isi tidak dicatat)")
    r = _agent_http("POST", "/send", {"pid": info["pid"], "text": text, "enter": enter, "mode": mode}, timeout=15)
    if not r.get("ok"):
        raise RemoteError(r.get("error", "gagal kirim"))
    return {"pid": info["pid"], "status": info["status"]}


def send_keys(sid, keys, ip):
    if not isinstance(keys, list) or not keys:
        raise RemoteError("daftar tombol kosong")
    info = _resolve(sid)
    ensure_agent()
    audit(ip, "keys", sid, f"pid={info['pid']} keys={','.join(map(str, keys))[:120]}")
    r = _agent_http("POST", "/keys", {"pid": info["pid"], "keys": keys})
    if not r.get("ok"):
        raise RemoteError(r.get("error", "gagal kirim tombol"))
    return {"pid": info["pid"]}


_resize_audit = {}


def resize_session(sid, cols, rows, restore, ip):
    """Ubah ukuran console sesi mengikuti area terminal di web (auto-fit); restore=True mengembalikan ukuran asli."""
    info = _resolve(sid)
    ensure_agent()
    body = {"pid": info["pid"], "restore": True} if restore else {"pid": info["pid"], "cols": int(cols), "rows": int(rows)}
    if restore or time.time() - _resize_audit.get(sid, 0) > 10:
        _resize_audit[sid] = time.time()
        audit(ip, "resize", sid, f"pid={info['pid']} " + ("restore" if restore else f"{cols}x{rows}"))
    r = _agent_http("POST", "/resize", body)
    if not r.get("ok"):
        raise RemoteError(r.get("error", "gagal mengubah ukuran"))
    r.pop("ok", None)
    return r


def read_screen(sid):
    info = _resolve(sid)
    ensure_agent()
    r = _agent_http("GET", f"/screen?pid={info['pid']}")
    if not r.get("ok"):
        raise RemoteError(r.get("error", "gagal baca layar"))
    r["status"] = info["status"]
    return r


def is_local_ip(ip):
    if os.environ.get("CLAUDE_WATCH_FORCE_REMOTE"):   # hanya untuk uji jalur auth klien LAN dari PC ini
        return False
    if ip in ("127.0.0.1", "::1"):
        return True
    try:
        return ip in {r[4][0] for r in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        return False


# ---------------- auto-jawab dialog "trust this folder" setelah Resume/Fork ----------------
class _PE32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD),
                ("szExeFile", wt.WCHAR * 260)]


_k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PE32)]
_k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PE32)]


def _claude_pids():
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    out = []
    if snap in (None, wt.HANDLE(-1).value):
        return out
    try:
        pe = _PE32()
        pe.dwSize = ctypes.sizeof(_PE32)
        ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == "claude.exe":
                out.append(pe.th32ProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        _k32.CloseHandle(snap)
    return out


def auto_trust_async(cwd, t_spawn, ip, sid):
    threading.Thread(target=_auto_trust, args=(cwd, t_spawn, ip, sid), daemon=True).start()


def _auto_trust(cwd, t_spawn, ip, sid):
    """Cari proses claude.exe yang baru dibuat, tunggu dialog trust, pilih 'Yes' HANYA bila folder di dialog
    sama dengan folder kerja sesi (dari transkrip). Berhenti begitu proses terdaftar di registry / timeout 60 dtk."""
    try:
        ensure_agent()
        cutoff = int((t_spawn - 2 + 11644473600) * 1e7)          # unix -> FILETIME
        want = re.sub(r"\s+", "", cwd or "").lower()
        if not want:
            return
        pid, last_enter, deadline = None, 0.0, time.time() + 60
        while time.time() < deadline:
            if pid is None:
                cands = []
                for p in _claude_pids():
                    pi = _proc_info(p)
                    if pi and pi[0] >= cutoff:
                        cands.append((pi[0], p))
                if not cands:
                    time.sleep(0.5)
                    continue
                pid = max(cands)[1]
            if any(i["pid"] == pid for i in live_sessions(max_age=0.5).values()):
                return                                              # sudah siap, tak ada dialog
            r = _agent_http("GET", f"/screen?pid={pid}")
            if not r.get("ok"):
                time.sleep(0.7)
                continue
            rows = r["rows"]
            flat = re.sub(r"\s+", "", "\n".join(rows)).lower()
            if "yes,itrustthisfolder" in flat and "accessingworkspace" in flat:
                if want not in flat:
                    audit(ip, "auto-trust-batal", sid, f"pid={pid} folder di dialog != {cwd!r}")
                    return
                if not any(x.lstrip().startswith(">") and "Yes, I trust" in x for x in rows):
                    _agent_http("POST", "/keys", {"pid": pid, "keys": ["down"]})
                elif time.time() - last_enter > 2.5:
                    audit(ip, "auto-trust", sid, f"pid={pid} cwd={cwd}")
                    _agent_http("POST", "/keys", {"pid": pid, "keys": ["enter"]})
                    last_enter = time.time()
            time.sleep(0.6)
    except Exception as e:
        audit(ip, "auto-trust-error", sid, f"{type(e).__name__}: {e}")


# ---------------- stop sesi ----------------
_k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]


def _proc_entry(pid):
    """(nama exe, pid induk) lewat toolhelp, atau None."""
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap in (None, wt.HANDLE(-1).value):
        return None
    try:
        pe = _PE32()
        pe.dwSize = ctypes.sizeof(_PE32)
        ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.th32ProcessID == pid:
                return pe.szExeFile, pe.th32ParentProcessID
            ok = _k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        _k32.CloseHandle(snap)
    return None


def _alive(pid, ft):
    pi = _proc_info(pid)
    return bool(pi and pi[0] == ft)


def _terminate(pid, ft):
    """Hentikan paksa HANYA bila pid masih proses yang sama (waktu pembuatan cocok)."""
    h = _k32.OpenProcess(0x1001, False, pid)      # TERMINATE | QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        c, e, k, u = (wt.FILETIME() for _ in range(4))
        _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u))
        if ((c.dwHighDateTime << 32) | c.dwLowDateTime) != ft:
            return False
        return bool(_k32.TerminateProcess(h, 1))
    finally:
        _k32.CloseHandle(h)


def _cmdline(pid):
    import subprocess
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                           capture_output=True, text=True, timeout=10, creationflags=0x08000000)
        return (r.stdout or "").strip()
    except Exception:
        return ""


_OUR_CMD = re.compile(r'cmd(\.exe)?"?\s+/k\s+""[^"]*claude(\.exe)?"\s+--resume\s', re.I)


def stop_session(sid, ip, force=False):
    """Hentikan sesi: Esc (bila sibuk) + Ctrl+C beruntun; kalau masih hidup, hentikan paksa pid yang terverifikasi.
    Jendela cmd induk ikut ditutup HANYA bila dibuat oleh fitur Resume/Fork (pola command line cocok)."""
    info = _resolve(sid)
    pid = info["pid"]
    pi = _proc_info(pid)
    if not pi:
        raise RemoteError("proses sudah tidak ada")
    ft = pi[0]
    ent = _proc_entry(pid)
    parent = ent[1] if ent else None
    pft = (_proc_info(parent) or (None,))[0] if parent else None
    audit(ip, "stop", sid, f"pid={pid} status={info['status']} force={force}")
    how = "force"
    if not force:
        ensure_agent()
        if info["status"] == "busy":
            _agent_http("POST", "/keys", {"pid": pid, "keys": ["esc"]})
            time.sleep(1.0)
        for _ in range(2):
            if not _alive(pid, ft):
                break
            for _i in range(2):
                _agent_http("POST", "/keys", {"pid": pid, "keys": ["ctrl+c"]})
                time.sleep(0.35)
            for _w in range(15):
                if not _alive(pid, ft):
                    break
                time.sleep(0.1)
        if not _alive(pid, ft):
            how = "graceful"
    if _alive(pid, ft):
        if not _terminate(pid, ft):
            raise RemoteError("gagal menghentikan proses claude")
        for _w in range(20):
            if not _alive(pid, ft):
                break
            time.sleep(0.1)
    closed = False
    if parent and pft:
        pent = _proc_entry(parent)
        if pent and pent[0].lower() == "cmd.exe" and _OUR_CMD.search(_cmdline(parent) or "") and _alive(parent, pft):
            closed = _terminate(parent, pft)
    _live_cache_reset()
    return {"stopped": not _alive(pid, ft), "how": how, "closedWindow": closed, "pid": pid}


def _live_cache_reset():
    global _live_cache
    with _live_lock:
        _live_cache = (0.0, {})
