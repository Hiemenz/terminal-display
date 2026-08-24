#!/usr/bin/env python3
"""Record a real PTY byte stream plus the screen it should produce.

The terminal emulator's whole job is to turn the bytes a program writes into
the grid of characters we paint on the panel. When pyte drops a sequence it
doesn't implement, that grid silently goes wrong — no exception, no log line,
just a display that lies. (`clear` inside tmux was exactly this: tmux spells
it as SU, `ESC[22S`, which pyte ignores, so the cleared text stayed on the
panel. See tests/test_terminal_conformance.py.)

This records both halves of that contract, using tmux as the oracle:

  * every byte tmux writes to the outer PTY while a command runs, and
  * what tmux itself says the pane contains afterwards (`capture-pane -p`).

Replaying the bytes through our screen has to reproduce that pane. Both
halves come from the same run, so a command with non-deterministic output
(htop, a clock) still makes a stable fixture: the bytes are frozen, and the
expected grid is what those exact bytes mean.

Usage (on a machine with tmux — the recorded fixtures are what CI replays):

    python tools/record_pty_stream.py scrolling -- ls -la /etc
    python tools/record_pty_stream.py clear     -- clear
    python tools/record_pty_stream.py --raw vim -- vim /etc/hostname

Two modes, because the app has two input paths:

  default   The command runs inside tmux and we record what tmux forwards.
            That is what the app sees with terminal_use_tmux (the default),
            and it is a small vocabulary — tmux interprets the program's
            escapes and re-emits its own minimal redraw.

  --raw     The command runs in a bare PTY and we record its own output, the
            full-fat escape stream the app sees with terminal_use_tmux off
            (alternate screen, direct cursor addressing, the lot). The oracle
            is still tmux: the recorded bytes are replayed into a tmux pane
            with `cat`, and that pane is captured.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import pty
import select
import shlex
import struct
import subprocess
import sys
import termios
import time
import uuid

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '..', 'tests', 'fixtures', 'pty_streams')
# The tmux status line would occupy a row the pane doesn't own; turn it off so
# the pane and the outer terminal are the same 80x24 grid.
TMUX_CONF = 'set -g status off\nset -g default-terminal "xterm-256color"\n'


def _drain(fd: int, seconds: float, chunks: list) -> None:
    """Collect output for `seconds`, in the 4096-byte reads the app uses."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:
            return
        if not data:
            return
        chunks.append(data)


def _capture_pane_of(socket_name: str) -> list:
    return subprocess.run(
        ['tmux', '-L', socket_name, 'capture-pane', '-p', '-t', 'rec'],
        capture_output=True, text=True, timeout=5,
    ).stdout.splitlines()


def _kill_server(socket_name: str) -> None:
    subprocess.run(['tmux', '-L', socket_name, 'kill-server'],
                   capture_output=True)


def _write_conf(socket_name: str) -> str:
    conf_path = '/tmp/%s.conf' % socket_name
    with open(conf_path, 'w') as fh:
        fh.write(TMUX_CONF)
    return conf_path


def record_raw(name: str, command: list, cols: int, rows: int,
               settle: float) -> dict:
    """Record a program's own escape stream, then let tmux tell us what it means.

    The bytes come from a bare PTY — no tmux in the middle to normalize them.
    To get an authoritative grid for those bytes we then `cat` them into a
    tmux pane, which interprets them as any terminal would, and capture it.
    """
    pid, fd = pty.fork()
    if pid == 0:
        os.environ['TERM'] = 'xterm-256color'
        os.environ['PS1'] = '$ '
        os.execvp(command[0], command)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    chunks: list = []
    try:
        _drain(fd, settle, chunks)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except (OSError, ChildProcessError, ProcessLookupError):
            pass

    raw_path = '/tmp/pty-raw-%s.bin' % uuid.uuid4().hex[:8]
    with open(raw_path, 'wb') as fh:
        fh.write(b''.join(chunks))

    socket_name = 'rec-' + uuid.uuid4().hex[:8]
    conf_path = _write_conf(socket_name)
    try:
        subprocess.run(
            ['tmux', '-L', socket_name, '-f', conf_path, 'new-session', '-d',
             '-s', 'rec', '-x', str(cols), '-y', str(rows),
             'sh', '-c', 'cat %s; sleep 30' % shlex.quote(raw_path)],
            capture_output=True, timeout=10, check=True,
        )
        time.sleep(1.5)
        expected = _capture_pane_of(socket_name)
    finally:
        _kill_server(socket_name)
        for path in (conf_path, raw_path):
            try:
                os.unlink(path)
            except OSError:
                pass

    return {'chunks': chunks, 'expected': expected}


