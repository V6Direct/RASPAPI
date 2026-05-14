# RaspAPI

**Realtime router & system telemetry API for V6Direct network nodes (AS213413)**

A lightweight [FastAPI](https://fastapi.tiangolo.com/) service designed to run on Raspberry Pi PoP routers and similar Linux-based nodes. It exposes system metrics — CPU, memory, disk, and per-interface network counters — over a secured REST API with built-in caching, rate limiting, and a rolling history buffer.

***

## Features

- **Full system snapshot** — CPU, memory, swap, disks, and all network interfaces in a single request
- **Per-resource endpoints** — query only the metrics you need
- **30-second in-memory cache** — reduces polling overhead; force-invalidate at any time via `POST /stats/refresh`
- **Rolling 60-sample history** — lightweight time-series buffer persisted to `history.json`
- **API key authentication** — all `/stats/*` endpoints require `X-API-Key` header
- **Rate limiting** — global 60 req/min default; stricter 30 req/min on stats routes via [slowapi](https://github.com/laurentS/slowapi)
- **Raspberry Pi temperature support** — reads `cpu_thermal` / `cpu-thermal` / `coretemp` / `thermal_zone0` via `psutil`
- **Node metadata** — exposes hostname, architecture, uptime, ASN, and configurable node role
- **Interactive docs** — Swagger UI at `/docs`, ReDoc at `/redoc`

***

## Requirements

- Python 3.10+
- Linux (tested on Raspberry Pi OS / Debian)

Install dependencies:

```bash
pip install fastapi uvicorn psutil slowapi pydantic
```

***

## Configuration

All configuration is done via environment variables:

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `changeme` | API key required in `X-API-Key` header |
| `NODE_ROLE` | `raspi-telemetry` | Node role label (`raspi-telemetry` or `pop-router`) |
| `NODE_ASN` | `AS213413` | ASN reported in node info |

> **Important:** Always set a strong `API_KEY` in production. The default `changeme` is insecure.

***

## Running

```bash
API_KEY=your-secret-key NODE_ROLE=pop-router uvicorn code:app --host 0.0.0.0 --port 8000
```

For production, use a systemd service or run behind a reverse proxy (nginx/caddy).

***


***

## Quick Start (Demo)

A public demo instance is available and uses the default demo API key:

```text
X-API-Key: changeme
```

```bash
# Health check (no auth)
curl https://raspapi.core01.eu/health

# Full system snapshot
curl -H "X-API-Key: changeme" https://raspapi.core01.eu/stats

# CPU only
curl -H "X-API-Key: changeme" https://raspapi.core01.eu/stats/cpu

# Memory & swap
curl -H "X-API-Key: changeme" https://raspapi.core01.eu/stats/memory

# Network interfaces (all)
curl -H "X-API-Key: changeme" https://raspapi.core01.eu/stats/network

# Filter by interface
curl -H "X-API-Key: changeme" "https://raspapi.core01.eu/stats/network?interface=eth0"

# Last 10 history samples
curl -H "X-API-Key: changeme" "https://raspapi.core01.eu/stats/history?limit=10"

# Force cache refresh
curl -X POST -H "X-API-Key: changeme" -H "Content-Type: application/json"      -d '{"reason": "testing", "full": false}'      https://demo.v6direct.org/stats/refresh
```

For local development, the same default key works unless `API_KEY` is overridden:

```bash
API_KEY=changeme uvicorn code:app --host 127.0.0.1 --port 8000
```

> If the public demo exposes Swagger, open `/docs`, click **Authorize**, and enter `changeme` as the API key.

## API Reference

### Authentication

All endpoints under `/stats/*` require an API key passed as a request header:

```
X-API-Key: your-secret-key
```

Requests without a valid key return `403 Forbidden`.

***

### Endpoints

#### `GET /`
API root — returns version info and a map of all available endpoints. No auth required.

#### `GET /health`
Lightweight liveness probe. Returns `{"status": "ok", "uptime_seconds": N}`. No auth required. Suitable for uptime monitors.

#### `GET /stats`
Full system snapshot including node info, CPU, memory, disks, and all network interfaces.

**Response fields:**
- `node` — hostname, platform, architecture, uptime, ASN, node role
- `cpu` — overall %, per-core %, frequency, temperature (if available), load averages (1m/5m/15m)
- `memory` — total/available/used MB, percent, swap stats
- `disks` — per-mountpoint: total/used/free GB and percent
- `interfaces` — per-interface: byte/packet counters, error/drop counts, IP addresses
- `cache_age_seconds` — seconds since last poll

#### `GET /stats/cpu`
CPU metrics only (usage, per-core breakdown, frequency, temperature, load averages).

#### `GET /stats/memory`
RAM and swap usage statistics.

#### `GET /stats/network`
Per-interface traffic counters. Optionally filter by interface name:

```
GET /stats/network?interface=eth0
GET /stats/network?interface=wg0
```

Returns `404` if the specified interface is not found.

#### `GET /stats/node`
Static node information: hostname, platform, architecture, Python version, boot time, uptime, ASN.

#### `GET /stats/history`
Rolling history of up to 60 metric samples (CPU %, memory %, swap %). Persisted to `history.json` across restarts.

```
GET /stats/history?limit=10
```

| Query param | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `60` | Number of samples to return (1–60) |

#### `POST /stats/refresh`
Force-invalidates the stats cache and triggers an immediate re-poll. Optionally clears the metric history.

**Request body:**
```json
{
  "reason": "Deploying new config",
  "full": false
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `reason` | string | `null` | Optional reason string included in the response message |
| `full` | bool | `false` | If `true`, clears the rolling history buffer as well |

***

## Rate Limits

| Endpoint group | Limit |
|---|---|
| `/`, `/health` | 60 req/min |
| `/stats/*` (GET) | 30 req/min |
| `/stats/refresh` (POST) | 10 req/min |

Exceeding limits returns `429 Too Many Requests`.

***

## Data Persistence

Metric history is saved to `history.json` in the working directory on every cache update and loaded on startup. The buffer holds a maximum of 60 samples (rolling). Use `POST /stats/refresh` with `"full": true` to clear it.

***

## License

MIT — © [V6Direct](https://v6direct.org)
