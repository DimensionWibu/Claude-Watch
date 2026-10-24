# Aturan repo (ketat)

Repo ini **hanya** berisi source code dan panduan instalasi. Tidak ada pengecualian.

## Boleh masuk repo
Hanya file yang terdaftar di `.gitignore` (daftar putih) **dan** `ALLOWED_FILES` di `scripts/check-repo.py`:
source (`*.py`, `index.html`, `*.ps1`), `config.example.json`, `README.md`, `REPO-RULES.md`, serta berkas penjaga
(`scripts/`, `.githooks/`, `.github/workflows/`). Menambah file = mengubah kedua daftar itu lewat review.

## Dilarang keras (tidak boleh ada di file, commit, message, issue, maupun PR)
- **PIN**, password, token, kunci API/privat, hash atau salt PIN, isi `config.json`, `agent.key`.
- Data pribadi: nama user Windows / path profil, email, hostname/domain pribadi, IP privat, nama mesin.
- Data runtime: transkrip/riwayat sesi Claude, tangkapan layar, `*.log`, audit log, `__pycache__`, file sementara.
- File uji/scratch/eksperimen, binary, hasil build, cadangan.
- PIN **tidak pernah ditulis literal**: di dokumentasi selalu `<PIN>`.

## Penegakan
1. `.gitignore` memakai daftar putih (`*` lalu `!file`). Jangan `git add -f`.
2. Hook pre-commit: aktifkan sekali per clone dengan `git config core.hooksPath .githooks`.
3. CI `repo-guard` menjalankan `python scripts/check-repo.py --all` pada setiap push/PR.
4. Berkas lokal `.repo-deny.local` (diabaikan git, tidak pernah di-commit) berisi kata pribadi yang dilarang
   (PIN, nama user, domain, hostname) satu per baris; hook ikut menolak commit yang memuatnya.

## Bila rahasia terlanjur ter-push
Anggap sudah bocor: ganti PIN (`python setpin.py <PIN>`) dan cabut token terkait **terlebih dahulu**, baru bersihkan history
(`git filter-repo`) dan paksa-push. Menghapus di commit baru tidak menghilangkan rahasia dari history.