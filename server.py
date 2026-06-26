#!/usr/bin/env python3
"""
Server Monitor - btop + neofetch + cPanel server information dashboard.
Zero external dependencies. Python 3 stdlib only.
"""

import os
import sys
import glob
import time
import json
import socket
import shutil
import platform
import threading
import subprocess
import re
import datetime
import configparser
import urllib.request
import urllib.parse
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

def get_cpu_frequencies():
    """Reads CPU frequencies from sysfs per-core if possible, else parses /proc/cpuinfo."""
    core_freqs = []  # list of (core_name, kHz as int)
    sys_glob = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"
    sys_paths = sorted(glob.glob(sys_glob))
    freq_by_idx = {}
    if sys_paths:
        for p in sys_paths:
            try:
                idx = int(p.split("cpu")[-2].split("/")[0])
                with open(p) as f:
                    v = int(f.read().strip())
                    freq_by_idx[idx] = v
            except Exception:
                pass
    if freq_by_idx:
        max_idx = max(freq_by_idx.keys())
        for i in range(max_idx+1):
            val = freq_by_idx.get(i, 0)
            core_freqs.append((f"cpu{i}", val))
        return core_freqs

    # Fallback to /proc/cpuinfo parsing
    per_core = []
    try:
        with open("/proc/cpuinfo") as f:
            current_core = -1
            for line in f:
                if line.startswith("processor"):
                    current_core = int(line.strip().split(":")[-1])
                elif "cpu MHz" in line:
                    mhz = float(line.split(":",1)[1].strip())
                    kHz = int(mhz * 1000)
                    per_core.append((f"cpu{current_core}", kHz))
    except Exception:
        pass
    return per_core

