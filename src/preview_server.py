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
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List

logger = logging.getLogger(__name__)

_SELECTION_FILE = '.selected'
_DISPLAY_W, _DISPLAY_H = 800, 480

_ALLOWED_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}


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
    with open(config_path, 'w') as f:
        f.write(content)
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
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
  <title>E-ink Terminal</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #111; color: #ccc; font-family: monospace;
            display: flex; flex-direction: column; height: 100vh; }}
    #top-bar {{ display: flex; justify-content: flex-end; align-items: center;
                gap: 12px; padding: 6px 10px;
                background: #1a1a1a; border-bottom: 1px solid #2a2a2a; }}
    #top-bar a {{ color: #7cf; text-decoration: none; font-size: 13px; }}
    #mode-btn {{ background: #2a2a2a; color: #bbb; border: 1px solid #444;
                 border-radius: 4px; padding: 4px 10px; font-family: monospace;
                 font-size: 12px; cursor: pointer; }}
    #mode-btn:active {{ background: #444; }}
    #display-wrap {{ flex: 1; overflow: hidden; display: flex;
                     align-items: center; justify-content: center; padding: 8px; }}
    img {{ width: 100%; max-width: 800px; image-rendering: pixelated;
           border: 1px solid #333; }}
    #input-wrap {{ padding: 8px 8px 12px; background: #1a1a1a;
                   border-top: 1px solid #333; }}
    #inp {{ width: 100%; padding: 10px; background: #222; color: #eee;
            border: 1px solid #444; border-radius: 4px;
            font-family: monospace; font-size: 16px; }}
    #inp:focus {{ outline: none; border-color: #777; }}
    #btns {{ display: flex; gap: 6px; margin-top: 6px; }}
    button {{ flex: 1; padding: 10px 4px; background: #2a2a2a; color: #bbb;
              border: 1px solid #444; border-radius: 4px;
              font-family: monospace; font-size: 13px; cursor: pointer; }}
    button:active {{ background: #444; }}
    #status {{ font-size: 10px; color: #555; margin-top: 5px; text-align: center; }}
  </style>
</head>
<body>
  <div id="top-bar">
    <button id="mode-btn" onclick="toggleMode()">…</button>
    <a href="/gallery">&#128247; Gallery</a>
  </div>
  <div id="display-wrap">
    <img id="d" src="/display" alt="display">
  </div>
  <div id="input-wrap">
    <input id="inp" type="text" placeholder="type command…"
           autocomplete="off" autocorrect="off"
           autocapitalize="off" spellcheck="false">
    <div id="btns">
      <button onclick="send()">Send &#x23CE;</button>
      <button onclick="sendRaw('\\x03')">Ctrl+C</button>
      <button onclick="sendRaw('\\x04')">Ctrl+D</button>
      <button onclick="sendRaw('\\x1b')">Esc</button>
      <button onclick="sendRaw('\\x09')">Tab</button>
    </div>
    <div id="status">ready</div>
  </div>
  <script>
    var inp = document.getElementById("inp");
    var status = document.getElementById("status");

    inp.addEventListener("keydown", function(e) {{
      if (e.key === "Enter") {{ e.preventDefault(); send(); }}
    }});

    function send() {{
      var text = inp.value;
      if (!text) return;
      sendRaw(text + "\\n");
      inp.value = "";
    }}

    function sendRaw(text) {{
      fetch("/send", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{text: text}})
      }}).then(function(r) {{
        status.textContent = r.ok
          ? "sent ✓ " + new Date().toLocaleTimeString()
          : "server error";
      }}).catch(function(e) {{
        status.textContent = "error: " + e;
      }});
    }}

    function refreshDisplay() {{
      var img = document.getElementById("d");
      var next = new Image();
      next.onload = function() {{ img.src = next.src; }};
      next.src = "/display?t=" + Date.now();
    }}
    setInterval(refreshDisplay, 3000);
    inp.focus();

    var _currentMode = "";
    function loadMode() {{
      fetch("/mode").then(function(r) {{ return r.json(); }}).then(function(d) {{
        _currentMode = d.mode;
        var btn = document.getElementById("mode-btn");
        btn.textContent = d.mode === "stats" ? "Switch to terminal" : "Switch to stats";
      }}).catch(function() {{}});
    }}
    function toggleMode() {{
      var next = _currentMode === "stats" ? "terminal" : "stats";
      document.getElementById("mode-btn").textContent = "Switching…";
      fetch("/mode", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{mode: next}})
      }}).then(function(r) {{ return r.json(); }}).then(function(d) {{
        if (d.ok) {{ _currentMode = next; loadMode(); }}
        else {{ loadMode(); }}
      }}).catch(function() {{ loadMode(); }});
    }}
    loadMode();
  </script>
