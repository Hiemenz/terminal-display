"""
Tiny HTTP preview server — serves the current e-ink display image over the LAN
and accepts text input from remote devices (phone keyboard → PTY).

Endpoints:
  GET /            → display image + mobile input form
  GET /display     → current display as PNG
  GET /snapshot    → same as /display (alias)
  POST /send       → JSON {"text": "..."} — queued for the terminal PTY
  GET /gallery     → photo gallery page
  GET /photos      → JSON list of available screensaver photos
  GET /photo/<n>   → serve a photo from the gallery (as JPEG)
  GET /preview/<n> → photo cropped to 800×480 grayscale (display preview)
  POST /upload     → multipart upload a new photo
  POST /select     → JSON {"photo": "name.jpg"} — set active screensaver

Usage:
  from preview_server import start_if_enabled
  server = start_if_enabled(config, output_path, photos_dir)
  if server:
      text = server.input_queue.get_nowait()
"""
import io
import json
import logging
import os
import platform
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import yaml
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List

logger = logging.getLogger(__name__)

_SELECTION_FILE = '.selected'
_DISPLAY_W, _DISPLAY_H = 800, 480

_ALLOWED_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}

_CONFIG_SCHEMA = [
    ['Display', [
        ['device_label',        'str',    'Device Label',         'Custom name shown on display (blank = hostname)'],
        ['dark_mode',           'bool',   'Dark Mode',            'White text on black background'],
        ['update_interval',     'int',    'Update Interval',      'Seconds between refreshes'],
        ['timezone',            'str',    'Timezone',             'IANA string, e.g. America/Chicago'],
        ['show_cpu',            'bool',   'Show CPU',             None],
        ['show_memory',         'bool',   'Show Memory',          None],
        ['show_disk',           'bool',   'Show Disk',            None],
        ['show_network',        'bool',   'Show Network',         None],
        ['show_load',           'bool',   'Show Load Average',    None],
        ['show_top_processes',  'bool',   'Show Top Processes',   None],
        ['top_process_count',   'int',    'Top Process Count',    'Number of processes to list'],
        ['disk_path',           'str',    'Disk Path',            'Filesystem path to monitor'],
        ['network_interface',   'str',    'Network Interface',    'Empty = auto-detect'],
        ['show_qr_code',        'bool',   'Show QR Code',         'QR code in stats screen'],
    ]],
    ['Night Mode', [
        ['night_mode',          'bool',   'Night Mode',           'Skip refreshes at night'],
        ['night_start',         'int',    'Night Start',          'Hour 0–23'],
        ['night_end',           'int',    'Night End',            'Hour 0–23'],
    ]],
    ['Terminal', [
        ['terminal_font_size',                  'select', 'Font Size',            [8, 10, 12, 14, 16, 18, 20]],
        ['terminal_dark_mode',                  'bool',   'Dark Mode',            'Inherits global Dark Mode if off'],
        ['terminal_cursor_style',               'select', 'Cursor Style',         ['block', 'underline']],
        ['terminal_show_qr',                    'bool',   'Show QR Code',         'URL QR in terminal corner'],
        ['terminal_prompt_custom',              'bool',   'Custom Prompt',        'Override shell PS1 with parts below'],
        ['terminal_prompt_show_user',           'bool',   'Prompt: User',         None],
        ['terminal_prompt_show_host',           'bool',   'Prompt: Host',         None],
        ['terminal_prompt_show_cwd',            'bool',   'Prompt: Dir',          None],
        ['terminal_prompt_show_git',            'bool',   'Prompt: Git Branch',   None],
        ['terminal_start_dir',                  'str',    'Start Directory',      "home, last, root, or a path"],
        ['terminal_idle_timeout',               'int',    'Idle Timeout (s)',      '0 = disabled'],
        ['terminal_full_refresh_interval',      'int',    'Full Refresh (s)',      '0 = disabled'],
        ['terminal_use_tmux',                   'bool',   'Use tmux',              None],
        ['terminal_tmux_session',               'str',    'tmux Session',          None],
        ['terminal_keyboard_prefer_bluetooth',  'bool',   'Prefer BT Keyboard',   'Rank Bluetooth keyboards first'],
        ['terminal_hq_render',                  'bool',   'HQ Render',             '2× supersample → sharper text'],
        ['terminal_region_flash',               'bool',   'Region Flash',          'Flash only the changed rows, not the whole panel'],
        ['terminal_full_refresh_interval',      'int',    'Full Flash Interval',   'Seconds between whole-panel flashes (0 = off)'],
        ['terminal_flash_idle_gap',             'int',    'Flash Idle Gap',        'Wait for this quiet gap before the full flash'],
        ['terminal_du_adaptive',                'bool',   'Adaptive DU',           'More DU frames for heavy/inverse content'],
        ['terminal_split_view',                 'bool',   'Split View',            '600 px terminal + 200 px sidebar'],
        ['terminal_status_bar_extras',          'bool',   'Status Bar Extras',     'Time, CWD, git branch'],
        ['terminal_status_bar_compact',         'bool',   'Compact Status Bar',    'Short labels, no uptime/sizes'],
        ['terminal_status_bar_show_time',       'bool',   'Status: Time',          None],
        ['terminal_status_bar_show_cwd',        'bool',   'Status: CWD',           None],
        ['terminal_status_bar_show_ip',         'bool',   'Status: IP',            None],
        ['terminal_status_bar_show_speed',      'bool',   'Status: Speed',         None],
        ['terminal_scrollback',                 'int',    'Scrollback Lines',      'Non-tmux mode only'],
        ['terminal_alert_cpu_threshold',        'int',    'CPU Alert %',           '0 = disabled'],
        ['terminal_alert_disk_free_threshold',  'int',    'Disk Free Alert %',     '0 = disabled'],
        ['terminal_alert_ssh_logins',           'bool',   'SSH Login Alerts',      None],
    ]],
    ['Screensaver', [
        ['screensaver_sleep_minutes',   'select', 'Sleep After (min)',    [0, 5, 10, 15, 30, 60]],
        ['screensaver_enabled',         'bool',   'Enabled',              None],
        ['screensaver_idle_timeout',    'int',    'Idle Timeout (s)',      'Before screensaver activates'],
        ['screensaver_mode',            'select', 'Mode',                 ['static', 'cycle']],
        ['screensaver_cycle_interval',  'int',    'Cycle Interval (min)', 'Cycle mode only'],
    ]],
    ['Startup & Web', [
        ['startup_mode',            'select', 'Startup Mode',          ['terminal', 'stats']],
        ['preview_server_enabled',  'bool',   'Web Server',            None],
        ['preview_server_port',     'int',    'Web Port',              None],
        ['speedtest_interval',      'int',    'Speedtest Interval (s)', '1200 = 20 min, 0 = disable'],
        ['command_display_seconds', 'int',    'Command Display (s)',   'Seconds to show output'],
    ]],
]

