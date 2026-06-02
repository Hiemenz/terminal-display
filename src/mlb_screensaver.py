"""
MLB Screensaver — fetch today's scores and standings, render 800×480 grayscale PIL image.

Design note: main.py and eink_terminal.py are mutually exclusive processes — only one
runs at a time. This means MLB updates from main.py naturally stop while the terminal
app is active, and resume when stats mode is restored. No locking is needed.

Only stdlib + PIL used here; no new dependencies.
"""
import os
import json
from datetime import datetime
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont

# Display dimensions
W, H = 800, 480

_MLB_SCHEDULE_URL = (
    'https://statsapi.mlb.com/api/v1/schedule'
    '?sportId=1&hydrate=linescore,team&date={date}'
)
_MLB_STANDINGS_URL = (
    'https://statsapi.mlb.com/api/v1/standings'
    '?leagueId=103,104&season={season}&standingsTypes=regularSeason'
)

_HTTP_TIMEOUT = 5  # seconds


# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

def _find_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf',
        '/System/Library/Fonts/Menlo.ttc',
        '/System/Library/Fonts/Supplemental/Andale Mono.ttf',
    ]
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _fetch_json(url: str) -> dict:
    """Fetch JSON from URL with timeout. Raises on error."""
    with urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _fetch_games(date_str: str) -> list:
    """
    Fetch today's games. Returns list of dicts:
      {away_team, home_team, away_score, home_score, status, inning, inning_half, game_time}
    status: 'Preview' | 'Live' | 'Final' | 'Postponed' | 'Cancelled'
    """
    url = _MLB_SCHEDULE_URL.format(date=date_str)
    data = _fetch_json(url)

    games = []
    for date_entry in data.get('dates', []):
        for g in date_entry.get('games', []):
            status_obj = g.get('status', {})
            abstract = status_obj.get('abstractGameState', 'Preview')  # Preview/Live/Final
            detailed = status_obj.get('detailedState', '')

            teams = g.get('teams', {})
            away = teams.get('away', {})
            home = teams.get('home', {})

            away_team = away.get('team', {}).get('abbreviation', '???')
            home_team = home.get('team', {}).get('abbreviation', '???')
            away_score = away.get('score')
            home_score = home.get('score')

            # Inning info
            linescore = g.get('linescore', {})
            inning = linescore.get('currentInning', 0)
            inning_half = linescore.get('inningHalf', '')  # 'Top' / 'Bottom'

            # Game time (for Preview games) — show UTC simply
            game_time_str = ''
            game_dt = g.get('gameDate', '')  # ISO 8601 UTC
            if game_dt:
                try:
                    dt = datetime.strptime(game_dt, '%Y-%m-%dT%H:%M:%SZ')
                    game_time_str = dt.strftime('%I:%M').lstrip('0') + ' UTC'
                except Exception:
                    pass

            # Normalize status
            if 'Postponed' in detailed or 'Cancelled' in detailed or 'Suspended' in detailed:
                status = detailed.split(' ')[0]
            elif abstract == 'Final':
                status = 'Final'
            elif abstract == 'Live':
                status = 'Live'
            else:
                status = 'Preview'

            games.append({
                'away_team': away_team,
                'home_team': home_team,
                'away_score': away_score if away_score is not None else '',
                'home_score': home_score if home_score is not None else '',
                'status': status,
                'inning': inning,
                'inning_half': inning_half,
                'game_time': game_time_str,
            })
    return games


def _fetch_standings(season: str) -> dict:
    """
    Fetch standings. Returns dict keyed by division name:
      {'AL East': [{'name': 'NYY', 'wins': 50, 'losses': 30, 'pct': '.625', 'gb': '-'}, ...], ...}
    Each division contains top-5 teams.
    """
    url = _MLB_STANDINGS_URL.format(season=season)
    data = _fetch_json(url)

    divisions = {}
    for record in data.get('records', []):
        div_name = record.get('division', {}).get('nameShort', '')
        if not div_name:
            div_name = record.get('division', {}).get('name', 'Unknown')

        teams = []
        for team_rec in record.get('teamRecords', []):
            abbrev = team_rec.get('team', {}).get('abbreviation', '???')
            wins = team_rec.get('wins', 0)
            losses = team_rec.get('losses', 0)
            pct = team_rec.get('winningPercentage', '.000')
            gb = team_rec.get('gamesBack', '-')
            if gb in ('0.0', 0, '0'):
                gb = '-'
            teams.append({
                'name': abbrev,
                'wins': wins,
                'losses': losses,
                'pct': pct,
                'gb': str(gb),
            })

        # API returns them sorted; keep top 5
        divisions[div_name] = teams[:5]

    return divisions


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

# Column split
_LEFT_W = 400
_PAD = 12

# Grayscale palette
_BG = 0        # black background
_FG = 255      # white text
_DIM = 160     # dimmed / secondary text
_ACCENT = 210  # accent (live game scores)


