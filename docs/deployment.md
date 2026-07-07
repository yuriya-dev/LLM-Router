# 🚀 Panduan Deployment ke Render

Dokumen ini menjelaskan langkah-langkah lengkap untuk men-deploy **LLM Router** ke [Render.com](https://render.com) sebagai *Web Service* yang berjalan persisten.

---

## Prasyarat

Sebelum memulai, pastikan Anda sudah memiliki:

- [ ] Akun [Render.com](https://render.com) (tier Free sudah cukup untuk testing)
- [ ] Akun [Supabase](https://supabase.com) dengan project yang sudah dibuat
- [ ] Repository kode ini sudah di-push ke GitHub / GitLab
- [ ] Minimal satu API key provider (Gemini, Groq, atau OpenRouter)

---

## Langkah 1 — Siapkan Database Supabase

### 1.1 Jalankan Schema SQL

Buka **Supabase Dashboard** → pilih project Anda → buka **SQL Editor**, lalu jalankan seluruh isi file [`supabase_schema.sql`](../supabase_schema.sql).

Script tersebut akan membuat:
- Tabel `provider_keys` — menyimpan API key provider dan statusnya
- Tabel `request_logs` — mencatat riwayat setiap request
- Tabel `model_routes` — konfigurasi routing model dinamis
- Function `increment_error_count()` — RPC untuk update error count secara atomic
- Index dan trigger yang diperlukan

> **Penting:** Jalankan script ini **sekali saja**. Jika dijalankan ulang, statement `IF NOT EXISTS` akan mencegah duplikasi tabel, namun `INSERT ... ON CONFLICT DO NOTHING` pada seed data juga aman diulang.

### 1.2 Ambil Kredensial Supabase

Di Supabase Dashboard → **Project Settings** → **API**, catat dua nilai berikut:

| Nilai | Digunakan untuk |
|-------|----------------|
| **Project URL** | `SUPABASE_URL` |
| **`service_role` key** (bukan `anon`) | `SUPABASE_KEY` |

> **Perhatian:** Gunakan key `service_role`, bukan `anon`. Key `anon` memiliki Row Level Security (RLS) yang akan memblokir operasi tulis dari backend.

### 1.3 Tambahkan API Key Provider (Opsional via SQL)

Anda bisa langsung menambahkan key via SQL Editor sebelum deploy:

```sql
INSERT INTO provider_keys (provider, key_name, key_value, priority)
VALUES
  ('gemini',     'Gemini Key 1',     'AIza...', 1),
  ('groq',       'Groq Key 1',       'gsk_...', 1),
  ('openrouter', 'OpenRouter Key 1', 'sk-or-...', 1);
```

Atau tambahkan setelah deploy lewat [Admin API](#admin-api-reference).

---

## Langkah 2 — Deploy ke Render

Ada dua cara deploy: otomatis via `render.yaml` atau manual via dashboard.

### Cara A: Deploy via `render.yaml` ✅ Direkomendasikan

File [`render.yaml`](../render.yaml) sudah dikonfigurasi. Cukup:

1. Buka [dashboard.render.com](https://dashboard.render.com)
2. Klik **New** → **Blueprint**
3. Hubungkan repository GitHub/GitLab Anda
4. Render otomatis mendeteksi `render.yaml` dan membuat service
5. **Isi environment variables** yang belum ada nilainya (lihat [Langkah 3](#langkah-3--konfigurasi-environment-variables))
6. Klik **Apply**

### Cara B: Deploy Manual via Dashboard

1. Buka [dashboard.render.com](https://dashboard.render.com)
2. Klik **New** → **Web Service**
3. Hubungkan repository Anda
4. Isi konfigurasi berikut:

| Field | Nilai |
|-------|-------|
| **Name** | `llm-router` (atau nama lain) |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn src.main:app --host 0.0.0.0 --port $PORT` |
| **Instance Type** | Free (atau Starter untuk production) |

5. Lanjut ke Langkah 3 untuk mengisi env vars.

---

## Langkah 3 — Konfigurasi Environment Variables

Di dashboard Render → service Anda → **Environment**, tambahkan semua variabel berikut:

### Wajib

| Variable | Nilai | Keterangan |
|----------|-------|------------|
| `SUPABASE_URL` | `https://xxxx.supabase.co` | Project URL dari Supabase |
| `SUPABASE_KEY` | `eyJhb...` | Service role key dari Supabase |

### Sangat Dianjurkan

| Variable | Contoh Nilai | Keterangan |
|----------|-------------|------------|
| `ROUTER_API_KEY` | `my-secret-router-key-123` | Kunci untuk proteksi endpoint router. Jika tidak diisi, endpoint terbuka untuk umum. |

### Opsional (sudah ada default)

| Variable | Default | Keterangan |
|----------|---------|------------|
| `RATE_LIMIT_RPM` | `60` | Maksimum request per menit per API key/IP |
| `RATE_LIMIT_BURST` | `10` | Maksimum burst request per detik |
| `COOLDOWN_RATE_LIMIT_SECS` | `60` | Durasi cooldown key saat terkena rate limit (HTTP 429) |
| `COOLDOWN_NETWORK_ERROR_SECS` | `15` | Durasi cooldown key saat error jaringan/server |

> **Tips:** Untuk production dengan traffic tinggi, pertimbangkan menaikkan `RATE_LIMIT_RPM` ke `120` atau lebih, dan turunkan `COOLDOWN_RATE_LIMIT_SECS` ke `30` jika Anda punya banyak key tersedia untuk rotasi.

---

## Langkah 4 — Verifikasi Deployment

Setelah deploy selesai, Render akan memberikan URL publik seperti `https://llm-router-xxxx.onrender.com`.

### Cek Health Endpoint

```bash
curl https://llm-router-xxxx.onrender.com/health
```

Response yang diharapkan (jika semua key sehat):
```json
{
  "status": "healthy",
  "timestamp": 1720000000.0,
  "db_connected": true,
  "key_pool": {
    "gemini":     {"healthy": 2, "cooldown": 0, "dead": 0},
    "groq":       {"healthy": 1, "cooldown": 0, "dead": 0},
    "openrouter": {"healthy": 3, "cooldown": 0, "dead": 0}
  }
}
```

Jika `"status": "degraded"`, artinya koneksi DB berhasil tapi belum ada key yang ditambahkan.

### Cek Daftar Model

```bash
curl https://llm-router-xxxx.onrender.com/v1/models \
  -H "Authorization: Bearer YOUR_ROUTER_API_KEY"
```

### Tambah API Key Provider via Admin API

```bash
curl -X POST https://llm-router-xxxx.onrender.com/admin/keys \
  -H "Authorization: Bearer YOUR_ROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "gemini",
    "key_name": "Gemini Key 1",
    "key_value": "AIza...",
    "priority": 1
  }'
```

### Test Chat Completion

```bash
curl -X POST https://llm-router-xxxx.onrender.com/v1/chat/completions \
  -H "Authorization: Bearer YOUR_ROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "combo-fast",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## Langkah 5 — Koneksi dari Aplikasi Klien

Router ini kompatibel dengan OpenAI API, sehingga bisa digunakan di semua tool yang mendukung custom OpenAI endpoint.

### Continue (VS Code Extension)

Di file `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "Router - Smart",
      "provider": "openai",
      "model": "combo-smart",
      "apiBase": "https://llm-router-xxxx.onrender.com/v1",
      "apiKey": "YOUR_ROUTER_API_KEY"
    },
    {
      "title": "Router - Fast",
      "provider": "openai",
      "model": "combo-fast",
      "apiBase": "https://llm-router-xxxx.onrender.com/v1",
      "apiKey": "YOUR_ROUTER_API_KEY"
    }
  ]
}
```

### Cline / Roo Code (VS Code Extension)

Di settings extension, isi:
- **API Provider**: `OpenAI Compatible`
- **Base URL**: `https://llm-router-xxxx.onrender.com/v1`
- **API Key**: `YOUR_ROUTER_API_KEY`
- **Model**: `combo-smart` atau `combo-fast`

### Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://llm-router-xxxx.onrender.com/v1",
    api_key="YOUR_ROUTER_API_KEY",
)

response = client.chat.completions.create(
    model="combo-smart",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

---

## Admin API Reference

Semua endpoint admin membutuhkan header `Authorization: Bearer YOUR_ROUTER_API_KEY`.

| Method | Endpoint | Fungsi |
|--------|----------|--------|
| `GET` | `/admin/status` | Snapshot kesehatan pool key per provider |
| `GET` | `/admin/keys` | List semua API key dan statusnya |
| `POST` | `/admin/keys` | Tambah API key baru |
| `PATCH` | `/admin/keys/{id}/reset` | Reset key ke status `healthy` |
| `DELETE` | `/admin/keys/{id}` | Hapus key secara permanen |
| `GET` | `/admin/stats?hours=24` | Statistik penggunaan per provider/model |
| `GET` | `/admin/routes` | List semua routing rule dari DB |
| `POST` | `/admin/routes` | Tambah routing rule baru |
| `POST` | `/admin/routes/refresh` | Force reload cache routing dari DB |

### Contoh: Lihat statistik 7 hari terakhir

```bash
curl "https://llm-router-xxxx.onrender.com/admin/stats?hours=168" \
  -H "Authorization: Bearer YOUR_ROUTER_API_KEY"
```

### Contoh: Reset key yang mati

```bash
# 1. Lihat ID key yang dead
curl "https://llm-router-xxxx.onrender.com/admin/keys?status=dead" \
  -H "Authorization: Bearer YOUR_ROUTER_API_KEY"

# 2. Reset ke healthy
curl -X PATCH "https://llm-router-xxxx.onrender.com/admin/keys/UUID-DI-SINI/reset" \
  -H "Authorization: Bearer YOUR_ROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status": "healthy"}'
```

### Contoh: Tambah routing rule baru tanpa deploy ulang

```bash
curl -X POST "https://llm-router-xxxx.onrender.com/admin/routes" \
  -H "Authorization: Bearer YOUR_ROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "virtual_model": "combo-premium",
    "provider": "openrouter",
    "target_model": "anthropic/claude-opus-4",
    "priority": 1
  }'
```

---

## Troubleshooting

### Service tidak mau start

Cek **Logs** di dashboard Render. Error umum:

| Error | Penyebab | Solusi |
|-------|----------|--------|
| `SUPABASE_URL field required` | Env var kosong | Isi di Environment settings |
| `ModuleNotFoundError` | Build gagal | Cek Build Command dan Python version |
| Port error | Start command salah | Pastikan menggunakan `$PORT` bukan port hardcoded |

### Health menunjukkan `"db_connected": false`

- Pastikan `SUPABASE_URL` dan `SUPABASE_KEY` benar
- Pastikan menggunakan key `service_role`, bukan `anon`
- Cek apakah schema SQL sudah dijalankan di Supabase

### Request selalu gagal dengan 503

- Belum ada key provider yang ditambahkan → tambahkan lewat `POST /admin/keys`
- Semua key sedang dalam status `dead` → reset lewat `PATCH /admin/keys/{id}/reset`
- Cek `/admin/status` untuk melihat kondisi pool

### Render Free tier: service tidur setelah 15 menit idle

Render Free tier akan mematikan service jika tidak ada traffic selama 15 menit. Request pertama setelah tidur akan lambat (~30 detik cold start). Untuk menghindari ini:

- Upgrade ke tier **Starter** ($7/bulan) untuk always-on
- Atau gunakan uptime monitor eksternal seperti [UptimeRobot](https://uptimerobot.com) yang melakukan ping ke `/health` setiap 10 menit (gratis)

---

## Update Deployment

Setiap kali Anda push ke branch utama repository, Render akan **otomatis re-deploy** jika auto-deploy aktif.

Untuk update manual:
1. Buka dashboard Render → service Anda
2. Klik **Manual Deploy** → **Deploy latest commit**

> **Catatan:** Routing rule yang disimpan di tabel `model_routes` Supabase **tidak memerlukan re-deploy** untuk berlaku — cache router di-refresh otomatis setiap 60 detik, atau bisa dipaksa via `POST /admin/routes/refresh`.