_CONFIG_HTML = '''\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0d0d0d">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>Config — e-ink</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0d0d0d; --surface: #161616; --surface2: #1e1e1e;
      --border: #2a2a2a; --text: #e2e2e2; --muted: #888;
      --accent: #3b82f6; --danger: #ef4444; --success: #22c55e;
      --radius: 12px;
    }
    html, body {
      min-height: 100dvh; background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      position: sticky; top: 0; z-index: 10;
      display: flex; align-items: center; gap: 10px;
      padding: 12px 16px; padding-top: max(12px, env(safe-area-inset-top));
      background: var(--surface); border-bottom: 1px solid var(--border);
    }
    .back {
      color: var(--accent); text-decoration: none; font-size: 14px;
      flex-shrink: 0; padding: 4px 0;
    }
    .back:active { opacity: 0.7; }
    header h1 { flex: 1; font-size: 17px; font-weight: 700; }
    #save-btn {
      background: var(--accent); color: #fff; border: none;
      border-radius: 8px; padding: 9px 18px; font-size: 14px;
      font-weight: 600; cursor: pointer; flex-shrink: 0;
    }
    #save-btn:active { background: #2563eb; }
    #save-btn:disabled { background: var(--surface2); color: var(--muted); cursor: default; }

    main {
      padding: 16px; padding-bottom: max(40px, env(safe-area-inset-bottom));
      display: flex; flex-direction: column; gap: 14px;
    }

    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); overflow: hidden;
    }
    .card-title {
      padding: 9px 16px; font-size: 11px; font-weight: 700;
      letter-spacing: 0.8px; text-transform: uppercase; color: var(--muted);
      border-bottom: 1px solid var(--border);
    }
    .row {
      display: flex; align-items: center; gap: 12px;
      padding: 13px 16px; border-bottom: 1px solid var(--border); min-height: 54px;
    }
    .row:last-child { border-bottom: none; }
    .row-lbl { flex: 1; min-width: 0; }
    .lbl { font-size: 14px; font-weight: 500; }
    .hint { font-size: 11px; color: var(--muted); margin-top: 2px; line-height: 1.35; }

    /* Toggle */
    .toggle { position: relative; flex-shrink: 0; width: 50px; height: 28px; }
    .toggle input { opacity: 0; width: 0; height: 0; position: absolute; }
    .track {
      position: absolute; inset: 0; cursor: pointer; background: #333;
      border-radius: 14px; transition: background 0.2s;
    }
    .toggle input:checked ~ .track { background: var(--accent); }
    .track::after {
      content: ""; position: absolute; left: 4px; top: 4px;
      width: 20px; height: 20px; background: #fff; border-radius: 50%;
      box-shadow: 0 1px 3px rgba(0,0,0,.4); transition: transform 0.2s;
    }
    .toggle input:checked ~ .track::after { transform: translateX(22px); }

    /* Stepper */
    .stepper {
      display: flex; align-items: center; flex-shrink: 0;
      border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
    }
    .step-btn {
      width: 36px; height: 38px; background: var(--surface2); border: none;
      color: var(--text); font-size: 20px; cursor: pointer;
      display: flex; align-items: center; justify-content: center; flex-shrink: 0;
      -webkit-tap-highlight-color: transparent;
    }
    .step-btn:active { background: #2e2e2e; }
    .step-inp {
      width: 60px; height: 38px; background: var(--surface2);
      border: none; border-left: 1px solid var(--border);
      border-right: 1px solid var(--border);
      color: var(--text); font-size: 14px; text-align: center;
      padding: 0; outline: none;
      -moz-appearance: textfield; appearance: textfield;
    }
    .step-inp::-webkit-inner-spin-button,
    .step-inp::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }

    /* Text / select */
    .txt-inp, .sel-inp {
      flex-shrink: 0; background: var(--surface2); border: 1px solid var(--border);
      border-radius: 8px; color: var(--text); font-size: 15px;
      padding: 9px 10px; outline: none; min-width: 0;
    }
    .txt-inp { width: 148px; }
    .txt-inp:focus { border-color: var(--accent); }
    .sel-inp {
      appearance: none; -webkit-appearance: none;
      padding-right: 28px; cursor: pointer;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'%3E%3Cpath fill='%23888' d='M0 0l5 6 5-6z'/%3E%3C/svg%3E");
      background-repeat: no-repeat; background-position: right 10px center; background-size: 10px;
    }

    /* Toast */
    #toast {
      position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
      background: var(--surface2); border: 1px solid var(--border); border-radius: 10px;
      padding: 12px 22px; font-size: 14px; font-weight: 600; white-space: nowrap;
      opacity: 0; transition: opacity 0.25s; pointer-events: none; z-index: 50;
    }
    #toast.show { opacity: 1; }
    #toast.ok  { border-color: var(--success); color: var(--success); }
    #toast.err { border-color: var(--danger);  color: var(--danger); }

    /* Restart overlay */
    #overlay {
      position: fixed; inset: 0; z-index: 100; background: rgba(13,13,13,.93);
      display: flex; align-items: center; justify-content: center;
      flex-direction: column; gap: 14px;
    }
    #overlay[hidden] { display: none; }
    .spin {
      width: 46px; height: 46px;
      border: 3px solid #333; border-top-color: var(--accent);
      border-radius: 50%; animation: spin .7s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .ov-title { font-size: 18px; font-weight: 700; }
    .ov-count { font-size: 54px; font-weight: 800; color: var(--accent); line-height: 1; }
    .ov-sub   { font-size: 13px; color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <a href="/" class="back">&#8592; e-ink</a>
    <h1>Config</h1>
    <button id="save-btn" onclick="save()">Save &amp; Restart</button>
  </header>
  <main id="root"></main>
  <div id="toast"></div>
  <div id="overlay" hidden>
    <div class="spin"></div>
    <div class="ov-title">Restarting&hellip;</div>
    <div class="ov-count" id="ov-n">5</div>
    <div class="ov-sub">Page reloads automatically</div>
  </div>
  <script>
    var CFG    = __CONFIG_JSON__;
    var SCHEMA = __SCHEMA_JSON__;

    var root = document.getElementById("root");
    SCHEMA.forEach(function(sec) {
      var card  = mk("div", {className: "card"});
      card.appendChild(mk("div", {className: "card-title"}, sec[0]));
      sec[1].forEach(function(f) { card.appendChild(makeRow(f)); });
      root.appendChild(card);
    });

    function makeRow(f) {
      var key = f[0], type = f[1], label = f[2], extra = f[3];
      var val = CFG[key];
      var row = mk("div", {className: "row"});
      var lbl = mk("div", {className: "row-lbl"});
      lbl.appendChild(mk("div", {className: "lbl"}, label));
      if (extra && !Array.isArray(extra))
        lbl.appendChild(mk("div", {className: "hint"}, extra));
      row.appendChild(lbl);

      var ctrl;
      if (type === "bool") {
        ctrl = mk("label", {className: "toggle"});
        var inp = mk("input"); inp.type = "checkbox";
        inp.checked = !!val; inp.dataset.key = key;
        ctrl.appendChild(inp);
        ctrl.appendChild(mk("span", {className: "track"}));
      } else if (type === "int") {
        ctrl = mk("div", {className: "stepper"});
        var minus = mk("button", {className: "step-btn", type: "button"}, "−");
        var si    = mk("input", {className: "step-inp", type: "number"});
        var plus  = mk("button", {className: "step-btn", type: "button"}, "+");
        si.dataset.key = key; si.value = val != null ? val : 0;
        minus.addEventListener("click", function() { si.value = parseInt(si.value||0) - 1; });
        plus.addEventListener("click",  function() { si.value = parseInt(si.value||0) + 1; });
        ctrl.appendChild(minus); ctrl.appendChild(si); ctrl.appendChild(plus);
      } else if (type === "select") {
        ctrl = mk("select", {className: "sel-inp"});
        ctrl.dataset.key = key;
        (Array.isArray(extra) ? extra : []).forEach(function(o) {
          var opt = mk("option", {value: o}, o);
          if (o === val) opt.selected = true;
          ctrl.appendChild(opt);
        });
      } else {
        ctrl = mk("input", {className: "txt-inp", type: "text"});
        ctrl.dataset.key = key;
        ctrl.value = val != null ? String(val) : "";
      }
      row.appendChild(ctrl);
      return row;
    }

    function mk(tag, props, text) {
      var e = document.createElement(tag);
      if (props) Object.assign(e, props);
      if (text != null) e.textContent = text;
      return e;
    }

    function collect() {
      var out = {};
      document.querySelectorAll("[data-key]").forEach(function(e) {
        var k = e.dataset.key;
        if (e.type === "checkbox")    out[k] = e.checked;
        else if (e.classList.contains("step-inp")) out[k] = parseInt(e.value) || 0;
        else out[k] = e.value;
      });
      return out;
    }

    function save() {
      var btn = document.getElementById("save-btn");
      btn.disabled = true;
      fetch("/config", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(collect())
      }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.ok) startRestart();
        else { toast("Error: " + (d.error || "?"), "err"); btn.disabled = false; }
      }).catch(function(e) {
        toast("Failed: " + e, "err"); btn.disabled = false;
      });
    }

    function startRestart() {
      var ov = document.getElementById("overlay");
      var nn = document.getElementById("ov-n");
      ov.removeAttribute("hidden");
      var n = 5;
      var t = setInterval(function() {
        n--; nn.textContent = n > 0 ? n : "↻";
        if (n <= 0) { clearInterval(t); poll(); }
      }, 1000);
    }

    function poll() {
      fetch("/config?_=" + Date.now(), {cache: "no-store"})
        .then(function(r) { if (r.ok) location.reload(); else setTimeout(poll, 600); })
        .catch(function()  { setTimeout(poll, 600); });
    }

    function toast(msg, cls) {
      var t = document.getElementById("toast");
      t.textContent = msg; t.className = "show" + (cls ? " " + cls : "");
      setTimeout(function() { t.className = ""; }, 3500);
    }
  </script>
</body>
</html>
'''


