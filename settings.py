"""Pengaturan lokal dari config.json (tidak masuk repo) + default yang tidak memuat data pribadi."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
_cache = (0.0, {})


def load():
    """Isi config.json (dibaca ulang bila file berubah). {} bila tidak ada / rusak."""
    global _cache
    try:
        m = os.path.getmtime(CONFIG_FILE)
    except OSError:
        return {}
    if m != _cache[0]:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:      # toleran BOM dari editor Windows
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        _cache = (m, data if isinstance(data, dict) else {})
    return _cache[1]


def claude_home():
    """Folder data Claude Code (~/.claude milik user yang memakai Claude, BUKAN profil akun service).
    Urutan: env CLAUDE_WATCH_HOME > config.json "claudeHome" > ~/.claude (hanya benar bila dijalankan sebagai user itu).
    install.ps1 menulis "claudeHome" karena service berjalan sebagai SYSTEM."""
    return os.environ.get("CLAUDE_WATCH_HOME") or load().get("claudeHome") or os.path.join(os.path.expanduser("~"), ".claude")