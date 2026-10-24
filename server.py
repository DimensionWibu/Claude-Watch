"""Claude Watch: pantau transkrip sesi Claude Code secara realtime lewat web (SSE).

Hanya stdlib. Default bind 0.0.0.0 (atas permintaan user). TANPA autentikasi: siapa pun yang bisa
menjangkau port ini membaca isi percakapan & output tool. Batasi lewat firewall (lihat install.ps1).
Set CLAUDE_WATCH_HOST=127.0.0.1 untuk lokal saja.
"""
import json
import os
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from http.cookies import SimpleCookie

import auth
import remote
import settings

HOST = os.environ.get("CLAUDE_WATCH_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLAUDE_WATCH_PORT", "64000"))
PROJECTS = os.environ.get("CLAUDE_WATCH_PROJECTS") or os.path.join(settings.claude_home(), "projects")
HERE = os.path.dirname(os.path.abspath(__file__))
ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
PROXY_HEADERS = ("CF-Connecting-IP", "CF-Ray", "X-Forwarded-For", "Forwarded", "X-Real-IP", "CF-Visitor")
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_cfg_cache = (0.0, frozenset())


def extra_hosts():
    """Hostname tambahan yang boleh di header Host (mis. hostname publik Cloudflare Tunnel), dari config.json."""
    global _cfg_cache
    try:
        m = os.path.getmtime(CONFIG_FILE)
    except OSError:
        return frozenset()
    if m != _cfg_cache[0]:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:      # utf-8-sig: toleran BOM dari editor Windows
                hosts = frozenset(str(h).strip().lower() for h in json.load(f).get("allowedHosts", []) if h)
        except (OSError, ValueError, AttributeError):
            hosts = frozenset()
        _cfg_cache = (m, hosts)
    return _cfg_cache[1]
MY_HOSTNAME = socket.gethostname().lower()
TEXT_MAX = 6000
RING = 600

# entri pembukuan internal: tidak ditampilkan sebagai event
BOOKKEEPING = {
    "mode", "permission-mode", "atis-latch", "file-history-snapshot", "file-history-delta",
    "api-request-shape", "api-request-blob", "last-prompt", "attachment", "queue-operation",
}


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def clip(s, n=TEXT_MAX):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"\n… (+{len(s) - n} karakter dipotong)"


def block_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return ""


