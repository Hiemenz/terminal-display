"""Tests for the config-driven custom shell prompt (PS1)."""
import subprocess

import pytest


def test_ps1_all_parts(make_app):
    app = make_app()
    ps1 = app._build_ps1()
    assert ps1.startswith(r'\u@\h:\w')        # user@host:cwd
    assert 'git branch --show-current' in ps1  # git segment
    assert ps1.endswith('$ ')                  # symbol + trailing space


def test_ps1_user_and_cwd_only(make_app):
    app = make_app(terminal_prompt_show_host=False, terminal_prompt_show_git=False)
    assert app._build_ps1() == r'\u:\w $ '


def test_ps1_symbol_only(make_app):
    app = make_app(
        terminal_prompt_show_user=False, terminal_prompt_show_host=False,
        terminal_prompt_show_cwd=False, terminal_prompt_show_git=False,
        terminal_prompt_symbol='%',
    )
    assert app._build_ps1() == '% '


def test_ps1_host_only_has_no_leading_at(make_app):
    app = make_app(terminal_prompt_show_user=False, terminal_prompt_show_cwd=False,
                   terminal_prompt_show_git=False)
    assert app._build_ps1() == r'\h $ '


def test_prompt_command_none_when_disabled(make_app, tmp_path, monkeypatch):
    import eink_terminal_app as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    app = make_app(terminal_prompt_custom=False)
    assert app._build_prompt_command() is None


def test_rcfile_written_and_contents(make_app, tmp_path, monkeypatch):
    import eink_terminal_app as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    app = make_app(terminal_prompt_custom=True)
    path = app._write_bash_rcfile()
    assert path is not None
    text = open(path).read()
    # Replays login startup, then pins PS1 last.
    assert '. /etc/profile' in text
    assert '.profile' in text
    assert text.rstrip().endswith("'")
    assert "PS1='" in text
    # PS1 line must be the final statement so nothing overrides it.
    assert text.strip().splitlines()[-1].startswith("PS1=")


def test_rcfile_quotes_single_quote_in_symbol(make_app, tmp_path, monkeypatch):
    import eink_terminal_app as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    app = make_app(terminal_prompt_custom=True, terminal_prompt_symbol="x'y")
    path = app._write_bash_rcfile()
    # The whole file must remain valid bash despite the embedded quote.
    r = subprocess.run(['bash', '-n', path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not __import__('shutil').which('bash'), reason='bash required')
def test_rcfile_sets_prompt_when_sourced(make_app, tmp_path, monkeypatch):
    """Integration: sourcing the rcfile in bash yields the expected expanded
    prompt (user@host … symbol). Run in tmp_path so there's no git branch."""
    import eink_terminal_app as m
    monkeypatch.setattr(m, '_REPO_ROOT', str(tmp_path))
    app = make_app(terminal_prompt_custom=True, terminal_prompt_show_git=False)
    path = app._write_bash_rcfile()
    script = 'source %s; printf "%%s" "${PS1@P}"' % path
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                       cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    assert '@' in out          # user@host
    assert out.endswith('$')   # symbol


def test_tmux_argv_plain(make_app):
    app = make_app(terminal_use_tmux=True)
    argv = app._tmux_launch_argv('eink', '/home/pi', None)
    assert argv[:5] == ['tmux', 'new-session', '-A', '-s', 'eink']
    assert '-c' in argv and '/home/pi' in argv
    # No prompt command → no default-command override.
    assert 'set-option' not in argv


def test_tmux_argv_with_prompt_sets_default_command(make_app):
    app = make_app(terminal_use_tmux=True, terminal_prompt_custom=True)
    promptcmd = 'exec bash --rcfile /tmp/eink_bashrc -i'
    argv = app._tmux_launch_argv('eink', '/home/pi', promptcmd)
    # The custom-prompt shell is both the initial pane command and the
    # default-command, so every future window/pane gets the prompt too.
    assert ';' in argv
    assert 'set-option' in argv
    assert 'default-command' in argv
    assert argv.count(promptcmd) == 2