def _atomic_write_config(config_path: str, content: str):
    """Validate then atomically replace config_path with content.

    Guards against a kill / power-loss mid-write leaving config.yaml truncated
    (which would make the next boot load an empty config and drop every setting).
    The new content must parse as a YAML mapping or the write is refused and the
    existing file is left intact.
    """
    parsed = yaml.safe_load(content)
    if not isinstance(parsed, dict):
        raise ValueError("refusing to write config: content is not a YAML mapping")
    dir_name = os.path.dirname(config_path) or '.'
    fd, tmp = tempfile.mkstemp(dir=dir_name, prefix='.config.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_path)   # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_config_values(config_path: str, updates: dict):
    """Write key-value pairs into config.yaml, replacing only the matching lines."""
    with open(config_path) as f:
        content = f.read()
    for key, value in updates.items():
        if isinstance(value, bool):
            val_str = 'true' if value else 'false'
        elif isinstance(value, (int, float)):
            val_str = str(int(value))
        else:
            s = str(value)
            needs_quotes = not s or any(c in s for c in ':#{}[]|>&!\'"@`')
            val_str = f'"{s}"' if needs_quotes else s
        pattern = rf'^({re.escape(key)}:\s*).*$'
        new_line = f'{key}: {val_str}'
        content, n = re.subn(pattern, new_line, content, flags=re.MULTILINE)
        if n == 0:
            content += f'\n{new_line}\n'
    _atomic_write_config(config_path, content)


def _build_config_html(config_data: dict) -> str:
    import json
    return (_CONFIG_HTML
            .replace('__CONFIG_JSON__', json.dumps(config_data))
            .replace('__SCHEMA_JSON__', json.dumps(_CONFIG_SCHEMA)))


def _get_startup_mode(config_path: str) -> str:
    try:
        with open(config_path) as f:
            for line in f:
                if line.startswith('startup_mode:'):
                    return line.split(':', 1)[1].strip().strip('"\'')
    except Exception:
        pass
    return 'stats'


def _set_startup_mode(config_path: str, mode: str):
    with open(config_path) as f:
        content = f.read()
    content = re.sub(r'^startup_mode:.*$', f'startup_mode: {mode}', content, flags=re.MULTILINE)
    _atomic_write_config(config_path, content)
    if platform.system() == 'Linux':
        subprocess.Popen(['sudo', 'systemctl', 'restart', 'eink-display'])


def _parse_upload_field(body: bytes, content_type: str):
    """Extract the 'photo' field from a multipart/form-data body. Returns (filename, bytes)."""
    boundary = ''
    for tok in content_type.split(';'):
        tok = tok.strip()
        if tok.startswith('boundary='):
            boundary = tok[9:].strip('"\'')
            break
    if not boundary:
        raise ValueError('No boundary in Content-Type')
    sep = b'--' + boundary.encode()
    for chunk in body.split(sep)[1:]:
        if chunk[:2] == b'--':
            break
        if b'\r\n\r\n' not in chunk:
            continue
        hdr_bytes, payload = chunk.split(b'\r\n\r\n', 1)
        if payload.endswith(b'\r\n'):
            payload = payload[:-2]
        headers = hdr_bytes.decode('utf-8', errors='replace')
        name = filename = None
        for line in headers.splitlines():
            if line.lower().startswith('content-disposition:'):
                for param in line.split(';')[1:]:
                    k, _, v = param.strip().partition('=')
                    v = v.strip('"\'')
                    if k.strip() == 'name':
                        name = v
                    elif k.strip() == 'filename':
                        filename = v
        if name == 'photo':
            return filename or 'upload.jpg', payload
    raise KeyError('photo')


def _crop_to_display(img):
    """Center-crop a PIL image to 800×480 without warping."""
    from PIL import Image
    src_w, src_h = img.size
    scale = max(_DISPLAY_W / src_w, _DISPLAY_H / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - _DISPLAY_W) // 2
    top = (new_h - _DISPLAY_H) // 2
    return img.crop((left, top, left + _DISPLAY_W, top + _DISPLAY_H))

_PAGE_HTML = '''\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0d0d0d">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>e-ink</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg: #0d0d0d;
      --surface: #161616;
      --surface2: #1e1e1e;
      --border: #2a2a2a;
      --text: #e2e2e2;
      --muted: #666;
      --accent: #3b82f6;
      --accent-active: #2563eb;
      --danger: #ef4444;
      --success: #22c55e;
      --warn: #f59e0b;
      --radius: 12px;
    }}

    html, body {{
      height: 100%;
      height: 100dvh;
      background: var(--bg);
      color: var(--text);
      font-family: ui-monospace, 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
      overflow: hidden;
      -webkit-tap-highlight-color: transparent;
    }}

    body {{ display: flex; flex-direction: column; }}

    /* ── Header ── */
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      padding-top: max(10px, env(safe-area-inset-top));
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
      gap: 10px;
    }}
    .logo {{
      font-size: 13px;
      font-weight: 600;
      color: var(--muted);
      letter-spacing: 1px;
      text-transform: uppercase;
    }}
    .header-right {{ display: flex; gap: 8px; align-items: center; }}

    .pill-btn {{
      display: inline-flex; align-items: center; gap: 5px;
      background: var(--surface2);
      border: 1px solid var(--border);
      color: var(--muted);
      padding: 6px 12px;
      border-radius: 20px;
      font: inherit;
      font-size: 12px;
      cursor: pointer;
      text-decoration: none;
      white-space: nowrap;
      transition: color 0.15s, border-color 0.15s;
    }}
    .pill-btn:active {{ color: var(--text); border-color: #444; background: #252525; }}

    /* ── Display ── */
    .display-wrap {{
      flex: 1;
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 10px;
      background: #080808;
      position: relative;
    }}
    #d {{
      max-width: 100%;
      max-height: 100%;
      width: auto;
      height: auto;
      border-radius: 6px;
      border: 1px solid #1e1e1e;
      image-rendering: pixelated;
      display: block;
    }}
    .refresh-indicator {{
      position: absolute;
      top: 8px; right: 8px;
      width: 7px; height: 7px;
      border-radius: 50%;
      background: var(--muted);
      opacity: 0;
      transition: opacity 0.2s;
    }}
    .refresh-indicator.pulse {{ opacity: 1; animation: blink 0.4s ease; }}
    @keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:0.2}} }}

    /* ── Input panel ── */
    .panel {{
      flex-shrink: 0;
      background: var(--surface);
      border-top: 1px solid var(--border);
      padding: 10px 14px;
      padding-bottom: max(14px, env(safe-area-inset-bottom));
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}

    /* Quick keys */
    .keys {{
      display: flex;
      gap: 5px;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
      padding-bottom: 1px;
    }}
    .keys::-webkit-scrollbar {{ display: none; }}
    .key {{
      flex-shrink: 0;
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font: inherit;
      font-size: 12px;
      padding: 8px 13px;
      cursor: pointer;
      white-space: nowrap;
      min-width: 44px;
      text-align: center;
    }}
    .key:active {{ background: #2a2a2a; }}
    .key.red {{ color: var(--danger); border-color: #3a2020; }}
    .key.red:active {{ background: #2a1515; }}

    /* Text input row */
    .input-row {{
      display: flex;
      gap: 8px;
      align-items: flex-end;
    }}
    #inp {{
      flex: 1;
      background: var(--surface2);
      border: 1.5px solid var(--border);
      border-radius: var(--radius);
      color: var(--text);
      font: inherit;
      font-size: 16px; /* prevents iOS auto-zoom */
      padding: 11px 14px;
      outline: none;
      resize: none;
      min-height: 44px;
      max-height: 110px;
      line-height: 1.4;
      -webkit-appearance: none;
    }}
    #inp:focus {{ border-color: var(--accent); }}
    #inp::placeholder {{ color: #3a3a3a; }}

    #send {{
      width: 44px;
      height: 44px;
      flex-shrink: 0;
      border-radius: var(--radius);
      border: none;
      background: var(--accent);
      color: #fff;
      font-size: 20px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: background 0.1s;
    }}
    #send:active {{ background: var(--accent-active); }}
    #send:disabled {{ background: var(--surface2); color: var(--muted); }}

    /* Status footer */
    .footer {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 16px;
    }}
    #status {{ font-size: 11px; color: var(--muted); }}
    .dot {{
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--border);
      transition: background 0.3s;
      flex-shrink: 0;
    }}
    .dot.ok {{ background: var(--success); }}
    .dot.busy {{ background: var(--warn); }}
    .dot.err {{ background: var(--danger); }}

    /* Action buttons */
    .actions {{
      display: flex;
      gap: 5px;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
      padding-bottom: 1px;
    }}
    .actions::-webkit-scrollbar {{ display: none; }}
    .act {{
      flex-shrink: 0;
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font: inherit;
      font-size: 11px;
      padding: 7px 11px;
      cursor: pointer;
      white-space: nowrap;
      min-width: 44px;
      text-align: center;
    }}
    .act:active {{ background: #2a2a2a; }}
    .act.accent {{ border-color: #1d3a60; color: var(--accent); }}
    .act.accent:active {{ background: #0f1e30; }}

    /* Message form */
    .msg-form {{
      display: flex;
      gap: 6px;
      align-items: flex-end;
    }}
    #msg-inp {{
      flex: 1;
      background: var(--surface2);
      border: 1.5px solid var(--border);
      border-radius: var(--radius);
      color: var(--text);
      font: inherit;
      font-size: 14px;
      padding: 9px 12px;
      outline: none;
      -webkit-appearance: none;
    }}
    #msg-inp:focus {{ border-color: var(--accent); }}
    #msg-inp::placeholder {{ color: #3a3a3a; }}
    #msg-send {{
      flex-shrink: 0;
      border-radius: var(--radius);
      border: none;
      background: #1d4a2a;
      color: var(--success);
      font: inherit;
      font-size: 12px;
      padding: 9px 14px;
      cursor: pointer;
      white-space: nowrap;
    }}
    #msg-send:active {{ background: #143520; }}

    /* WiFi section */
    .wifi-row {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    #wifi-ssid {{
      flex: 1;
      font-size: 12px;
      color: var(--muted);
    }}
    #wifi-btn {{
      flex-shrink: 0;
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font: inherit;
      font-size: 12px;
      padding: 7px 14px;
      cursor: pointer;
    }}
    #wifi-btn:active {{ background: #2a2a2a; }}
    #wifi-qr-wrap {{
      text-align: center;
      padding: 8px 0 4px;
      display: none;
    }}
    #wifi-qr-wrap img {{ max-width: 180px; border-radius: 4px; }}
    #wifi-qr-wrap p {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}
  </style>
</head>
<body>
  <header>
    <span class="logo">e-ink</span>
    <div class="header-right">
      <a href="/config" class="pill-btn">&#9881; Config</a>
      <a href="/gallery" class="pill-btn">&#128247; Gallery</a>
      <a href="/clipboard" class="pill-btn">&#128203; Clips</a>
      <button id="mode-btn" class="pill-btn" onclick="toggleMode()">&#8644; Mode</button>
    </div>
  </header>

  <div class="display-wrap">
    <img id="d" src="/display" alt="e-ink display">
    <div class="refresh-indicator" id="ri"></div>
  </div>

  <div class="panel">
    <!-- 7 quick display actions -->
    <div class="actions">
      <button class="act accent" onclick="doAction('force_refresh')" title="Clear ghosting (flash)">&#9678; Refresh</button>
      <button class="act" onclick="doAction('toggle_dark')" title="Toggle dark/light mode">&#9681; Dark</button>
      <button class="act" onclick="sendRaw('\\x1b[20~')" title="Smaller font">A&#8315;</button>
      <button class="act" onclick="sendRaw('\\x1b[24~')" title="Larger font">A&#8314;</button>
      <button class="act" onclick="doAction('screensaver')" title="Show screensaver now">&#9167; Sleep</button>
      <button class="act" onclick="sendRaw('\\x1b[23~')" title="Switch to stats">&#128200; Stats</button>
      <button class="act" onclick="doAction('toggle_qr')" title="Toggle QR code">&#9638; QR</button>
    </div>

    <!-- Send text to display -->
    <div class="msg-form">
      <input id="msg-inp" type="text" placeholder="Send text to display…"
        autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
        onkeydown="if(event.key==='Enter')sendMessage()">
      <button id="msg-send" onclick="sendMessage()">&#128204; Show</button>
    </div>

    <!-- WiFi join -->
    <div class="wifi-row">
      <span id="wifi-ssid">Loading WiFi&#8230;</span>
      <button id="wifi-btn" onclick="toggleWifi()">&#128246; Join WiFi</button>
    </div>
    <div id="wifi-qr-wrap">
      <img id="wifi-qr-img" src="" alt="WiFi QR">
      <p>Scan to join this network</p>
    </div>

    <div class="keys" id="keys">
      <button class="key" onclick="sendRaw('\\x03')">^C</button>
      <button class="key" onclick="sendRaw('\\x04')">^D</button>
      <button class="key" onclick="sendRaw('\\x1b')">Esc</button>
      <button class="key" onclick="sendRaw('\\t')">Tab</button>
      <button class="key" onclick="sendRaw('\\x1b[A')">&#8593;</button>
      <button class="key" onclick="sendRaw('\\x1b[B')">&#8595;</button>
      <button class="key" onclick="sendRaw('\\x1b[D')">&#8592;</button>
      <button class="key" onclick="sendRaw('\\x1b[C')">&#8594;</button>
      <button class="key" onclick="sendRaw('\\x1b[5~')">PgUp</button>
      <button class="key" onclick="sendRaw('\\x1b[6~')">PgDn</button>
      <button class="key red" onclick="sendRaw('\\x15')">Clear</button>
    </div>

    <div class="input-row">
      <textarea id="inp" rows="1"
        placeholder="command…"
        autocomplete="off" autocorrect="off"
        autocapitalize="off" spellcheck="false"></textarea>
      <button id="send" onclick="send()" aria-label="Send">&#8629;</button>
    </div>

    <div class="footer">
      <span id="status">ready</span>
      <div class="dot" id="dot"></div>
    </div>
  </div>

  <script>
    var inp = document.getElementById("inp");
    var statusEl = document.getElementById("status");
    var dotEl = document.getElementById("dot");
    var ri = document.getElementById("ri");

    /* Auto-resize textarea */
    inp.addEventListener("input", function() {{
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 110) + "px";
    }});
    inp.addEventListener("keydown", function(e) {{
      if (e.key === "Enter" && !e.shiftKey) {{ e.preventDefault(); send(); }}
    }});

    function setStatus(msg, state) {{
      statusEl.textContent = msg;
      dotEl.className = "dot" + (state ? " " + state : "");
    }}

    function send() {{
      var t = inp.value;
      if (!t) return;
      inp.value = "";
      inp.style.height = "auto";
      sendRaw(t + "\\n");
    }}

    function sendRaw(text) {{
      setStatus("sending…", "busy");
      fetch("/send", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{text: text}})
      }}).then(function(r) {{
        if (r.ok) {{
          setStatus("sent", "ok");
          /* Refresh display promptly after the terminal renders */
          setTimeout(refreshDisplay, 700);
          setTimeout(refreshDisplay, 1600);
          setTimeout(function(){{ setStatus("ready", ""); }}, 3000);
        }} else {{
          setStatus("error " + r.status, "err");
        }}
      }}).catch(function() {{
        setStatus("offline", "err");
      }});
    }}

    function refreshDisplay() {{
      var next = new Image();
      next.onload = function() {{
        document.getElementById("d").src = next.src;
        ri.className = "refresh-indicator pulse";
        setTimeout(function(){{ ri.className = "refresh-indicator"; }}, 500);
      }};
      next.src = "/display?t=" + Date.now();
    }}

    /* Auto-refresh every 1.5 s */
    setInterval(refreshDisplay, 1500);
    inp.focus();

    /* Mode toggle */
    var _mode = "";
    function loadMode() {{
      fetch("/mode").then(function(r){{ return r.json(); }}).then(function(d) {{
        _mode = d.mode;
        document.getElementById("mode-btn").textContent =
          d.mode === "stats" ? "&#8644; Terminal" : "&#8644; Stats";
      }}).catch(function(){{}});
    }}
    function toggleMode() {{
      var next = _mode === "stats" ? "terminal" : "stats";
      document.getElementById("mode-btn").textContent = "&#8644; …";
      fetch("/mode", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{mode: next}})
      }}).then(function(r){{ return r.json(); }}).then(function(d) {{
        if (d.ok) {{ _mode = next; loadMode(); }}
        else loadMode();
      }}).catch(loadMode);
    }}
    loadMode();

    /* Display actions */
    function doAction(action) {{
      if (action === 'toggle_dark') {{
        sendRaw('\\x1b[18~'); return;
      }}
      setStatus(action + '…', 'busy');
      fetch("/action", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{action: action}})
      }}).then(function(r) {{
        if (r.ok) {{ setStatus('done', 'ok'); setTimeout(function(){{ setStatus('ready',''); }}, 2000); }}
        else setStatus('error', 'err');
      }}).catch(function() {{ setStatus('offline','err'); }});
    }}

    /* Send text message to display */
    function sendMessage() {{
      var t = document.getElementById('msg-inp').value.trim();
      if (!t) return;
      setStatus('sending…', 'busy');
      fetch("/message", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{text: t, label: 'Message'}})
      }}).then(function(r) {{
        if (r.ok) {{
          document.getElementById('msg-inp').value = '';
          setStatus('displayed', 'ok');
          setTimeout(refreshDisplay, 800);
          setTimeout(function(){{ setStatus('ready',''); }}, 3000);
        }} else setStatus('error', 'err');
      }}).catch(function() {{ setStatus('offline','err'); }});
    }}

    /* WiFi join QR */
    var _wifiShown = false;
    function loadWifi() {{
      fetch("/wifi-info").then(function(r){{ return r.json(); }}).then(function(d) {{
        document.getElementById('wifi-ssid').textContent =
          d.ssid ? 'WiFi: ' + d.ssid : 'No WiFi detected';
        if (d.qr_url) {{
          document.getElementById('wifi-qr-img').src = d.qr_url;
        }} else {{
          document.getElementById('wifi-btn').style.display = 'none';
        }}
      }}).catch(function() {{
        document.getElementById('wifi-ssid').textContent = 'WiFi info unavailable';
      }});
    }}
    function toggleWifi() {{
      _wifiShown = !_wifiShown;
      document.getElementById('wifi-qr-wrap').style.display = _wifiShown ? 'block' : 'none';
    }}
    loadWifi();
  </script>
</body>
</html>
'''

_GALLERY_HTML = '''\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0d0d0d">
  <title>Gallery</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #0d0d0d; --surface: #161616; --surface2: #1e1e1e; --border: #2a2a2a;
      --text: #e2e2e2; --muted: #666; --accent: #3b82f6; --danger: #ef4444;
      --success: #22c55e; --radius: 12px;
    }}
    body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            padding: 16px; padding-top: max(16px, env(safe-area-inset-top)); min-height: 100dvh; }}
    header {{ display: flex; align-items: center; justify-content: space-between;
              margin-bottom: 20px; gap: 12px; }}
    h1 {{ font-size: 20px; font-weight: 700; }}
    .back {{ color: var(--accent); text-decoration: none; font-size: 14px; display: flex;
             align-items: center; gap: 4px; }}
    .back:active {{ opacity: 0.7; }}

    /* Upload zone */
    .upload-zone {{ border: 2px dashed var(--border); border-radius: var(--radius);
                    padding: 24px 16px; text-align: center; margin-bottom: 20px; cursor: pointer;
                    background: var(--surface); transition: border-color 0.2s, background 0.2s; }}
    .upload-zone.drag {{ border-color: var(--accent); background: #0f1a2e; }}
    .upload-zone input {{ display: none; }}
    .upload-zone p {{ color: var(--muted); font-size: 14px; margin-bottom: 12px; }}
    .upload-btn {{ background: var(--accent); color: #fff; border: none; border-radius: 8px;
                   padding: 10px 20px; font-size: 14px; font-weight: 600; cursor: pointer; }}
    .upload-btn:active {{ background: #2563eb; }}
    #upload-status {{ margin-top: 10px; font-size: 13px; color: var(--accent); min-height: 18px; }}

    /* Gallery grid */
    .gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; }}
    .card {{ border: 2px solid var(--border); border-radius: var(--radius);
             overflow: hidden; background: var(--surface); position: relative; }}
    .card.selected {{ border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }}
    .card-img {{ position: relative; width: 100%; padding-bottom: 60%; background: #000; overflow: hidden; }}
    .card-img img {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }}
    .card-footer {{ padding: 8px; display: flex; flex-direction: column; gap: 6px; }}
    .card-name {{ font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .card-actions {{ display: flex; gap: 5px; }}
    .set-btn {{ flex: 1; padding: 7px 4px; border-radius: 7px; font-size: 12px; font-weight: 500;
                border: 1px solid var(--border); background: var(--surface2); color: var(--text);
                cursor: pointer; text-align: center; }}
    .set-btn.active {{ background: #0f2a1f; color: var(--success); border-color: #1a4a30; cursor: default; }}
    .set-btn:not(.active):active {{ background: #2a2a2a; }}
    .del-btn {{ padding: 7px 10px; border-radius: 7px; font-size: 13px; border: 1px solid #3a1a1a;
                background: #1a0a0a; color: var(--danger); cursor: pointer; flex-shrink: 0; }}
    .del-btn:active {{ background: #2a1010; }}
    .empty {{ color: var(--muted); text-align: center; padding: 48px 0; font-size: 15px; }}

    /* Screensaver selection status + hint */
    .ss-status {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
                  padding: 12px 14px; font-size: 14px; font-weight: 600; margin-bottom: 8px; }}
    .ss-status .badge {{ color: var(--success); }}
    .ss-hint {{ color: var(--muted); font-size: 13px; margin-bottom: 16px; line-height: 1.4; }}
    .card .pick {{ position: absolute; top: 8px; left: 8px; width: 26px; height: 26px; border-radius: 50%;
                   background: rgba(0,0,0,0.55); border: 2px solid #fff; color: #fff; font-size: 15px;
                   display: flex; align-items: center; justify-content: center; }}
    .card.selected .pick {{ background: var(--accent); border-color: var(--accent); }}
  </style>
</head>
<body>
  <header>
    <a href="/" class="back">&#8592; Back</a>
    <h1>Gallery</h1>
    <span></span>
  </header>

  <div class="upload-zone" id="drop-zone" onclick="document.getElementById('file-in').click()">
    <input type="file" id="file-in" accept="image/*" multiple onchange="uploadFiles(this.files)">
    <p>Tap to choose photos — or drag &amp; drop</p>
    <button class="upload-btn" onclick="event.stopPropagation(); document.getElementById('file-in').click()">
      Choose Photos
    </button>
    <div id="upload-status"></div>
  </div>

  <div id="ss-status" class="ss-status"></div>
  <p class="ss-hint">Tap photos to choose what the screensaver shows. Pick <b>1</b> for a still
     image, or <b>2+</b> to cycle through them.</p>

  <div id="gallery" class="gallery"></div>

  <script>
    var _selected = "";
    var _cycle = [];

    function esc(s) {{ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }}

    function updateStatus() {{
      var s = document.getElementById("ss-status");
      var n = _cycle.length;
      if (n === 0) s.innerHTML = "No photos selected — showing the default screensaver.";
      else if (n === 1) s.innerHTML = "Showing <span class=\\"badge\\">1 photo</span> (still).";
      else s.innerHTML = "Cycling <span class=\\"badge\\">" + n + " photos</span>.";
    }}

    function loadGallery() {{
      fetch("/photos").then(function(r){{ return r.json(); }}).then(function(data) {{
        _selected = data.selected || "";
        _cycle = data.cycle || [];
        updateStatus();
        var g = document.getElementById("gallery");
        if (!data.photos || !data.photos.length) {{
          g.innerHTML = "<div class=\\"empty\\">No photos yet — upload one above.</div>";
          return;
        }}
        g.innerHTML = "";
        data.photos.forEach(function(name) {{
          var enc = encodeURIComponent(name);
          var isSel = (_cycle.indexOf(name) !== -1);
          var card = document.createElement("div");
          card.className = "card" + (isSel ? " selected" : "");
          card.innerHTML =
            "<div class=\\"card-img\\" onclick=\\"toggleSelect('" + enc + "')\\">" +
              "<img src=\\"/preview/" + enc + "?t=" + Date.now() + "\\" loading=\\"lazy\\">" +
              "<div class=\\"pick\\">" + (isSel ? "&#10003;" : "") + "</div>" +
            "</div>" +
            "<div class=\\"card-footer\\">" +
              "<div class=\\"card-name\\">" + esc(name) + "</div>" +
              "<div class=\\"card-actions\\">" +
                "<button class=\\"set-btn" + (isSel ? " active" : "") + "\\" onclick=\\"toggleSelect('" + enc + "')\\">" +
                  (isSel ? "&#10003; Selected" : "Select") +
                "</button>" +
                "<button class=\\"del-btn\\" onclick=\\"deletePhoto('" + enc + "')\\">&#128465;</button>" +
              "</div>" +
            "</div>";
          g.appendChild(card);
        }});
      }});
    }}

    function toggleSelect(enc) {{
      var name = decodeURIComponent(enc);
      var on = (_cycle.indexOf(name) === -1);
      fetch("/cycle", {{method:"POST", headers:{{"Content-Type":"application/json"}},
        body:JSON.stringify({{photo:name, on:on}})}})
        .then(function(r){{ return r.json(); }})
        .then(function(d){{ if(d.ok) {{ _cycle = d.cycle || []; loadGallery(); }} }});
    }}

    function deletePhoto(enc) {{
      if (!confirm("Delete " + decodeURIComponent(enc) + "?")) return;
      fetch("/photo/" + enc, {{method:"DELETE"}})
        .then(function(r){{ return r.json(); }})
        .then(function(d){{
          if (d.ok) loadGallery();
          else alert("Error: " + (d.error || "unknown"));
        }});
    }}

    function uploadFiles(files) {{
      if (!files || !files.length) return;
      var status = document.getElementById("upload-status");
      Array.from(files).forEach(function(file) {{
        status.textContent = "Uploading " + file.name + "…";
        var fd = new FormData();
        fd.append("photo", file);
        fetch("/upload", {{method:"POST", body:fd}})
          .then(function(r){{ return r.json(); }})
          .then(function(d){{
            if (d.ok) {{
              status.textContent = "Uploaded " + file.name + (d.auto_selected ? " — set active" : "");
              loadGallery();
            }} else {{
              status.textContent = "Error: " + (d.error || "unknown");
            }}
          }}).catch(function(e){{ status.textContent = "Upload failed: " + e; }});
      }});
    }}

    var dropZone = document.getElementById("drop-zone");
    dropZone.addEventListener("dragover", function(e){{ e.preventDefault(); dropZone.classList.add("drag"); }});
    dropZone.addEventListener("dragleave", function(){{ dropZone.classList.remove("drag"); }});
    dropZone.addEventListener("drop", function(e){{
      e.preventDefault(); dropZone.classList.remove("drag");
      uploadFiles(e.dataTransfer.files);
    }});

    loadGallery();
  </script>
</body>
</html>
'''


def _list_photos(photos_dir: str) -> list:
    if not os.path.isdir(photos_dir):
        return []
    names = []
    for f in sorted(os.listdir(photos_dir)):
        if f.startswith('.'):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in _ALLOWED_EXTS:
            names.append(f)
    return names


def _get_selected(photos_dir: str) -> str:
    sel_path = os.path.join(photos_dir, _SELECTION_FILE)
    try:
        name = open(sel_path).read().strip()
        if name and os.path.exists(os.path.join(photos_dir, name)):
            return name
    except Exception:
        pass
    photos = _list_photos(photos_dir)
    return photos[0] if photos else ''


def _set_selected(photos_dir: str, name: str):
    sel_path = os.path.join(photos_dir, _SELECTION_FILE)
    with open(sel_path, 'w') as f:
        f.write(name)


def get_screensaver_path(photos_dir: str) -> str:
    """Return absolute path to the currently selected screensaver photo."""
    name = _get_selected(photos_dir)
    if name:
        return os.path.join(photos_dir, name)
    return ''


_CYCLE_FILE = '.cycle'


def _get_cycle(photos_dir: str) -> list:
    """Names chosen for the cycle rotation (filtered to photos that still exist)."""
    try:
        with open(os.path.join(photos_dir, _CYCLE_FILE)) as f:
            names = json.load(f)
    except Exception:
        return []
    if not isinstance(names, list):
        return []
    existing = set(_list_photos(photos_dir))
    # Preserve saved order, drop any that were deleted.
    return [n for n in names if isinstance(n, str) and n in existing]


def _set_cycle(photos_dir: str, names: list):
    os.makedirs(photos_dir, exist_ok=True)
    with open(os.path.join(photos_dir, _CYCLE_FILE), 'w') as f:
        json.dump(list(names), f)


def get_screensaver_images(photos_dir: str, config: dict) -> tuple:
    """Resolve what the screensaver should display.

    Returns ``(names, is_cycle)`` where ``names`` is an ordered list of photo
    file names (basenames within photos_dir) and ``is_cycle`` says whether to
    rotate through them.

    Rotation set wins when set: 2+ chosen photos cycle, exactly 1 shows static.
    With no rotation set, fall back to the legacy ``screensaver_mode`` config
    (cycle = all photos, static = the single selected photo).
    """
    cycle = _get_cycle(photos_dir)
    if len(cycle) >= 2:
        return cycle, True
    if len(cycle) == 1:
        return [cycle[0]], False
    mode = config.get('screensaver_mode', 'static')
    photos = _list_photos(photos_dir)
    if mode == 'cycle' and len(photos) >= 2:
        return photos, True
    sel = _get_selected(photos_dir)
    return ([sel] if sel else []), False


def _load_clipboard_json(clipboard_path: str) -> list:
    try:
        with open(clipboard_path) as f:
            items = json.load(f)
        return [i for i in items if isinstance(i, dict) and 'text' in i][:20]
    except Exception:
        return []


def _save_clipboard_json(clipboard_path: str, items: list):
    os.makedirs(os.path.dirname(clipboard_path) or '.', exist_ok=True)
    with open(clipboard_path, 'w') as f:
        json.dump(items, f, indent=2)


_CLIPBOARD_HTML = '''\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Clipboard</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d0d0d; color: #e2e2e2; font-family: ui-monospace, monospace; padding: 16px; }}
    h1 {{ font-size: 18px; margin-bottom: 14px; color: #fff; }}
    .back {{ color: #3b82f6; text-decoration: none; display: inline-block; margin-bottom: 14px; font-size: 14px; }}
    .add-form {{ display: flex; flex-direction: column; gap: 8px; border: 1px solid #2a2a2a;
                 border-radius: 10px; padding: 14px; margin-bottom: 16px; background: #161616; }}
    .add-form label {{ font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }}
    .add-form input, .add-form textarea {{
      background: #1e1e1e; color: #eee; border: 1px solid #333; border-radius: 8px;
      padding: 10px 12px; font-family: inherit; font-size: 15px; width: 100%; outline: none; }}
    .add-form input:focus, .add-form textarea:focus {{ border-color: #3b82f6; }}
    .add-form textarea {{ resize: vertical; min-height: 60px; }}
    .add-btn {{ background: #3b82f6; color: #fff; border: none; border-radius: 8px;
               padding: 12px; cursor: pointer; font-family: inherit; font-size: 14px; font-weight: 600; }}
    .add-btn:active {{ background: #2563eb; }}
    .entry {{ display: flex; align-items: flex-start; gap: 10px; border: 1px solid #222;
              border-radius: 10px; padding: 12px; margin-bottom: 8px; background: #161616; }}
    .entry-body {{ flex: 1; min-width: 0; }}
    .entry-label {{ font-size: 14px; font-weight: 600; color: #fff; margin-bottom: 4px; }}
    .entry-text {{ font-size: 12px; color: #666; white-space: pre-wrap; word-break: break-all;
                   max-height: 50px; overflow: hidden; }}
    .del-btn {{ background: #2a0a0a; color: #ef4444; border: 1px solid #4a1a1a;
               border-radius: 8px; padding: 8px 12px; cursor: pointer;
               font-family: inherit; font-size: 12px; flex-shrink: 0; }}
    .del-btn:active {{ background: #3a1515; }}
    #status {{ font-size: 12px; color: #3b82f6; min-height: 18px; margin-top: 8px; }}
    .empty {{ color: #444; text-align: center; padding: 30px; }}
  </style>
</head>
<body>
  <a href="/" class="back">&#8592; Back</a>
  <h1>Clipboard</h1>
  <div class="add-form">
    <label for="lbl">Label</label>
    <input id="lbl" type="text" placeholder="e.g. git push">
    <label for="txt">Command / text</label>
    <textarea id="txt" placeholder="e.g. git push origin main"></textarea>
    <button class="add-btn" onclick="addEntry()">Add to clipboard</button>
  </div>
  <div id="status"></div>
  <div id="list"></div>
  <script>
    function load() {{
      fetch("/clipboard/list").then(function(r){{ return r.json(); }}).then(function(data) {{
        var el = document.getElementById("list");
        if (!data.length) {{ el.innerHTML = "<div class=\\"empty\\">No entries yet.</div>"; return; }}
        el.innerHTML = "";
        data.forEach(function(item, i) {{
          var d = document.createElement("div"); d.className = "entry";
          d.innerHTML = "<div class=\\"entry-body\\"><div class=\\"entry-label\\">" + esc(item.label||item.text) +
            "</div><div class=\\"entry-text\\">" + esc(item.text) + "</div></div>" +
            "<button class=\\"del-btn\\" onclick=\\"del(" + i + ")\\">Del</button>";
          el.appendChild(d);
        }});
      }});
    }}
    function esc(s) {{ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }}
    function addEntry() {{
      var lbl = document.getElementById("lbl").value.trim();
      var txt = document.getElementById("txt").value.trim();
      if (!txt) {{ setStatus("Text is required"); return; }}
      fetch("/clipboard/add",{{method:"POST",headers:{{"Content-Type":"application/json"}},
        body:JSON.stringify({{label:lbl||txt,text:txt}})}})
        .then(function(r){{ return r.json(); }}).then(function(d) {{
          if (d.ok) {{ document.getElementById("lbl").value=""; document.getElementById("txt").value=""; setStatus("Added"); load(); }}
          else setStatus("Error: "+(d.error||"unknown"));
        }});
    }}
    function del(i) {{
      fetch("/clipboard/"+i,{{method:"DELETE"}}).then(function(r){{ return r.json(); }})
        .then(function(d){{ if(d.ok){{setStatus("Deleted");load();}} else setStatus("Error"); }});
    }}
    function setStatus(msg) {{ var el=document.getElementById("status"); el.textContent=msg; setTimeout(function(){{el.textContent="";}},3000); }}
    load();
  </script>
</body>
</html>
'''


def _get_wifi_info() -> dict:
    """Return {'ssid': str, 'qr_url': str} for the current WiFi connection."""
    ssid = ''
    try:
        import subprocess as _sp
        r = _sp.run(['iwgetid', '-r'], capture_output=True, text=True, timeout=3)
        ssid = r.stdout.strip()
    except Exception:
        pass
    if not ssid:
        return {'ssid': '', 'qr_url': ''}
    # Build WIFI QR code URL (data URL)
    wifi_str = f'WIFI:T:WPA;S:{ssid};;'
    try:
        import io as _io
        import qrcode as _qr
        qr = _qr.QRCode(error_correction=_qr.constants.ERROR_CORRECT_L, box_size=5, border=2)
        qr.add_data(wifi_str)
        qr.make(fit=True)
        buf = _io.BytesIO()
        qr.make_image(fill_color='black', back_color='white').get_image().save(buf, format='PNG')
        import base64 as _b64
        qr_url = 'data:image/png;base64,' + _b64.b64encode(buf.getvalue()).decode()
        return {'ssid': ssid, 'qr_url': qr_url}
    except Exception:
        return {'ssid': ssid, 'qr_url': ''}


_BEAM_HTML = '''\
<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beamed screen</title><style>
body{margin:0;background:#0d0d0d;color:#e2e2e2;
font-family:-apple-system,BlinkMacSystemFont,sans-serif}
header{position:sticky;top:0;display:flex;gap:10px;align-items:center;
padding:12px 16px;background:#161616;border-bottom:1px solid #2a2a2a}
h1{flex:1;font-size:16px;margin:0}
button{background:#3b82f6;color:#fff;border:0;border-radius:8px;
padding:8px 14px;font-size:14px}
pre{margin:0;padding:16px;white-space:pre-wrap;word-break:break-word;
font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
</style></head><body><header><h1>Beamed screen</h1>
<button onclick="navigator.clipboard.writeText(document.getElementById('t').textContent)">Copy</button>
</header><pre id="t">__TEXT__</pre></body></html>'''


def _render_beam_page(text: str) -> str:
    """A minimal page showing the beamed terminal text with a copy button."""
    import html as _html
    return _BEAM_HTML.replace('__TEXT__', _html.escape(text))


def _make_handler(bmp_path: str, input_queue: queue.Queue,
                  activity_ref: List[float], photos_dir: str, config_path: str = '',
                  clipboard_path: str = '', display_queue: queue.Queue = None,
                  beam_ref: List[str] = None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            activity_ref[0] = time.time()
            path = self.path.split('?')[0]
            if path in ('/', '/live'):
                self._respond(200, 'text/html; charset=utf-8', _PAGE_HTML.encode())
            elif path in ('/display', '/snapshot'):
                self._serve_display()
            elif path == '/gallery':
                self._respond(200, 'text/html; charset=utf-8', _GALLERY_HTML.encode())
            elif path == '/photos':
                self._serve_photos_json()
            elif path.startswith('/photo/'):
                self._serve_photo(path[7:])
            elif path.startswith('/preview/'):
                self._serve_preview(path[9:])
            elif path == '/mode':
                mode = _get_startup_mode(config_path) if config_path else 'stats'
                self._respond(200, 'application/json', json.dumps({'mode': mode}).encode())
            elif path == '/config':
                self._serve_config()
            elif path == '/wifi-info':
                info = _get_wifi_info()
                self._respond(200, 'application/json', json.dumps(info).encode())
            elif path == '/clipboard':
                self._respond(200, 'text/html; charset=utf-8', _CLIPBOARD_HTML.encode())
            elif path == '/clipboard/list':
                items = _load_clipboard_json(clipboard_path) if clipboard_path else []
                self._respond(200, 'application/json', json.dumps(items).encode())
            elif path == '/beam':
                text = (beam_ref[0] if beam_ref else '') or '(nothing beamed yet)'
                self._respond(200, 'text/html; charset=utf-8',
                              _render_beam_page(text).encode())
            else:
                self._respond(404, 'text/plain', b'Not found')

        def do_POST(self):
            activity_ref[0] = time.time()
            path = self.path.split('?')[0]
            if path == '/send':
                self._handle_send()
            elif path == '/message':
                self._handle_message()
            elif path == '/action':
                self._handle_action()
            elif path == '/upload':
                self._handle_upload()
            elif path == '/select':
                self._handle_select()
            elif path == '/cycle':
                self._handle_cycle()
            elif path == '/mode':
                self._handle_mode()
            elif path == '/config':
                self._handle_config_post()
            elif path == '/clipboard/add':
                self._handle_clipboard_add()
            else:
                self._respond(404, 'text/plain', b'Not found')

        def do_DELETE(self):
            activity_ref[0] = time.time()
            path = self.path.split('?')[0]
            if path.startswith('/clipboard/'):
                self._handle_clipboard_delete(path[len('/clipboard/'):])
            elif path.startswith('/photo/'):
                self._handle_photo_delete(path[7:])
            else:
                self._respond(404, 'text/plain', b'Not found')

        # ── GET handlers ────────────────────────────────────────────────────

        def _serve_display(self):
            try:
                from PIL import Image
                img = Image.open(bmp_path)
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                self._respond(200, 'image/png', buf.getvalue())
            except FileNotFoundError:
                self._respond(503, 'text/plain', b'Display not yet rendered')
            except Exception as e:
                self._respond(500, 'text/plain', str(e).encode())

        def _serve_photos_json(self):
            photos = _list_photos(photos_dir)
            selected = _get_selected(photos_dir)
            cycle = _get_cycle(photos_dir)
            body = json.dumps({'photos': photos, 'selected': selected,
                               'cycle': cycle}).encode()
            self._respond(200, 'application/json', body)

        def _serve_photo(self, raw_name: str):
            name = os.path.basename(urllib.parse.unquote(raw_name))
            if not name or '..' in name:
                self._respond(400, 'text/plain', b'Bad name')
                return
            photo_path = os.path.join(photos_dir, name)
            if not os.path.exists(photo_path):
                self._respond(404, 'text/plain', b'Not found')
                return
            try:
                from PIL import Image
                img = Image.open(photo_path)
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=80)
                self._respond(200, 'image/jpeg', buf.getvalue())
            except Exception as e:
                self._respond(500, 'text/plain', str(e).encode())

        def _serve_preview(self, raw_name: str):
            """Serve photo cropped to 800×480 grayscale — exactly what shows on display."""
            name = os.path.basename(urllib.parse.unquote(raw_name))
            if not name or '..' in name:
                self._respond(400, 'text/plain', b'Bad name')
                return
            photo_path = os.path.join(photos_dir, name)
            if not os.path.exists(photo_path):
                self._respond(404, 'text/plain', b'Not found')
                return
            try:
                from PIL import Image
                img = Image.open(photo_path).convert('L')
                img = _crop_to_display(img)
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=85)
                self._respond(200, 'image/jpeg', buf.getvalue())
            except Exception as e:
                self._respond(500, 'text/plain', str(e).encode())

        # ── DELETE handlers ─────────────────────────────────────────────────

        def _handle_photo_delete(self, raw_name: str):
            name = os.path.basename(urllib.parse.unquote(raw_name))
            if not name or '..' in name:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Bad name"}')
                return
            photo_path = os.path.join(photos_dir, name)
            if not os.path.exists(photo_path):
                self._respond(404, 'application/json', b'{"ok":false,"error":"Not found"}')
                return
            try:
                os.remove(photo_path)
                # If this was the selected photo, clear the selection
                sel_path = os.path.join(photos_dir, _SELECTION_FILE)
                try:
                    if open(sel_path).read().strip() == name:
                        os.remove(sel_path)
                except Exception:
                    pass
                # Prune the cycle rotation: _get_cycle already drops the file we
                # just removed (and any other missing ones); persist the result.
                _set_cycle(photos_dir, _get_cycle(photos_dir))
                self._respond(200, 'application/json', b'{"ok":true}')
            except Exception as e:
                self._respond(500, 'application/json',
                              json.dumps({'ok': False, 'error': str(e)}).encode())

        # ── POST handlers ───────────────────────────────────────────────────

        def _handle_send(self):
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                text = json.loads(raw).get('text', '')
            except Exception:
                text = raw.decode('utf-8', errors='replace')
            if text:
                input_queue.put(text)
            self._respond(200, 'application/json', b'{"ok":true}')

        def _handle_upload(self):
            if not photos_dir:
                self._respond(500, 'application/json', b'{"ok":false,"error":"No gallery dir configured"}')
                return
            os.makedirs(photos_dir, exist_ok=True)
            content_type = self.headers.get('Content-Type', '')
            length = int(self.headers.get('Content-Length', 0))
            if length > 20 * 1024 * 1024:
                self._respond(413, 'application/json', b'{"ok":false,"error":"File too large (max 20 MB)"}')
                return
            body = self.rfile.read(length)
            try:
                raw_name, file_bytes = _parse_upload_field(body, content_type)
                safe_name = os.path.basename(raw_name).replace(' ', '_')
                ext = os.path.splitext(safe_name)[1].lower()
                if ext not in _ALLOWED_EXTS:
                    self._respond(400, 'application/json',
                                  json.dumps({'ok': False, 'error': 'Unsupported file type'}).encode())
                    return
                # Convert to grayscale JPEG and save at full resolution
                from PIL import Image
                img = Image.open(io.BytesIO(file_bytes)).convert('L')
                if ext not in ('.jpg', '.jpeg'):
                    safe_name = os.path.splitext(safe_name)[0] + '.jpg'
                dest = os.path.join(photos_dir, safe_name)
                img.save(dest, format='JPEG', quality=90)
                _set_selected(photos_dir, safe_name)
                # Add new uploads to the rotation so they show up automatically;
                # upload several and they cycle.
                cycle = _get_cycle(photos_dir)
                if safe_name not in cycle:
                    cycle.append(safe_name)
                    _set_cycle(photos_dir, cycle)
                self._respond(200, 'application/json',
                              json.dumps({'ok': True, 'name': safe_name, 'auto_selected': True}).encode())
            except KeyError:
                self._respond(400, 'application/json', b'{"ok":false,"error":"No photo field in form"}')
            except Exception as e:
                self._respond(500, 'application/json',
                              json.dumps({'ok': False, 'error': str(e)}).encode())

        def _handle_select(self):
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                name = json.loads(raw).get('photo', '')
            except Exception:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Bad JSON"}')
                return
            name = os.path.basename(name)
            if not name or not os.path.exists(os.path.join(photos_dir, name)):
                self._respond(404, 'application/json', b'{"ok":false,"error":"Photo not found"}')
                return
            _set_selected(photos_dir, name)
            self._respond(200, 'application/json', b'{"ok":true}')

        def _handle_cycle(self):
            """Add/remove a photo from the cycle rotation, or set the whole list.

            Body: {"photo": name, "on": true|false}  — toggle one, or
                  {"photos": [name, ...]}            — replace the set.
            Responds with the resulting cycle list.
            """
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except Exception:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Bad JSON"}')
                return
            existing = _list_photos(photos_dir)
            cycle = _get_cycle(photos_dir)
            if 'photos' in data and isinstance(data['photos'], list):
                cycle = [os.path.basename(n) for n in data['photos']
                         if os.path.basename(n) in existing]
            else:
                name = os.path.basename(data.get('photo', ''))
                if not name or name not in existing:
                    self._respond(404, 'application/json', b'{"ok":false,"error":"Photo not found"}')
                    return
                on = bool(data.get('on', name not in cycle))
                if on and name not in cycle:
                    cycle.append(name)
                elif not on and name in cycle:
                    cycle.remove(name)
            _set_cycle(photos_dir, cycle)
            self._respond(200, 'application/json',
                          json.dumps({'ok': True, 'cycle': cycle}).encode())

        def _handle_message(self):
            """POST /message — display text on the e-ink screen."""
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
                text  = data.get('text', '').strip()
                label = data.get('label', '').strip()
            except Exception:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Bad JSON"}')
                return
            if not text:
                self._respond(400, 'application/json', b'{"ok":false,"error":"text required"}')
                return
            if display_queue is not None:
                display_queue.put({'type': 'message', 'text': text, 'label': label})
            self._respond(200, 'application/json', b'{"ok":true}')

        def _handle_action(self):
            """POST /action — send a display command (screensaver, toggle_qr, force_refresh)."""
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                action = json.loads(raw).get('action', '')
            except Exception:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Bad JSON"}')
                return
            _VALID = {'screensaver', 'toggle_qr', 'force_refresh'}
            if action not in _VALID:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Unknown action"}')
                return
            if display_queue is not None:
                display_queue.put({'type': action})
            self._respond(200, 'application/json', b'{"ok":true}')

        def _handle_mode(self):
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                mode = json.loads(raw).get('mode', '')
            except Exception:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Bad JSON"}')
                return
            if mode not in ('stats', 'terminal'):
                self._respond(400, 'application/json', b'{"ok":false,"error":"Invalid mode"}')
                return
            if not config_path:
                self._respond(500, 'application/json', b'{"ok":false,"error":"No config path"}')
                return
            try:
                _set_startup_mode(config_path, mode)
                self._respond(200, 'application/json', b'{"ok":true}')
            except Exception as e:
                self._respond(500, 'application/json',
                              json.dumps({'ok': False, 'error': str(e)}).encode())

        # ── Config handlers ──────────────────────────────────────────────────

        def _serve_config(self):
            if not config_path:
                self._respond(500, 'text/plain', b'No config path configured')
                return
            try:
                import yaml
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
                html = _build_config_html(cfg)
                self._respond(200, 'text/html; charset=utf-8', html.encode())
            except Exception as e:
                self._respond(500, 'text/plain', str(e).encode())

        def _handle_config_post(self):
            if not config_path:
                self._respond(500, 'application/json', b'{"ok":false,"error":"No config path"}')
                return
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                updates = json.loads(raw)
            except Exception:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Bad JSON"}')
                return
            try:
                _save_config_values(config_path, updates)
                self._respond(200, 'application/json', b'{"ok":true}')
                if platform.system() == 'Linux':
                    subprocess.Popen(['sudo', 'systemctl', 'restart', 'eink-display'])
            except Exception as e:
                self._respond(500, 'application/json',
                              json.dumps({'ok': False, 'error': str(e)}).encode())

        # ── Clipboard handlers ───────────────────────────────────────────────

        def _handle_clipboard_add(self):
            if not clipboard_path:
                self._respond(500, 'application/json', b'{"ok":false,"error":"No clipboard path"}')
                return
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
                text  = data.get('text', '').strip()
                label = data.get('label', '').strip() or text
            except Exception:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Bad JSON"}')
                return
            if not text:
                self._respond(400, 'application/json', b'{"ok":false,"error":"text required"}')
                return
            items = _load_clipboard_json(clipboard_path)
            items.append({'label': label, 'text': text})
            _save_clipboard_json(clipboard_path, items[:20])
            self._respond(200, 'application/json', b'{"ok":true}')

        def _handle_clipboard_delete(self, raw_idx: str):
            if not clipboard_path:
                self._respond(500, 'application/json', b'{"ok":false,"error":"No clipboard path"}')
                return
            try:
                idx = int(raw_idx)
            except ValueError:
                self._respond(400, 'application/json', b'{"ok":false,"error":"Bad index"}')
                return
            items = _load_clipboard_json(clipboard_path)
            if idx < 0 or idx >= len(items):
                self._respond(404, 'application/json', b'{"ok":false,"error":"Out of range"}')
                return
            items.pop(idx)
            _save_clipboard_json(clipboard_path, items)
            self._respond(200, 'application/json', b'{"ok":true}')

        # ── Helpers ─────────────────────────────────────────────────────────

        def _respond(self, code, content_type, body):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(body))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


