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
import queue as _queue
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
from render import render, render_output, render_screensaver
from display import send_to_display
from display_eink import EinkDriver
from refresh_tracker import needs_full_refresh
from util import output_path
from preview_server import start_if_enabled as _start_preview, get_screensaver_path

import socket

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_OUTPUT_IMAGE = os.path.join(_REPO_ROOT, 'output', 'terminal.bmp')

_F11 = b'\x1b[23~'  # F11 escape sequence — switch to terminal mode


def _primary_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except Exception:
        return ''


def _has_active_sessions() -> bool:
    """Return True if any users are currently logged in (SSH or local tty)."""
    try:
        result = subprocess.run(['who'], capture_output=True, text=True, timeout=3)
        return bool(result.stdout.strip())
    except Exception:
        return False


def _keyboard_watcher(switch_event: threading.Event, stop_event: threading.Event):
    """
    Background thread: watch stdin for any keypress to switch to terminal mode.
    Ctrl+C stops the program. Silently exits if stdin is not a tty (e.g. systemd).
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
                    if b'\x03' in data:  # Ctrl+C — stop
                        stop_event.set()
                        return
                    if data:  # any other key → switch to terminal
                        switch_event.set()
                        return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


def _evdev_watcher(switch_event: threading.Event, stop_event: threading.Event, config: dict):
    """
    Background thread: watch the evdev keyboard for any keypress.
    Handles the case where stdin is not a tty (e.g. systemd service).
    Does NOT grab the device — terminal mode will grab it later.
    """
    try:
        from evdev_input import find_keyboard
        import evdev
        kbd_path = config.get('terminal_keyboard_device', 'auto')
        dev = find_keyboard(kbd_path if kbd_path != 'auto' else '')
        if dev is None:
            return
        while not stop_event.is_set():
            r, _, _ = select.select([dev.fileno()], [], [], 0.5)
            if not r:
                continue
            try:
                for event in dev.read():
                    if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                        switch_event.set()
                        return
            except Exception:
                pass
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


def _dequeue(server):
    """Non-blocking: return next queued web input string, or None."""
    if server is None:
        return None
    try:
        return server.input_queue.get_nowait()
    except _queue.Empty:
        return None


def _run_and_render(cmd: str, config: dict, local: bool, driver=None):
    """Run a shell command, render its output to the display."""
    print(f"[web] $ {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10,
            cwd=os.path.expanduser('~'),
        )
        lines = (result.stdout + result.stderr).splitlines()
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        lines = ['(timed out after 10s)']
        exit_code = -1
    except Exception as e:
        lines = [str(e)]
        exit_code = -1

    img = render_output(cmd, lines, exit_code, config)
    if not local:
        if driver is not None:
            driver.full_refresh(img, _OUTPUT_IMAGE)
        else:
            os.makedirs(os.path.dirname(_OUTPUT_IMAGE), exist_ok=True)
            img.save(_OUTPUT_IMAGE)
            send_to_display(_OUTPUT_IMAGE)
    else:
        os.makedirs(os.path.dirname(_OUTPUT_IMAGE), exist_ok=True)
        img.save(_OUTPUT_IMAGE)


def _loop_cycle(config: dict, local: bool, driver: EinkDriver):
    """Stats fetch → render → partial (or periodic full) display cycle."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Collecting stats…")
    stats = collect(config)
    print("Rendering image…")
    img = render(stats, config)
    if local:
        os.makedirs(os.path.dirname(_OUTPUT_IMAGE), exist_ok=True)
        img.save(_OUTPUT_IMAGE)
        print(f"Saved → {_OUTPUT_IMAGE}")
        return
    if needs_full_refresh():
        driver.full_refresh(img, _OUTPUT_IMAGE)
    else:
        driver.partial_refresh_diff(img, _OUTPUT_IMAGE)


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

    driver = EinkDriver(local=local)

    print(f"Terminal Display starting — refresh every {interval}s")
    print("Press F11 to switch to terminal mode.")
    photos_dir = os.path.join(_REPO_ROOT, 'assets', 'gallery')
    _config_path = args.config or os.path.join(_REPO_ROOT, 'config', 'config.yaml')
    server = _start_preview(config, _OUTPUT_IMAGE, photos_dir, _config_path)
    cmd_display_secs = config.get('command_display_seconds', 15)
    cmd_display_until = 0.0

    # Screensaver settings
    screensaver_enabled = config.get('screensaver_enabled', True)
    screensaver_timeout = config.get('screensaver_idle_timeout', 3600)
    _static_screensaver = config.get('screensaver_image_path', 'assets/screensaver.jpg')
    if not os.path.isabs(_static_screensaver):
        _static_screensaver = os.path.join(_REPO_ROOT, _static_screensaver)
    preview_port = config.get('preview_server_port', 8080)
    screensaver_mode = config.get('screensaver_mode', 'static')
    mlb_last_render = 0.0

    # Activity tracking
    last_active = time.monotonic()   # start as active (just booted)
    server_activity_seen = server.last_activity if server else 0.0
    in_screensaver = False
    screensaver_ip = None            # track IP for QR code refresh

    switch_event = threading.Event()
    stop_event = threading.Event()
    kb_thread = threading.Thread(
        target=_keyboard_watcher,
        args=(switch_event, stop_event),
        daemon=True,
    )
    kb_thread.start()
    evdev_thread = threading.Thread(
        target=_evdev_watcher,
        args=(switch_event, stop_event, config),
        daemon=True,
    )
    evdev_thread.start()

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

        now = time.monotonic()

        # --- Activity detection ---
        if _has_active_sessions():
            last_active = now
        if server and server.last_activity != server_activity_seen:
            server_activity_seen = server.last_activity
            last_active = now

        # Check for mobile input — run command and show output
        cmd = _dequeue(server)
        if cmd:
            cmd = cmd.strip()
            if cmd:
                last_active = now
                in_screensaver = False
                _run_and_render(cmd, config, local, driver)
                cmd_display_until = time.monotonic() + cmd_display_secs
                continue

        # While showing command output, just sleep and keep checking for input
        if now < cmd_display_until:
            time.sleep(0.5)
            continue

        # --- Screensaver state machine ---
        idle_secs = now - last_active
        should_screensave = screensaver_enabled and (idle_secs > screensaver_timeout)

        if should_screensave:
            current_ip = _primary_ip()
            mlb_refresh_due = (screensaver_mode == 'mlb' and (now - mlb_last_render) > 900)
            if not in_screensaver or current_ip != screensaver_ip or mlb_refresh_due:
                in_screensaver = True
                screensaver_ip = current_ip
                qr_url = f'http://{current_ip}:{preview_port}/' if current_ip else ''
                try:
                    if screensaver_mode == 'mlb':
                        from mlb_screensaver import render_mlb_screensaver
                        img = render_mlb_screensaver(config)
                        if img is None:
                            print("MLB fetch failed — falling back to static screensaver")
                            active_image = get_screensaver_path(photos_dir) or _static_screensaver
                            img = render_screensaver(active_image, qr_url, config)
                        else:
                            mlb_last_render = now
                            print(f"Screensaver (MLB) — idle {idle_secs:.0f}s")
                    else:
                        active_image = get_screensaver_path(photos_dir) or _static_screensaver
                        print(f"Screensaver — idle {idle_secs:.0f}s  img={os.path.basename(active_image)}  QR→ {qr_url}")
                        img = render_screensaver(active_image, qr_url, config)
                    if not local:
                        driver.full_refresh(img, _OUTPUT_IMAGE)
                    else:
                        os.makedirs(os.path.dirname(_OUTPUT_IMAGE), exist_ok=True)
                        img.save(_OUTPUT_IMAGE)
                except Exception as e:
                    print(f"Screensaver render error: {e}")

            # Responsive idle sleep: wake quickly on activity
            end_time = time.monotonic() + 10
            while not stop_event.is_set() and not switch_event.is_set():
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    break
                if _has_active_sessions():
                    last_active = time.monotonic()
                    break
                if server and server.last_activity != server_activity_seen:
                    server_activity_seen = server.last_activity
                    last_active = time.monotonic()
                    break
                cmd = _dequeue(server)
                if cmd and cmd.strip():
                    last_active = time.monotonic()
                    in_screensaver = False
                    _run_and_render(cmd.strip(), config, local, driver)
                    cmd_display_until = time.monotonic() + cmd_display_secs
                    break
                time.sleep(min(0.5, remaining))
            continue

        # Waking from screensaver
        if in_screensaver:
            in_screensaver = False
            screensaver_ip = None
            mlb_last_render = 0.0
            print("Activity detected — resuming stats display")

        # Normal stats render
        try:
            if _is_night(config):
                print("Night mode — sleeping 5 min…")
                time.sleep(300)
                continue

            _loop_cycle(config, local, driver)
        except KeyboardInterrupt:
            stop_event.set()
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error: {e}")

        # Responsive sleep: react to Ctrl+C, F11, and incoming web commands
        end_time = time.monotonic() + interval
        while not stop_event.is_set() and not switch_event.is_set():
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                break
            if _has_active_sessions():
                last_active = time.monotonic()
            if server and server.last_activity != server_activity_seen:
                server_activity_seen = server.last_activity
                last_active = time.monotonic()
            cmd = _dequeue(server)
            if cmd:
                cmd = cmd.strip()
                if cmd:
                    last_active = time.monotonic()
                    _run_and_render(cmd, config, local, driver)
                    cmd_display_until = time.monotonic() + cmd_display_secs
                    break
            time.sleep(min(0.5, remaining))


if __name__ == '__main__':
    main()
