"""
Terminal Display — pipeline orchestrator.

Fetch system stats → render image → push to e-ink.

Usage:
    python main.py [--once] [--local] [--config PATH]

Flags:
    --once    Run one cycle and exit (default: loop forever)
    --local   Skip e-ink push; just save the image (useful on macOS)
    --config  Path to config.yaml
"""
import sys
import os
import argparse
import select
import subprocess
import threading
import time
import platform
from datetime import datetime

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config_loader import load_config, add_config_arg
from system_stats import collect
from render import render
from display import send_to_display
from util import output_path
from preview_server import start_if_enabled as _start_preview

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_OUTPUT_IMAGE = os.path.join(_REPO_ROOT, 'output', 'terminal.bmp')

_F11 = b'\x1b[23~'  # F11 escape sequence — switch to terminal mode


def _keyboard_watcher(switch_event: threading.Event, stop_event: threading.Event):
    """
    Background thread: watch stdin for F11 to switch to terminal mode.
    Silently exits if stdin is not a terminal (e.g. systemd service).
    """
    try:
        import tty
        import termios
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            while not stop_event.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.5)
                if r:
                    data = os.read(fd, 16)
                    if _F11 in data:
                        switch_event.set()
                        return
                    if b'\x03' in data:  # Ctrl+C — let main loop handle it
                        stop_event.set()
                        return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


def _is_night(config: dict) -> bool:
    """Return True if we're in the night window and should skip."""
    if not config.get('night_mode', False):
        return False
    now_hour = datetime.now().hour
    start = config.get('night_start', 2)
    end = config.get('night_end', 7)
    if start <= end:
        return start <= now_hour < end
    # wraps midnight
    return now_hour >= start or now_hour < end


def run_once(config: dict, local: bool = False):
    """One fetch → render → display cycle."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Collecting stats…")
    stats = collect(config)

    print("Rendering image…")
    img = render(stats, config)

    os.makedirs(os.path.dirname(_OUTPUT_IMAGE), exist_ok=True)
    img.save(_OUTPUT_IMAGE)
    print(f"Saved → {_OUTPUT_IMAGE}")

    if not local:
        send_to_display(_OUTPUT_IMAGE)
    else:
        print("--local: skipping e-ink push")


def main():
    parser = argparse.ArgumentParser(
        description='Terminal Display — system stats on e-ink',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python main.py --once --local          # render once, no hardware
  python main.py --config config/my.yaml # use alternate config
  python main.py                          # loop forever
        ''',
    )
    parser.add_argument('--once', action='store_true',
                        help='Run one cycle then exit')
    parser.add_argument('--local', action='store_true',
                        help='Skip e-ink push (save image only)')
    add_config_arg(parser)
    args = parser.parse_args()

    config = load_config(args.config)
    interval = config.get('update_interval', 30)

    # On macOS always use local mode (no hardware)
    local = args.local or (platform.system() == 'Darwin')

    if args.once:
        run_once(config, local=local)
        return

    print(f"Terminal Display starting — refresh every {interval}s")
    print("Press F11 to switch to terminal mode.")
    _start_preview(config, _OUTPUT_IMAGE)

    switch_event = threading.Event()
    stop_event = threading.Event()
    kb_thread = threading.Thread(
        target=_keyboard_watcher,
        args=(switch_event, stop_event),
        daemon=True,
    )
    kb_thread.start()

    while True:
        if switch_event.is_set():
            print("Switching to terminal mode…")
            term_py = os.path.join(_REPO_ROOT, 'eink_terminal.py')
            subprocess.Popen(
                [sys.executable, term_py] + (['--local'] if local else []),
                close_fds=True,
                start_new_session=True,
            )
            break

        if stop_event.is_set():
            print("\nStopped.")
            break

        try:
            if _is_night(config):
                print("Night mode — sleeping 5 min…")
                time.sleep(300)
                continue

            run_once(config, local=local)
        except KeyboardInterrupt:
            stop_event.set()
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error: {e}")

        # Responsive sleep: check events every 0.5 s so Ctrl+C and F11 react quickly
        end_time = time.monotonic() + interval
        while not stop_event.is_set() and not switch_event.is_set():
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))


if __name__ == '__main__':
    main()
