"""
Collect system statistics for display.

Returns a dict with CPU, memory, disk, network, load, uptime, and top processes.
Requires: psutil
"""
import os
import time
import platform
import socket
from datetime import datetime, timedelta

import psutil


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


def _format_bytes(n: float) -> str:
    """Format bytes into human-readable string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


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
    now = datetime.now()
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

    return {
        'hostname': socket.gethostname().split('.')[0],
        'platform': platform.system(),
        'uptime': _get_uptime(),
        'time': now.strftime('%H:%M:%S'),
        'date': now.strftime('%a, %b %d %Y'),
        'cpu_percent': cpu_pct,
        'cpu_count': cpu_count,
        'cpu_freq_mhz': cpu_freq_mhz,
        'memory': memory,
        'disk': disk_info,
        'network': net_info,
        'load': load,
        'top_processes': top_procs,
    }
