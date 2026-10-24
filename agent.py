"""Claude Watch agent: berjalan di sesi login USER (bukan SYSTEM) agar bisa AttachConsole ke claude.exe.

Hanya mendengarkan 127.0.0.1 dan wajib header X-Agent-Key (isi file agent.key). Dipanggil oleh service
(server.py); service-lah yang memvalidasi pid milik sesi claude yang sah. Dijalankan dengan pythonw.exe.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import consolectl  # noqa: E402

AGENT_VERSION = 5
_ORIG_SIZE = {}   # pid -> (kolom, baris) sebelum auto-fit pertama
PORT = int(os.environ.get("CLAUDE_WATCH_AGENT_PORT", "64001"))
KEY_FILE = os.path.join(HERE, "agent.key")
MAX_TEXT = 20000
ALLOWED_KEYS = set(consolectl.KEYS) | {chr(c) for c in range(ord("0"), ord("9") + 1)} | {chr(c) for c in range(ord("a"), ord("z") + 1)}


def load_key():
    with open(KEY_FILE, "r", encoding="ascii") as f:
        return f.read().strip()


KEY = load_key()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        import hmac
        return hmac.compare_digest(self.headers.get("X-Agent-Key", ""), KEY)

    def do_GET(self):
        if not self._auth():
            return self._json(403, {"ok": False, "error": "key salah"})
        if self.path == "/health":
            return self._json(200, {"ok": True, "version": AGENT_VERSION, "pid": os.getpid()})
        if self.path.startswith("/screen?pid="):
            try:
                return self._json(200, {"ok": True, **consolectl.read_screen(int(self.path.split("=", 1)[1]))})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        self._json(404, {"ok": False})

    def do_POST(self):
        if not self._auth():
            return self._json(403, {"ok": False, "error": "key salah"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 200000)) or b"{}")
        except Exception:
            return self._json(400, {"ok": False, "error": "JSON tidak valid"})
        try:
            if self.path == "/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            pid = int(body["pid"])
            if self.path == "/resize":
                # simpan ukuran asli saat pertama kali diubah supaya bisa dikembalikan (restore)
                if body.get("restore"):
                    orig = _ORIG_SIZE.pop(pid, None)
                    if not orig:
                        return self._json(200, {"ok": True, "restored": False})
                    res = consolectl.resize(pid, orig[0], orig[1])
                    return self._json(200, {"ok": True, "restored": True, **res})
                cols, rows = int(body["cols"]), int(body["rows"])
                if not (20 <= cols <= 400 and 8 <= rows <= 200):
                    return self._json(400, {"ok": False, "error": "ukuran di luar batas (20-400 kolom, 8-200 baris)"})
                res = consolectl.resize(pid, cols, rows)
                if res.get("changed") and pid not in _ORIG_SIZE:
                    _ORIG_SIZE[pid] = tuple(res["before"])
                return self._json(200, {"ok": True, **res, "orig": list(_ORIG_SIZE.get(pid, res["before"]))})
            if self.path == "/send":
                text = str(body.get("text", ""))[:MAX_TEXT]
                mode = body.get("mode") if body.get("mode") in ("paste", "type") else "paste"
                cnt = consolectl.send_text(pid, text, enter=bool(body.get("enter")), mode=mode)
                return self._json(200, {"ok": True, "records": cnt})
            if self.path == "/keys":
                keys = [str(k) for k in body.get("keys", [])][:50]
                bad = [k for k in keys if k not in ALLOWED_KEYS]
                if bad:
                    return self._json(400, {"ok": False, "error": f"tombol tidak diizinkan: {bad}"})
                return self._json(200, {"ok": True, "records": consolectl.send_keys(pid, keys)})
        except Exception as e:
            return self._json(500, {"ok": False, "error": str(e)})
        self._json(404, {"ok": False})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    srv.allow_reuse_address = False
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
