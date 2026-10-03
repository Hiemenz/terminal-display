"""MLB screensaver tile: follows the postseason game that's on."""
import json
from datetime import date
from unittest.mock import patch

from tile_fetcher import TileFetcher

TODAY = date.today().strftime('%Y-%m-%d')


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _game(away, home, game_date, state='Scheduled', a=None, h=None,
          series='NL Division Series'):
    return {
        'gameDate': game_date,
        'officialDate': game_date[:10],
        'seriesDescription': series,
        'status': {'detailedState': state},
        'teams': {'away': {'team': {'abbreviation': away}, 'score': a},
                  'home': {'team': {'abbreviation': home}, 'score': h}},
        'linescore': {'currentInningOrdinal': '5th', 'isTopInning': False},
        'venue': {'name': 'Park'},
    }


def _fetcher():
    return TileFetcher({'screensaver_tiles_mlb_team_id': 147,
                        'screensaver_tiles_mlb_team_abbr': 'NYY'})


def _fetch(post, reg=None):
    seen = []

    def fake(url, timeout=10):
        seen.append(url)
        is_post = 'gameType=F,D,L,W' in url
        return _Resp(post if is_post else (reg or {'dates': []}))
    with patch('urllib.request.urlopen', fake):
        return _fetcher()._fetch_mlb(), seen


def _slate(*games, day=TODAY):
    return {'dates': [{'date': day, 'games': list(games)}]}


def test_follows_live_game_regardless_of_team():
    out, seen = _fetch(_slate(
        _game('CWS', 'CLE', f'{TODAY}T17:00:00Z', 'Final', 3, 0),
        _game('ATL', 'LAD', f'{TODAY}T20:00:00Z', 'In Progress', 2, 1),
        _game('SD', 'MIL', f'{TODAY}T23:30:00Z'),
    ))
    assert (out['away'], out['home'], out['status']) == ('ATL', 'LAD', 'In Progress')
    assert (out['away_score'], out['home_score']) == (2, 1)
    assert out['team_abbr'] == 'NL DIVISION SERIES'
    assert 'teamId' not in seen[0]
    # the schedule API only returns team abbreviations when teams are hydrated
    assert 'team' in seen[0].split('hydrate=')[1].split(',')


def test_no_live_game_shows_next_upcoming():
    out, _ = _fetch(_slate(
        _game('CWS', 'CLE', f'{TODAY}T17:00:00Z', 'Final', 3, 0),
        _game('SD', 'MIL', f'{TODAY}T23:30:00Z'),
        _game('NYY', 'TB', f'{TODAY}T22:30:00Z'),
    ))
    assert (out['away'], out['home']) == ('NYY', 'TB')


def test_all_final_shows_most_recent():
    out, _ = _fetch(_slate(
        _game('CWS', 'CLE', f'{TODAY}T17:00:00Z', 'Final', 3, 0),
        _game('ATL', 'LAD', f'{TODAY}T20:00:00Z', 'Final', 4, 5),
    ))
    assert (out['away'], out['home'], out['status']) == ('ATL', 'LAD', 'Final')


def test_picks_next_day_with_games_when_none_today():
    later = '2099-01-02'
    out, _ = _fetch(_slate(_game('A', 'B', f'{later}T20:00:00Z'), day=later))
    assert out['game_date'] == later


def test_missing_series_name_falls_back_to_postseason():
    out, _ = _fetch(_slate(_game('A', 'B', f'{TODAY}T20:00:00Z', series='')))
    assert out['team_abbr'] == 'POSTSEASON'


def test_falls_back_to_team_regular_season_when_no_postseason():
    reg = _slate(_game('NYY', 'BOS', f'{TODAY}T23:00:00Z', series=''))
    out, _ = _fetch({'dates': []}, reg)
    assert (out['team_abbr'], out['away'], out['home']) == ('NYY', 'NYY', 'BOS')


def test_postseason_fetch_error_falls_back():
    reg = _slate(_game('NYY', 'BOS', f'{TODAY}T23:00:00Z'))

    def fake(url, timeout=10):
        if 'gameType=F,D,L,W' in url:
            raise OSError('boom')
        return _Resp(reg)
    with patch('urllib.request.urlopen', fake):
        out = _fetcher()._fetch_mlb()
    assert out['home'] == 'BOS'