def _draw_games(draw: ImageDraw.ImageDraw, games: list, y_start: int,
                font_sm: ImageFont.ImageFont, fav_team: str,
                height_limit: int) -> int:
    """Draw game list in the left column. Returns final y position."""
    x = _PAD
    y = y_start
    row_h = 17

    if not games:
        draw.text((x, y), 'No games scheduled today', font=font_sm, fill=_DIM)
        return y + row_h

    shown = 0
    for g in games:
        if y + row_h > height_limit:
            remaining = len(games) - shown
            if remaining > 0:
                draw.text((x, y), f'+ {remaining} more…', font=font_sm, fill=_DIM)
            break

        away = g['away_team']
        home = g['home_team']
        status = g['status']
        is_fav = bool(fav_team) and (fav_team.upper() in (away.upper(), home.upper()))

        if status == 'Final':
            line = f'{away:<3} {g["away_score"]:>2}  {home:<3} {g["home_score"]:>2}  F'
            color = _DIM
        elif status == 'Live':
            half = g['inning_half'][:1] if g['inning_half'] else ''  # 'T' or 'B'
            inn = g['inning']
            a_sc = g['away_score'] if g['away_score'] != '' else '0'
            h_sc = g['home_score'] if g['home_score'] != '' else '0'
            line = f'{away:<3} {a_sc:>2}  {home:<3} {h_sc:>2}  {half}{inn}'
            color = _ACCENT
        elif status == 'Preview':
            gt = g['game_time'] or ''
            line = f'{away:<3}  @  {home:<3}  {gt}'
            color = _DIM
        else:
            # Postponed, Cancelled, etc.
            line = f'{away:<3}  @  {home:<3}  {status}'
            color = _DIM

        if is_fav:
            color = _FG  # full brightness for favorite team's games

        draw.text((x, y), line, font=font_sm, fill=color)
        y += row_h
        shown += 1

    return y


def _draw_division(draw: ImageDraw.ImageDraw, name: str, teams: list,
                   x: int, y: int, font_hd: ImageFont.ImageFont,
                   font_sm: ImageFont.ImageFont, fav_team: str,
                   col_right_edge: int, height_limit: int) -> int:
    """Draw one division standings block. Returns final y."""
    # Division header + underline
    draw.text((x, y), name, font=font_hd, fill=_FG)
    hdr_h = font_hd.getbbox(name)[3] + 2
    draw.line([(x, y + hdr_h), (col_right_edge - _PAD, y + hdr_h)], fill=_FG, width=1)
    y += hdr_h + 3

    # Column headers
    draw.text((x, y), f"{'TM':<4} {'W':>3} {'L':>3}  {'PCT':>5}  {'GB':>4}",
              font=font_sm, fill=_DIM)
    y += 14

    for t in teams:
        if y + 14 > height_limit:
            break
        is_fav = bool(fav_team) and t['name'].upper() == fav_team.upper()
        color = _FG if is_fav else _DIM
        row = f"{t['name']:<4} {t['wins']:>3} {t['losses']:>3}  {t['pct']:>5}  {t['gb']:>4}"
        draw.text((x, y), row, font=font_sm, fill=color)
        y += 14

    return y + 6  # small gap after division


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_mlb_screensaver(config: dict):
    """
    Fetch MLB scores/standings and render as 800×480 PIL image.

    Returns an 'L' (grayscale) PIL Image on success.
    Returns None on fetch/parse failure so the caller can fall back to the
    static screensaver.
    """
    today = datetime.now()
    date_str = today.strftime('%Y-%m-%d')
    season = today.strftime('%Y')

    # Fetch data independently; partial failure is tolerated
    try:
        games = _fetch_games(date_str)
    except Exception as e:
        print(f'[mlb] Failed to fetch games: {e}')
        games = None

    try:
        standings = _fetch_standings(season)
    except Exception as e:
        print(f'[mlb] Failed to fetch standings: {e}')
        standings = None

    # If both failed there's nothing to show
    if games is None and standings is None:
        return None

    games = games or []
    standings = standings or {}

    # ── Build image ────────────────────────────────────────────────────────────
    img = Image.new('L', (W, H), _BG)
    draw = ImageDraw.Draw(img)

    font_title = _find_font(20)
    font_hd = _find_font(13)
    font_sm = _find_font(11)

    fav_team = config.get('screensaver_mlb_team', '').strip()

    # ── Header ─────────────────────────────────────────────────────────────────
    header = f'MLB — {today.strftime("%a %b %-d")}'
    bbox = draw.textbbox((0, 0), header, font=font_title)
    hdr_w = bbox[2] - bbox[0]
    hdr_h = bbox[3] - bbox[1]
    draw.text(((W - hdr_w) // 2, _PAD), header, font=font_title, fill=_FG)
    hdr_bottom = _PAD + hdr_h + 4
    draw.line([(0, hdr_bottom), (W, hdr_bottom)], fill=_DIM, width=1)

    content_top = hdr_bottom + 8
    content_bottom = H - _PAD

    # ── Left column: Today's Games ─────────────────────────────────────────────
    games_label = "TODAY'S GAMES"
    draw.text((_PAD, content_top), games_label, font=font_hd, fill=_FG)
    lbl_h = font_hd.getbbox(games_label)[3] + 2
    draw.line(
        [(_PAD, content_top + lbl_h), (_LEFT_W - _PAD, content_top + lbl_h)],
        fill=_FG, width=1,
    )
    games_top = content_top + lbl_h + 5

    _draw_games(draw, games, games_top, font_sm, fav_team, content_bottom)

    # Vertical divider between columns
    draw.line([(_LEFT_W, content_top), (_LEFT_W, content_bottom)], fill=_DIM, width=1)

    # ── Right column: Standings ────────────────────────────────────────────────
    rx = _LEFT_W + _PAD
    ry = content_top

    # Preferred display order
    preferred_order = [
        'AL East', 'AL Central', 'AL West',
        'NL East', 'NL Central', 'NL West',
    ]
    ordered_divs = []
    for name in preferred_order:
        if name in standings:
            ordered_divs.append((name, standings[name]))
    # Append any leftovers not in the preferred list
    for name, teams in standings.items():
        if name not in preferred_order:
            ordered_divs.append((name, teams))

    if not ordered_divs:
        draw.text((rx, ry), 'Standings unavailable', font=font_sm, fill=_DIM)
    else:
        for div_name, teams in ordered_divs:
            if ry >= content_bottom - 30:
                break
            ry = _draw_division(
                draw, div_name, teams,
                rx, ry, font_hd, font_sm, fav_team,
                W, content_bottom,
            )

    return img
