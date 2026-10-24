"""Ganti PIN akses remote: python setpin.py <PIN>  (disimpan sebagai hash di config.json; service membaca ulang otomatis)."""
import sys
import auth

if len(sys.argv) != 2 or not (4 <= len(sys.argv[1]) <= 64):
    sys.exit("pakai: python setpin.py <PIN 4-64 karakter>   (disarankan >= 8 digit bila dibuka ke internet)")
auth.set_pin(sys.argv[1])
print("PIN diperbarui. Sesi login yang sedang aktif tetap berlaku sampai habis; restart service untuk mencabut semuanya.")
