# Custom LLM Router (FastAPI + Supabase)

Custom LLM Router v2.0 adalah **Production-Grade LLM Gateway API** berbasis FastAPI (Python) yang terintegrasi dengan Supabase untuk mengelola multi-account API keys, rotasi kunci otomatis (*key rotation*), pemulihan rate limit (*auto-cooldown*), penanganan kunci mati (*auto-blacklist*), *circuit breaker*, enkripsi kunci *at-rest*, multi-tenancy, dan *fallback chain* antarlayanan (Gemini, Groq, OpenRouter, DashScope/Qwen, Mistral, OpenAI, Moonshot).

---

## 🚀 Fitur Utama

- **Unified OpenAI-Compatible Endpoint**: Endpoint tunggal standar OpenAI (`POST /v1/chat/completions` & `GET /v1/models`).
- **Multi-Account & Multi-Platform**: Mengelola API key untuk Gemini, Groq, OpenRouter, DashScope/Qwen, OpenAI, Moonshot, Cerebras, dan Mistral.
- **In-Memory Key Pool Cache**: Caching lokal tanpa DB round-trip pada setiap request, meningkatkan performa hingga 10x.
- **Per-Provider Circuit Breaker**: State machine (`CLOSED` → `OPEN` → `HALF-OPEN`) untuk mencegah panggilan berulang ke provider yang sedang *outage*.
- **Automatic Retry + Exponential Backoff**: Retry otomatis untuk error jaringan sementara (*transient error*) dengan delay bertahap (`0.5s`, `1s`, `2s`).
- **At-Rest Fernet Encryption**: Enkripsi simetris (AES-128-CBC + HMAC) untuk mengamankan API key di database Supabase.
- **Multi-Tenant Authentication**: Setiap klien dapat memiliki API Key sendiri (`ck_...`) dengan batasan model (*model whitelist*) dan kuota token harian.
- **Semantic Multimodal Routing**: Otomatis mendeteksi request dengan gambar (`image_url`) dan mengarahkannya ke model yang mendukung vision.
- **End-to-End Tracing (`X-Request-ID`)**: Setiap request dilengkapi ID unik untuk tracking dan audit log.
- **Per-Request Timeout Override**: Klien dapat menentukan timeout khusus via header `X-Request-Timeout` atau body request.
- **Structured JSON Logging**: Log berformat JSON standar produksi untuk memudahkan agregasi (Datadog, Loki, CloudWatch).
- **Virtual Model Combos**: Menyediakan preset rantai fallback cerdas untuk use case spesifik (*anti-limit*, *chatbot hemat*, *coding*).

---

## 📚 Dokumentasi Lengkap

- [Arsitektur & Alur Kerja v2.0](file:///Users/wahyutricahya/Work/llm-router/docs/architectur.md)
- [Dokumen Rincian Fitur v2.0](file:///Users/wahyutricahya/Work/llm-router/completion.md)
- [API Reference](file:///Users/wahyutricahya/Work/llm-router/docs/api_reference.md)
- [Panduan Deployment](file:///Users/wahyutricahya/Work/llm-router/docs/deployment.md)

---

## 🎯 Model Virtual & Combo Preset

| Model Virtual | Deskripsi & Fallback Chain | Use Case |
| :--- | :--- | :--- |
| **`combo-anti-limit`** | **Chain 5 Provider**: `dashscope` (DeepSeek v4.1) ➔ `gemini` (Gemini 2.0 Flash) ➔ `groq` (Llama 3.3 70B) ➔ `dashscope` (Qwen 3.7 Flash) ➔ `openrouter` | General High Availability |
| **`combo-chatbot-cheap`**<br>`combo-chatbot-hemat` | **Chain Super Hemat**: `dashscope` (Qwen 3.5 Flash) ➔ `gemini` (Gemini 2.0 Flash) ➔ `groq` (Llama 3.1 8B) ➔ `dashscope` (Qwen 3.6 Flash) ➔ `openrouter` | Chatbot Volume Tinggi |
| **`combo-coding-antilimit`**<br>`combo-coding` | **Chain Khusus Coding**: `dashscope` (Qwen 3 Coder Plus) ➔ `dashscope` (Qwen 3 Coder Flash) ➔ `dashscope` (DeepSeek v4.1) ➔ `openrouter` (Claude 3.5 Sonnet) ➔ `groq` (Llama 3.3 70B) | Coding Assistant & IDE |
| **`combo-smart`** | **Smart Quality Chain**: `openrouter` (Claude 3.5 Sonnet) ➔ `gemini` (Gemini 2.5 Pro) | Complex Reasoning |
| **`combo-fast`** | **Fast Response Chain**: `groq` (Llama 3.3 70B) ➔ `gemini` (Gemini 2.0 Flash) | Ultra Low Latency |

---

## 🛠️ Langkah Setup Database (Supabase)

1. Buka dashboard proyek **Supabase** Anda.
2. Navigasi ke menu **SQL Editor** lalu buat query baru.
3. Salin dan jalankan seluruh isi skema dari berkas [supabase_schema.sql](supabase_schema.sql).
4. Masukkan API Key provider Anda ke tabel `provider_keys`:

```sql
INSERT INTO provider_keys (provider, key_name, key_value, priority)
VALUES
    ('dashscope', 'DashScope Utama', 'sk-dashscope-key...', 1),
    ('gemini', 'Gemini Utama', 'AIzaSy_Gemini_Key_1...', 1),
    ('groq', 'Groq Utama', 'gsk_Groq_Key_1...', 1),
    ('openrouter', 'OpenRouter Utama', 'sk-or-v1-Key_1...', 1);
```

---

## 💻 Cara Menjalankan secara Lokal

### 1. Buat Virtual Environment & Instal Dependensi
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Konfigurasikan File `.env`
Salin `.env.example` menjadi `.env`:
```env
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_KEY=your-supabase-key
PORT=8000
ROUTER_API_KEY=your-admin-secret-key

# Optional Multi-Tenancy & Encryption
MULTI_TENANT=false
ENCRYPTION_KEY=
```

### 3. Jalankan Server
```bash
python -m src.main
```
Server akan berjalan secara lokal di: `http://localhost:8000`

---

## 🔌 Integration Guide (VS Code, Continue, Chatbot)

### 1. Chatbot & Aplikasi Custom (Python)
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="<ROUTER_API_KEY Anda>",
)

completion = client.chat.completions.create(
    model="combo-chatbot-cheap",
    messages=[{"role": "user", "content": "Halo! Ada yang bisa dibantu?"}],
)
print(completion.choices[0].message.content)
```

### 2. VS Code (Cline / Roo Code)
- **API Provider**: `OpenAI Compatible`
- **Base URL**: `http://localhost:8000/v1`
- **API Key**: `<ROUTER_API_KEY Anda>`
- **Model ID**: `combo-coding-antilimit` atau `combo-anti-limit`

---

## ☁️ Deployment

Proyek ini dilengkapi dengan berkas blueprint Render [render.yaml](render.yaml) dan mendukung deployment ke Vercel atau persistent container.
