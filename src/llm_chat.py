#!/usr/bin/env python3
"""Local LLM chat REPL — talks to a GGUF model on-device via llama-cpp-python.

Runs entirely offline: no network calls, no API key, no data leaving the Pi.
Launched in its own terminal tab from the F6 command palette ("Chat with
local LLM") or by cycling modes with Ctrl+N; see _open_llm_chat in
tabs_mixin.py. Can also be run directly for testing:

    poetry run python3 src/llm_chat.py

Model/context/thread settings come from terminal_llm_* in config.yaml.

Enter submits the message; Shift+Enter inserts a literal newline so a prompt
can span multiple lines before it's sent — see evdev_input.py, which sends a
bare LF for Shift+Enter and CR for plain Enter, and _read_composer below,
which is the raw-mode reader that tells the two apart.
"""
from __future__ import annotations

import argparse
import os
import select
import subprocess
import sys
import termios
import tty

from config_loader import add_config_arg, load_config

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_MODEL = 'data/models/qwen2.5-1.5b-instruct-q4_k_m.gguf'
_DEFAULT_SYSTEM_PROMPT = 'You are a helpful, concise assistant running locally on a Raspberry Pi.'

# label -> (help text, shell command to run). The shell commands are the same
# `notes`/`terminal` scripts _install_command_scripts drops on PATH for typing
# directly into a shell tab — see src/shell_mixin.py.
_MODE_COMMANDS = {
    '/notes': ('switch to Notes mode', 'notes'),
    '/terminal': ('switch to Terminal mode', 'terminal'),
}


def _command_rows() -> list:
    """(label, description) for every slash command — shared by /help's
    static box and /menu's interactive picker so the two can't drift apart."""
    return [
        ('/help', 'show this help'),
        ('/reset', 'clear conversation history'),
    ] + [(label, desc) for label, (desc, _cmd) in _MODE_COMMANDS.items()] + [
        ('/exit', 'quit (Ctrl+C also works)'),
    ]


def _print_help() -> None:
    rows = _command_rows()
    label_w = max(len(label) for label, _ in rows)
    body = [f'{label:<{label_w}}  {desc}' for label, desc in rows]
    body.append('')
    body.append('/menu opens an interactive picker for the above')
    body.append('Shift+Enter adds a newline instead of sending')
    inner_w = max(len(line) for line in body)
    title = ' Commands '
    top = '┌' + title + '─' * (inner_w + 2 - len(title)) + '┐'
    bottom = '└' + '─' * (inner_w + 2) + '┘'
    print(top)
    for line in body:
        print('│ ' + line.ljust(inner_w) + ' │')
    print(bottom + '\n')


def _read_menu_key(fd: int) -> str:
    """Read one key in raw mode for the /menu picker, decoding arrow keys to
    'UP'/'DOWN'. A bare Esc and the start of an arrow-key escape sequence both
    begin with \\x1b, so a short select() peek after it is what tells a real
    Escape press apart from '\\x1b[A' etc. arriving a byte at a time."""
    ch = os.read(fd, 1).decode(errors='ignore')
    if ch != '\x1b':
        if ch == '\r':
            return 'ENTER'
        if ch in ('\x03', '\x04'):
            return 'ESC'
        return ''
    r, _, _ = select.select([fd], [], [], 0.05)
    if not r:
        return 'ESC'
    ch2 = os.read(fd, 1).decode(errors='ignore')
    if ch2 != '[':
        return 'ESC'
    r2, _, _ = select.select([fd], [], [], 0.05)
    ch3 = os.read(fd, 1).decode(errors='ignore') if r2 else ''
    return {'A': 'UP', 'B': 'DOWN'}.get(ch3, '')


def _show_menu() -> str | None:
    """/menu: an interactive picker over every slash command — arrows move
    the selection, Enter runs it, Esc/Ctrl+C cancels. Returns the chosen
    command string (e.g. '/reset') or None if cancelled."""
    items = _command_rows()
    idx = 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    label_w = max(len(label) for label, _ in items)

    def render(first: bool) -> None:
        if not first:
            _write(f'\x1b[{len(items)}A')
        for i, (label, desc) in enumerate(items):
            marker = '>' if i == idx else ' '
            _write('\x1b[2K\r' + f'{marker} {label:<{label_w}}  {desc}\n')

    try:
        tty.setraw(fd)
        _write('\n')
        render(first=True)
        while True:
            key = _read_menu_key(fd)
            if key == 'UP':
                idx = (idx - 1) % len(items)
                render(first=False)
            elif key == 'DOWN':
                idx = (idx + 1) % len(items)
                render(first=False)
            elif key == 'ENTER':
                return items[idx][0]
            elif key == 'ESC':
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()


