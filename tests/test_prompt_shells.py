"""Tests for shell-aware prompt generation (zsh / fish) and the editor preview."""
import shutil
import subprocess

import pytest


def test_zsh_prompt_escapes(make_app):
    app = make_app()
    p = app._build_prompt_string('zsh')
    assert p.startswith('%n@%m:%~')   # zsh escapes, not bash's \u\h\w
    assert p.endswith('$ ')


def test_zsh_dotdir_written(make_app, tmp_path, monkeypatch):
    import eink_terminal_app as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    app = make_app(terminal_prompt_custom=True)
    d = app._write_zsh_dotdir()
    assert d is not None
    zshrc = open('%s/.zshrc' % d).read()
    assert 'setopt prompt_subst' in zshrc
    assert "PROMPT='%n@%m:%~" in zshrc
    assert 'source "$HOME/.zshrc"' in zshrc      # chains the user's real rc


def test_fish_prompt_function(make_app):
    app = make_app()
    f = app._build_fish_prompt()
    assert f.startswith('function fish_prompt')
    assert f.rstrip().endswith('end')
    assert '(prompt_pwd)' in f
    assert 'git branch --show-current' in f


@pytest.mark.skipif(not shutil.which('fish'), reason='fish not installed')
def test_fish_prompt_is_valid(make_app):
    app = make_app()
    body = app._build_fish_prompt()
    # fish parses the function definition without error.
    r = subprocess.run(['fish', '-c', body], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_prompt_command_picks_shell(make_app, tmp_path, monkeypatch):
    import eink_terminal_app as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    app = make_app(terminal_prompt_custom=True)

    monkeypatch.setattr(app, '_detect_shell', lambda: 'bash')
    assert 'bash --rcfile' in app._build_prompt_command()

    if shutil.which('zsh'):
        monkeypatch.setattr(app, '_detect_shell', lambda: 'zsh')
        assert 'zsh -i' in app._build_prompt_command()
    if shutil.which('fish'):
        monkeypatch.setattr(app, '_detect_shell', lambda: 'fish')
        assert 'fish -i -C' in app._build_prompt_command()

    # Unknown shell → falls back to bash (prompt escapes are bash syntax).
    monkeypatch.setattr(app, '_detect_shell', lambda: 'dash')
    assert 'bash --rcfile' in app._build_prompt_command()


def test_preview_off_when_custom_disabled(make_app):
    app = make_app(terminal_prompt_custom=False)
    assert 'off' in app._prompt_preview().lower()


def test_preview_reflects_staged_parts(make_app):
    app = make_app()
    # Stage: turn the custom prompt on, drop host + git.
    app._settings_pending = {
        'terminal_prompt_custom': True,
        'terminal_prompt_show_host': False,
        'terminal_prompt_show_git': False,
    }
    prev = app._prompt_preview()
    assert '~/project' in prev    # cwd still on
    assert '(main)' not in prev   # git staged off
    assert prev.rstrip().endswith('$')
