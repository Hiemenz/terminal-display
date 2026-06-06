"""Rolling history of system stats for sparkline trends.

A small disk-persisted ring buffer (so trends survive restarts). One sample
is appended each time full stats are collected (the slower stats cadence — see
main._loop_cycle), and samples older than the configured window are dropped.

Stored per sample: cpu %, memory %, 1-minute load, and cumulative network
bytes. Network throughput is derived at read time as the byte delta between
consecutive samples, so we don't have to persist any rate state.
"""
import json
import os
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(_REPO_ROOT, 'data', 'stats_history.json')

DEFAULT_WINDOW_MIN = 60      # how long to keep history (minutes)
_MAX_SAMPLES = 720           # hard cap so the file can't grow unbounded


def _window_seconds(config: dict) -> int:
    return int(config.get('stats_history_minutes', DEFAULT_WINDOW_MIN)) * 60


def _load() -> list:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data.get('samples', []) if isinstance(data, dict) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(samples: list):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'samples': samples}, f)
    os.replace(tmp, STATE_FILE)  # atomic — never leave a half-written file


def record(stats: dict, config: dict):
    """Append one sample from a freshly-collected stats dict and trim the window."""
    net = stats.get('network', {}) or {}
    net_bytes = (net.get('bytes_sent') or 0) + (net.get('bytes_recv') or 0)
    load = stats.get('load')
    load1 = float(load[0]) if load else None

    sample = {
        't': time.time(),
        'cpu': float(stats.get('cpu_percent') or 0.0),
        'mem': float((stats.get('memory', {}) or {}).get('percent') or 0.0),
        'load': load1,
        'net_bytes': int(net_bytes),
    }

    samples = _load()
    samples.append(sample)

    cutoff = time.time() - _window_seconds(config)
    samples = [s for s in samples if s.get('t', 0) >= cutoff][-_MAX_SAMPLES:]

    try:
        _save(samples)
    except OSError:
        pass


def snapshot(config: dict) -> dict:
    """Return per-metric value series within the window, for rendering.

    {
      'cpu':  [float, ...],   # %
      'mem':  [float, ...],   # %
      'load': [float, ...],   # 1-min load (may be empty if load unavailable)
      'net':  [float, ...],   # bytes/sec, derived from consecutive samples
      'window_minutes': int,
    }
    """
    cutoff = time.time() - _window_seconds(config)
    samples = [s for s in _load() if s.get('t', 0) >= cutoff]

    cpu  = [s['cpu'] for s in samples if s.get('cpu') is not None]
    mem  = [s['mem'] for s in samples if s.get('mem') is not None]
    load = [s['load'] for s in samples if s.get('load') is not None]

    # Network throughput = byte delta / time delta between consecutive samples.
    net = []
    for prev, cur in zip(samples, samples[1:]):
        dt = cur.get('t', 0) - prev.get('t', 0)
        db = cur.get('net_bytes', 0) - prev.get('net_bytes', 0)
        net.append(max(0.0, db / dt) if dt > 0 and db >= 0 else 0.0)

    return {
        'cpu': cpu,
        'mem': mem,
        'load': load,
        'net': net,
        'window_minutes': int(config.get('stats_history_minutes', DEFAULT_WINDOW_MIN)),
    }
