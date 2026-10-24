"""Penjaga repo: hanya source code + panduan install. Gagal (exit 1) bila ada file di luar daftar putih atau
konten sensitif. Pakai:  python scripts/check-repo.py --staged   (hook pre-commit)
                         python scripts/check-repo.py --all      (CI / audit seluruh file terlacak)
Opsional: berkas lokal `.repo-deny.local` (TIDAK ikut repo) berisi kata/string pribadi yang dilarang, satu per baris
(mis. PIN, nama user, domain, hostname). Isi berkas itu tidak boleh dicantumkan di repo."""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWED_FILES = {
    ".gitignore", "README.md", "REPO-RULES.md", "config.example.json",
    "agent.py", "auth.py", "consolectl.py", "index.html", "install.ps1", "uninstall.ps1",
    "remote.py", "server.py", "service.py", "settings.py", "setpin.py", "spawn.py",
    "scripts/check-repo.py", ".githooks/pre-commit", ".github/workflows/repo-guard.yml",
}
MAX_BYTES = 400_000
PLACEHOLDER_USER = re.compile(r"^(<[^>]*>|%[^%]*%|\.\.\.|you|user|username|name)$", re.I)

RULES = [
    ("hash/secret hex 64", re.compile(r"\b[0-9a-fA-F]{64}\b")),
    ("salt hex", re.compile(r'"salt"\s*:\s*"[0-9a-fA-F]{8,}')),
    ("objek PIN di config", re.compile(r'"pin"\s*:\s*[{"\d]')),
    ("PIN angka di perintah/dokumen", re.compile(r"setpin(\.py)?\s+[\"']?\d{4,}")),
    ("PIN/password diberi nilai literal", re.compile(r"(?i)\b(pin|passwd|password|secret)\b[\"']?\s*[:=]\s*([\"'][^\"'\s]{4,}[\"']|\d{4,}\b)")),
    ("PIN diikuti angka 6+ digit", re.compile(r"(?i)\bpin\b[^\n]{0,24}?\b\d{6,}\b")),
    ("token JWT/Cloudflare", re.compile(r"eyJ[A-Za-z0-9_\-]{20,}")),
    ("token GitHub/AWS/slack", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})")),
    ("kunci privat", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("alamat email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("IP privat", re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")),
]
USER_PATH = re.compile(r"[A-Za-z]:\\+Users\\+([^\\\s\"'<>|*?]+)")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True).stdout


def deny_words():
    p = os.path.join(ROOT, ".repo-deny.local")
    if not os.path.isfile(p):
        return []
    with open(p, "r", encoding="utf-8-sig") as f:
        return [w.strip() for w in f if w.strip() and not w.startswith("#")]


def scan(path, data, deny):
    """-> daftar (baris, nama aturan). Isi yang cocok TIDAK dicetak agar rahasia tak bocor ke log."""
    out = []
    if b"\x00" in data:
        return [(0, "file biner dilarang")]
    if len(data) > MAX_BYTES:
        out.append((0, f"file terlalu besar (> {MAX_BYTES} byte)"))
    text = data.decode("utf-8", "replace")
    for n, line in enumerate(text.splitlines(), 1):
        for name, rx in RULES:
            if rx.search(line):
                out.append((n, name))
        for m in USER_PATH.finditer(line):
            if not PLACEHOLDER_USER.match(m.group(1)):
                out.append((n, "path profil user pribadi"))
        low = line.lower()
        for w in deny:
            if w.lower() in low:
                out.append((n, "kata terlarang dari .repo-deny.local"))
    return out


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--staged"
    deny = deny_words()
    if mode == "--staged":
        names = [n for n in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode("utf-8", "replace").split("\0") if n]
        read = lambda n: git("show", ":" + n)
    elif mode == "--all":
        names = [n for n in git("ls-files", "-z").decode("utf-8", "replace").split("\0") if n]
        def read(n):
            with open(os.path.join(ROOT, n), "rb") as f:
                return f.read()
    else:
        sys.exit("pakai: check-repo.py --staged | --all")
    bad = 0
    for n in names:
        if n not in ALLOWED_FILES:
            print(f"DITOLAK  {n}: file di luar daftar putih (repo hanya source code + panduan install)")
            bad += 1
            continue
        for line, rule in scan(n, read(n), deny):
            print(f"DITOLAK  {n}:{line}: {rule}")
            bad += 1
    if bad:
        print(f"\n{bad} pelanggaran. Commit/CI dibatalkan. Baca REPO-RULES.md. Jika rahasia terlanjur terkirim: rotasi PIN/token, "
              "lalu bersihkan history (git filter-repo); menghapus di commit baru TIDAK cukup.")
        sys.exit(1)
    print(f"repo-guard: {len(names)} file lolos.")


if __name__ == "__main__":
    main()