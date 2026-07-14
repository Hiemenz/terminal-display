"""Tests for the terminal_start_dir preference and 'last' persistence."""
import os


def test_home(make_app):
    app = make_app(terminal_start_dir='home')
    assert app._resolve_start_dir() == os.path.expanduser('~')


def test_root(make_app):
    app = make_app(terminal_start_dir='root')
    assert app._resolve_start_dir() == '/'


def test_explicit_path(make_app, tmp_path):
    app = make_app(terminal_start_dir=str(tmp_path))
    assert app._resolve_start_dir() == str(tmp_path)


def test_explicit_missing_path_falls_back_home(make_app):
    app = make_app(terminal_start_dir='/no/such/dir/here')
    assert app._resolve_start_dir() == os.path.expanduser('~')


def test_blank_defaults_home(make_app):
    app = make_app(terminal_start_dir='')
    assert app._resolve_start_dir() == os.path.expanduser('~')


def test_last_falls_back_home_when_unset(make_app, tmp_path, monkeypatch):
    import shell_mixin as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    app = make_app(terminal_start_dir='last')
    assert app._resolve_start_dir() == os.path.expanduser('~')


def test_last_reads_saved_dir(make_app, tmp_path, monkeypatch):
    import shell_mixin as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    target = tmp_path / 'project'
    target.mkdir()
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'last_cwd.txt').write_text(str(target) + '\n')
    app = make_app(terminal_start_dir='last')
    assert app._resolve_start_dir() == str(target)


def test_last_ignores_stale_missing_dir(make_app, tmp_path, monkeypatch):
    import shell_mixin as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'last_cwd.txt').write_text('/gone/away\n')
    app = make_app(terminal_start_dir='last')
    assert app._resolve_start_dir() == os.path.expanduser('~')


def test_save_then_read_roundtrip_non_tmux(make_app, tmp_path, monkeypatch):
    import shell_mixin as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    # Non-tmux path reads /proc/<pid>/cwd; point it at our own process.
    app = make_app(terminal_use_tmux=False, terminal_start_dir='last')
    app._child_pid = os.getpid()
    app._save_last_cwd()
    saved = app._read_last_cwd()
    assert saved == os.getcwd()
