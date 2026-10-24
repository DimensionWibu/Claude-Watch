"""PIN + sesi 1 jam untuk akses NON-LOKAL (LAN / tunnel). Klien dari PC ini (loopback tanpa header proxy) dibebaskan.

- PIN disimpan sebagai hash PBKDF2-SHA256 di config.json ("pin"), diganti lewat `python setpin.py <pin>`.
- Sesi: cookie HttpOnly + SameSite=Strict, kedaluwarsa mutlak (default 60 menit, "sessionMinutes"), disimpan di memori
  (restart service = login ulang).
- PIN 6 digit lemah terhadap tebakan, jadi ada lockout: 5 salah / 10 menit per IP -> kunci 15 menit; 20 salah / 10 menit
  secara global -> semua login non-lokal dikunci 10 menit. Belum diset PIN = ditolak (fail closed).
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
ITERS = 200_000
IP_MAX_FAILS, IP_WINDOW, IP_LOCK = 5, 600, 900
GLOBAL_MAX_FAILS, GLOBAL_LOCK = 20, 600

_lock = threading.Lock()
_sessions = {}        # token -> expiry (epoch)
_ip_fails = {}        # ip -> [ts]
_ip_locked = {}       # ip -> until
_global_fails = []
_global_locked = 0.0
_cfg_cache = (0.0, {})


def _cfg():
    global _cfg_cache
    try:
        m = os.path.getmtime(CONFIG_FILE)
    except OSError:
        return {}
    if m != _cfg_cache[0]:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        _cfg_cache = (m, data if isinstance(data, dict) else {})
    return _cfg_cache[1]


def session_seconds():
    try:
        return max(60, int(float(_cfg().get("sessionMinutes", 60)) * 60))
    except (TypeError, ValueError):
        return 3600


def pin_configured():
    p = _cfg().get("pin")
    return isinstance(p, dict) and bool(p.get("salt")) and bool(p.get("hash"))


def _derive(pin, salt_hex, iters):
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), iters).hex()


def set_pin(pin):
    """Tulis hash PIN ke config.json (mempertahankan kunci lain)."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    salt = secrets.token_hex(16)
    data["pin"] = {"salt": salt, "iters": ITERS, "hash": _derive(pin, salt, ITERS)}
    data.setdefault("sessionMinutes", 60)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def check_pin(ip, pin):
    """-> (ok, pesan, retry_detik). Semua percobaan dihitung terhadap IP dan batas global."""
    now = time.time()
    global _global_locked
    with _lock:
        if now < _global_locked:
            return False, "Login dikunci sementara karena terlalu banyak percobaan salah.", int(_global_locked - now)
        if now < _ip_locked.get(ip, 0):
            return False, "Terlalu banyak percobaan salah. Coba lagi nanti.", int(_ip_locked[ip] - now)
    if not pin_configured():
        return False, "PIN belum diset di server (jalankan setpin.py).", 0
    p = _cfg()["pin"]
    try:
        ok = isinstance(pin, str) and 1 <= len(pin) <= 64 and hmac.compare_digest(
            _derive(pin, p["salt"], int(p.get("iters", ITERS))), str(p["hash"]))
    except (ValueError, TypeError):
        ok = False
    with _lock:
        if ok:
            _ip_fails.pop(ip, None)
            return True, "", 0
        fl = [t for t in _ip_fails.get(ip, []) if now - t < IP_WINDOW] + [now]
        _ip_fails[ip] = fl
        _global_fails[:] = [t for t in _global_fails if now - t < IP_WINDOW] + [now]
        if len(_global_fails) >= GLOBAL_MAX_FAILS:
            _global_locked = now + GLOBAL_LOCK
        if len(fl) >= IP_MAX_FAILS:
            _ip_locked[ip] = now + IP_LOCK
            return False, "PIN salah. Percobaan habis, dikunci 15 menit.", IP_LOCK
        return False, f"PIN salah. Sisa percobaan: {IP_MAX_FAILS - len(fl)}.", 0


def new_session(seconds=None):
    token = secrets.token_urlsafe(32)
    with _lock:
        now = time.time()
        for t in [t for t, e in _sessions.items() if e < now]:
            del _sessions[t]
        _sessions[token] = now + (seconds or session_seconds())
    return token


def remaining(token):
    """Sisa detik sesi (0 bila tidak valid / kedaluwarsa)."""
    if not token:
        return 0
    with _lock:
        e = _sessions.get(token, 0)
        if e <= time.time():
            _sessions.pop(token, None)
            return 0
        return int(e - time.time())


def drop(token):
    with _lock:
        _sessions.pop(token, None)


LOGIN_HTML = """<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Masuk · Claude Watch</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--text:#1b1f27;--muted:#667085;--line:#d9dde5;--accent:#2563eb;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--panel:#171b23;--text:#e6e9ef;--muted:#98a2b3;--line:#2a303b;--accent:#60a5fa;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,"Segoe UI",sans-serif}
main{width:min(92vw,360px);padding:28px 24px;background:var(--panel);border:1px solid var(--line);border-radius:14px}
h1{font-size:18px;margin:0 0 4px}p{margin:0 0 18px;color:var(--muted);font-size:13.5px}
input{width:100%;padding:12px 14px;font:600 24px ui-monospace,Consolas,monospace;letter-spacing:.35em;text-align:center;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px}
input:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button{width:100%;margin-top:14px;padding:12px;font:600 15px system-ui,sans-serif;background:var(--accent);color:#fff;border:0;border-radius:10px;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}#err{min-height:1.4em;margin-top:12px;color:var(--bad);font-size:13.5px;text-align:center}
</style></head><body><main>
<h1>Claude Watch</h1><p>Masukkan PIN untuk membuka. Sesi berlaku 1 jam.</p>
<form id="f" autocomplete="off"><input id="pin" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="32" autocomplete="off" aria-label="PIN" autofocus>
<button id="go" type="submit">Masuk</button><div id="err" role="alert"></div></form>
<script>
const f=document.getElementById('f'),pin=document.getElementById('pin'),err=document.getElementById('err'),go=document.getElementById('go');
f.addEventListener('submit',async e=>{e.preventDefault();if(!pin.value)return;go.disabled=true;err.textContent='';
 try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:pin.value})});
  const d=await r.json().catch(()=>({}));
  if(r.ok&&d.ok){location.href='/'+location.search;return}
  err.textContent=d.error||('Gagal ('+r.status+')');pin.value='';pin.focus()}
 catch(x){err.textContent='Tidak bisa menghubungi server'}
 go.disabled=false});
</script></main></body></html>
"""
