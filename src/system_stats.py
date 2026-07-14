"""
Collect system statistics for display.

Returns a dict with CPU, memory, disk, network, load, uptime, and top processes.
Requires: psutil
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import psutil

# Cache for _get_pending_updates: polling apt's package lists isn't free, and
# the answer doesn't change faster than an `apt update` runs, so we re-check
# on a slow independent timer rather than every collect() cycle.
_updates_cache: dict = {'count': None, 'checked_at': 0.0}

# Cache for _get_ci_status: the GitHub Actions API is rate-limited and a
# build's conclusion doesn't change between polls, so it's checked on its
# own slow timer rather than every collect() cycle.
_ci_cache: dict = {'status': None, 'checked_at': 0.0}


def _get_uptime() -> str:
    """Return human-readable system uptime string."""
    boot_time = psutil.boot_time()
    uptime_seconds = time.time() - boot_time
    td = timedelta(seconds=int(uptime_seconds))
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


def _get_network_interface(preferred: str = '') -> str:
    """Pick the best network interface to display stats for."""
    if preferred:
        stats = psutil.net_io_counters(pernic=True)
        if preferred in stats:
            return preferred

    # Auto-detect: prefer eth0, en0, wlan0, then first non-loopback
    candidates = ['eth0', 'en0', 'wlan0', 'wlan1', 'ens3', 'enp0s3']
    stats = psutil.net_io_counters(pernic=True)
    for c in candidates:
        if c in stats:
            return c
    for name in stats:
        if name != 'lo':
            return name
    return 'lo'


def _get_ip_addresses() -> dict:
    """Return {interface: ip} for non-loopback IPv4 addresses, ordered by preference."""
    import socket as _socket
    result = {}
    prefer = ['eth0', 'en0', 'wlan0', 'wlan1']
    addrs = psutil.net_if_addrs()
    # Preferred interfaces first
    for iface in prefer:
        if iface in addrs:
            for snic in addrs[iface]:
                if snic.family == _socket.AF_INET and not snic.address.startswith('127.'):
                    result[iface] = snic.address
    # Then any remaining non-loopback IPv4
    for iface, snics in addrs.items():
        if iface in result or iface == 'lo':
            continue
        for snic in snics:
            if snic.family == _socket.AF_INET and not snic.address.startswith('127.'):
                result[iface] = snic.address
    return result


def _format_bytes(n: float) -> str:
    """Format bytes into human-readable string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _get_pending_updates(config: dict) -> int | None:
    """Count of upgradable apt packages, cached for
    updates_check_interval_minutes (default 60). Returns None where apt isn't
    available (macOS/dev, or a non-Debian Linux)."""
    if not shutil.which('apt'):
        return None
    interval = config.get('updates_check_interval_minutes', 60) * 60
    now = time.monotonic()
    if (_updates_cache['count'] is not None
            and (now - _updates_cache['checked_at']) < interval):
        return _updates_cache['count']
    try:
        r = subprocess.run(['apt', 'list', '--upgradable'],
                           capture_output=True, text=True, timeout=10)
        count = sum(1 for ln in r.stdout.splitlines()
                    if ln and not ln.startswith('Listing'))
    except Exception:
        count = _updates_cache['count']  # keep the stale value on a transient failure
    _updates_cache['count'] = count
    _updates_cache['checked_at'] = now
    return count


