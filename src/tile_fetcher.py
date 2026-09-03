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


# ── Video frame manager ───────────────────────────────────────────────────────

class VideoFrameManager:
    """Extracts and advances frames from a video file using ffmpeg.

    Call advance() once per screensaver render to step forward by
    `advance_seconds` of video time.  get_frame_path() returns the most
    recently extracted JPEG, or None if not ready.
    """

    def __init__(self, video_path: str, data_dir: str, advance_seconds: int = 60):
        self._video_path = video_path
        self._frame_path = os.path.join(data_dir, 'video_current.jpg')
        self._state_path = os.path.join(data_dir, 'video_state.json')
        self._advance    = advance_seconds
        self._state      = self._load_state()

    def get_frame_path(self) -> str | None:
        return self._frame_path if os.path.exists(self._frame_path) else None

    def advance(self) -> None:
        """Step forward one increment and extract the new frame."""
        if not os.path.exists(self._video_path):
            return
        dur = self._state.get('duration', 0.0)
        if dur <= 0:
            dur = self._probe_duration()
            if dur <= 0:
                return
            self._state['duration'] = dur
        ts = self._state.get('ts', 0.0)
        ts += self._advance
        if ts >= dur:
            ts = 0.0   # loop
        self._extract(ts)
        self._state['ts'] = ts
        self._save_state()

    # ── internals ─────────────────────────────────────────────────────────────

    def _extract(self, ts: float) -> None:
        try:
            os.makedirs(os.path.dirname(self._frame_path) or '.', exist_ok=True)
            subprocess.run(
                ['ffmpeg', '-ss', f'{ts:.3f}', '-i', self._video_path,
                 '-frames:v', '1', '-q:v', '3', self._frame_path, '-y'],
                capture_output=True, timeout=20,
            )
        except Exception as exc:
            logger.debug('video frame extract error: %s', exc)

    def _probe_duration(self) -> float:
        try:
            r = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                 '-show_format', self._video_path],
                capture_output=True, text=True, timeout=10,
            )
            return float(json.loads(r.stdout)['format']['duration'])
        except Exception:
            return 0.0

    def _load_state(self) -> dict:
        try:
            with open(self._state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._state_path) or '.', exist_ok=True)
            with open(self._state_path, 'w') as f:
                json.dump(self._state, f)
        except Exception:
            pass


# ── Business idea manager ─────────────────────────────────────────────────────

class IdeaManager:
    """Cycles through business ideas parsed from markdown files in a repo.

    Scans for ``### N. Title`` headers, extracts the first paragraph and the
    Monetization line.  Advances one idea per advance() call.
    """

    def __init__(self, repo_dir: str, data_dir: str):
        self._repo_dir   = repo_dir
        self._state_path = os.path.join(data_dir, 'idea_state.json')
        self._cache_path = os.path.join(data_dir, 'idea_cache.json')
        self._state      = self._load_state()
        self._ideas: list = self._load_cache()

    def current(self) -> dict | None:
        """Return current idea dict, or None if no ideas loaded."""
        if not self._ideas:
            self._ideas = self._scan()
            self._save_cache()
        if not self._ideas:
            return None
        idx = self._state.get('idx', 0) % len(self._ideas)
        return self._ideas[idx]

    def advance(self) -> None:
        """Move to the next idea, refreshing the cache if needed."""
        if not self._ideas:
            self._ideas = self._scan()
            self._save_cache()
        if self._ideas:
            self._state['idx'] = (self._state.get('idx', 0) + 1) % len(self._ideas)
            self._save_state()

    # ── internals ─────────────────────────────────────────────────────────────

    def _scan(self) -> list:
        ideas = []
        try:
            for root, _, files in os.walk(self._repo_dir):
                for fname in sorted(files):
                    if not fname.endswith('.md'):
                        continue
                    path = os.path.join(root, fname)
                    ideas.extend(self._parse_file(path))
        except Exception as exc:
            logger.debug('idea scan error: %s', exc)
        return ideas

    def _parse_file(self, path: str) -> list:
        results = []
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                text = f.read()
            # Split on ### headers
            import re
            parts = re.split(r'^###\s+', text, flags=re.MULTILINE)
            for part in parts[1:]:
                lines = part.strip().splitlines()
                if not lines:
                    continue
                title = re.sub(r'^\d+\.\s*', '', lines[0]).strip()
                # First non-empty body paragraph
                body = ''
                in_para = False
                for line in lines[1:]:
                    stripped = line.strip()
                    if stripped.startswith('Monetization:'):
                        mono = stripped[len('Monetization:'):].strip()
                        break
                    if stripped and not in_para:
                        in_para = True
                        body = stripped
                    elif stripped and in_para:
                        body += ' ' + stripped
                    elif in_para and not stripped:
                        break
                else:
                    mono = ''
                # Find Monetization line if we broke early
                mono_match = re.search(r'Monetization:\s*(.+)', part)
                if mono_match:
                    mono = mono_match.group(1).split('.')[0].strip()
                if title:
                    results.append({
                        'title': title[:60],
                        'body':  body[:200],
                        'mono':  mono[:80],
                        'file':  os.path.basename(path),
                    })
        except Exception:
            pass
        return results

    def _load_state(self) -> dict:
        try:
            with open(self._state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._state_path) or '.', exist_ok=True)
            with open(self._state_path, 'w') as f:
                json.dump(self._state, f)
        except Exception:
            pass

    def _load_cache(self) -> list:
        try:
            with open(self._cache_path) as f:
                return json.load(f)
        except Exception:
            return []

    def _save_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._cache_path) or '.', exist_ok=True)
            with open(self._cache_path, 'w') as f:
                json.dump(self._ideas, f)
        except Exception:
            pass


class TileFetcher:
    def __init__(self, config: dict):
        self._config = config
        self._cache: dict[str, tuple] = {}   # key -> (data, fetched_at)
        self._lock  = threading.Lock()
        self._stop  = threading.Event()
        self._thread: threading.Thread | None = None

        data_dir = config.get('_data_dir', 'data')

        video_path = config.get('screensaver_tiles_video_path', '').strip()
        self._video = (
            VideoFrameManager(
                video_path, data_dir,
                advance_seconds=int(config.get('screensaver_tiles_video_advance_seconds', 60)),
            ) if video_path else None
        )

        idea_repo = config.get('screensaver_tiles_idea_repo', '').strip()
        self._ideas = IdeaManager(idea_repo, data_dir) if idea_repo else None

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

    def advance(self) -> None:
        """Advance one-shot tiles (video frame, business idea) on each screensaver render."""
        if self._video is not None:
            try:
                self._video.advance()
            except Exception as exc:
                logger.debug('video advance error: %s', exc)
        if self._ideas is not None:
            try:
                self._ideas.advance()
            except Exception as exc:
                logger.debug('idea advance error: %s', exc)

    def video_frame_path(self) -> str | None:
        return self._video.get_frame_path() if self._video else None

    def current_idea(self) -> dict | None:
        return self._ideas.current() if self._ideas else None

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
