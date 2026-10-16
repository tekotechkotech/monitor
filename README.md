# Server Monitor

Lightweight, high-density server monitoring dashboard inspired by **btop**, **neofetch**, and **cPanel Server Information**.

Zero external Python dependencies — powered exclusively by Python 3 standard library and Linux `/proc` filesystem.

---

## Fitur Utama

- **Neofetch System Specifications:**
  - OS Distro & Version
  - Kernel Release & Architecture
  - CPU Model, Logical Cores & Frequency
  - Hostname & Network IPs
  - Uptime & System Load

- **btop Dynamic Metrics:**
  - Total CPU % and per-core activity gauges
  - RAM & Swap usage (Used, Free, Cached, Buffers)
  - Mounted partitions & disk usage (`/`, `/home`, dll)
  - Realtime network throughput per interface (RX / TX KB/s & MB/s)
  - Active process inspector with CPU% and MEM% sorting

- **cPanel Server Information:**
  - Real-time service status lights (SSH, Web HTTP/HTTPS, Database, Redis, Docker, Cloudflare Tunnel)
  - Quick RAM & Root disk gauges

---

## Cara Menjalankan

### 1. Direct Python (Sangat Ringan, Rekomendasi)

```bash
python3 server.py
```
Akses di browser: `http://localhost:8088`

### 2. Docker / Docker Compose

```bash
docker compose up -d --build
```

---

## Konfigurasi Port & Host

Default port adalah `8088` dan host `0.0.0.0`. Ubah melalui environment variables:

```bash
PORT=9090 HOST=127.0.0.1 python3 server.py
```

---

## API Endpoints

- `GET /api/stats` : Mengembalikan seluruh metrik hardware, OS, disk, network, dan service dalam format JSON.
- `GET /api/processes?sort=cpu|mem` : Mengembalikan daftar proses aktif teratas.
- `GET /health` : Healthcheck endpoint (`{"status": "ok"}`).
