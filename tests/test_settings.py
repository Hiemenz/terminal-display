"""Tests for the on-display config editor (Settings overlay)."""
from eink_terminal_app import (_SETTINGS_SCHEMA, _SETTINGS_OPEN,
                               _SETTINGS_LIVE, _SETTINGS_SHELL)


def test_schema_only_editable_types():
    # On-device editor handles bool/select only (no on-screen text entry).
    for key, typ, label, opts in _SETTINGS_SCHEMA:
        assert typ in ('bool', 'select')
        if typ == 'select':
            # Either a concrete list of options or a dynamic sentinel.
            assert isinstance(opts, list) or opts == '__FONTS__'


def test_rows_render_current_values(make_app):
    app = make_app()
    rows = app._settings_rows()
    # One row per setting + Save + Cancel.
    assert len(rows) == len(_SETTINGS_SCHEMA) + 2
    assert any('Save' in r for r in rows)
    assert any('Cancel' in r for r in rows)
    # Bool shows on/off, select shows the value.
    joined = '\n'.join(rows)
    assert 'QR Code' in joined and '[ on ]' in joined
    assert 'block' in joined  # cursor style


def test_value_precedence_pending_over_config(make_app):
    app = make_app(terminal_show_qr=True)
    assert app._settings_value('terminal_show_qr') is True
    app._settings_pending['terminal_show_qr'] = False
    assert app._settings_value('terminal_show_qr') is False


def test_change_toggles_bool(make_app):
    app = make_app(terminal_show_qr=True)
    app._settings_idx = next(i for i, s in enumerate(_SETTINGS_SCHEMA)
                             if s[0] == 'terminal_show_qr')
    app._settings_change(+1)
    assert app._settings_pending['terminal_show_qr'] is False
    app._settings_change(+1)
    assert app._settings_pending['terminal_show_qr'] is True


def test_change_cycles_select_both_directions(make_app):
    app = make_app(terminal_cursor_style='block')
    app._settings_idx = next(i for i, s in enumerate(_SETTINGS_SCHEMA)
                             if s[0] == 'terminal_cursor_style')
    app._settings_change(+1)
    assert app._settings_value('terminal_cursor_style') == 'underline'
    app._settings_change(-1)
    assert app._settings_value('terminal_cursor_style') == 'block'


def test_change_on_action_row_is_noop(make_app):
    app = make_app()
    app._settings_idx = len(_SETTINGS_SCHEMA)  # the Save row
    app._settings_change(+1)
    assert app._settings_pending == {}


def test_dirty_marker(make_app):
    app = make_app(terminal_show_qr=True)
    idx = next(i for i, s in enumerate(_SETTINGS_SCHEMA)
              if s[0] == 'terminal_show_qr')
    app._settings_idx = idx
    app._settings_change(+1)
    assert app._settings_rows()[idx].startswith('*')  # staged change marked


def test_live_only_save_applies_without_restart(make_app, monkeypatch):
    import eink_terminal_app as m
    saved = {}
    monkeypatch.setattr(m, '_save_config_values',
                        lambda path, updates: saved.update(updates))
    calls = []
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: calls.append(a))
    app = make_app(terminal_cursor_style='block', terminal_dark_mode=False)
    app._settings_active = True
    app._settings_pending = {'terminal_cursor_style': 'underline',
                             'terminal_dark_mode': True, 'terminal_show_qr': False}
    app._settings_save()
    # Persisted to disk...
    assert saved == app._settings_pending or saved == {
        'terminal_cursor_style': 'underline', 'terminal_dark_mode': True,
        'terminal_show_qr': False}
    # ...applied live, no restart.
    assert calls == []
    assert app._running is True
    assert app._cursor_style == 'underline'
    assert app._dark_mode is True
    assert app._config['terminal_show_qr'] is False
    assert app._settings_active is False


def test_shell_level_save_restarts(make_app, monkeypatch):
    import eink_terminal_app as m
    monkeypatch.setattr(m, '_save_config_values', lambda path, updates: None)
    calls = []
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: calls.append(a))
    app = make_app()
    app._settings_active = True
    app._settings_pending = {'terminal_prompt_custom': True}
    app._settings_save()
    assert calls and 'systemctl' in calls[0][0]
    assert app._running is False


def test_save_with_no_changes_just_closes(make_app, monkeypatch):
    import eink_terminal_app as m
    calls = []
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: calls.append(a))
    app = make_app()
    app._settings_active = True
    app._settings_pending = {}
    app._settings_save()
    assert calls == []            # no restart
    assert app._running is True   # not stopped
    assert app._settings_active is False


def test_live_and_shell_sets_disjoint_and_cover_schema():
    keys = {s[0] for s in _SETTINGS_SCHEMA}
    assert _SETTINGS_LIVE.isdisjoint(_SETTINGS_SHELL)
    # Every editable key is classified as either live-apply or shell-level.
    assert keys <= (_SETTINGS_LIVE | _SETTINGS_SHELL)


def test_settings_open_label_present():
    assert 'config' in _SETTINGS_OPEN.lower()