</body>
</html>
'''

_GALLERY_HTML = '''\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Screensaver Gallery</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #111; color: #ccc; font-family: sans-serif; padding: 16px; }}
    h1 {{ font-size: 20px; margin-bottom: 16px; }}
    .back {{ color: #7cf; text-decoration: none; display: inline-block; margin-bottom: 14px; font-size: 14px; }}
    .upload-zone {{ border: 2px dashed #444; border-radius: 8px; padding: 20px;
                    text-align: center; margin-bottom: 20px; cursor: pointer;
                    transition: border-color 0.2s; }}
    .upload-zone.drag {{ border-color: #7cf; background: #1a2a3a; }}
    .upload-zone input {{ display: none; }}
    .upload-zone p {{ color: #888; margin-bottom: 10px; font-size: 14px; }}
    .upload-btn {{ background: #1a3a5a; color: #7cf; border: 1px solid #2a5a8a;
                   border-radius: 4px; padding: 10px 20px; cursor: pointer; font-size: 14px; }}
    #upload-status {{ margin-top: 10px; font-size: 13px; color: #7cf; min-height: 18px; }}
    .gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }}
    .card {{ border: 2px solid #333; border-radius: 8px; overflow: hidden;
             background: #1a1a1a; cursor: pointer; }}
    .card.selected {{ border-color: #4af; box-shadow: 0 0 8px #4af4; }}
    /* 480/800 = 60% padding-bottom preserves exact display aspect ratio */
    .card-img {{ position: relative; width: 100%; padding-bottom: 60%; background: #000; overflow: hidden; }}
    .card-img img {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: cover; display: block; }}
    .card-footer {{ padding: 8px; }}
    .card-name {{ font-size: 11px; color: #888; white-space: nowrap; overflow: hidden;
                  text-overflow: ellipsis; margin-bottom: 5px; }}
    .set-btn {{ width: 100%; padding: 6px; background: #1a3a5a; color: #7cf;
                border: 1px solid #2a5a8a; border-radius: 3px; cursor: pointer; font-size: 12px; }}
    .set-btn.active {{ background: #0a2a0a; color: #4f4; border-color: #2a5a2a; cursor: default; }}
    .empty {{ color: #555; text-align: center; padding: 40px; }}
  </style>
</head>
<body>
  <a href="/" class="back">&#8592; Back to terminal</a>
  <h1>Screensaver Gallery</h1>

  <div class="upload-zone" id="drop-zone" onclick="document.getElementById('file-in').click()">
    <input type="file" id="file-in" accept="image/*" multiple onchange="uploadFiles(this.files)">
    <p>Tap to choose a photo — or drag &amp; drop</p>
    <button class="upload-btn" onclick="event.stopPropagation(); document.getElementById('file-in').click()">
      Choose Photo
    </button>
    <div id="upload-status"></div>
  </div>

  <div id="gallery" class="gallery"></div>

  <script>
    var selected = "";

    function loadGallery() {{
      fetch("/photos").then(function(r) {{ return r.json(); }}).then(function(data) {{
        selected = data.selected || "";
        var g = document.getElementById("gallery");
        if (!data.photos || !data.photos.length) {{
          g.innerHTML = "<div class=\\"empty\\">No photos yet — upload one above.</div>";
          return;
        }}
        g.innerHTML = "";
        data.photos.forEach(function(name) {{
          var card = document.createElement("div");
          card.className = "card" + (name === selected ? " selected" : "");
          var enc = encodeURIComponent(name);
          var isSel = name === selected;
          card.innerHTML =
            "<div class=\\"card-img\\"><img src=\\"/preview/" + enc + "?t=" + Date.now() + "\\" loading=\\"lazy\\"></div>" +
            "<div class=\\"card-footer\\">" +
              "<div class=\\"card-name\\">" + escHtml(name) + "</div>" +
              "<button class=\\"set-btn" + (isSel ? " active" : "") + "\\" " +
                "onclick=\\"selectPhoto('" + enc + "')\\">" +
                (isSel ? "&#10003; Current" : "Set as screensaver") +
              "</button>" +
            "</div>";
          g.appendChild(card);
        }});
      }});
    }}

    function escHtml(s) {{
      return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    }}

    function selectPhoto(encodedName) {{
      var name = decodeURIComponent(encodedName);
      fetch("/select", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{photo: name}})
      }}).then(function(r) {{ return r.json(); }}).then(function(d) {{
        if (d.ok) loadGallery();
      }});
    }}

    function uploadFiles(files) {{
      if (!files || !files.length) return;
      var status = document.getElementById("upload-status");
      var done = 0;
      Array.from(files).forEach(function(file) {{
        status.textContent = "Uploading " + file.name + "…";
        var fd = new FormData();
        fd.append("photo", file);
        fetch("/upload", {{method: "POST", body: fd}})
          .then(function(r) {{ return r.json(); }})
          .then(function(d) {{
            done++;
            if (d.ok) {{
              status.textContent = "Uploaded " + file.name + (d.auto_selected ? " — set as screensaver" : "");
              loadGallery();
            }} else {{
              status.textContent = "Error: " + (d.error || "unknown");
            }}
          }})
          .catch(function(e) {{ status.textContent = "Upload failed: " + e; }});
      }});
    }}

    var dropZone = document.getElementById("drop-zone");
    dropZone.addEventListener("dragover", function(e) {{
      e.preventDefault(); dropZone.classList.add("drag");
    }});
    dropZone.addEventListener("dragleave", function() {{
      dropZone.classList.remove("drag");
    }});
    dropZone.addEventListener("drop", function(e) {{
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


def _make_handler(bmp_path: str, input_queue: queue.Queue,
                  activity_ref: List[float], photos_dir: str, config_path: str = ''):
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
            else:
                self._respond(404, 'text/plain', b'Not found')

        def do_POST(self):
            activity_ref[0] = time.time()
            path = self.path.split('?')[0]
            if path == '/send':
                self._handle_send()
            elif path == '/upload':
                self._handle_upload()
            elif path == '/select':
                self._handle_select()
            elif path == '/mode':
                self._handle_mode()
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
            body = json.dumps({'photos': photos, 'selected': selected}).encode()
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
    def __init__(self, port: int, bmp_path: str, photos_dir: str, config_path: str = ''):
        self._port = port
        self._bmp_path = bmp_path
        self._photos_dir = photos_dir
        self._config_path = config_path
        self._server = None
        self.input_queue: queue.Queue = queue.Queue()
        self._activity_ref: List[float] = [time.time()]

    @property
    def last_activity(self) -> float:
        """Epoch time of the most recent page visit or command submission."""
        return self._activity_ref[0]

    def start(self):
        handler = _make_handler(
            self._bmp_path, self.input_queue,
            self._activity_ref, self._photos_dir, self._config_path,
        )
        self._server = HTTPServer(('', self._port), handler)
        self._server.allow_reuse_address = True
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        logger.info('Preview server started on http://0.0.0.0:%d/', self._port)
        print(f'Preview: http://localhost:{self._port}/')


def start_if_enabled(config: dict, bmp_path: str, photos_dir: str = '', config_path: str = ''):
    """Start the preview server if enabled. Returns the PreviewServer or None."""
    if not config.get('preview_server_enabled', False):
        return None
    port = config.get('preview_server_port', 8080)
    try:
        server = PreviewServer(port, bmp_path, photos_dir, config_path)
        server.start()
        return server
    except OSError as e:
        logger.debug('Preview server not started (port %d): %s', port, e)
        return None
