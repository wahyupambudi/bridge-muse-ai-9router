# Deploy muse-bridge ke Railway

Bridge ini HARUS jalan di tempat yang bisa diakses publik (Railway),
karena 9router kamu juga di Railway dan sandbox VPS tidak bisa
menerima koneksi inbound (tunnel pinggy diblokir proxy).

## Cara deploy

1. Buat service baru di Railway project yang sama dengan 9router
   (atau project baru) dari folder `muse-bridge/` ini.
   - Build: Python terdeteksi otomatis via `requirements.txt`
   - Start command: `python3 bridge.py` (sudah di `Procfile`)
2. Set environment variables di Railway:
   - `BRIDGE_API_KEY` = string acak yang panjang (ini API key yang
     nanti dimasukkan ke 9router sebagai provider)
   - `BRIDGE_TIMEOUT` = `100` (opsional, default 100 detik)
   - `BRIDGE_MODEL` = `koda/muse` (opsional)
   - `PORT` diisi otomatis oleh Railway — jangan di-set manual.
3. Generate public domain untuk service ini di Railway
   (Settings → Networking → Generate Domain).
   Catat URL-nya, mis. `https://muse-bridge-production-xxxx.up.railway.app`

## Daftarkan ke 9router

Di dashboard 9router (https://9router-production-51af.up.railway.app/),
tambah provider node baru:
- Name: `Koda`, Prefix: `koda`
- Type: OpenAI-compatible / Custom
- Base URL: `https://<bridge-domain>/v1`
- API Key: isi `BRIDGE_API_KEY` yang tadi
- Model: `koda/muse`

Tes: `curl https://<bridge-domain>/v1/models -H "Authorization: Bearer <BRIDGE_API_KEY>"`
harus mengembalikan `{"object":"list","data":[{"id":"koda/muse",...}]}`.

## Setelah deploy

Kasih URL bridge + API key ke subali (API key lewat form aman),
lalu subali akan:
1. Menyimpan API key bridge di Secure Vault
2. Memasang cron poller yang tiap interval mengecek
   `GET /internal/queue` dan menjawab via `POST /internal/respond`

Alur chat: kamu → 9router → bridge (tahan koneksi s/d 100 dtk)
→ subali ambil dari antrean → jawab → 9router → kamu.
