"""Tests that _save_config_values writes the new keys correctly."""
import shutil

from config_loader import load_config
from preview_server import _save_config_values


def _copy_config(tmp_path):
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dst = tmp_path / 'config.yaml'
    shutil.copy(os.path.join(repo, 'config', 'config.yaml'), dst)
    return str(dst)


def test_roundtrip_new_keys(make_app, tmp_path):
    path = _copy_config(tmp_path)
    updates = {
        'terminal_cursor_style': 'underline',
        'terminal_show_qr': False,
        'terminal_prompt_custom': True,
        'terminal_prompt_show_git': False,
        'terminal_font_size': 16,
        'terminal_start_dir': 'last',
    }
    _save_config_values(path, updates)
    cfg = load_config(path)
    for key, val in updates.items():
        assert cfg[key] == val


def test_append_unknown_key(tmp_path):
    path = _copy_config(tmp_path)
    _save_config_values(path, {'a_brand_new_key': True})
    assert load_config(path)['a_brand_new_key'] is True


def test_shipped_config_has_new_defaults():
    cfg = load_config()  # the repo's config/config.yaml
    assert cfg['terminal_cursor_style'] == 'block'
    assert cfg['terminal_start_dir'] == 'home'
    assert cfg['terminal_prompt_custom'] is False
    for k in ('terminal_prompt_show_user', 'terminal_prompt_show_host',
              'terminal_prompt_show_cwd', 'terminal_prompt_show_git'):
        assert k in cfg
    # Refresh behavior: region flash on, whole-panel flash every 5 minutes.
    assert cfg['terminal_region_flash'] is True
    assert cfg['terminal_full_refresh_interval'] == 300
