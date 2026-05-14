"""
Router Telemtary for YSWS RaspAPI
"""


from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Request, Security, Depends
from fastapi.responses import JSONResponse
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from typing import Optional
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import psutil
import time
import datetime
import platform
import socket
import os
import json
from collections import deque


# Rate limiter � must be before app
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


# APP


app = FastAPI(
    title="V6D RaspAPI",
    description="""V6D Network Telemntary""",
    version="1.0.0",
    contact={
        "name": "V6Direct",
        "url": "https://v6direct.org",
    },
    license_info={
        "name": "MIT",
    },
    docs_url="/docs",
    redoc_url="/redoc",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# API Key Auth

API_KEY = os.getenv("API_KEY", "changeme")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_key(key: Optional[str] = Security(api_key_header)):
    if not key or key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key. Pass it as X-API-Key header.")


# In-Memory Cache + History


CACHE_TTL_SECONDS = 30
HISTORY_FILE = "history.json"
_cache: dict = {}
_cache_ts: float = 0.0
_history: deque = deque(maxlen=60) # rollin 60 sample history


def _load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                for sample in json.load(f):
                    _history.append(sample)
        except Exception:
            pass

def _save_history():
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(list(_history), f)
    except Exception:
        pass

_load_history()


# Pydantic Models


class CPUStats(BaseModel):
    usage_percent: float = Field(..., description="Overall CPU usage (%)")
    per_core: list[float] = Field(..., description="Per-core CPU usage (%)")
    frequency_mhz: Optional[float] = Field(None, description="Current CPU frequency in Mhz")
    temperature_celcius: Optional[float] = Field(None, description="CPU Temperature (if available)")
    load_avg_1m: float = Field(..., description="1-minute load averagee")
    load_avg_5m: float = Field(..., description="5-minute load averagee")
    load_avg_15m: float = Field(..., description="15-minute load averagee")


class MemoryStats(BaseModel):
    total_mb: float
    available_mb: float
    used_mb: float
    percent: float
    swap_total_mb: float
    swap_used_mb: float
    swap_percent: float


class DiskStats(BaseModel):
    mountpoint: str
    total_gb: float
    used_gb: float
    free_gb: float
    percent: float


class InterfaceStats(BaseModel):
    name: str
    bytes_sent: int
    bytes_recv: int
    packets_sent: int
    packets_recv: int
    errors_in: int
    errors_out: int
    drop_in: int
    drop_out: int
    addresses: list[str]


class NodeInfo(BaseModel):
    hostname: str
    platform: str
    architecture: str
    python_version: str
    boot_time_utc: str
    uptime_seconds: int
    node_role: str = "raspi-telemetry"   # "raspi-telemetry" / "pop-router"
    network_name: str = "V6Direct"
    asn: str = "AS213413"


class SystemSnapshots(BaseModel):
    timestamp_utc: str
    node: NodeInfo
    cpu: CPUStats
    memory: MemoryStats
    disks: list[DiskStats]
    interfaces: list[InterfaceStats]
    cache_age_seconds: float


class RefreshRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Optional reason for forced refresh", json_schema_extra={"example": "Deploying new config"})
    full: bool = Field(False, description="If true, also clears metric history")


class RefreshResponse(BaseModel):
    success: bool
    message: str
    cleared_history: bool
    new_snapshot_timestamp: str


class HistorySample(BaseModel):
    timestamp_utc: str
    cpu_percent: float
    memory_percent: float
    swap_percent: float


# Core Polling logic


def _poll_stats() -> dict:
    """Poll all system metrics and return as a serialisable dict."""
    now = datetime.datetime.utcnow()

    # CPU
    cpu_pct = psutil.cpu_percent(interval=0.5)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    freq = psutil.cpu_freq()
    load = psutil.getloadavg()

    # Temperature (Raspberry Pi exposes this)
    temp = None
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for key in ("cpu_thermal", "coretemp", "cpu-thermal", "thermal_zone0"):
                if key in temps and temps[key]:
                    temp = temps[key][0].current
                    break
    except Exception:
        pass

    cpu = {
        "usage_percent": cpu_pct,
        "per_core": per_core,
        "frequency_mhz": round(freq.current, 2) if freq else None,
        "temperature_celcius": round(temp, 2) if temp else None,
        "load_avg_1m": round(load[0], 3),
        "load_avg_5m": round(load[1], 3),
        "load_avg_15m": round(load[2], 3),
    }

    # Memory
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    memory = {
        "total_mb": round(vm.total / 1e6, 2),
        "available_mb": round(vm.available / 1e6, 2),
        "used_mb": round(vm.used / 1e6, 2),
        "percent": vm.percent,
        "swap_total_mb": round(sw.total / 1e6, 2),
        "swap_used_mb": round(sw.used / 1e6, 2),
        "swap_percent": sw.percent,
    }

    # Disks
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mountpoint": part.mountpoint,
                "total_gb": round(usage.total / 1e9, 3),
                "used_gb": round(usage.used / 1e9, 3),
                "free_gb": round(usage.free / 1e9, 3),
                "percent": usage.percent,
            })
        except PermissionError:
            pass

    # Network Interfaces
    net_io = psutil.net_io_counters(pernic=True)
    net_addrs = psutil.net_if_addrs()
    interfaces = []
    for iface, counters in net_io.items():
        addrs = []
        for addr in net_addrs.get(iface, []):
            if addr.address and not addr.address.startswith("fe80"):
                addrs.append(addr.address)
        interfaces.append({
            "name": iface,
            "bytes_sent": counters.bytes_sent,
            "bytes_recv": counters.bytes_recv,
            "packets_sent": counters.packets_sent,
            "packets_recv": counters.packets_recv,
            "errors_in": counters.errin,
            "errors_out": counters.errout,
            "drop_in": counters.dropin,
            "drop_out": counters.dropout,
            "addresses": addrs,
        })

    # Node Info
    boot_ts = psutil.boot_time()
    uptime = int(time.time() - boot_ts)
    node = {
        "hostname": socket.gethostname(),
        "platform": platform.system() + " " + platform.release(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "boot_time_utc": datetime.datetime.utcfromtimestamp(boot_ts).isoformat() + "Z",
        "uptime_seconds": uptime,
        "node_role": os.getenv("NODE_ROLE", "raspi-telemetry"),
        "network_name": "V6Direct",
        "asn": os.getenv("NODE_ASN", "AS213413"),
    }

    snapshots = {
        "timestamp_utc": now.isoformat() + "Z",
        "node": node,
        "cpu": cpu,
        "memory": memory,
        "disks": disks,
        "interfaces": interfaces,
        "cache_age_seconds": 0.0,
    }
    return snapshots


def _get_cached_stats(force: bool = False) -> dict:
    global _cache, _cache_ts
    now = time.monotonic()
    if force or not _cache or (now - _cache_ts) >= CACHE_TTL_SECONDS:
        _cache = _poll_stats()
        _cache_ts = now
        # Push history (pray this works)
        _history.append({
            "timestamp_utc": _cache["timestamp_utc"],
            "cpu_percent": _cache["cpu"]["usage_percent"],
            "memory_percent": _cache["memory"]["percent"],
            "swap_percent": _cache["memory"]["swap_percent"],
        })
        _save_history()
    else:
        _cache["cache_age_seconds"] = round(now - _cache_ts, 2)
    return _cache


# Routes /GET


@app.get(
    "/",
    summary="API root",
    tags=["Meta"],
    response_class=JSONResponse,
)
@limiter.limit("60/minute")
def root(request: Request):
    """Welcome message and link to docs."""
    return {
        "name": "V6Direct RaspAPI",
        "version": "1.0.0",
        "description": "Realtime Router Monitoring software",
        "docs": "/docs",
        "redoc": "/redoc",
        "endpoints": {
            "GET /stats": "Full system snapshot (cached)",
            "GET /stats/cpu": "CPU metrics only",
            "GET /stats/memory": "Memory & swap metrics",
            "GET /stats/network": "Per-interface traffic counters",
            "GET /stats/history": "Rolling 60-sample metric history",
            "POST /stats/refresh": "Force-invalidate cache and re-poll",
        },
    }


@app.get(
    "/stats",
    response_model=SystemSnapshots,
    summary="Full system snapshot",
    tags=["Stats"],
)
@limiter.limit("30/minute")
def get_full_stats(request: Request, _: None = Depends(verify_key)):
    """
    Returns a full system snapshot including ressource usage
    """
    return _get_cached_stats()


@app.get(
    "/stats/cpu",
    response_model=CPUStats,
    summary="CPU Metrics",
    tags=["Stats"],
)
@limiter.limit("30/minute")
def get_cpu(request: Request, _: None = Depends(verify_key)):
    """
    Returns CPU usahe per core breakdown and more
    """
    snap = _get_cached_stats()
    return snap["cpu"]


@app.get(
    "/stats/memory",
    response_model=MemoryStats,
    summary="memory and swap stats",
    tags=["Stats"],
)
@limiter.limit("30/minute")
def get_memory(request: Request, _: None = Depends(verify_key)):
    """
    Returns ram and swap usage and stats
    """
    snap = _get_cached_stats()
    return snap["memory"]


@app.get(
    "/stats/network",
    response_model=list[InterfaceStats],
    summary="Network interface stats",
    tags=["Stats"],
)
@limiter.limit("30/minute")
def get_network(
    request: Request,
    interface: Optional[str] = Query(
        None,
        description="Filter by interface name e.g. `eth0`, `wg0`"
    ),
    _: None = Depends(verify_key),
):
    """
    Returns Per-Interface stats
    """
    snap = _get_cached_stats()
    ifaces = snap["interfaces"]
    if interface:
        ifaces = [i for i in ifaces if i["name"] == interface]
        if not ifaces:
            raise HTTPException(status_code=404, detail=f"Interface '{interface}' not found.")
    return ifaces


@app.get(
    "/stats/history",
    response_model=list[HistorySample],
    summary="Rolling metric history",
    tags=["Stats"],
)
@limiter.limit("30/minute")
def get_history(
    request: Request,
    limit: int = Query(
        60,
        ge=1,
        le=60,
        description="Number of samples to return (max 60)"
    ),
    _: None = Depends(verify_key),
):
    """
    Returns the last N cached metric samples
    """
    samples = list(_history)[-limit:]
    if not samples:
        return []
    return samples


@app.get(
    "/stats/node",
    response_model=NodeInfo,
    tags=["Stats"],
)
@limiter.limit("30/minute")
def get_node_info(request: Request, _: None = Depends(verify_key)):
    """
    Returns static node information
    """
    snap = _get_cached_stats()
    return snap["node"]


# Post Route


@app.post(
    "/stats/refresh",
    response_model=RefreshResponse,
    summary="Force refresh stats cache",
    tags=["Control"],
    status_code=200,
)
@limiter.limit("10/minute")
def force_refresg(request: Request, body: RefreshRequest, background_tasks: BackgroundTasks, _: None = Depends(verify_key)):
    """
    I have no idea how to explain it.
    """
    if body.full:
        _history.clear()

    new_snap = _get_cached_stats(force=True)
    return {
        "success": True,
        "message": f"Cache refreshed sucessfully.{' Reason: ' + body.reason if body.reason else ''}",
        "cleared_history": body.full,
        "new_snapshot_timestamp": new_snap["timestamp_utc"],
    }


# Health check � no auth needed for uptime monitors
@app.get(
    "/health",
    summary="Health Check",
    tags=["Meta"],
    include_in_schema=True,
)
@limiter.limit("60/minute")
def health(request: Request):
    """Lightweight liveness probe"""
    snap = _get_cached_stats()
    return {"status": "ok", "uptime_seconds": snap["node"]["uptime_seconds"]}
