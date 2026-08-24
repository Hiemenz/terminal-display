"""Settings written from the web editor must survive the round trip.

A fractional setting used to save as 0 — _save_config_values coerced every
number with str(int(value)) — which quietly moved the DU heavy-content
threshold to "everything is heavy".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from preview_server import _CONFIG_SCHEMA, _save_config_values  # noqa: E402


def _roundtrip(tmp_path, initial, updates):
    path = os.path.join(str(tmp_path), 'config.yaml')
    with open(path, 'w') as fh:
        fh.write(initial)
    _save_config_values(path, updates)
    with open(path) as fh:
        return fh.read()


def test_float_keeps_its_fraction(tmp_path):
    out = _roundtrip(tmp_path, 'terminal_du_heavy_threshold: 0.22\n',
                     {'terminal_du_heavy_threshold': 0.35})
    assert 'terminal_du_heavy_threshold: 0.35' in out


def test_int_stays_int(tmp_path):
    out = _roundtrip(tmp_path, 'terminal_full_refresh_interval: 300\n',
                     {'terminal_full_refresh_interval': 600})
    assert 'terminal_full_refresh_interval: 600' in out


def test_bool_stays_yaml_bool(tmp_path):
    out = _roundtrip(tmp_path, 'terminal_region_flash: true\n',
                     {'terminal_region_flash': False})
    assert 'terminal_region_flash: false' in out


def test_schema_has_no_duplicate_keys():
    """A key listed twice gives the page two controls writing the same setting,
    and whichever renders last silently wins."""
    keys = [field[0] for _section, fields in _CONFIG_SCHEMA for field in fields]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, 'duplicate settings keys: %s' % sorted(duplicates)


def test_every_field_type_is_one_the_editor_renders():
    known = {'bool', 'int', 'float', 'select', 'str'}
    for _section, fields in _CONFIG_SCHEMA:
        for field in fields:
            assert field[1] in known, '%s has unknown type %r' % (field[0], field[1])