class PreviewServer:
    def __init__(self, port: int, bmp_path: str, photos_dir: str,
                 config_path: str = '', clipboard_path: str = ''):
        self._port = port
        self._bmp_path = bmp_path
        self._photos_dir = photos_dir
        self._config_path = config_path
        self._clipboard_path = clipboard_path
        self._server = None
        self.input_queue:   queue.Queue = queue.Queue()
        self.display_queue: queue.Queue = queue.Queue()
        self._activity_ref: List[float] = [time.time()]
        self._beam_ref:     List[str] = ['']   # latest "beam to phone" text

    def set_beam_text(self, text: str):
        """Store the text shown at /beam (called by the app on beam-to-phone)."""
        self._beam_ref[0] = text or ''

    @property
    def last_activity(self) -> float:
        """Epoch time of the most recent page visit or command submission."""
        return self._activity_ref[0]

    def start(self):
        handler = _make_handler(
            self._bmp_path, self.input_queue,
            self._activity_ref, self._photos_dir, self._config_path,
            clipboard_path=self._clipboard_path,
            display_queue=self.display_queue,
            beam_ref=self._beam_ref,
        )
        self._server = HTTPServer(('', self._port), handler)
        self._server.allow_reuse_address = True
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        logger.info('Preview server started on http://0.0.0.0:%d/', self._port)
        print(f'Preview: http://localhost:{self._port}/')


def start_if_enabled(config: dict, bmp_path: str, photos_dir: str = '',
                     config_path: str = '', clipboard_path: str = ''):
    """Start the preview server if enabled. Returns the PreviewServer or None."""
    if not config.get('preview_server_enabled', False):
        return None
    port = config.get('preview_server_port', 8080)
    try:
        server = PreviewServer(port, bmp_path, photos_dir, config_path, clipboard_path)
        server.start()
        return server
    except OSError as e:
        logger.debug('Preview server not started (port %d): %s', port, e)
        return None
