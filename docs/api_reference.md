# API Reference - Custom LLM Router

Dokumen ini menjelaskan secara lengkap seluruh endpoint API yang tersedia pada Custom LLM Router, baik untuk klien (penggunaan umum) maupun untuk administrator (pengelolaan pool kunci API dan rute model).

Entrypoint aplikasi didefinisikan pada [src/main.py](file:///Users/wahyutricahya/Work/llm-router/src/main.py) dan endpoint admin didefinisikan pada [src/routers/admin.py](file:///Users/wahyutricahya/Work/llm-router/src/routers/admin.py).

---

## 🔐 Autentikasi

Beberapa endpoint API dilindungi oleh autentikasi menggunakan **HTTP Bearer Token**. Token ini disesuaikan dengan nilai konfigurasi `ROUTER_API_KEY` pada file konfigurasi/env Anda.

```http
Authorization: Bearer <ROUTER_API_KEY>
```

*   **Endpoint Klien**: Memerlukan header `Authorization` jika `ROUTER_API_KEY` dikonfigurasi.
*   **Endpoint Admin**: Selalu memerlukan `ROUTER_API_KEY` jika dikonfigurasi.

---

## 🚀 Endpoint Klien (Client Endpoints)

### 1. Root Status
Mengembalikan status dasar untuk memastikan service router sedang berjalan.

*   **URL**: `/`
*   **Method**: `GET` / `HEAD`
*   **Autentikasi**: Tidak ada (Bebas akses)
*   **Response Sukses (200 OK)**:
    ```json
    {
      "message": "LLM Router is running. Access /health for pool status."
    }
    ```

---

### 2. Health Check
Mengembalikan status operasional router dan ringkasan kondisi pool kunci provider (apakah terhubung ke database dan jumlah kunci sehat).

*   **URL**: `/health`
*   **Method**: `GET` / `HEAD`
*   **Autentikasi**: Tidak ada (Aman untuk monitoring uptime)
*   **Response Sukses (200 OK)**:
    ```json
    {
      "status": "healthy",
      "timestamp": 1784228178.95,
      "db_connected": true,
      "key_pool": {
        "gemini": {
          "healthy": 3,
          "cooldown": 0,
          "dead": 0
        },
        "groq": {
          "healthy": 1,
          "cooldown": 1,
          "dead": 0
        }
      }
    }
    ```

---

### 3. List Models
Mengembalikan daftar model virtual dan model asli yang didukung oleh router saat ini secara dinamis (dimuat dari database & fallback statis).

*   **URL**: `/v1/models`
*   **Method**: `GET`
*   **Autentikasi**: Memerlukan `verify_api_key`
*   **Response Sukses (200 OK)**:
    ```json
    {
      "object": "list",
      "data": [
        {
          "id": "combo-smart",
          "object": "model",
          "owned_by": "custom-router"
        },
        {
          "id": "gemini-1.5-pro",
          "object": "model",
          "owned_by": "google"
        }
      ]
    }
    ```

---

### 4. Quota and Rate Limits
Mengembalikan informasi limit kuota, sisa kuota, dan waktu reset dari pengguna berdasarkan identitas (Bearer Token atau alamat IP).

*   **URL**: `/v1/quota`
*   **Method**: `GET`
*   **Autentikasi**: Memerlukan `verify_api_key`
*   **Response Sukses (200 OK)**:
    ```json
    {
      "identity": "test-tok...",
      "rate_limit_minute": {
        "limit": 60,
        "remaining": 59,
        "reset_seconds": 58.21,
        "reset_time": 1784228236.46
      },
      "rate_limit_burst_second": {
        "limit": 10,
        "remaining": 9,
        "reset_seconds": 0.85,
        "reset_time": 1784228178.95
      }
    }
    ```

---

### 5. Chat Completions
Endpoint utama yang kompatibel dengan format OpenAI untuk melakukan chat completion dengan fitur fallback otomatis dan rotasi kunci API.

*   **URL**: `/v1/chat/completions`
*   **Method**: `POST`
*   **Autentikasi**: Memerlukan `verify_api_key`
*   **Response Headers** (Ditambahkan secara otomatis untuk memonitor sisa kuota):
    *   `X-RateLimit-Limit`: Batas request per menit.
    *   `X-RateLimit-Remaining`: Sisa request yang dapat dilakukan pada menit berjalan.
    *   `X-RateLimit-Reset`: UNIX timestamp reset kuota.
*   **Body Request**: Format JSON standard OpenAI (Mendukung `"stream": true` dan `"stream_options": {"include_usage": true}`).
    ```json
    {
      "model": "combo-smart",
      "messages": [
        {
          "role": "user",
          "content": "Halo, siapa kamu?"
        }
      ],
      "stream": false
    }
    ```
*   **Response Sukses (200 OK)**: Mengembalikan respons JSON standar OpenAI dari provider yang terpilih.

---

## 🛠️ Endpoint Admin (Admin Endpoints)

Seluruh endpoint admin berada di bawah prefix `/admin` dan didefinisikan pada [src/routers/admin.py](file:///Users/wahyutricahya/Work/llm-router/src/routers/admin.py). Endpoint ini memerlukan header `Authorization: Bearer <ROUTER_API_KEY>`.

### 1. List Kunci API (`GET /admin/keys`)
Mendapatkan daftar semua kunci API provider di pool beserta status dan metrik errornya.

*   **Query Parameters**:
    *   `provider` (Optional): Filter berdasarkan provider (`gemini`, `groq`, dll.).
    *   `status` (Optional): Filter berdasarkan status (`healthy`, `cooldown`, `dead`).
*   **Response Sukses (200 OK)**:
    ```json
    {
      "keys": [
        {
          "id": "a82b930d-...",
          "provider": "gemini",
          "key_name": "Gemini Key 1",
          "status": "healthy",
          "cooldown_until": null,
          "priority": 1,
          "error_count": 0,
          "last_used_at": "2026-07-17T01:50:00+00:00",
          "created_at": "2026-07-16T12:00:00+00:00"
        }
      ],
      "total": 1
    }
    ```

---

### 2. Tambah Kunci API (`POST /admin/keys`)
Menambahkan kunci API baru untuk provider tertentu ke dalam pool.

*   **Body Request**:
    ```json
    {
      "provider": "gemini",
      "key_name": "Kunci Gemini Tambahan",
      "key_value": "AIzaSy...",
      "priority": 1
    }
    ```
*   **Response Sukses (201 Created)**: Mengembalikan objek kunci yang berhasil dibuat.

---

### 3. Reset Status Kunci API (`PATCH /admin/keys/{key_id}/reset`)
Mengatur ulang status kunci API (misalnya membawa kunci berstatus `dead` atau `cooldown` kembali menjadi `healthy`).

*   **Body Request**:
    ```json
    {
      "status": "healthy"
    }
    ```
*   **Response Sukses (200 OK)**: Mengembalikan objek kunci dengan status terbaru.

---

### 4. Hapus Kunci API (`DELETE /admin/keys/{key_id}`)
Menghapus kunci API secara permanen dari database.

*   **Response Sukses (200 OK)**:
    ```json
    {
      "message": "Key deleted"
    }
    ```

---

### 5. Ringkasan Statistik Penggunaan (`GET /admin/stats`)
Mendapatkan agregasi penggunaan token, latensi rata-rata, dan rasio sukses/gagal request LLM dalam rentang waktu tertentu.

*   **Query Parameters**:
    *   `hours` (Optional, default 24, max 720): Rentang waktu statistik dalam jam.
*   **Response Sukses (200 OK)**:
    ```json
    {
      "window_hours": 24,
      "since": "2026-07-16T01:50:00.000Z",
      "stats": [
        {
          "provider": "gemini",
          "model": "gemini-1.5-flash",
          "total_requests": 150,
          "success_requests": 148,
          "error_requests": 2,
          "total_prompt_tokens": 45000,
          "total_completion_tokens": 12000,
          "avg_latency_ms": 420
        }
      ]
    }
    ```

---

### 6. Ringkasan Cepat Pool (`GET /admin/status`)
Mendapatkan total jumlah kunci berdasarkan status (`healthy`, `cooldown`, `dead`) di setiap provider.

*   **Response Sukses (200 OK)**:
    ```json
    {
      "pool": {
        "gemini": {
          "healthy": 3,
          "cooldown": 0,
          "dead": 0
        },
        "groq": {
          "healthy": 1,
          "cooldown": 1,
          "dead": 0
        }
      }
    }
    ```

---

### 7. Tambah Rute Model (`POST /admin/routes`)
Menambahkan aturan rute model virtual baru ke dalam database routing dinamis.

*   **Body Request**:
    ```json
    {
      "virtual_model": "combo-smart",
      "provider": "openrouter",
      "target_model": "anthropic/claude-3.5-sonnet",
      "priority": 1,
      "enabled": true
    }
    ```
*   **Response Sukses (201 Created)**: Mengembalikan rute baru yang dibuat dan otomatis melakukan refresh cache.

---

### 8. List Rute Model (`GET /admin/routes`)
Mendapatkan daftar semua pemetaan rute model virtual yang terdaftar di database.

*   **Response Sukses (200 OK)**: Mengembalikan array berisi seluruh rute model.

---

### 9. Force Refresh Cache Rute (`POST /admin/routes/refresh`)
Memaksa router untuk memuat ulang tabel pemetaan rute model dari database Supabase ke memori server cache lokal saat itu juga.

*   **Response Sukses (200 OK)**:
    ```json
    {
      "message": "Route cache refreshed successfully"
    }
    ```
