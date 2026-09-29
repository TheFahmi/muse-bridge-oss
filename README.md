# Muse Bridge

Endpoint **OpenAI-compatible** yang request-nya dijawab **live oleh Muse** (agent kamu), bukan oleh provider API lain. Dibuat untuk dipakai sebagai provider di [9Router](https://github.com/decolua/9router), tapi bisa juga dipakai langsung oleh aplikasi OpenAI-compatible apa pun.

```
App/klien --(OpenAI API)--> bridge.py :8765 --queue--> worker (agent Muse)
                                                     menjawab --> response
```

## Fitur

- **OpenAI-compatible** — `/v1/models`, `/v1/chat/completions` (termasuk SSE streaming).
- **Multi-account** — 1 akun Muse = 1 API key (`muse_sk_...`), masing-masing dengan kuota sendiri.
- **Kuota real per akun** — reporter kecil (`report_quota.py`) jalan di mesin tempat tiap akun login, lapor subscription quota asli tiap 30 menit. Key di-block (429) kalau kuota manual habis **atau** weekly limit real akunnya sudah 100%.
- **Dashboard admin** (`/admin/`) — kelola API key, lihat kuota real tiap akun, salin prompt reporter otomatis, ganti password admin.
- **Landing page** (`/`) — dokumentasi + contoh curl, base URL otomatis ngikutin domain.
- **Push trigger (opsional)** — bridge bisa POST ke worker supaya bangun instan; kalau tidak diset, worker cukup polling `queue/pending/`.

## Prasyarat

- VPS Ubuntu (akses sudo), Python 3.8+ (stdlib saja, tanpa dependency).
- Node.js 18+ — hanya untuk build dashboard (sekali saja).
- Agent Muse yang bisa SSH ke VPS (untuk menjawab antrean).

## Instalasi

```bash
sudo mkdir -p /home/ubuntu/muse-bridge/queue/pending /home/ubuntu/muse-bridge/queue/done
sudo cp bridge.py landing.html report_quota.py /home/ubuntu/muse-bridge/
sudo chown -R ubuntu:ubuntu /home/ubuntu/muse-bridge
sudo cp muse-bridge.service /etc/systemd/system/muse-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now muse-bridge

# Verifikasi:
curl -s http://127.0.0.1:8765/health     # {"ok": true}
```

**Password admin** dibuat otomatis saat pertama jalan — lihat di log:

```bash
sudo journalctl -u muse-bridge | grep -A1 "Generated admin password"
```

Lalu buka `http://<vps>:8765/admin/` (atau domain publikmu `/admin/`) dan login.

### Build dashboard

Dashboard ditulis dengan Next.js (static export), jadi perlu di-build sekali tiap ada perubahan source:

```bash
cd admin-next
npm install
./deploy.sh   # build + upload out/ ke /home/ubuntu/muse-bridge/admin-next/out
```

`deploy.sh` mengasumsikan akses SSH ke VPS. Kalau deploy manual: `npm run build`, lalu copy isi `out/` ke `/home/ubuntu/muse-bridge/admin-next/out`.

> Bridge menyajikan `/admin/*` dari `admin-next/out`. Kalau folder itu belum ada, otomatis fallback ke `admin.html` lama (kalau ada).

## Daftarkan sebagai provider 9Router

1. Tambah provider → tipe **OpenAI-compatible chat**. Nama: `Muse`, Base URL: `http://127.0.0.1:8765/v1`, prefix: `muse`.
2. Isi API key dengan salah satu key dari dashboard (`/admin/`).
3. **Test Connection** — bridge menjawab probe dashboard (`"hi"`) secara instan, jadi harus langsung hijau.
4. Buat combo `muse` berisi model `muse/muse`.

## Worker: yang menjawab antrean

Bridge **tidak** menjawab sendiri — ia mengantrekan request sebagai file di `queue/pending/<id>.json` dan menunggu jawaban di `queue/done/<id>.json` maksimal 240 detik. Yang menjawab adalah agent Muse (cron):

**Mode polling (default):** cron tiap N detik: list `queue/pending/`, jawab tiap request, tulis `{"content": "..."}` ke `queue/done/<id>.json`, hapus file pending. Request berumur > 220 detik dilewati (bridge membersihkannya sendiri).

**Mode push (opsional):** set `PUSH_URL` — bridge POST `{"id": "<request-id>"}` (Bearer `push_secret`) ke worker setiap ada request baru, jadi worker bangun dalam hitungan detik, bukan menunggu interval cron.

## Multi-account & reporter kuota

1. Di dashboard `/admin/` → **+ Tambah**: isi nama akun + kuota manual → API key dibuat, dan **prompt reporter otomatis disalin**.
2. Di mesin tempat akun Muse itu login, tempel prompt ke agent (atau manual):
   ```bash
   curl -sO https://<domain-kamu>/admin/report_quota.py
   API_KEY=muse_sk_... python3 report_quota.py   # harus: reported: 200
   # cron tiap 30 menit:
   */30 * * * * API_KEY=muse_sk_... /usr/bin/python3 /path/to/report_quota.py
   ```
3. Dashboard menampilkan kuota real tiap akun. Data > 3 jam dianggap **DATA BASI** (enforcement fail-open).

## Konfigurasi

| Variable | Default | Keterangan |
|---|---|---|
| `PUSH_URL` | _(kosong)_ | Endpoint push trigger worker. Kosong = mode polling. |
| `PUBLIC_BASE_URL` | _(dari Host header)_ | Base URL publik untuk landing page & snippet. |

File-file penting di `/home/ubuntu/muse-bridge/`:

| File | Keterangan |
|---|---|
| `api_keys.json` | API key per akun (`0600`) |
| `admin_secret` | Password dashboard (`0600`, dibuat otomatis) |
| `push_secret` | Bearer token untuk push trigger (`0600`, opsional) |
| `real_quotas/<id>.json` | Kuota real per akun dari reporter |
| `queue/pending/`, `queue/done/` | Antrean request |

## Endpoint

| Method | Path | Auth | Keterangan |
|---|---|---|---|
| GET | `/health` | — | `{"ok": true}` |
| GET/POST | `/v1/models`, `/v1/chat/completions` | API key | OpenAI-compatible |
| GET | `/v1/quota` | API key | Kuota key pemanggil |
| GET/POST | `/v1/real-quota` | API key | Kuota real akun pemanggil |
| GET | `/admin/` | password | Dashboard |
| * | `/admin/api/*` | password | CRUD key, ganti password |
| GET | `/admin/report_quota.py` | — | Download reporter |

## Troubleshooting

| Gejala | Penyebab umum |
|---|---|
| Dashboard "Test Connection" gagal | Bridge tidak jalan / port salah |
| HTTP 504 "did not answer in time" | Worker tidak jalan atau SSH gagal |
| HTTP 429 "bridge busy" | Antrean penuh (5 pending) |
| Kartu akun "DATA BASI" | Reporter di mesin akun itu tidak jalan > 3 jam |

## Lisensi

MIT — lihat `LICENSE`.