def _get_ci_status(config: dict) -> str | None:
    """Conclusion of the latest completed GitHub Actions run on
    ci_status_repo/ci_status_branch (e.g. 'success', 'failure'), cached for
    ci_status_check_interval_minutes (default 15). Returns None when
    unconfigured (ci_status_repo empty) or on any network/parse error —
    a bad connection must never block the render loop."""
    repo = config.get('ci_status_repo', '')
    if not repo:
        return None
    interval = config.get('ci_status_check_interval_minutes', 15) * 60
    now = time.monotonic()
    if (_ci_cache['status'] is not None
            and (now - _ci_cache['checked_at']) < interval):
        return _ci_cache['status']
    branch = config.get('ci_status_branch', 'main')
    url = (f'https://api.github.com/repos/{repo}/actions/runs'
           f'?branch={urllib.parse.quote(branch)}&status=completed&per_page=1')
    try:
        req = urllib.request.Request(
            url, headers={'Accept': 'application/vnd.github+json',
                          'User-Agent': 'terminal-display'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
        runs = data.get('workflow_runs', [])
        status = runs[0].get('conclusion') if runs else None
    except Exception:
        status = _ci_cache['status']  # keep the stale value on a transient failure
    _ci_cache['status'] = status
    _ci_cache['checked_at'] = now
    return status


def collect_time(config: dict) -> dict:
    """Cheap clock-only fields — no psutil sampling.

    Lets the displayed time tick every loop while the heavier stats are
    refreshed on a slower cadence.  Kept here so the time/date format lives
    in one place and can't drift from collect().
    """
    now = datetime.now()
    return {
        'uptime': _get_uptime(),
        'time': now.strftime('%H:%M:%S'),
        'date': now.strftime('%a, %b %d %Y'),
    }


def collect(config: dict) -> dict:
    """
    Collect all system stats.

    Returns:
        {
            'hostname': str,
            'platform': str,
            'uptime': str,
            'time': str,           # HH:MM:SS
            'date': str,           # Day, Mon DD YYYY
            'cpu_percent': float,
            'cpu_count': int,
            'cpu_freq_mhz': float | None,
            'cpu_temp_c': float | None,
            'memory': {
                'used': int, 'total': int, 'percent': float,
                'used_str': str, 'total_str': str,
            },
            'disk': {
                'used': int, 'total': int, 'percent': float,
                'used_str': str, 'total_str': str, 'path': str,
            },
            'network': {
                'interface': str,
                'bytes_sent_str': str,
                'bytes_recv_str': str,
                'bytes_sent': int,
                'bytes_recv': int,
            },
            'load': tuple[float, float, float] | None,
            'top_processes': list[dict],
        }
    """
    disk_path = config.get('disk_path', '/')
    net_iface = _get_network_interface(config.get('network_interface', ''))

    # CPU — use interval=0.5 for a quick real sample
    cpu_pct = psutil.cpu_percent(interval=0.5)
    cpu_count = psutil.cpu_count(logical=True)
    try:
        cpu_freq = psutil.cpu_freq()
        cpu_freq_mhz = cpu_freq.current if cpu_freq else None
    except Exception:
        cpu_freq_mhz = None

    # CPU temperature (Pi/Linux; None where unsupported, e.g. macOS)
    cpu_temp_c = None
    try:
        temps = psutil.sensors_temperatures()
        for key in ('cpu_thermal', 'cpu-thermal', 'coretemp', 'soc_thermal'):
            if temps.get(key):
                cpu_temp_c = temps[key][0].current
                break
        else:
            for entries in temps.values():
                if entries:
                    cpu_temp_c = entries[0].current
                    break
    except Exception:
        pass

    # Memory
    mem = psutil.virtual_memory()
    memory = {
        'used': mem.used,
        'total': mem.total,
        'percent': mem.percent,
        'used_str': _format_bytes(mem.used),
        'total_str': _format_bytes(mem.total),
    }

    # Disk
    try:
        disk = psutil.disk_usage(disk_path)
        disk_info = {
            'used': disk.used,
            'total': disk.total,
            'percent': disk.percent,
            'used_str': _format_bytes(disk.used),
            'total_str': _format_bytes(disk.total),
            'path': disk_path,
        }
    except Exception:
        disk_info = {
            'used': 0, 'total': 0, 'percent': 0,
            'used_str': '?', 'total_str': '?', 'path': disk_path,
        }

    # Network totals
    try:
        net = psutil.net_io_counters(pernic=True).get(net_iface)
        if net:
            net_info = {
                'interface': net_iface,
                'bytes_sent': net.bytes_sent,
                'bytes_recv': net.bytes_recv,
                'bytes_sent_str': _format_bytes(net.bytes_sent),
                'bytes_recv_str': _format_bytes(net.bytes_recv),
            }
        else:
            net_info = {'interface': net_iface, 'bytes_sent': 0, 'bytes_recv': 0,
                        'bytes_sent_str': '?', 'bytes_recv_str': '?'}
    except Exception:
        net_info = {'interface': net_iface, 'bytes_sent': 0, 'bytes_recv': 0,
                    'bytes_sent_str': '?', 'bytes_recv_str': '?'}

    # Load average (not on Windows)
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):
        load = None

    # Top processes by CPU
    n = config.get('top_process_count', 5)
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
        try:
            procs.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs.sort(key=lambda p: p.get('cpu_percent') or 0, reverse=True)
    top_procs = procs[:n]

    ip_addrs = _get_ip_addresses()
    primary_ip = next(iter(ip_addrs.values()), '')

    return {
        'hostname': socket.gethostname().split('.')[0],
        'platform': platform.system(),
        **collect_time(config),
        'cpu_percent': cpu_pct,
        'cpu_count': cpu_count,
        'cpu_freq_mhz': cpu_freq_mhz,
        'cpu_temp_c': cpu_temp_c,
        'memory': memory,
        'disk': disk_info,
        'network': net_info,
        'load': load,
        'top_processes': top_procs,
        'ip_addresses': ip_addrs,
        'primary_ip': primary_ip,
        'pending_updates': _get_pending_updates(config),
        'ci_status': _get_ci_status(config),
    }
