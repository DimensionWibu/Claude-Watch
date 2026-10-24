"""Buka jendela cmd di desktop user yang sedang login untuk `claude --resume <id> [--fork-session]`.

Service berjalan sebagai SYSTEM di session 0 (tanpa desktop), jadi launcher dijalankan memakai token
user sesi aktif. Perintah: cmd /c start "<judul>" cmd /k ""<claude.exe>" --resume <sid> [--fork-session]"
Folder kerja diteruskan lewat lpCurrentDirectory (BUKAN lewat command line) agar isi transkrip yang
tak tepercaya tidak bisa menyisipkan perintah. Session id divalidasi ketat (hex & strip).
"""
import os
import re
import subprocess
import sys

SID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
SYSTEM_CMD = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")


def build_cmdline(claude_exe, sid, fork):
    if not SID_RE.match(sid):
        raise ValueError("session id tidak valid")
    title = f"Claude {'fork' if fork else 'resume'} {sid[:8]}"
    tail = f"--resume {sid}" + (" --fork-session" if fork else "")
    return f'"{SYSTEM_CMD}" /d /c start "{title}" "{SYSTEM_CMD}" /k ""{claude_exe}" {tail}"'


def _in_session0():
    try:
        import win32ts
        return win32ts.ProcessIdToSessionId(os.getpid()) == 0
    except Exception:
        return False


def _find_claude(profile_dir):
    for p in (os.path.join(profile_dir, ".local", "bin", "claude.exe"),
              os.path.join(profile_dir, "AppData", "Roaming", "npm", "claude.cmd")):
        if os.path.isfile(p):
            return p
    return "claude"   # biarkan PATH user yang mencari


def _pythonw():
    base = os.path.dirname(sys.executable) if "pythonservice" not in sys.executable.lower() else sys.prefix
    for name in ("pythonw.exe", "python.exe"):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return os.path.join(sys.prefix, "pythonw.exe")


def spawn_agent(script, workdir):
    """Jalankan agent.py di sesi login user (bukan session 0). Mengembalikan pid launcher."""
    cmdline = f'"{_pythonw()}" "{script}"'
    if not _in_session0():
        p = subprocess.Popen(cmdline, cwd=workdir, creationflags=subprocess.CREATE_NO_WINDOW)
        return p.pid

    import win32con
    import win32process
    import win32profile
    import win32ts

    sess = win32ts.WTSGetActiveConsoleSessionId()
    if sess in (0xFFFFFFFF, 0):
        raise RuntimeError("tidak ada user yang sedang login di console Windows")
    token = win32ts.WTSQueryUserToken(sess)
    env = win32profile.CreateEnvironmentBlock(token, False)
    si = win32process.STARTUPINFO()
    si.lpDesktop = "winsta0\\default"
    flags = win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_NO_WINDOW
    hproc, hthread, pid, _ = win32process.CreateProcessAsUser(
        token, None, cmdline, None, None, False, flags, env, workdir, si)
    hproc.Close()
    hthread.Close()
    token.Close()
    return pid


def spawn_resume(sid, cwd, fork):
    """Mengembalikan dict info. Raise RuntimeError dengan pesan ramah bila gagal."""
    if not cwd or not os.path.isdir(cwd):
        raise RuntimeError(f"folder kerja sesi tidak ada: {cwd!r} (resume harus dari folder proyek aslinya)")

    if not _in_session0():
        # dijalankan langsung sebagai user biasa (mode dev): cukup buat console baru
        exe = _find_claude(os.path.expanduser("~"))
        cmdline = build_cmdline(exe, sid, fork)
        subprocess.Popen(cmdline, cwd=cwd, creationflags=subprocess.CREATE_NO_WINDOW)
        return {"mode": "user", "cwd": cwd}

    import win32con
    import win32process
    import win32profile
    import win32ts

    sess = win32ts.WTSGetActiveConsoleSessionId()
    if sess in (0xFFFFFFFF, 0):
        raise RuntimeError("tidak ada user yang sedang login di console Windows")
    try:
        token = win32ts.WTSQueryUserToken(sess)
    except Exception as e:  # pywintypes.error
        raise RuntimeError(f"gagal ambil token user sesi {sess}: {e}")
    env = win32profile.CreateEnvironmentBlock(token, False)
    profile = win32profile.GetUserProfileDirectory(token)
    cmdline = build_cmdline(_find_claude(profile), sid, fork)
    si = win32process.STARTUPINFO()
    si.lpDesktop = "winsta0\\default"
    flags = win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_NO_WINDOW
    hproc, hthread, pid, _ = win32process.CreateProcessAsUser(
        token, None, cmdline, None, None, False, flags, env, cwd, si)
    hproc.Close()
    hthread.Close()
    token.Close()
    return {"mode": "session", "session": sess, "launcherPid": pid, "cwd": cwd}