def metrics_background_collector():
    """Continuously computes delta rates for CPU and Network, adds frequencies."""
    prev_cpu = parse_cpu_stat()
    prev_net = parse_net_dev()
    prev_time = time.time()

    while True:
        time.sleep(1.0)
        curr_time = time.time()
        time_delta = max(curr_time - prev_time, 0.001)

        curr_cpu = parse_cpu_stat()
        curr_net = parse_net_dev()
        # --- Begin freq read ---
        freq_items = get_cpu_frequencies()
        core_freq_map = dict(freq_items)
        freq_vals = [v for v in core_freq_map.values() if v > 0]
        avg_khz = int(sum(freq_vals)/len(freq_vals)) if freq_vals else 0
        avg_ghz = round(avg_khz/1e6, 2) if avg_khz else 0.0
        # --- End freq read ---

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
                # Add freq
                core_khz = core_freq_map.get(name, 0)
                core_ghz = round(core_khz/1e6, 2) if core_khz else 0.0
                cpu_cores.append({"core": name, "usage": pct, "freq_khz": core_khz, "ghz": core_ghz})

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
            _latest_metrics["cur_freq_khz"] = avg_khz
            _latest_metrics["cur_ghz"] = avg_ghz
        
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
        {"name": "OKX Trading Agent (24/7)", "port": None, "category": "Trading", "check": "okx"},
    ]

    docker_running = os.path.exists("/var/run/docker.sock")
    
    cf_running = False
    try:
        p = subprocess.run(["pgrep", "-f", "cloudflared"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        cf_running = p.returncode == 0
    except Exception:
        pass

    okx_running = False
    try:
        p_okx = subprocess.run(["systemctl", "--user", "is-active", "okx-trading-agent.service"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        okx_running = p_okx.stdout.strip() == "active"
    except Exception:
        pass

    results = []
    for s in services:
        status = "offline"
        if s.get("check") == "cf":
            status = "online" if cf_running else "offline"
        elif s.get("check") == "docker":
            status = "online" if docker_running else "offline"
        elif s.get("check") == "okx":
            status = "online" if okx_running else "offline"
        elif s.get("port") is not None:
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

def get_gdrive_info():
    """Checks Google Drive mounts (faizzzzz, faizclcp) and token expiration."""
    drives = [
        {"name": "faizzzzz", "path": "/home/faizen/gdrive-faizzzzz"},
        {"name": "faizclcp", "path": "/home/faizen/gdrive-faizclcp"}
    ]
    conf_path = os.path.expanduser("~/.config/rclone/rclone.conf")
    if not os.path.exists(conf_path) and os.path.exists("/home/faizen/.config/rclone/rclone.conf"):
        conf_path = "/home/faizen/.config/rclone/rclone.conf"

    conf = configparser.ConfigParser()
    try:
        conf.read(conf_path)
    except Exception:
        pass

    results = []
    for d in drives:
        name = d["name"]
        path = d["path"]
        mounted = os.path.ismount(path)

        disk_total, disk_used, disk_free, used_pct = 0, 0, 0, 0.0
        if mounted:
            try:
                u = shutil.disk_usage(path)
                disk_total = u.total
                disk_used = u.used
                disk_free = u.free
                if disk_total > 0:
                    used_pct = round((disk_used / disk_total) * 100, 1)
            except Exception:
                pass

        token_valid = False
        expires_in_sec = None
        expiry_iso = None
        if conf.has_section(name) and "token" in conf[name]:
            try:
                tok_data = json.loads(conf[name]["token"])
                if "expiry" in tok_data:
                    raw_exp = tok_data["expiry"]
                    clean_exp = re.sub(r'(\.\d{6})\d+', r'\1', raw_exp)
                    dt_exp = datetime.datetime.fromisoformat(clean_exp)
                    dt_now = datetime.datetime.now(dt_exp.tzinfo)
                    diff = int((dt_exp - dt_now).total_seconds())
                    expires_in_sec = diff
                    expiry_iso = dt_exp.isoformat()
                    token_valid = diff > 0
            except Exception:
                pass

        results.append({
            "name": name,
            "path": path,
            "mounted": mounted,
            "status": "online" if mounted else "offline",
            "token_valid": token_valid,
            "expires_in_seconds": expires_in_sec,
            "expiry": expiry_iso,
            "total": disk_total,
            "used": disk_used,
            "free": disk_free,
            "used_percent": used_pct
        })
    return results

def get_rclone_conf_path():
    path = os.path.expanduser("~/.config/rclone/rclone.conf")
    if not os.path.exists(path) and os.path.exists("/home/faizen/.config/rclone/rclone.conf"):
        path = "/home/faizen/.config/rclone/rclone.conf"
    return path

def get_gdrive_auth_url(remote_name):
    """Generates the Google OAuth authorization URL for the specified remote."""
    conf_path = get_rclone_conf_path()
    conf = configparser.RawConfigParser()
    try:
        conf.read(conf_path)
    except Exception as e:
        return {"error": f"Gagal membaca rclone.conf: {str(e)}"}

    if not conf.has_section(remote_name):
        return {"error": f"Remote '{remote_name}' tidak ditemukan di rclone.conf"}

    client_id = conf.get(remote_name, "client_id", fallback="")
    if not client_id:
        return {"error": f"client_id tidak ditemukan untuk {remote_name}"}

    params = {
        "client_id": client_id,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/drive",
        "access_type": "offline",
        "prompt": "consent"
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return {"status": "ok", "remote": remote_name, "auth_url": auth_url}

def verify_and_reconnect_gdrive(remote_name, code):
    """Exchanges authorization code for token, updates rclone.conf, and restarts mount service."""
    conf_path = get_rclone_conf_path()
    conf = configparser.RawConfigParser()
    try:
        conf.read(conf_path)
    except Exception as e:
        return {"error": f"Gagal membaca rclone.conf: {str(e)}"}

    if not conf.has_section(remote_name):
        return {"error": f"Remote '{remote_name}' tidak ditemukan di rclone.conf"}

    client_id = conf.get(remote_name, "client_id", fallback="")
    client_secret = conf.get(remote_name, "client_secret", fallback="")
    if not client_id or not client_secret:
        return {"error": f"client_id / client_secret kosong untuk {remote_name}"}

    token_url = "https://oauth2.googleapis.com/token"
    token_params = {
        "code": code.strip(),
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "authorization_code"
    }
    post_data = urllib.parse.urlencode(token_params).encode("utf-8")
    req = urllib.request.Request(token_url, data=post_data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "rclone-monitor-agent"
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_resp = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        return {"error": f"Google OAuth error: {err_msg}"}
    except Exception as e:
        return {"error": f"Koneksi ke Google gagal: {str(e)}"}

    if "access_token" not in token_resp:
        return {"error": "Respon Google tidak menyertakan access_token"}

    # Form rclone token JSON structure
    expires_in = token_resp.get("expires_in", 3600)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    expiry_dt = now_utc + datetime.timedelta(seconds=expires_in)
    
    rclone_token_dict = {
        "access_token": token_resp["access_token"],
        "token_type": token_resp.get("token_type", "Bearer"),
        "refresh_token": token_resp.get("refresh_token", ""),
        "expiry": expiry_dt.isoformat(),
        "expires_in": expires_in
    }

    # If Google did not return a new refresh_token, retain existing refresh_token
    if not rclone_token_dict["refresh_token"]:
        old_token_raw = conf.get(remote_name, "token", fallback="{}")
        try:
            old_token = json.loads(old_token_raw)
            rclone_token_dict["refresh_token"] = old_token.get("refresh_token", "")
        except Exception:
            pass

    # Save to rclone.conf
    conf.set(remote_name, "token", json.dumps(rclone_token_dict))
    try:
        with open(conf_path, "w") as f:
            conf.write(f)
    except Exception as e:
        return {"error": f"Gagal menyimpan ke rclone.conf: {str(e)}"}

    # Restart user systemd mount service
    service_name = f"rclone-gdrive-{remote_name}.service"
    try:
        subprocess.run(["systemctl", "--user", "restart", service_name], check=True, timeout=10)
        restarted = True
    except Exception as e:
        restarted = False

    return {
        "status": "ok",
        "message": f"Berhasil memperbarui token {remote_name}!",
        "service_restarted": restarted
    }

_backup_cache = {"time": 0, "data": None}

def get_backup_status():
    """Inspects last 24h backup state and forecasts next 24h backup safety."""
    global _backup_cache
    now = time.time()
    if _backup_cache["data"] and (now - _backup_cache["time"]) < 20:
        return _backup_cache["data"]

    backup_dir = "/home/faizen/lazisnu-cilacap/backup-siftnu"
    status_24h = {
        "has_run": False,
        "success": False,
        "status": "UNKNOWN",
        "last_run": None,
        "last_log": None,
        "start_time": None,
        "finish_time": None,
        "duration": None,
        "parts_count": 0,
        "total_size_mb": 0.0,
        "target_storage": "Google Drive (faizzzzz:backup-siftnu)",
        "details": ""
    }

    forecast = {
        "status": "AMAN",
        "checks": [],
        "summary": "Kapasitas server & remote siap untuk backup 24 jam ke depan."
    }

    try:
        if os.path.exists(backup_dir):
            logs = glob.glob(os.path.join(backup_dir, "*_backup.log"))
            if logs:
                logs.sort(key=os.path.getmtime, reverse=True)
                latest_log = logs[0]
                mtime = os.path.getmtime(latest_log)
                status_24h["last_log"] = os.path.basename(latest_log)
                status_24h["last_run"] = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

                is_within_24h = (now - mtime) <= 86400
                status_24h["has_run"] = is_within_24h

                with open(latest_log, "r", errors="ignore") as f:
                    content = f.read()

                if "BACKUP BERHASIL" in content:
                    status_24h["success"] = True
                    status_24h["status"] = "BERHASIL"
                elif "BACKUP GAGAL" in content or "ERROR" in content:
                    status_24h["success"] = False
                    status_24h["status"] = "GAGAL"
                else:
                    status_24h["status"] = "BERJALAN" if (now - mtime) < 600 else "SELESAI"

                m_start = re.search(r"Mulai backup:\s*([^\n]+)", content)
                if m_start:
                    status_24h["start_time"] = m_start.group(1).strip()

                m_end = re.search(r"Selesai:\s*([^\n]+)", content)
                if m_end:
                    status_24h["finish_time"] = m_end.group(1).strip()

                if m_start and m_end:
                    try:
                        t1 = datetime.datetime.strptime(m_start.group(1).strip(), "%d-%m-%Y %H:%M:%S")
                        t2 = datetime.datetime.strptime(m_end.group(1).strip(), "%d-%m-%Y %H:%M:%S")
                        secs = int((t2 - t1).total_seconds())
                        mins = secs // 60
                        rem_sec = secs % 60
                        status_24h["duration"] = f"{mins}m {rem_sec}s" if mins > 0 else f"{rem_sec}s"
                    except Exception:
                        pass

                prefix = os.path.basename(latest_log).split("_backup.log")[0]
                part_files = glob.glob(os.path.join(backup_dir, f"{prefix}*.sql.gz"))
                status_24h["parts_count"] = len(part_files)
                tot_bytes = sum(os.path.getsize(p) for p in part_files if os.path.exists(p))
                status_24h["total_size_mb"] = round(tot_bytes / (1024 * 1024), 1)
    except Exception as e:
        status_24h["details"] = str(e)

    # 1. Google Drive mount check
    gdrive_ok = False
    try:
        g_usage = shutil.disk_usage("/home/faizen/gdrive-faizzzzz")
        gdrive_free_gb = round(g_usage.free / (1024**3), 1)
        gdrive_ok = g_usage.free > (2 * 1024**3)
        forecast["checks"].append({
            "name": "Google Drive faizzzzz",
            "status": "OK" if gdrive_ok else "WARNING",
            "detail": f"{gdrive_free_gb} GB free (>9000 siklus)"
        })
    except Exception as e:
        forecast["checks"].append({
            "name": "Google Drive faizzzzz",
            "status": "ERROR",
            "detail": str(e)
        })

    # 2. Local buffer root disk check
    root_ok = False
    try:
        r_usage = shutil.disk_usage("/")
        root_free_gb = round(r_usage.free / (1024**3), 1)
        root_ok = r_usage.free > (2 * 1024**3)
        forecast["checks"].append({
            "name": "Buffer Staging Disk (/)",
            "status": "OK" if root_ok else "WARNING",
            "detail": f"{root_free_gb} GB free (>60 siklus)"
        })
    except Exception as e:
        forecast["checks"].append({
            "name": "Buffer Staging Disk (/)",
            "status": "ERROR",
            "detail": str(e)
        })

    # 3. Remote DB check
    remote_ok = False
    t0 = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("srv173.niagahoster.com", 65002))
        s.close()
        lat = round((time.time() - t0) * 1000, 1)
        remote_ok = True
        forecast["checks"].append({
            "name": "Remote DB (srv173:65002)",
            "status": "OK",
            "detail": f"Terhubung ({lat} ms)"
        })
    except Exception as e:
        forecast["checks"].append({
            "name": "Remote DB (srv173:65002)",
            "status": "WARNING",
            "detail": f"Gagal/Timeout: {e}"
        })

    # 4. RAM check
    try:
        mem = get_memory_info()
        avail_mb = mem.get("available", 0) / (1024 * 1024)
        forecast["checks"].append({
            "name": "RAM Server",
            "status": "OK" if avail_mb > 500 else "WARNING",
            "detail": f"{round(avail_mb/1024, 1)} GB tersedia"
        })
    except Exception:
        pass

    if gdrive_ok and root_ok and remote_ok:
        forecast["status"] = "AMAN"
        forecast["summary"] = "Semua parameter aman (Kapasitas GDrive, staging disk, dan koneksi remote DB stabil)."
    elif not gdrive_ok or not root_ok:
        forecast["status"] = "KRITIS"
        forecast["summary"] = "Peringatan: Kapasitas penyimpanan tidak mencukupi untuk siklus 24 jam ke depan."
    else:
        forecast["status"] = "WASPADA"
        forecast["summary"] = "Peringatan: Koneksi remote database atau komponen server memerlukan perhatian."

    result = {
        "status_24h": status_24h,
        "forecast": forecast
    }
    _backup_cache = {"time": now, "data": result}
    return result

def get_okx_trading_agent_info():
    """Reads OKX 24/7 Trading Agent status, SQLite dual-ledger, and recent journal log (Strictly Read-Only)."""
    info = {
        "installed": False,
        "service_status": "offline",
        "pid": None,
        "memory_mb": 0.0,
        "target": "BTC-USDT-SWAP",
        "last_price": None,
        "last_eval": None,
        "demo": {
            "name": "Runner 'demo' (Skill 1 - Quant Momentum)",
            "status": "NORMAL",
            "equity": 0.0,
            "hwm": 0.0,
            "drawdown_pct": 0.0,
            "daily_loss_usd": 0.0,
            "trades_count": 0,
            "win_rate": 0.0,
            "realized_pnl": 0.0,
            "avg_r": 0.0,
            "profit_factor": 0.0,
            "edge": "Top-Down MTF + EV >= +0.35R",
            "circuit": "Daily 2% / Kill 10%"
        },
        "demoo": {
            "name": "Runner 'demoo' (Skill 2 - Trading Risk Guard)",
            "status": "NORMAL",
            "equity": 0.0,
            "hwm": 0.0,
            "drawdown_pct": 0.0,
            "daily_loss_usd": 0.0,
            "trades_count": 0,
            "win_rate": 0.0,
            "realized_pnl": 0.0,
            "avg_r": 0.0,
            "profit_factor": 0.0,
            "edge": "Fixed 1R=$100 + Trailing +1.0R",
            "circuit": "Daily 2% / Kill 10%"
        },
        "recent_logs": []
    }

    db_path = "/home/faizen/dev/okx-trading-agent/db/ledger.db"
    if not os.path.exists(db_path):
        return info

    info["installed"] = True

    # 1. Service Status
    try:
        p = subprocess.run(["systemctl", "--user", "is-active", "okx-trading-agent.service"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        is_active = p.stdout.strip() == "active"
        info["service_status"] = "online" if is_active else "offline"
    except Exception:
        pass

    # 2. PID & Memory
    try:
        p = subprocess.run(["systemctl", "--user", "show", "okx-trading-agent.service", "--property=MainPID"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in p.stdout.strip().split("\n"):
            if line.startswith("MainPID="):
                pid = int(line.split("=")[1])
                if pid > 0:
                    info["pid"] = pid
                    try:
                        with open(f"/proc/{pid}/status", "r") as f:
                            for sline in f:
                                if sline.startswith("VmRSS:"):
                                    kb = float(sline.split()[1])
                                    info["memory_mb"] = round(kb / 1024, 1)
                                    break
                    except Exception:
                        pass
    except Exception:
        pass

    # 3. Read SQLite dual-ledger (Strict Read-Only Mode)
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        c = conn.cursor()

        for prof in ["demo", "demoo"]:
            # Latest equity snapshot
            row = c.execute("SELECT total_equity, high_water_mark, drawdown_pct FROM equity_snapshots WHERE profile = ? ORDER BY id DESC LIMIT 1", (prof,)).fetchone()
            if row:
                info[prof]["equity"] = round(float(row[0]), 2)
                info[prof]["hwm"] = round(float(row[1]), 2)
                info[prof]["drawdown_pct"] = round(float(row[2]) * 100, 2)

            # Trades stats
            trades_rows = c.execute("SELECT pnl_usd, r_multiple, status FROM trades WHERE profile = ? AND status = 'CLOSED'", (prof,)).fetchall()
            info[prof]["trades_count"] = len(trades_rows)
            if trades_rows:
                wins = sum(1 for t in trades_rows if float(t[0]) > 0)
                info[prof]["win_rate"] = round((wins / len(trades_rows)) * 100, 1)
                info[prof]["realized_pnl"] = round(sum(float(t[0]) for t in trades_rows), 2)
                info[prof]["avg_r"] = round(sum(float(t[1]) for t in trades_rows) / len(trades_rows), 2)
                gross_win = sum(float(t[0]) for t in trades_rows if float(t[0]) > 0)
                gross_loss = abs(sum(float(t[0]) for t in trades_rows if float(t[0]) < 0))
                info[prof]["profit_factor"] = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)

            # Circuit breaker check
            cb_row = c.execute("SELECT event_type FROM circuit_breaker_events WHERE profile = ? ORDER BY id DESC LIMIT 1", (prof,)).fetchone()
            if cb_row:
                info[prof]["status"] = cb_row[0]

        conn.close()
    except Exception:
        pass

    # 4. Recent Journal Logs & last price / signal
    try:
        p = subprocess.run(["journalctl", "--user", "-u", "okx-trading-agent.service", "-n", "10", "--no-pager"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if p.returncode == 0:
            lines = [l.strip() for l in p.stdout.strip().split("\n") if l.strip()]
            info["recent_logs"] = lines[-4:]
            for l in reversed(lines):
                if "Live: $" in l:
                    match = re.search(r"Live:\s*\$([0-9\.,]+)", l)
                    if match and not info["last_price"]:
                        info["last_price"] = match.group(1)
                elif "Close:" in l:
                    match = re.search(r"Close:\s*([0-9\.]+)", l)
                    if match and not info["last_price"]:
                        info["last_price"] = f"{float(match.group(1)):,.1f}"
                elif "Evaluated Signal" in l:
                    if not info["last_eval"]:
                        info["last_eval"] = l.split("okx.daemon")[-1].strip()
    except Exception:
        pass

    return info

def get_cron_tasks_info():
    """Reads cronjob logs for wifi and siftnu and returns structured state."""
    cron_dir = "/home/faizen/scripts/cronjobs/logs"
    now = datetime.datetime.now()

    # 1. WiFi Task
    wifi_log_file = os.path.join(cron_dir, "wifi.log")
    today_slots = [
        (datetime.time(8, 0), "08:00 WIB (Tutup)"),
        (datetime.time(12, 0), "12:00 WIB (Buka)"),
        (datetime.time(13, 0), "13:00 WIB (Tutup)"),
        (datetime.time(16, 0), "16:00 WIB (Buka)")
    ]
    next_slot = "Besok 08:00 WIB (Tutup)"
    for slot_time, label in today_slots:
        if now.time() < slot_time:
            next_slot = f"Hari ini {label}"
            break

    wifi_info = {
        "name": "WiFi 2.4G Router",
        "target": "cron",
        "log_name": "wifi",
        "state": "UNKNOWN",
        "last_run": "-",
        "last_status": "Belum ada riwayat log",
        "schedule": "08:00 (Tutup), 12:00 (Buka), 13:00 (Tutup), 16:00 (Buka)",
        "next_schedule": next_slot
    }

    if os.path.exists(wifi_log_file):
        try:
            mtime = os.path.getmtime(wifi_log_file)
            wifi_info["last_run"] = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            with open(wifi_log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = [line.strip() for line in f if line.strip()]
            if lines:
                wifi_info["last_status"] = lines[-1]
                content = "\n".join(lines[-25:])
                if "WIFI OPEN" in content or "wifi-open" in content or "UNLOCK" in content:
                    wifi_info["state"] = "OPEN"
                elif "WIFI CLOSE" in content or "wifi-close" in content or "LOCK" in content:
                    wifi_info["state"] = "CLOSED"
                elif "[INIT]" in content:
                    wifi_info["state"] = "INIT"

                if any(x in lines[-1].lower() for x in ["failed", "gagal", "error", "exit code"]):
                    wifi_info["state"] = "ERROR"
        except Exception as e:
            wifi_info["last_status"] = f"Error reading log: {e}"

    # 2. Siftnu Backup Task
    siftnu_log_file = os.path.join(cron_dir, "siftnu.log")
    siftnu_info = {
        "name": "Backup Siftnu Harian",
        "target": "cron",
        "log_name": "siftnu",
        "state": "UNKNOWN",
        "last_run": "-",
        "last_status": "Belum ada riwayat log",
        "schedule": "00:00 WIB (Setiap Tengah Malam)",
        "next_schedule": "Pukul 00:00 WIB"
    }

    if os.path.exists(siftnu_log_file):
        try:
            mtime = os.path.getmtime(siftnu_log_file)
            siftnu_info["last_run"] = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            with open(siftnu_log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = [line.strip() for line in f if line.strip()]
            if lines:
                content = "\n".join(lines[-30:])
                if "BACKUP BERHASIL" in content or "✅" in content:
                    siftnu_info["state"] = "SUCCESS"
                    siftnu_info["last_status"] = "✅ Backup Siftnu Berhasil"
                elif "BACKUP GAGAL" in content or "❌" in content:
                    siftnu_info["state"] = "ERROR"
                    siftnu_info["last_status"] = "❌ Backup Siftnu Gagal"
                elif "[INIT]" in content:
                    siftnu_info["state"] = "INIT"
                    siftnu_info["last_status"] = lines[-1]
                else:
                    siftnu_info["last_status"] = lines[-1]
        except Exception as e:
            siftnu_info["last_status"] = f"Error reading log: {e}"

    return {
        "wifi": wifi_info,
        "siftnu": siftnu_info
    }


def get_power_info():
    """Reads power supply, AC status, and battery metrics."""
    ac_online = False
    battery = None

    try:
        for path in sorted(glob.glob("/sys/class/power_supply/*")):
            name = os.path.basename(path)
            data = {"name": name}
            for attr in ["type", "status", "capacity", "online", "model_name", "manufacturer", "power_now", "voltage_now"]:
                f = os.path.join(path, attr)
                if os.path.exists(f):
                    try:
                        with open(f) as fp:
                            data[attr] = fp.read().strip()
                    except Exception:
                        pass

            sup_type = data.get("type", "")
            if sup_type in ("Mains", "USB") or "AC" in name:
                if data.get("online") == "1":
                    ac_online = True

            if sup_type == "Battery" or "BAT" in name:
                try:
                    cap = int(data.get("capacity", "0"))
                except ValueError:
                    cap = 0
                status = data.get("status", "Unknown")
                p_watts = 0.0
                if "power_now" in data:
                    try:
                        p_watts = round(int(data["power_now"]) / 1000000.0, 1)
                    except ValueError:
                        pass
                v_volts = 0.0
                if "voltage_now" in data:
                    try:
                        v_volts = round(int(data["voltage_now"]) / 1000000.0, 2)
                    except ValueError:
                        pass

                battery = {
                    "name": name,
                    "present": data.get("present", "1") == "1",
                    "percent": cap,
                    "status": status,
                    "model": data.get("model_name") or data.get("manufacturer") or "Generic Battery",
                    "power_watts": p_watts,
                    "voltage": v_volts,
                    "is_charging": status.lower() == "charging",
                    "plugged": ac_online or status.lower() in ("charging", "not charging", "full")
                }
    except Exception:
        pass

    if battery and not ac_online and battery["status"].lower() in ("charging", "not charging", "full"):
        ac_online = True

    return {
        "has_battery": battery is not None,
        "ac_online": ac_online,
        "battery": battery
    }

def get_temperatures():
    """Reads temperature sensors from /sys/class/hwmon and /sys/class/thermal."""
    sensors = []
    seen = set()

    try:
        for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name_f = os.path.join(h, "name")
            chip_name = "hwmon"
            if os.path.exists(name_f):
                try:
                    with open(name_f) as fp:
                        chip_name = fp.read().strip()
                except Exception:
                    pass

            for inp in sorted(glob.glob(os.path.join(h, "temp*_input"))):
                lbl_file = inp.replace("_input", "_label")
                sub_label = ""
                if os.path.exists(lbl_file):
                    try:
                        with open(lbl_file) as fp:
                            sub_label = fp.read().strip()
                    except Exception:
                        pass

                try:
                    with open(inp) as fp:
                        val = int(fp.read().strip()) / 1000.0
                    if val <= 0 or val > 150:
                        continue

                    display_name = chip_name
                    if chip_name in ("k10temp", "coretemp"):
                        display_name = "CPU"
                        if sub_label:
                            display_name += f" ({sub_label})"
                    elif chip_name == "amdgpu":
                        display_name = "GPU (AMD)"
                        if sub_label:
                            display_name += f" ({sub_label})"
                    elif chip_name == "nvme":
                        display_name = "NVMe SSD"
                        if sub_label:
                            display_name += f" ({sub_label})"
                    elif chip_name == "acpitz":
                        display_name = "Motherboard (ACPI)"
                    else:
                        if sub_label:
                            display_name += f" - {sub_label}"

                    if display_name not in seen:
                        seen.add(display_name)
                        sensors.append({
                            "chip": chip_name,
                            "label": display_name,
                            "temp": round(val, 1)
                        })
                except Exception:
                    pass
    except Exception:
        pass

    if not sensors:
        try:
            for t in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
                try:
                    with open(os.path.join(t, "type")) as fp:
                        t_type = fp.read().strip()
                    with open(os.path.join(t, "temp")) as fp:
                        val = int(fp.read().strip()) / 1000.0
                    if 0 < val <= 150:
                        sensors.append({
                            "chip": "thermal_zone",
                            "label": t_type,
                            "temp": round(val, 1)
                        })
                except Exception:
                    pass
        except Exception:
            pass

    def sort_order(s):
        lbl = s["label"].lower()
        if "cpu" in lbl:
            return 0
        if "gpu" in lbl:
            return 1
        if "nvme" in lbl or "ssd" in lbl:
            return 2
        return 3

    return sorted(sensors, key=sort_order)

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
                cur_freq_khz = _latest_metrics.get("cur_freq_khz", 0)
                cur_ghz = _latest_metrics.get("cur_ghz", 0.0)

            payload = {
                "system": get_os_info(),
                "uptime_load": get_uptime_and_load(),
                "cpu": {
                    "usage_percent": cpu_total,
                    "cur_freq_khz": cur_freq_khz,
                    "cur_ghz": cur_ghz,
                    "cores": cpu_cores
                },
                "memory": get_memory_info(),
                "disks": get_disk_info(),
                "network": net_rates,
                "services": get_service_statuses(),
                "gdrive": get_gdrive_info(),
                "backup": get_backup_status(),
                "cron_tasks": get_cron_tasks_info(),
                "okx_trading": get_okx_trading_agent_info(),
                "power": get_power_info(),
                "temperatures": get_temperatures(),
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

        if self.path.startswith("/api/logs"):
            parsed_url = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed_url.query)

            target = params.get("target", [""])[0].strip().lower()
            name = params.get("name", [""])[0].strip()
            lines_raw = params.get("lines", ["60"])[0].strip()

            try:
                lines = min(max(1, int(lines_raw)), 200)
            except ValueError:
                lines = 60

            # Strict sanitization: alphanumeric, underscore, hyphen, dot
            if not name or not re.match(r'^[a-zA-Z0-9_\-\.]+$', name):
                res = {"status": "error", "message": "Parameter 'name' tidak valid atau kosong."}
                data = json.dumps(res).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return

            log_text = ""
            if target == "cron":
                log_file = os.path.join("/home/faizen/scripts/cronjobs/logs", f"{name}.log")
                if not os.path.exists(log_file):
                    res = {"status": "error", "message": f"File log '{name}.log' tidak ditemukan."}
                    data = json.dumps(res).encode("utf-8")
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        all_lines = f.readlines()
                        log_text = "".join(all_lines[-lines:])
                except Exception as e:
                    res = {"status": "error", "message": f"Gagal membaca file log: {str(e)}"}
                    data = json.dumps(res).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return

            elif target == "systemd":
                try:
                    cmd = ["journalctl", "--user", "-u", name, "-n", str(lines), "--no-pager"]
                    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
                    log_text = p.stdout or p.stderr
                    if not log_text and p.returncode != 0:
                        log_text = f"journalctl exit code {p.returncode}: {p.stderr}"
                except Exception as e:
                    res = {"status": "error", "message": f"Gagal menjalankan journalctl: {str(e)}"}
                    data = json.dumps(res).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return

            elif target == "docker":
                try:
                    cmd = ["docker", "logs", "--tail", str(lines), name]
                    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
                    log_text = p.stdout
                    if p.returncode != 0 and not log_text:
                        log_text = f"docker logs exit code {p.returncode}"
                except Exception as e:
                    res = {"status": "error", "message": f"Gagal menjalankan docker logs: {str(e)}"}
                    data = json.dumps(res).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return

            else:
                res = {"status": "error", "message": f"Target '{target}' tidak valid. Pilihan: cron, systemd, docker."}
                data = json.dumps(res).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return

            res = {
                "status": "ok",
                "target": target,
                "name": name,
                "lines": lines,
                "logs": log_text
            }
            data = json.dumps(res).encode("utf-8")
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

    def do_POST(self):
        if self.path.startswith("/api/gdrive/auth-url"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8")) if body else {}
                remote = payload.get("remote", "faizzzzz")
                res = get_gdrive_auth_url(remote)
                status_code = 200 if "error" not in res else 400
                data = json.dumps(res).encode("utf-8")
            except Exception as e:
                status_code = 500
                data = json.dumps({"error": str(e)}).encode("utf-8")

            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == "/api/gdrive/verify-token":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8")) if body else {}
                remote = payload.get("remote", "")
                code = payload.get("code", "")
                if not remote or not code:
                    res = {"error": "Parameter remote dan code wajib diisi."}
                    status_code = 400
                else:
                    res = verify_and_reconnect_gdrive(remote, code)
                    status_code = 200 if "error" not in res else 400
                data = json.dumps(res).encode("utf-8")
            except Exception as e:
                status_code = 500
                data = json.dumps({"error": str(e)}).encode("utf-8")

            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        # Keep stdout clean; suppress spammy GET polling logs
        try:
            msg = format % args
            if "GET /api/" in msg or "GET /health" in msg:
                return
            super().log_message(format, *args)
        except Exception:
            pass

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
