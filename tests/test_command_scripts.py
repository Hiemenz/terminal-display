"""Tests for _install_command_scripts / _write_signal_script — the typeable
shell commands (settings, eink, clear-eink, notes, llmchat, terminal) that
signal the running EinkTerminal process by PID (from /tmp/eink-terminal-active).
See src/shell_mixin.py."""
import os
import signal
import stat


def test_write_signal_script_content_and_permissions(make_app, tmp_path):
    app = make_app()
    bindir = str(tmp_path)

    app._write_signal_script(bindir, ('foo', 'bar'), signal.SIGUSR1, 'Does a thing.')

    for name in ('foo', 'bar'):
        path = os.path.join(bindir, name)
        assert os.path.isfile(path)
        assert os.stat(path).st_mode & stat.S_IXUSR
        content = open(path).read()
        assert content.startswith('#!/bin/sh\n')
        assert f'kill -{int(signal.SIGUSR1)} ' in content
        assert 'Does a thing.' in content


def test_install_command_scripts_writes_all_six_names(make_app, tmp_path, monkeypatch):
    import shell_mixin
    monkeypatch.setattr(shell_mixin, '_REPO_ROOT', str(tmp_path))
    app = make_app()

    app._install_command_scripts()

    bindir = os.path.join(str(tmp_path), 'data', 'bin')
    for name in ('settings', 'eink', 'clear-eink', 'notes', 'llmchat', 'terminal'):
        assert os.path.isfile(os.path.join(bindir, name)), name


def test_install_command_scripts_uses_distinct_signals_for_modes(make_app, tmp_path, monkeypatch):
    import shell_mixin
    monkeypatch.setattr(shell_mixin, '_REPO_ROOT', str(tmp_path))
    app = make_app()

    app._install_command_scripts()

    bindir = os.path.join(str(tmp_path), 'data', 'bin')
    notes_sig = int(signal.SIGRTMIN) + 1
    llm_sig = int(signal.SIGRTMIN) + 2
    terminal_sig = int(signal.SIGRTMIN) + 3
    assert f'kill -{notes_sig} ' in open(os.path.join(bindir, 'notes')).read()
    assert f'kill -{llm_sig} ' in open(os.path.join(bindir, 'llmchat')).read()
    assert f'kill -{terminal_sig} ' in open(os.path.join(bindir, 'terminal')).read()


def test_install_command_scripts_prepends_bindir_to_path(make_app, tmp_path, monkeypatch):
    import shell_mixin
    monkeypatch.setattr(shell_mixin, '_REPO_ROOT', str(tmp_path))
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    app = make_app()

    app._install_command_scripts()

    bindir = os.path.join(str(tmp_path), 'data', 'bin')
    assert os.environ['PATH'].split(os.pathsep)[0] == bindir