def record_tmux(name: str, command: list, cols: int, rows: int,
                settle: float) -> dict:
    socket_name = 'rec-' + uuid.uuid4().hex[:8]
    conf_path = _write_conf(socket_name)

    pid, fd = pty.fork()
    if pid == 0:
        os.environ['TERM'] = 'xterm-256color'
        os.environ['PS1'] = '$ '
        os.environ.pop('PROMPT_COMMAND', None)
        os.execvp('tmux', ['tmux', '-L', socket_name, '-f', conf_path,
                           'new-session', '-s', 'rec', 'bash', '--norc'])

    # Size the PTY before tmux paints anything, so every recorded byte belongs
    # to the grid we are going to assert on.
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    chunks: list = []
    try:
        _drain(fd, 2.0, chunks)            # session start + first prompt
        os.write(fd, ' '.join(shlex.quote(a) for a in command).encode() + b'\n')
        _drain(fd, settle, chunks)

        expected = _capture_pane_of(socket_name)
    finally:
        _kill_server(socket_name)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except (OSError, ChildProcessError):
            pass
        try:
            os.unlink(conf_path)
        except OSError:
            pass

    return {'chunks': chunks, 'expected': expected}


def record(name: str, command: list, cols: int, rows: int, settle: float,
           raw: bool) -> str:
    recorded = (record_raw if raw else record_tmux)(name, command, cols, rows,
                                                    settle)
    chunks, expected = recorded['chunks'], recorded['expected']
    fixture = {
        'name': name,
        'command': command,
        'source': 'raw-pty' if raw else 'tmux',
        'cols': cols,
        'rows': rows,
        'recorded': time.strftime('%Y-%m-%d'),
        'tmux': subprocess.run(['tmux', '-V'], capture_output=True,
                               text=True).stdout.strip(),
        # base64 so the fixture stays a readable, diffable JSON file even
        # though the payload is raw control codes.
        'chunks': [base64.b64encode(c).decode() for c in chunks],
        'expected': [line.rstrip() for line in expected],
    }
    path = os.path.join(FIXTURE_DIR, name + '.json')
    with open(path, 'w') as fh:
        json.dump(fixture, fh, indent=1)
        fh.write('\n')
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('name', help='fixture name (tests/fixtures/pty_streams/<name>.json)')
    # (the command itself is taken from after `--`; see below)
    parser.add_argument('--cols', type=int, default=80)
    parser.add_argument('--rows', type=int, default=24)
    parser.add_argument('--settle', type=float, default=2.5,
                        help='seconds to keep reading after the command is sent')
    parser.add_argument('--raw', action='store_true',
                        help="record the program's own escape stream (no tmux "
                             'in the middle) — the non-tmux input path')
    # Split on the first `--` by hand: argparse.REMAINDER would happily
    # swallow the options too when they follow the fixture name.
    argv = sys.argv[1:]
    if '--' not in argv:
        parser.error('give the command after --, e.g. record_pty_stream.py foo -- ls -la')
    split = argv.index('--')
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    if not command:
        parser.error('give a command after --')

    path = record(args.name, command, args.cols, args.rows, args.settle,
                  args.raw)
    print('wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
