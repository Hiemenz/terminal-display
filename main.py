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

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_OUTPUT_IMAGE = os.path.join(_REPO_ROOT, 'output', 'terminal.bmp')


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
    while True:
        try:
            if _is_night(config):
                print("Night mode — sleeping 5 min…")
                time.sleep(300)
                continue

            run_once(config, local=local)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error: {e}")

        time.sleep(interval)


if __name__ == '__main__':
    main()
