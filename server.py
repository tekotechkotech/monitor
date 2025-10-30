#!/usr/bin/env python3
"""
Server Monitor - btop + neofetch + cPanel server information dashboard.
Zero external dependencies. Python 3 stdlib only.
"""

import os
import sys
import time
import json
import socket
import shutil
import platform
import threading
import subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

PORT = int(os.environ.get("PORT", 8088))
HOST = os.environ.get("HOST", "0.0.0.0")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

# Thread-safe global state for calculated rate metrics
_metrics_lock = threading.Lock()
_latest_metrics = {
    "cpu_total": 0.0,
    "cpu_cores": [],
    "net_rates": {},
    "last_update": 0
}

def parse_cpu_stat():
    """Reads /proc/stat and returns {core_name: (idle, total)}"""
    cores = {}
    try:
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("cpu"):
                    parts = line.split()
                    name = parts[0]
                    # values: user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
                    nums = [float(x) for x in parts[1:]]
                    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
                    total = sum(nums)
                    cores[name] = (idle, total)
    except Exception:
        pass
    return cores

def parse_net_dev():
    """Reads /proc/net/dev and returns {iface: {'rx': bytes, 'tx': bytes}}"""
    devs = {}
    try:
        with open("/proc/net/dev", "r") as f:
            lines = f.readlines()
            for line in lines[2:]:
                if ":" in line:
                    iface, data = line.split(":", 1)
                    iface = iface.strip()
                    parts = data.split()
                    rx_bytes = int(parts[0])
                    tx_bytes = int(parts[8])
                    devs[iface] = {"rx": rx_bytes, "tx": tx_bytes}
    except Exception:
        pass
    return devs

def metrics_background_collector():
    """Continuously computes delta rates for CPU and Network."""
    prev_cpu = parse_cpu_stat()
    prev_net = parse_net_dev()
    prev_time = time.time()

    while True:
        time.sleep(1.0)
        curr_time = time.time()
        time_delta = max(curr_time - prev_time, 0.001)

        curr_cpu = parse_cpu_stat()
        curr_net = parse_net_dev()

        # Compute CPU usage
        cpu_total = 0.0
        cpu_cores = []

        if "cpu" in curr_cpu and "cpu" in prev_cpu:
            prev_idle, prev_tot = prev_cpu["cpu"]
            curr_idle, curr_tot = curr_cpu["cpu"]
            tot_delta = curr_tot - prev_tot
            idle_delta = curr_idle - prev_idle
            if tot_delta > 0:
                cpu_total = round(max(0.0, min(100.0, (1.0 - idle_delta / tot_delta) * 100)), 1)

        # Cores cpu0, cpu1...
        core_names = sorted([k for k in curr_cpu.keys() if k != "cpu"], 
                            key=lambda x: int(x.replace("cpu", "")) if x.replace("cpu", "").isdigit() else 999)
        for name in core_names:
            if name in prev_cpu:
                p_idle, p_tot = prev_cpu[name]
                c_idle, c_tot = curr_cpu[name]
                t_diff = c_tot - p_tot
                i_diff = c_idle - p_idle
                pct = 0.0
                if t_diff > 0:
                    pct = round(max(0.0, min(100.0, (1.0 - i_diff / t_diff) * 100)), 1)
                cpu_cores.append({"core": name, "usage": pct})

        # Compute Network rates
        net_rates = {}
        for iface, c_data in curr_net.items():
            if iface in prev_net:
                p_data = prev_net[iface]
                rx_delta = max(0, c_data["rx"] - p_data["rx"])
                tx_delta = max(0, c_data["tx"] - p_data["tx"])
                rx_rate = rx_delta / time_delta
                tx_rate = tx_delta / time_delta
                net_rates[iface] = {
                    "rx_speed": rx_rate,
                    "tx_speed": tx_rate,
                    "rx_total": c_data["rx"],
                    "tx_total": c_data["tx"]
                }
            else:
                net_rates[iface] = {
                    "rx_speed": 0,
                    "tx_speed": 0,
                    "rx_total": c_data["rx"],
                    "tx_total": c_data["tx"]
                }

        with _metrics_lock:
            _latest_metrics["cpu_total"] = cpu_total
            _latest_metrics["cpu_cores"] = cpu_cores
            _latest_metrics["net_rates"] = net_rates
            _latest_metrics["last_update"] = curr_time

        prev_cpu = curr_cpu
        prev_net = curr_net
        prev_time = curr_time

