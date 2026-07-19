#!/usr/bin/env python3
"""
E-ink terminal emulator — run a shell on the Waveshare 7.5" V2 e-ink display.

Usage:
    python eink_terminal.py [--font-size N] [--local] [--config PATH]

Flags:
    --font-size N   Override the font size from config (pt, default 14)
    --local         Skip e-ink push; save frames to output/terminal.bmp only
    --config PATH   Path to config.yaml

Hotkeys (while the terminal is active):
    F9             Decrease font size (−2 pt)
    F12            Increase font size (+2 pt)
    F10            Force a full display refresh (clears ghosting)
    F11            Switch to stats dashboard (launches main.py)
    Ctrl+C         Kill the foreground process (forwarded to shell normally)

Typeable commands (run from the shell):
    settings / eink   Open the on-display config editor
    clear-eink        Clear the screen + e-ink ghosting (keeps the shell)
    notes             Switch to Notes mode (nano on terminal_notes_file)
    llmchat           Switch to local LLM chat mode (src/llm_chat.py)
    terminal          Switch back to a plain shell tab

llm_chat.py also understands /notes and /terminal as slash commands typed
into the chat itself, plus /help and /reset — see src/llm_chat.py.
"""
import argparse
import logging
import os
import platform
import sys

# Route app logs (src/*.py use logging) to stderr → journald. Without this the
# logger has no handler and screensaver/keyboard/error events are invisible.
logging.basicConfig(
    level=os.environ.get('EINK_LOG_LEVEL', 'INFO').upper(),
    format='%(levelname)s %(name)s: %(message)s',
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config_loader import add_config_arg, load_config
from eink_terminal_app import EinkTerminal


def main():
    parser = argparse.ArgumentParser(
        description='E-ink terminal emulator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python eink_terminal.py                    # terminal on e-ink (Pi)
  python eink_terminal.py --local            # save frames only (macOS dev)
  python eink_terminal.py --font-size 16     # larger text
        ''',
    )
    parser.add_argument('--font-size', type=int, default=None,
                        help='Font size in pt (overrides config)')
    parser.add_argument('--local', action='store_true',
                        help='Skip e-ink push (save image only)')
    add_config_arg(parser)
    args = parser.parse_args()

    config = load_config(args.config)

    if args.font_size is not None:
        config['terminal_font_size'] = args.font_size

    local = args.local or (platform.system() == 'Darwin')

    cols, rows = _preview_dimensions(config)
    print(f'E-ink terminal — {cols}×{rows} chars at {config.get("terminal_font_size", 14)}pt')
    print('Hotkeys: F9=Font-  F12=Font+  F10=FullRefresh  F11=Stats  Ctrl+C=Kill')

    terminal = EinkTerminal(config, local=local)
    terminal.run()


def _preview_dimensions(config):
    from terminal_renderer import terminal_dimensions
    fs = config.get('terminal_font_size', 14)
    fp = config.get('terminal_font_path', '')
    cols, rows, _, _ = terminal_dimensions(fs, fp)
    return cols, rows


if __name__ == '__main__':
    main()
