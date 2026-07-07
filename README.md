# Custom LLM Router (FastAPI + Supabase)

Custom LLM Router adalah gateway API lokal/cloud berbasis FastAPI (Python) yang terintegrasi dengan Supabase untuk mengelola banyak API key (multi-account) dan platform LLM secara dinamis. Router ini mendukung rotasi kunci otomatis (*key rotation*), pemulihan rate limit (*cooldown mechanism*), penonaktifan kunci mati (*blacklist*), dan fallback antarlayanan (Gemini, Groq, OpenRouter).

## 🚀 Fitur Utama

- **Unified Endpoint**: Menyediakan endpoint tunggal standar OpenAI-compatible (`POST /v1/chat/completions`).
- **Multi-Account & Multi-Platform**: Mengelola banyak API key untuk Gemini, Groq, dan OpenRouter dalam satu database.
- **Smart Rotation & Priority**: Rotasi kunci otomatis berbasis LRU (Least-Recently-Used) dengan penentuan prioritas (*priority tiers*).
- **Auto Cooldown & Self-Healing**: Secara otomatis mendeteksi error rate-limit (HTTP 429) dan meng-cooldown kunci tersebut selama 60 detik sebelum dicoba kembali.
- **Auto Blacklist**: Mendeteksi kunci yang mati (HTTP 401/403) dan menandai statusnya sebagai `dead`.
- **Flexible Fallback Chain**: Rantai fallback otomatis antarlayanan (misal: jika semua kunci Gemini limit/mati, otomatis beralih ke OpenRouter).
- **Streaming Support**: Mendukung respon streaming penuh (`stream: true`).
- **Supabase Connected**: Semua status kunci, penambahan akun, dan statistik panggilan (*request logs*) disimpan terpusat di cloud Supabase.

---

## 🛠️ Langkah Setup Database (Supabase)

1. Buka dashboard proyek **Supabase** Anda.
2. Navigasi ke menu **SQL Editor** lalu buat query baru.
3. Salin dan jalankan seluruh isi skema dari berkas [supabase_schema.sql](supabase_schema.sql).
4. Masukkan API Key Anda ke tabel `provider_keys` melalui **Table Editor** Supabase atau jalankan perintah SQL berikut di SQL Editor:

```sql
INSERT INTO provider_keys (provider, key_name, key_value, priority)
VALUES
    -- Contoh API Key Gemini
    ('gemini', 'Gemini Utama', 'AIzaSy_Gemini_Key_1...', 1),
    ('gemini', 'Gemini Cadangan', 'AIzaSy_Gemini_Key_2...', 2),

    -- Contoh API Key Groq
    ('groq', 'Groq Utama', 'gsk_Groq_Key_1...', 1),

    -- Contoh API Key OpenRouter
    ('openrouter', 'OpenRouter Utama', 'sk-or-v1-Key_1...', 1);
```

---

## 💻 Cara Menjalankan Secara Lokal

### 1. Kloning / Buka Workspace
Pastikan Anda berada di direktori proyek:
```bash
cd /Users/wahyutricahya/Work/9router
```

### 2. Buat Virtual Environment & Instal Dependensi
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Konfigurasikan File `.env`
Salin file `.env.example` menjadi `.env` lalu lengkapi nilai konfigurasi Supabase Anda:
```bash
cp .env.example .env
```
Isi dari `.env`:
```env
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_KEY=your-supabase-anon-key-or-service-role-key
PORT=8000
ROUTER_API_KEY=your-custom-router-api-key # Isi jika ingin mengamankan endpoint router Anda
```

### 4. Jalankan Server
```bash
python -m src.main
```
Server Anda akan berjalan secara lokal di: `http://localhost:8000`

---

## 🔌 Cara Integrasi dengan Aplikasi/Klien Anda

Arahkan aplikasi coding, ekstensi editor, atau klien AI Anda ke endpoint router ini dengan detail berikut:

- **Base URL / Endpoint**: `http://localhost:8000/v1`
- **API Key**: Gunakan token yang Anda isi pada `ROUTER_API_KEY` di file `.env` (isi dengan teks sembarang jika `ROUTER_API_KEY` dikosongkan).
- **Model Pilihan**:
  - `combo-smart` (Fallback: OpenRouter Claude 3.5 Sonnet ➔ Gemini 1.5 Pro)
  - `combo-fast` (Fallback: Groq Llama 3 8B ➔ Gemini 1.5 Flash)
  - `gemini-1.5-pro` (Fallback: Gemini Lokal ➔ OpenRouter Gemini Pro)
  - `gemini-1.5-flash`
  - `claude-3-5-sonnet`
  - `llama3-8b`

### 1. VS Code (Cline / Roo Code / Roo Cline)
1. Buka ekstensi **Cline / Roo Code** di VS Code.
2. Klik ikon **Settings** (ikon roda gigi).
3. Pada dropdown **API Provider**, pilih **OpenAI Compatible**.
4. Masukkan konfigurasi berikut:
   * **Base URL**: `http://localhost:8000/v1`
   * **API Key**: `<ROUTER_API_KEY Anda>` (dari file `.env`)
   * **Model ID**: `combo-smart` atau `combo-fast`

### 2. VS Code (Continue)
1. Buka berkas konfigurasi Continue di folder home Anda (biasanya di `~/.continue/config.json`).
2. Tambahkan entri model baru di dalam array `"models"`:
   ```json
   {
     "models": [
       {
         "title": "Custom Router Smart",
         "provider": "openai",
         "model": "combo-smart",
         "apiBase": "http://localhost:8000/v1",
         "apiKey": "your-router-api-key"
       }
     ]
   }
   ```
3. Simpan berkas tersebut, lalu pilih model **Custom Router Smart** di antarmuka Continue.

### 3. Google Antigravity (AGY CLI / IDE)
Antigravity dirancang untuk terintegrasi secara mendalam dengan model Google Gemini secara langsung. 
- **Model Utama**: Tidak ada setelan bawaan untuk mengalihkan chat asisten utama ke router OpenAI pihak ketiga.
- **Penggunaan dalam Script**: Jika Anda menulis script atau program Python di dalam Antigravity, Anda dapat memanggil router ini dengan library standard `openai` atau `httpx` dengan mengarahkan target API ke `http://localhost:8000/v1`.


---

## ☁️ Cara Deploy ke Render (Production)

Proyek ini telah dilengkapi dengan berkas konfigurasi blueprint Render yaitu [render.yaml](render.yaml).

1. Hubungkan repository GitHub Anda ke **Render**.
2. Buat layanan baru di Render menggunakan pilihan **Blueprints**.
3. Render akan membaca [render.yaml](render.yaml) secara otomatis dan memproses pembuatan Web Service.
4. Jangan lupa untuk mengisi Variabel Lingkungan (*Environment Variables*) di dashboard Render:
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
   - `ROUTER_API_KEY`