class Tracker:
    """Membaca satu file jsonl secara inkremental dan menyimpan ringkasan + ring buffer event."""

    def __init__(self, path, sid):
        self.path, self.sid = path, sid
        self.lock = threading.Lock()
        self.offset = 0
        self.partial = b""
        self.entries = deque(maxlen=RING)
        self.seq = 0
        self.title = None
        self.cwd = None
        self.forked_from = None
        self.model = None
        self.loading = True
        self.last_poll = 0.0
        self.last_event_ts = None      # event bermakna terakhir (epoch)
        self.state = {"kind": "unknown", "detail": ""}
        self.prev_ts = None
        self.req = {}                  # requestId -> usage terakhir
        self.req_seen = set()
        self.n_requests = 0            # jumlah api-request
        self.last_rate = None
        self.last_turn_ms = None

    # ---- parsing ----
    def _emit(self, ts, kind, **kw):
        self.seq += 1
        e = {"seq": self.seq, "ts": ts, "kind": kind}
        e.update(kw)
        self.entries.append(e)

    def _handle(self, j):
        t = j.get("type")
        if t == "ai-title":
            self.title = j.get("aiTitle") or self.title
            return
        if j.get("cwd") and not self.cwd:
            self.cwd = j["cwd"]
        s2 = j.get("session_id")
        if s2 and s2 != self.sid:
            self.forked_from = s2
        if t in BOOKKEEPING:
            return
        ts = parse_ts(j.get("timestamp", ""))
        if ts is None:
            return
        gap = None if self.prev_ts is None else max(0.0, ts - self.prev_ts)
        self.prev_ts = ts
        self.last_event_ts = ts

        if t == "api-request":
            self.n_requests += 1
            self.model = (j.get("params") or {}).get("model", self.model)
            self.state = {"kind": "waiting_model", "detail": self.model or ""}
            self._emit(ts, "request", gap=gap, text=f"request → {self.model or '?'}")
        elif t == "user":
            c = (j.get("message") or {}).get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        self._emit(ts, "tool_result", gap=gap, text=clip(block_text(b.get("content"))),
                                   error=bool(b.get("is_error")))
                self.state = {"kind": "waiting_model", "detail": "tool selesai"}
            else:
                self._emit(ts, "user", gap=gap, text=clip(block_text(c)))
                self.state = {"kind": "waiting_model", "detail": "prompt user"}
        elif t == "assistant":
            m = j.get("message") or {}
            u = m.get("usage") or {}
            rid = j.get("requestId") or m.get("id")
            rate = None
            first = rid not in self.req_seen
            if rid and u:
                self.req[rid] = {
                    "in": u.get("input_tokens", 0) or 0,
                    "cr": u.get("cache_read_input_tokens", 0) or 0,
                    "cw": u.get("cache_creation_input_tokens", 0) or 0,
                    "out": u.get("output_tokens", 0) or 0,
                }
                if first:
                    self.req_seen.add(rid)
                    if gap and gap > 0.3 and self.req[rid]["out"] > 0:
                        rate = round(self.req[rid]["out"] / gap, 1)
                        self.last_rate = rate
            self.model = m.get("model") or self.model
            usage = None
            if u:
                usage = {"in": u.get("input_tokens", 0), "cr": u.get("cache_read_input_tokens", 0),
                         "cw": u.get("cache_creation_input_tokens", 0), "out": u.get("output_tokens", 0)}
            last_kind = None
            for b in (m.get("content") or []):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    self._emit(ts, "text", gap=gap, text=clip(b.get("text", "")), usage=usage, rate=rate)
                    last_kind = ("text", "")
                elif bt == "thinking":
                    self._emit(ts, "thinking", gap=gap, text=clip(b.get("thinking", "")) or "(thinking tersembunyi)",
                               ms=j.get("thinkingDurationMs"), usage=usage, rate=rate)
                    last_kind = ("thinking", "")
                elif bt == "tool_use":
                    inp = b.get("input")
                    self._emit(ts, "tool_use", gap=gap, tool=b.get("name"), text=clip(inp), usage=usage, rate=rate)
                    last_kind = ("running_tool", b.get("name") or "")
                gap, rate, usage = 0.0, None, None   # info hanya di blok pertama
            sr = m.get("stop_reason")
            if last_kind and last_kind[0] == "running_tool":
                self.state = {"kind": "running_tool", "detail": last_kind[1]}
            elif sr == "end_turn" or (last_kind and last_kind[0] == "text"):
                self.state = {"kind": "idle", "detail": "menunggu input"}
            else:
                self.state = {"kind": "waiting_model", "detail": "model lanjut menulis"}
        elif t == "system":
            sub = j.get("subtype", "system")
            if sub == "turn_duration":
                self.last_turn_ms = j.get("durationMs")
                self.state = {"kind": "idle", "detail": "menunggu input"}
                self._emit(ts, "system", gap=gap, text=f"turn selesai · {round((j.get('durationMs') or 0) / 1000, 1)} dtk")
            else:
                self._emit(ts, "system", gap=gap, text=clip(j.get("content") or sub, 1500), level=j.get("level"))

    def poll(self):
        """Baca byte baru. Dipanggil oleh thread poller."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            self.loading = False       # file belum ada (mis. fork baru: transkrip dibuat saat pesan pertama)
            return
        with self.lock:
            if size < self.offset:           # file dipotong/diganti
                self.__init__(self.path, self.sid)
            if size == self.offset:
                self.loading = False
                self.last_poll = time.time()
                return
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                # baca per potongan agar sesi besar (ratusan MB) tidak menahan memori
                while True:
                    chunk = f.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    self.offset += len(chunk)
                    data = self.partial + chunk
                    lines = data.split(b"\n")
                    self.partial = lines.pop()
                    for ln in lines:
                        if not ln.strip():
                            continue
                        # lewati blob besar tanpa parse
                        if ln.startswith(b'{"type":"api-request-blob"') or ln.startswith(b'{"type":"api-request-shape"'):
                            continue
                        try:
                            self._handle(json.loads(ln))
                        except Exception:
                            continue
            self.loading = False
            self.last_poll = time.time()

    # ---- ringkasan ----
    def summary(self):
        with self.lock:
            tot = {"in": 0, "cr": 0, "cw": 0, "out": 0}
            for v in self.req.values():
                for k in tot:
                    tot[k] += v[k]
            ctx = None
            if self.req:
                last = list(self.req.values())[-1]
                ctx = last["in"] + last["cr"] + last["cw"]
            return {
                "sid": self.sid, "forkedFrom": self.forked_from, "title": self.title, "cwd": self.cwd, "model": self.model,
                "state": self.state, "lastEventTs": self.last_event_ts, "now": time.time(),
                "tokens": tot, "context": ctx, "requests": self.n_requests,
                "rate": self.last_rate, "turnMs": self.last_turn_ms, "loading": self.loading,
                "seq": self.seq,
            }

    def since(self, seq, limit=None):
        with self.lock:
            out = [e for e in self.entries if e["seq"] > seq]
        return out[-limit:] if limit else out


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.trackers = {}
        self.list_cache = (0.0, [])
        self.meta_cache = {}   # path -> (title, cwd)
        self.started = time.time()

    def list_sessions(self, max_age=2.0):
        now = time.time()
        if now - self.list_cache[0] < max_age:
            return self.list_cache[1]
        items = []
        try:
            for d in os.scandir(PROJECTS):
                if not d.is_dir():
                    continue
                for f in os.scandir(d.path):
                    if f.is_file() and f.name.endswith(".jsonl") and ID_RE.match(f.name[:-6]):
                        st = f.stat()
                        items.append({"sid": f.name[:-6], "path": f.path, "project": d.name,
                                      "mtime": st.st_mtime, "size": st.st_size})
        except OSError:
            pass
        items.sort(key=lambda x: x["mtime"], reverse=True)
        items = items[:60]
        for it in items:
            it["title"], it["cwd"], it["forked_from"] = self._meta(it["path"], it["mtime"])
        kids = {}
        for it in items:
            if it["forked_from"]:
                kids[it["forked_from"]] = kids.get(it["forked_from"], 0) + 1
        for it in items:
            it["forks"] = kids.get(it["sid"], 0)   # jumlah fork dari sesi ini yang ada di daftar
        # sesi yang prosesnya hidup tapi transkripnya belum ada (fork/resume baru dibuka): tampilkan juga,
        # path dihitung dari folder kerja (semua karakter non-alfanumerik -> '-'); tracker membaca begitu file muncul
        have = {it["sid"] for it in items}
        for sid, lv in remote.live_sessions(max_age=1.0).items():
            if sid in have or not lv.get("cwd"):
                continue
            proj = re.sub(r"[^A-Za-z0-9]", "-", lv["cwd"])
            items.append({"sid": sid, "path": os.path.join(PROJECTS, proj, sid + ".jsonl"), "project": proj,
                          "mtime": lv.get("startedAt") or now, "size": 0, "title": "(sesi baru, belum ada transkrip)",
                          "cwd": lv["cwd"], "forked_from": None, "forks": 0})
        items.sort(key=lambda x: x["mtime"], reverse=True)
        self.list_cache = (now, items)
        return items

    def _meta(self, path, mtime):
        """(title, cwd, forked_from). Fork menyalin riwayat sesi induk; entri salinan masih membawa
        field session_id milik induk, jadi session_id != id file => file ini fork dari session_id itu."""
        own = os.path.basename(path)[:-6]
        tr = self.trackers.get(path)
        if tr and (tr.title or tr.cwd):
            return tr.title, tr.cwd, tr.forked_from
        c = self.meta_cache.get(path)
        if c and c[3] == mtime:
            return c[0], c[1], c[2]
        title = cwd = None
        first_user = None
        forked = c[2] if c else None
        try:
            with open(path, "rb") as f:
                head = f.read(256 * 1024)
            for ln in head.split(b"\n")[:400]:
                if ln.startswith(b'{"type":"api-request'):
                    continue
                try:
                    j = json.loads(ln)
                except Exception:
                    continue
                cwd = cwd or j.get("cwd")
                s2 = j.get("session_id")
                if s2 and s2 != own:
                    forked = s2
                if j.get("type") == "ai-title":
                    title = j.get("aiTitle")
                if j.get("type") == "last-prompt" and not first_user:
                    first_user = j.get("lastPrompt")
                if j.get("type") == "user" and not first_user:
                    t = block_text((j.get("message") or {}).get("content"))
                    if t and not t.startswith("<"):
                        first_user = t[:80]
        except OSError:
            pass
        title = title or first_user
        self.meta_cache[path] = (title, cwd, forked, mtime)
        return title, cwd, forked

    def tracker_for(self, sid):
        for it in self.list_sessions(max_age=5.0):
            if it["sid"] == sid:
                with self.lock:
                    tr = self.trackers.get(it["path"])
                    if not tr:
                        tr = Tracker(it["path"], sid)
                        self.trackers[it["path"]] = tr
                return tr
        return None

    def poll_loop(self):
        while True:
            try:
                with self.lock:
                    trs = list(self.trackers.values())
                for tr in trs:
                    tr.poll()
                # lepas tracker yang tak diminati lagi (tak ada klien > 10 menit)
                now = time.time()
                with self.lock:
                    for p, tr in list(self.trackers.items()):
                        if now - getattr(tr, "last_client", now) > 600:
                            del self.trackers[p]
            except Exception:
                pass
            time.sleep(0.4)


REG = Registry()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClaudeWatch/1.0"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        # Tolak Host berupa nama domain asing (DNS rebinding). Yang diizinkan: IP literal,
        # localhost, dan nama komputer ini. Akses via IP LAN (mis. 192.168.x.x) tetap lolos.
        h = (self.headers.get("Host") or "").lower()
        name, _, port = h.rpartition(":") if not h.endswith("]") and ":" in h else (h, "", "")
        if name in extra_hosts():          # hostname publik tunnel (config.json -> allowedHosts)
            return True
        if port and port != str(PORT):
            return False
        return bool(IPV4_RE.match(name)) or name in ("localhost", MY_HOSTNAME)

    def _client_ip(self):
        """IP klien sebenarnya: bila lewat tunnel (peer = loopback), pakai CF-Connecting-IP untuk audit."""
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):
            ip = (self.headers.get("CF-Connecting-IP") or "").strip()
            if ip:
                return ip[:64]
        return peer

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _is_local_client(self):
        # spawn hanya boleh dari PC ini (loopback / salah satu IP milik mesin ini), bukan dari LAN.
        # Trafik yang masuk lewat tunnel/reverse proxy juga datang dari 127.0.0.1 (cloudflared), jadi
        # request yang membawa header proxy TIDAK dianggap lokal.
        if any(self.headers.get(h) for h in PROXY_HEADERS):
            return False
        return remote.is_local_ip(self.client_address[0])

    def _same_origin_ok(self):
        # Origin (bila ada) harus sama dengan Host; Sec-Fetch-Site lintas-situs ditolak
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def _json_err(self, code, msg):
        return self._send(code, json.dumps({"ok": False, "error": msg}, ensure_ascii=False))

    def _session_token(self):
        raw = self.headers.get("Cookie") or ""
        try:
            c = SimpleCookie(raw)
        except Exception:
            return ""
        return c["cw_session"].value if "cw_session" in c else ""

    def _authed(self):
        """Klien dari PC ini (loopback tanpa header proxy) bebas; sisanya (LAN/tunnel) wajib cookie sesi PIN yang masih berlaku."""
        return self._is_local_client() or auth.remaining(self._session_token()) > 0

    def _is_https(self):
        return (self.headers.get("X-Forwarded-Proto", "") == "https") or ('"scheme":"https"' in (self.headers.get("CF-Visitor") or "").replace(" ", ""))

    def _remote_auth(self):
        """Gerbang login sudah di do_GET/do_POST; di sini tinggal anti-CSRF (same-origin)."""
        if not self._same_origin_ok():
            self._json_err(403, "permintaan ditolak")
            return False
        return True

    def _handle_login(self):
        if not self._same_origin_ok():
            return self._json_err(403, "permintaan ditolak")
        body = self._read_json(2000)
        if body is None:
            return
        ip = self._client_ip()
        ok, msg, retry = auth.check_pin(ip, str(body.get("pin", "")))
        if not ok:
            remote.audit(ip, "login-gagal", "-", msg)
            code = 429 if retry else 401
            return self._send(code, json.dumps({"ok": False, "error": msg, "retry": retry}, ensure_ascii=False),
                              extra={"Retry-After": str(retry)} if retry else None)
        token = auth.new_session()
        remote.audit(ip, "login-ok", "-", f"sesi {auth.session_seconds() // 60} mnt")
        cookie = f"cw_session={token}; Path=/; Max-Age={auth.session_seconds()}; HttpOnly; SameSite=Strict" + ("; Secure" if self._is_https() else "")
        return self._send(200, json.dumps({"ok": True, "expires": auth.session_seconds()}), extra={"Set-Cookie": cookie})

    def _read_json(self, limit=40000):
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            self._json_err(403, "permintaan ditolak")
            return None
        try:
            if len(self._raw) > limit:
                raise ValueError("terlalu besar")
            return json.loads(self._raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json_err(400, "JSON tidak valid")
            return None

    def _remote_call(self, fn, *a):
        try:
            return self._send(200, json.dumps({"ok": True, **fn(*a)}, ensure_ascii=False))
        except remote.RemoteError as e:
            return self._json_err(409, str(e))
        except Exception as e:
            return self._json_err(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        # Baca body SEBELUM membalas apa pun. Kalau tidak, body yang belum terbaca menumpuk di
        # koneksi keep-alive dan merusak request berikutnya (mis. setelah balasan 401/403).
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 200000:
            self.close_connection = True
            return self._json_err(413, "body terlalu besar")
        self._raw = self.rfile.read(n) if n > 0 else b""
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        if u.path == "/api/login":
            return self._handle_login()
        if u.path == "/api/logout":
            auth.drop(self._session_token())
            return self._send(200, json.dumps({"ok": True}), extra={"Set-Cookie": "cw_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
        if not self._authed():
            return self._json_err(401, "login diperlukan")
        if u.path in ("/api/remote/send", "/api/remote/keys", "/api/remote/stop", "/api/remote/resize"):
            if not self._remote_auth():
                return
            body = self._read_json()
            if body is None:
                return
            sid = str(body.get("session", ""))
            ip = self._client_ip()
            if u.path.endswith("/resize"):
                try:
                    cols, rows = int(body.get("cols", 0)), int(body.get("rows", 0))
                except (TypeError, ValueError):
                    return self._json_err(400, "ukuran tidak valid")
                if body.get("restore") is not True and not (20 <= cols <= 400 and 8 <= rows <= 200):
                    return self._json_err(400, "ukuran di luar batas (20-400 kolom, 8-200 baris)")
                return self._remote_call(remote.resize_session, sid, cols, rows, body.get("restore") is True, ip)
            if u.path.endswith("/stop"):
                return self._remote_call(remote.stop_session, sid, ip, body.get("force") is True)
            if u.path.endswith("/send"):
                return self._remote_call(remote.send_message, sid, body.get("text", ""), ip,
                                         body.get("enter", True) is not False, body.get("mode", "paste"), body.get("live") is True)
            return self._remote_call(remote.send_keys, sid, body.get("keys"), ip)
        if u.path != "/api/resume":
            return self._send(404, "not found", "text/plain")
        if not self._same_origin_ok():
            return self._json_err(403, "permintaan ditolak")
        body = self._read_json(4096)
        if body is None:
            return
        sid, mode = str(body.get("session", "")), body.get("mode")
        if mode not in ("resume", "fork") or not ID_RE.match(sid):
            return self._send(400, json.dumps({"ok": False, "error": "parameter tidak valid"}))
        item = next((i for i in REG.list_sessions(max_age=0) if i["sid"] == sid), None)
        if not item:
            return self._send(404, json.dumps({"ok": False, "error": "sesi tidak ditemukan"}))
        try:
            import spawn
            t_spawn = time.time()
            info = spawn.spawn_resume(sid, item.get("cwd"), mode == "fork")
        except Exception as e:
            return self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        auto = body.get("autoTrust", True) is not False
        if auto:   # jawab dialog "trust this folder" di jendela baru (hanya bila foldernya = folder kerja sesi)
            remote.auto_trust_async(item.get("cwd"), t_spawn, self._client_ip(), sid)
        return self._send(200, json.dumps({"ok": True, "autoTrust": auto, **info}, ensure_ascii=False))

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/login":                       # halaman PIN (juga bisa dilihat dari PC ini)
            return self._send(200, auth.LOGIN_HTML, "text/html; charset=utf-8")
        if u.path != "/api/health" and not self._authed():
            if u.path in ("/", "/index.html"):
                return self._send(200, auth.LOGIN_HTML, "text/html; charset=utf-8")
            return self._json_err(401, "login diperlukan")
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"})
        if u.path == "/api/sessions":
            now = time.time()
            data = [{k: v for k, v in it.items() if k != "path"} | {"age": now - it["mtime"], "live": remote.live_public(it["sid"])}
                    for it in REG.list_sessions()]
            return self._send(200, json.dumps({"sessions": data, "now": now}))
        if u.path == "/api/capabilities":
            local = self._is_local_client()
            return self._send(200, json.dumps({"spawn": True, "auth": {
                "required": not local, "remaining": None if local else auth.remaining(self._session_token())}}))
        if u.path == "/api/live":
            lv = {sid: remote.live_public(sid) for sid in remote.live_sessions(max_age=0.5)}
            return self._send(200, json.dumps({"live": lv, "now": time.time()}))
        if u.path == "/api/remote/screen":
            if not self._remote_auth():
                return
            return self._remote_call(remote.read_screen, (q.get("session") or [""])[0])
        if u.path == "/api/health":
            return self._send(200, json.dumps({"ok": True, "uptime": time.time() - REG.started}))
        if u.path == "/api/stream":
            return self._stream(q)
        self._send(404, "not found", "text/plain")

    def _stream(self, q):
        sid = (q.get("session") or [""])[0]
        if not ID_RE.match(sid):
            return self._send(400, "bad session", "text/plain")
        tr = REG.tracker_for(sid)
        if not tr:
            return self._send(404, "session tidak ditemukan", "text/plain")
        try:
            backlog = max(0, min(int((q.get("backlog") or ["60"])[0]), RING))
        except ValueError:
            backlog = 60
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True

        def send(ev, data):
            if ev == "status":
                data = {**data, "live": remote.live_public(sid)}
            self.wfile.write(f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            tr.last_client = time.time()
            # tunggu pemindaian awal selesai (sesi besar bisa beberapa detik)
            t0 = time.time()
            while tr.loading and time.time() - t0 < 120:
                tr.last_client = time.time()
                send("status", tr.summary())
                time.sleep(0.5)
            first = tr.since(0, backlog)
            send("backlog", {"entries": first})
            cur = tr.seq
            last_beat = 0
            while True:
                tr.last_client = time.time()
                new = tr.since(cur)
                if new:
                    cur = new[-1]["seq"]
                    send("entries", {"entries": new})
                if new or time.time() - last_beat >= 1.0:
                    send("status", tr.summary())
                    last_beat = time.time()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False   # di Windows True berarti bisa double-bind port; matikan


def make_server():
    return Server((HOST, PORT), Handler)


def start_background():
    threading.Thread(target=REG.poll_loop, daemon=True).start()


if __name__ == "__main__":
    start_background()
    srv = make_server()
    print(f"Claude Watch di http://{HOST}:{PORT}  (projects: {PROJECTS})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
