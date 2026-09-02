"""
Background data fetcher for the tile screensaver.

Each source runs on its own TTL; a single background thread polls all of them
every minute and re-fetches anything that has gone stale.

Usage::

    fetcher = TileFetcher(config)
    fetcher.start()
    data = fetcher.data()
    # {'weather': {...}, 'git': [...], 'mlb': {...}, 'cpu_temp': float|None}
    fetcher.stop()
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_WEATHER_TTL  = 1800   # 30 min
_GIT_TTL      = 300    # 5 min
_MLB_TTL      = 300    # 5 min
_CPU_TEMP_TTL = 60     # 1 min
_POLL         = 60     # how often the background thread wakes up


class TileFetcher:
    def __init__(self, config: dict):
        self._config = config
        self._cache: dict[str, tuple] = {}   # key -> (data, fetched_at)
        self._lock  = threading.Lock()
        self._stop  = threading.Event()
        self._thread: threading.Thread | None = None

    # ── public ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name='tile-fetcher', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def data(self) -> dict:
        with self._lock:
            return {k: v[0] for k, v in self._cache.items()}

    # ── background loop ───────────────────────────────────────────────────────

    def _run(self) -> None:
        self._refresh_all()
        while not self._stop.is_set():
            self._stop.wait(_POLL)
            if not self._stop.is_set():
                self._refresh_all()

    def _refresh_all(self) -> None:
        self._maybe('cpu_temp', _CPU_TEMP_TTL, self._fetch_cpu_temp)
        self._maybe('git',      _GIT_TTL,      self._fetch_git)
        self._maybe('weather',  _WEATHER_TTL,  self._fetch_weather)
        self._maybe('mlb',      _MLB_TTL,      self._fetch_mlb)

    def _maybe(self, key: str, ttl: int, fn) -> None:
        with self._lock:
            entry = self._cache.get(key)
            if entry and (time.time() - entry[1]) < ttl:
                return
        try:
            result = fn()
            with self._lock:
                self._cache[key] = (result, time.time())
        except Exception as exc:
            logger.debug('TileFetcher[%s] skipped: %s', key, exc)

    # ── CPU temperature ───────────────────────────────────────────────────────

    def _fetch_cpu_temp(self) -> float | None:
        try:
            import psutil
            temps = psutil.sensors_temperatures()
            for key in ('cpu_thermal', 'cpu-thermal', 'coretemp', 'soc_thermal'):
                if temps.get(key):
                    return temps[key][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass
        return None

    # ── weather (wttr.in, no API key required) ────────────────────────────────

    def _fetch_weather(self) -> dict | None:
        location = self._config.get('screensaver_tiles_weather_location', '').strip()
        if not location:
            return None
        url = f'https://wttr.in/{urllib.parse.quote(location)}?format=j1'
        req = urllib.request.Request(url, headers={'User-Agent': 'terminal-display/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
        cur   = raw['current_condition'][0]
        today = raw['weather'][0]
        return {
            'location':    location,
            'temp_f':      int(cur.get('temp_F', 0)),
            'temp_c':      int(cur.get('temp_C', 0)),
            'desc':        cur['weatherDesc'][0]['value'],
            'humidity':    int(cur.get('humidity', 0)),
            'high_f':      int(today.get('maxtempF', 0)),
            'low_f':       int(today.get('mintempF', 0)),
            'high_c':      int(today.get('maxtempC', 0)),
            'low_c':       int(today.get('mintempC', 0)),
            'feels_like_f': int(cur.get('FeelsLikeF', 0)),
            'feels_like_c': int(cur.get('FeelsLikeC', 0)),
        }

    # ── git activity ──────────────────────────────────────────────────────────

    def _fetch_git(self) -> list:
        scan_dir = self._config.get('screensaver_tiles_git_scan_dir', '').strip()
        if not scan_dir or not os.path.isdir(scan_dir):
            return []
        repos = []
        try:
            for entry in sorted(os.scandir(scan_dir), key=lambda e: e.name):
                if entry.is_dir() and os.path.isdir(os.path.join(entry.path, '.git')):
                    info = self._git_repo_info(entry.path, entry.name)
                    if info is not None:
                        repos.append(info)
        except Exception as exc:
            logger.debug('git scan error: %s', exc)
        return repos

    def _git_repo_info(self, repo_path: str, name: str) -> dict | None:
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            r1 = subprocess.run(
                ['git', '-C', repo_path, 'log', '--oneline',
                 f'--after={today} 00:00:00'],
                capture_output=True, text=True, timeout=5,
            )
            commits_today = len([l for l in r1.stdout.strip().splitlines() if l])

            r2 = subprocess.run(
                ['git', '-C', repo_path, 'log', '-1', '--format=%ar\t%s\t%D'],
                capture_output=True, text=True, timeout=5,
            )
            rel_time, subject, branch = '', '', 'main'
            if r2.stdout.strip():
                parts = r2.stdout.strip().split('\t', 2)
                rel_time = parts[0] if parts else ''
                subject  = parts[1][:30] if len(parts) > 1 else ''
                refs_str = parts[2]      if len(parts) > 2 else ''
                for ref in refs_str.split(','):
                    ref = ref.strip()
                    if ref.startswith('HEAD -> '):
                        branch = ref[8:]
                        break
                    if 'origin/' in ref:
                        branch = ref.split('origin/')[-1].strip()

            return {
                'name':          name,
                'branch':        branch,
                'commits_today': commits_today,
                'last_relative': rel_time,
                'last_subject':  subject,
            }
        except Exception:
            return None

    # ── MLB game (free statsapi.mlb.com, no key) ──────────────────────────────

    def _fetch_mlb(self) -> dict:
        team_id   = int(self._config.get('screensaver_tiles_mlb_team_id',   147))
        team_abbr = self._config.get('screensaver_tiles_mlb_team_abbr', 'NYY').strip()
        today_str = date.today().strftime('%Y-%m-%d')
        end_str   = (date.today() + timedelta(days=7)).strftime('%Y-%m-%d')

        url = (
            'https://statsapi.mlb.com/api/v1/schedule?sportId=1'
            f'&teamId={team_id}&startDate={today_str}&endDate={end_str}'
            '&gameType=R&hydrate=linescore'
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = json.loads(resp.read())

        games = [g for d in raw.get('dates', []) for g in d.get('games', [])]
        if not games:
            return {'status': 'no_game', 'team_abbr': team_abbr}

        def _priority(g):
            state = g.get('status', {}).get('detailedState', '')
            gdate = g.get('officialDate', '')
            if state in ('In Progress', 'Warmup', 'Pre-Game'):
                return 0
            if gdate == today_str:
                return 1
            return 2

        g = sorted(games, key=_priority)[0]
        state = g.get('status', {}).get('detailedState', '')
        home  = g.get('teams', {}).get('home', {})
        away  = g.get('teams', {}).get('away', {})

        home_abbr  = home.get('team', {}).get('abbreviation', '?')
        away_abbr  = away.get('team', {}).get('abbreviation', '?')
        home_score = home.get('score')
        away_score = away.get('score')

        ls      = g.get('linescore', {})
        inning  = ls.get('currentInningOrdinal', '')
        top     = ls.get('isTopInning', True)

        start_str = _format_game_time(g.get('gameDate', ''))
        game_date = g.get('officialDate', today_str)

        return {
            'team_abbr':  team_abbr,
            'status':     state,
            'home':       home_abbr,
            'away':       away_abbr,
            'home_score': home_score,
            'away_score': away_score,
            'inning':     inning,
            'top':        top,
            'start_str':  start_str,
            'game_date':  game_date,
            'venue':      g.get('venue', {}).get('name', ''),
        }


def _format_game_time(game_date_str: str) -> str:
    """Convert ISO UTC game time to a readable Eastern time string."""
    if not game_date_str:
        return ''
    try:
        dt_utc = datetime.fromisoformat(game_date_str.replace('Z', '+00:00'))
        # Eastern: UTC-4 during summer, UTC-5 otherwise.
        # Use a fixed -4 offset (EDT); good enough for the MLB season.
        et = dt_utc.astimezone(timezone(timedelta(hours=-4)))
        return et.strftime('%-I:%M %p ET')
    except Exception:
        return game_date_str[11:16] + ' UTC'