def get_os_info():
    """Extracts Neofetch-style OS and hardware info."""
    os_name = "Linux"
    if os.path.exists("/etc/os-release"):
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        os_name = line.split("=", 1)[1].strip().strip('"')
                        break
        except Exception:
            pass
    elif os.path.exists("/etc/issue"):
        try:
            with open("/etc/issue") as f:
                os_name = f.readline().strip()
        except Exception:
            pass

    # CPU Model
    cpu_model = platform.processor() or "Unknown CPU"
    cpu_cores_logical = os.cpu_count() or 1
    cpu_mhz = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    cpu_model = line.split(":", 1)[1].strip()
                elif "cpu MHz" in line and not cpu_mhz:
                    cpu_mhz = f"{float(line.split(':', 1)[1].strip()):.1f} MHz"
    except Exception:
        pass

    # IP addresses
    ips = []
    try:
        cmd = subprocess.run(["hostname", "-I"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1)
        if cmd.returncode == 0 and cmd.stdout:
            ips = [ip.strip() for ip in cmd.stdout.split() if ip.strip() and not ip.startswith("127.")]
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips = [s.getsockname()[0]]
            s.close()
        except Exception:
            ips = ["127.0.0.1"]

    return {
        "hostname": socket.gethostname(),
        "os": os_name,
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_model": cpu_model,
        "cpu_cores_logical": cpu_cores_logical,
        "cpu_mhz": cpu_mhz,
        "python": platform.python_version(),
        "ips": ips
    }

def get_memory_info():
    """Reads memory metrics from /proc/meminfo."""
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    val = v.strip().split()[0]
                    if val.isdigit():
                        mem[k.strip()] = int(val) * 1024  # in bytes
    except Exception:
        pass

    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    free = mem.get("MemFree", 0)
    buffers = mem.get("Buffers", 0)
    cached = mem.get("Cached", 0)
    used = max(0, total - avail)

    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)

    return {
        "total": total,
        "used": used,
        "free": free,
        "available": avail,
        "buffers": buffers,
        "cached": cached,
        "used_percent": round((used / total * 100), 1) if total > 0 else 0,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_free": swap_free,
        "swap_percent": round((swap_used / swap_total * 100), 1) if swap_total > 0 else 0
    }

