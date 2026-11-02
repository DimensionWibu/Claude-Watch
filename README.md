# Claude Watch

Monitor dan remote-control sesi **Claude Code** lewat web (Windows). Satu service Windows kecil (Python, tanpa dependensi
web) yang menampilkan transkrip realtime semua sesi, status busy/idle yang akurat, chat langsung ke sesi yang sedang
berjalan, layar terminal live, serta Start/Stop/Resume/Fork sesi — dari browser di PC sendiri, LAN, atau lewat tunnel.

## Persyaratan
- Windows 10/11, **Python 3.8+ instalasi penuh** dari python.org (bukan stub Microsoft Store), `py`/`python` ada di PATH.
- Claude Code CLI terpasang dan sudah dipakai sekali (folder `~\.claude` ada).
- Hak admin (UAC) untuk memasang service satu kali.

## Instalasi
```powershell
git clone https://github.com/DimensionWibu/Claude-Watch.git
cd Claude-Watch
.\install.ps1
```
Installer (satu prompt UAC) akan: memasang `pywin32` ke Python sistem, membuat `config.json` lokal (berisi folder data Claude
milik **akun yang menjalankan installer**), mendaftarkan service `ClaudeWatch` (otomatis start tiap boot, jalan sebagai
SYSTEM), membuka firewall port 64000 **hanya untuk subnet lokal**, memberi akun Anda hak start/stop/restart service tanpa
UAC, dan membuat auto-buka browser saat login (`-NoAutoOpen` untuk menonaktifkan).

Lalu buka <http://127.0.0.1:64000>. Dari PC ini tidak perlu PIN.

## PIN untuk akses dari luar PC
Akses dari perangkat lain (LAN / tunnel) **ditolak sampai PIN diset**:
```powershell
python setpin.py <PIN>
```
Gunakan PIN sepanjang mungkin (disarankan 8 digit atau lebih bila dibuka ke internet). PIN disimpan sebagai hash PBKDF2 di
`config.json` lokal; sesi login berlaku 60 menit (`sessionMinutes`). Percobaan salah dibatasi: 5 per 10 menit per IP
(kunci 15 menit) dan 20 per 10 menit secara global (kunci 10 menit).

## Cloudflare Tunnel (opsional)
1. Tambahkan route tunnel ke `http://127.0.0.1:64000` (jangan ubah "HTTP Host Header").
2. Tambahkan hostname publik ke `config.json`: `"allowedHosts": ["nama.domainanda.com"]`.
3. **Sangat disarankan** memasang Cloudflare Access (login email/OTP) di depan hostname itu sebagai lapis kedua.

Trafik yang membawa header proxy dianggap bukan lokal, jadi selalu kena PIN.

## Penggunaan
- Tab **Transkrip**: aliran event + filter per kolom. **Chat**: kirim pesan ke sesi yang berjalan. **Terminal**: layar live
  dengan keyboard langsung (desktop dan HP). Alt+1/2/3 ganti tab.
- **Auto-fit**: area terminal di web diukur (kolom x baris) lalu jendela cmd di PC otomatis menyesuaikan, sehingga tidak perlu mengubah ukuran manual (desktop maupun HP). Ukuran asli dikembalikan saat tab web ditutup, atau lewat tombol *Ukuran asli*. Hanya untuk jendela cmd/conhost biasa (Windows Terminal ukurannya ikut jendelanya sendiri). Dibatasi ukuran terbesar yang muat di layar PC.
- Sesi yang tidak berjalan menampilkan panel **Start** (Resume atau Fork). Tombol **Stop** menutup proses sesi.
- Tautan langsung ke satu sesi: `/?s=<session-id>&tab=chat`.

## Pilih program: Claude atau Clauseek
Di bilah atas ada pilihan program untuk **Resume/Fork**: *Claude* (Claude Code asli) atau *Clauseek* (engine Claude Code
yang sama, tetapi seluruh request dialihkan ke DeepSeek lewat launcher `clauseek.cmd` di `~\.local\bin`). Fork sesi Claude
dengan Clauseek = melanjutkan obrolan yang sama memakai DeepSeek (isi transkrip ikut dikirim ke DeepSeek). Agar sesi
Clauseek tampil di sini, folder `projects` dan `sessions` milik Clauseek harus berupa junction ke `~\.claude` (dibuat
installer Clauseek). Tanpa `clauseek.cmd`, memilih Clauseek ditolak (tidak pernah jatuh diam-diam ke Claude).

## PIN sementara per sesi (API)
`POST /api/remote-control/enable {"session": "<id>", "minutes": 10}` (hanya diterima dari PC ini) membuat PIN 8 digit
**sekali pakai** (default 10 menit, maks 60) yang hanya membuka **satu sesi**: daftar sesi, stream, layar, kirim
teks/tombol, resize, dan Stop untuk sesi lain ditolak (403), dan Start/Resume/Fork dinonaktifkan. PIN utama
(`setpin.py`) tidak terpengaruh. Percobaan salah PIN sementara dihitung ke batas lockout yang sama.
(Perintah `/remote-control` milik Clauseek **tidak** memakai ini: ia punya halaman web mandiri per sesi.)

## Impor transkrip dari mesin lain (mis. newdeepseek)
Transkrip format Codex "rollout" (`<CODEX_HOME>\sessions\YYYY\MM\DD\rollout-*.jsonl`) bisa dipilih di daftar dan dilihat
(label `⇩ <nama>`), lalu di-**Resume/Fork** dengan **Claude atau Clauseek**. Daftarkan sumbernya di `config.json` lokal:
```json
"importSources": [{ "name": "nama-sumber", "kind": "codex", "home": "<folder CODEX_HOME>" }]
```
Cara kerja: Resume/Fork mengonversi rollout menjadi sesi Claude Code baru di `projects\` (percakapan dimuat sebagai teks;
panggilan tool dilipat jadi ringkasan; hanya giliran terakhir ±120 ribu karakter; injeksi sistem seperti AGENTS.md
dibuang), lalu membuka sesi itu. *Resume* mengimpor **sekali** (id tetap; berikutnya melanjutkan hasil impor yang sudah
berkembang), *Fork* selalu membuat salinan baru dari kondisi sumber saat ini. Sumber aslinya tidak diubah. Melanjutkan
dengan **Claude** mengirim isi percakapan ke Anthropic (ada konfirmasi); dengan **Clauseek** ke DeepSeek.

## Operasional
- Setelah mengubah file `*.py`: `Restart-Service ClaudeWatch` (tanpa UAC). `index.html` dibaca ulang di tiap request.
- Log: `service.log`, audit aksi remote: `remote-audit.log` (tidak berisi isi ketikan).
- Hapus: `.\uninstall.ps1` (`config.json` tidak dihapus).

## Struktur
`server.py` (HTTP/SSE) · `auth.py` (PIN + sesi) · `remote.py` (registry sesi, kontrol, audit) · `agent.py` + `consolectl.py`
(proses di sesi login user yang menyuntik input ke console `claude.exe`) · `spawn.py` (membuka cmd di desktop user) ·
`service.py` (service Windows) · `settings.py` · `setpin.py` · `importer.py` (impor transkrip Codex) · `index.html` (UI).

## Keamanan
Mengontrol sesi berarti dapat menyuruh Claude menjalankan perintah di PC Anda. Jangan buka ke internet tanpa PIN kuat dan
sebaiknya Cloudflare Access. Repo ini hanya berisi source code dan panduan ini; aturan ketatnya ada di
[REPO-RULES.md](REPO-RULES.md).