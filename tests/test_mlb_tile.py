"""MLB screensaver tile: postseason slate fetch + render."""
import json
from datetime import date
from unittest.mock import patch

from PIL import Image, ImageDraw

import render
from tile_fetcher import TileFetcher


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _game(away, home, game_date, state='Scheduled', a=None, h=None):
    return {
        'gameDate': game_date,
        'officialDate': game_date[:10],
        'status': {'detailedState': state},
        'teams': {'away': {'team': {'abbreviation': away}, 'score': a},
                  'home': {'team': {'abbreviation': home}, 'score': h}},
        'linescore': {'currentInningOrdinal': '5th', 'isTopInning': False},
        'venue': {'name': 'Park'},
    }


def _fetcher():
    return TileFetcher({'screensaver_tiles_mlb_team_id': 147,
                        'screensaver_tiles_mlb_team_abbr': 'NYY'})


def _urls_patch(by_type):
    """urlopen stub keyed on whether the URL asks for postseason game types."""
    seen = []

    def fake(url, timeout=10):
        seen.append(url)
        return _Resp(by_type['post' if 'gameType=F,D,L,W' in url else 'reg'])
    return fake, seen


def test_postseason_lists_all_games_not_just_primary_team():
    today = date.today().strftime('%Y-%m-%d')
    slate = {'dates': [{'date': today, 'games': [
        _game('SD', 'MIL', f'{today}T23:30:00Z'),
        _game('ATL', 'LAD', f'{today}T20:00:00Z', 'In Progress', 2, 1),
    ]}]}
    fake, seen = _urls_patch({'post': slate, 'reg': {'dates': []}})
    with patch('urllib.request.urlopen', fake):
        out = _fetcher()._fetch_mlb()

    assert out['status'] == 'postseason'
    assert [(g['away'], g['home']) for g in out['games']] == [('ATL', 'LAD'), ('SD', 'MIL')]
    assert 'teamId' not in seen[0]
    # the schedule API only returns team abbreviations when teams are hydrated
    assert 'team' in seen[0].split('hydrate=')[1].split(',')


def test_postseason_picks_next_day_with_games():
    later = '2099-01-02'
    slate = {'dates': [{'date': later, 'games': [_game('A', 'B', f'{later}T20:00:00Z')]}]}
    fake, _ = _urls_patch({'post': slate, 'reg': {'dates': []}})
    with patch('urllib.request.urlopen', fake):
        out = _fetcher()._fetch_mlb()
    assert out['game_date'] == later


def test_falls_back_to_team_regular_season_when_no_postseason():
    today = date.today().strftime('%Y-%m-%d')
    reg = {'dates': [{'date': today, 'games': [_game('NYY', 'BOS', f'{today}T23:00:00Z')]}]}
    fake, _ = _urls_patch({'post': {'dates': []}, 'reg': reg})
    with patch('urllib.request.urlopen', fake):
        out = _fetcher()._fetch_mlb()
    assert out['status'] == 'Scheduled'
    assert (out['away'], out['home']) == ('NYY', 'BOS')


def test_postseason_fetch_error_falls_back():
    today = date.today().strftime('%Y-%m-%d')
    reg = {'dates': [{'date': today, 'games': [_game('NYY', 'BOS', f'{today}T23:00:00Z')]}]}

    def fake(url, timeout=10):
        if 'gameType=F,D,L,W' in url:
            raise OSError('boom')
        return _Resp(reg)
    with patch('urllib.request.urlopen', fake):
        out = _fetcher()._fetch_mlb()
    assert out['home'] == 'BOS'


def test_row_status():
    assert render._mlb_row_status({'status': 'Final'}) == 'Final'
    assert render._mlb_row_status(
        {'status': 'In Progress', 'inning': '5th', 'top': False}) == 'Bot 5th'
    assert render._mlb_row_status(
        {'status': 'Scheduled', 'start_str': '8:00 PM ET'}) == '8:00 PM ET'


def test_postseason_tile_draws_without_error():
    today = date.today().strftime('%Y-%m-%d')
    mlb = {'status': 'postseason', 'team_abbr': 'POSTSEASON', 'game_date': today,
           'games': [{'status': 'Scheduled', 'away': 'ATL', 'home': 'LAD',
                      'start_str': '4:00 PM ET', 'inning': '', 'top': True}] * 8}
    img = Image.new('L', (266, 230), 255)
    d = ImageDraw.Draw(img)
    render._draw_mlb_tile(d, 0, 0, 265, 229, '', mlb, 0, 255)
    assert img.getextrema()[0] == 0