def get_uptime_and_load():
    """Reads uptime and load averages."""
    uptime_sec = 0.0
    try:
        with open("/proc/uptime") as f:
            uptime_sec = float(f.read().split()[0])
    except Exception:
        pass

    days = int(uptime_sec // 86400)
    hours = int((uptime_sec % 86400) // 3600)
    mins = int((uptime_sec % 3600) // 60)
    secs = int(uptime_sec % 60)
    uptime_str = f"{days}d {hours}h {mins}m {secs}s" if days > 0 else f"{hours}h {mins}m {secs}s"

    load1, load5, load15 = 0.0, 0.0, 0.0
    entities = ""
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            load1 = float(parts[0])
            load5 = float(parts[1])
            load15 = float(parts[2])
            entities = parts[3] if len(parts) > 3 else ""
    except Exception:
        pass

    return {
        "uptime_seconds": int(uptime_sec),
        "uptime_formatted": uptime_str,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "entities": entities
    }

def get_disk_info():
    """Parses mounted disk partitions using df."""
    disks = []
    try:
        # Run df -B1 to get exact byte counts, exclude virtual fs
        cmd = subprocess.run(
            ["df", "-B1", "-P", "-T", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs", "-x", "overlay"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2
        )
        if cmd.returncode == 0:
            lines = cmd.stdout.strip().split("\n")
            seen_mounts = set()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 7:
                    fs = parts[0]
                    fstype = parts[1]
                    total = int(parts[2])
                    used = int(parts[3])
                    avail = int(parts[4])
                    pct_str = parts[5].replace("%", "")
                    mount = parts[6]
                    if mount not in seen_mounts and total > 0:
                        seen_mounts.add(mount)
                        disks.append({
                            "filesystem": fs,
                            "type": fstype,
                            "mount": mount,
                            "total": total,
                            "used": used,
                            "available": avail,
                            "used_percent": float(pct_str) if pct_str.isdigit() else round(used / total * 100, 1)
                        })
    except Exception:
        pass

    if not disks:
        try:
            usage = shutil.disk_usage("/")
            disks.append({
                "filesystem": "root",
                "type": "ext4",
                "mount": "/",
                "total": usage.total,
                "used": usage.used,
                "available": usage.free,
                "used_percent": round(usage.used / usage.total * 100, 1)
            })
        except Exception:
            pass

    return sorted(disks, key=lambda x: x["mount"])

def check_tcp_port(port, host="127.0.0.1", timeout=0.15):
    """Fast check whether local port is listening."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False

def get_service_statuses():
    """Checks typical cPanel / server services."""
    services = [
        {"name": "SSH", "port": 22, "category": "System"},
        {"name": "Web (HTTP)", "port": 80, "category": "Web"},
        {"name": "Web (HTTPS)", "port": 443, "category": "Web"},
        {"name": "MariaDB / MySQL", "port": 3306, "category": "Database"},
        {"name": "Redis", "port": 6379, "category": "Database"},
        {"name": "Cloudflare Tunnel", "port": None, "category": "Network", "check": "cf"},
        {"name": "Docker Engine", "port": None, "category": "System", "check": "docker"},
    ]

    docker_running = os.path.exists("/var/run/docker.sock")
    
    cf_running = False
    try:
        p = subprocess.run(["pgrep", "-f", "cloudflared"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        cf_running = p.returncode == 0
    except Exception:
        pass

    results = []
    for s in services:
        status = "offline"
        if s["check"] == "cf" if "check" in s else False:
            status = "online" if cf_running else "offline"
        elif s["check"] == "docker" if "check" in s else False:
            status = "online" if docker_running else "offline"
        elif s["port"] is not None:
            # Check primary port or fallback (e.g. MariaDB on 3310 if 3306 not open)
            online = check_tcp_port(s["port"])
            if not online and s["name"] == "MariaDB / MySQL":
                online = check_tcp_port(3310)
            status = "online" if online else "offline"

        results.append({
            "name": s["name"],
            "category": s["category"],
            "port": s["port"],
            "status": status
        })
    return results

def get_top_processes(sort_by="cpu", limit=12):
    """Retrieves top processes using ps."""
    sort_flag = "-%cpu" if sort_by == "cpu" else "-%mem"
    procs = []
    try:
        cmd = subprocess.run(
            ["ps", "-eo", "pid,user,%cpu,%mem,stat,comm", f"--sort={sort_flag}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2
        )
        if cmd.returncode == 0:
            lines = cmd.stdout.strip().split("\n")
            for line in lines[1:limit + 1]:
                parts = line.split(None, 5)
                if len(parts) >= 6:
                    procs.append({
                        "pid": parts[0],
                        "user": parts[1],
                        "cpu": float(parts[2]),
                        "mem": float(parts[3]),
                        "stat": parts[4],
                        "name": parts[5]
                    })
    except Exception:
        pass
    return procs

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class MonitorRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            return super().do_GET()

        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return

        if self.path.startswith("/api/stats"):
            with _metrics_lock:
                cpu_total = _latest_metrics["cpu_total"]
                cpu_cores = list(_latest_metrics["cpu_cores"])
                net_rates = dict(_latest_metrics["net_rates"])
                last_update = _latest_metrics["last_update"]

            payload = {
                "system": get_os_info(),
                "uptime_load": get_uptime_and_load(),
                "cpu": {
                    "usage_percent": cpu_total,
                    "cores": cpu_cores
                },
                "memory": get_memory_info(),
                "disks": get_disk_info(),
                "network": net_rates,
                "services": get_service_statuses(),
                "timestamp": time.time(),
                "last_update": last_update
            }

            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path.startswith("/api/processes"):
            sort_by = "cpu"
            if "sort=mem" in self.path:
                sort_by = "mem"
            procs = get_top_processes(sort_by=sort_by, limit=14)
            data = json.dumps(procs).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
            return

        # Serve static assets
        return super().do_GET()

    def log_message(self, format, *args):
        # Keep stdout clean; suppress spammy GET polling logs
        if "GET /api/" in (args[0] if args else "") or "GET /health" in (args[0] if args else ""):
            return
        super().log_message(format, *args)

def main():
    # Start background collector thread
    collector = threading.Thread(target=metrics_background_collector, daemon=True)
    collector.start()

    # Pre-warm collector with initial pass
    time.sleep(0.5)

    server_address = (HOST, PORT)
    httpd = ThreadedHTTPServer(server_address, MonitorRequestHandler)
    print(f"🚀 Monitor Server running at http://{HOST}:{PORT}")
    print(f"📡 API Endpoints: /api/stats, /api/processes, /health")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        httpd.server_close()
        sys.exit(0)

if __name__ == "__main__":
    main()