def _switch_mode(label: str) -> None:
    """Ask the parent e-ink terminal app to switch tabs/mode (see the
    `notes`/`terminal` scripts in src/shell_mixin.py). This chat process keeps
    running in the background so its history survives the switch — cycling
    back to LLM chat mode (Ctrl+N or `llmchat`) resumes this same session."""
    _desc, cmd = _MODE_COMMANDS[label]
    try:
        subprocess.run([cmd], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f'({_desc} — this chat keeps running in the background)\n')
    except (OSError, subprocess.CalledProcessError):
        print(f'(could not {_desc.lower()} — is the e-ink terminal app running?)\n')


def _resolve_model_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(_REPO_ROOT, path)


def _write(text: str) -> None:
    """Write to stdout with '\\n' -> '\\r\\n' — raw mode leaves OPOST off, so
    a bare '\\n' would otherwise just drop a line without returning left."""
    sys.stdout.write(text.replace('\n', '\r\n'))
    sys.stdout.flush()


def _read_composer(prompt: str) -> str:
    """Raw-mode line reader: CR (Enter) submits, LF (Shift+Enter) inserts a
    newline into the buffer instead. Supports backspace and Ctrl+C/Ctrl+D."""
    _write(prompt)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf: list = []
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch == '':
                raise EOFError
            if ch == '\r':
                break
            if ch == '\n':
                buf.append('\n')
                _write('\n')
            elif ch in ('\x7f', '\x08'):
                if buf:
                    buf.pop()
                    _write('\x08 \x08')
            elif ch == '\x03':
                raise KeyboardInterrupt
            elif ch == '\x04':
                if not buf:
                    raise EOFError
            else:
                buf.append(ch)
                _write(ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    _write('\n')
    return ''.join(buf)


def main() -> None:
    parser = argparse.ArgumentParser(description='Chat with a local LLM (llama.cpp), offline.')
    add_config_arg(parser)
    args = parser.parse_args()
    config = load_config(args.config)

    model_path = _resolve_model_path(config.get('terminal_llm_model_path', _DEFAULT_MODEL))
    if not os.path.isfile(model_path):
        print(f'Model not found: {model_path}', file=sys.stderr)
        print('Set terminal_llm_model_path in config.yaml to a GGUF file.', file=sys.stderr)
        sys.exit(1)

    try:
        from llama_cpp import Llama
    except ImportError:
        print('llama-cpp-python is not installed. Run: poetry add llama-cpp-python', file=sys.stderr)
        sys.exit(1)

    n_ctx = int(config.get('terminal_llm_context_size', 4096))
    n_threads = int(config.get('terminal_llm_threads', 4))
    max_tokens = int(config.get('terminal_llm_max_tokens', 512))
    system_prompt = config.get('terminal_llm_system_prompt') or _DEFAULT_SYSTEM_PROMPT

    print(f'Loading {os.path.basename(model_path)} ...', flush=True)
    llm = Llama(model_path=model_path, n_ctx=n_ctx, n_threads=n_threads, verbose=False)
    print('Ready. Enter sends, Shift+Enter adds a line. Type /menu for commands.\n')

    messages = [{'role': 'system', 'content': system_prompt}]

    while True:
        try:
            user_input = _read_composer('you> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue

        if user_input == '/menu':
            choice = _show_menu()
            if choice is None:
                print('(cancelled)\n')
                continue
            user_input = choice   # fall through to normal command handling

        if user_input in ('/exit', '/quit'):
            break
        if user_input == '/help':
            _print_help()
            continue
        if user_input == '/reset':
            messages = [{'role': 'system', 'content': system_prompt}]
            print('(conversation cleared)\n')
            continue
        if user_input in _MODE_COMMANDS:
            _switch_mode(user_input)
            continue

        messages.append({'role': 'user', 'content': user_input})
        print('llm> ', end='', flush=True)
        reply = ''
        try:
            for chunk in llm.create_chat_completion(
                messages=messages, max_tokens=max_tokens, stream=True,
            ):
                delta = chunk['choices'][0]['delta'].get('content', '')
                if delta:
                    print(delta, end='', flush=True)
                    reply += delta
        except KeyboardInterrupt:
            print('\n(interrupted)')
        print('\n')
        messages.append({'role': 'assistant', 'content': reply})


if __name__ == '__main__':
    main()
