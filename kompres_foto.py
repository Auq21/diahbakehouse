"""
Kecilkan foto galeri supaya website tetap cepat.

Cara pakai (dari folder diahbakehouse, venv aktif):
    python kompres_foto.py

- Ambil semua foto dari folder  foto_asli/
- Putar otomatis sesuai orientasi HP, perkecil sisi terpanjang jadi 1400 px
- Simpan sebagai JPG kualitas 82 ke  static/galeri/  (biasanya 150–350 KB per foto)
"""
import os, re, sys
from PIL import Image, ImageOps

ASAL, TUJUAN, MAKS, KUALITAS = 'foto_asli', os.path.join('static', 'galeri'), 1400, 82
EKS = ('.jpg', '.jpeg', '.png', '.webp', '.heic')

def slug(nama):
    nama = re.sub(r'[^\w\s-]', '', nama.lower()).strip()
    return re.sub(r'[\s_]+', '-', nama) or 'foto'

def main():
    if not os.path.isdir(ASAL):
        sys.exit(f'Folder {ASAL}/ tidak ada. Buat dulu lalu isi dengan foto.')
    os.makedirs(TUJUAN, exist_ok=True)
    files = sorted(f for f in os.listdir(ASAL) if f.lower().endswith(EKS))
    if not files:
        sys.exit(f'Tidak ada foto di {ASAL}/')
    tanpa_nomor = 90  # foto tanpa nomor urut ditaruh di akhir: 90-, 91-, ...
    for f in files:
        dasar = os.path.splitext(f)[0]
        if not re.match(r'^\d+', dasar):
            dasar = f'{tanpa_nomor:02d}-{dasar}'
            tanpa_nomor += 1
        keluar = os.path.join(TUJUAN, slug(dasar) + '.jpg')
        try:
            with Image.open(os.path.join(ASAL, f)) as im:
                im = ImageOps.exif_transpose(im).convert('RGB')
                im.thumbnail((MAKS, MAKS), Image.LANCZOS)
                im.save(keluar, 'JPEG', quality=KUALITAS, optimize=True, progressive=True)
            print(f'✓ {f}  →  {keluar}  ({os.path.getsize(keluar)//1024} KB)')
        except Exception as e:
            print(f'✗ {f}: {e}  (foto .heic dari iPhone: ubah dulu ke JPG)')
    print('\nSelesai. Cek folder static/galeri lalu git add + push.')

if __name__ == '__main__':
    main()
