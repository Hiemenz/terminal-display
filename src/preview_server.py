"""
Tiny HTTP preview server — serves the current e-ink display image over the LAN.

Endpoints:
  GET /          → auto-refreshing HTML page (refreshes every 3 s)
  GET /display   → current display as PNG
  GET /snapshot  → same as /display (alias)

Usage:
  from preview_server import start_if_enabled
  start_if_enabled(config, output_path)   # no-op if preview_server_enabled is false

Runs in a daemon thread; does not block the main process.
"""
import io
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)

_REFRESH_HTML = '''\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>E-ink Display</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #1a1a1a; display: flex;
           flex-direction: column; align-items: center;
           font-family: monospace; color: #ccc; }}
    h1 {{ font-size: 13px; margin: 8px 0 4px; letter-spacing: 1px; }}
    img {{ width: 100%; max-width: 800px; image-rendering: pixelated;
           border: 1px solid #444; }}
    p  {{ font-size: 11px; color: #666; margin: 4px 0 8px; }}
  </style>
</head>
<body>
  <h1>E-INK DISPLAY</h1>
  <img id="d" src="/display?t={ts}" alt="display">
  <p id="s">loading…</p>
  <script>
    var ts = 0;
    function reload() {{
      var img = document.getElementById("d");
      var now = Date.now();
      img.src = "/display?t=" + now;
      img.onload  = function() {{ document.getElementById("s").textContent =
        "updated " + new Date().toLocaleTimeString(); }};
      img.onerror = function() {{ document.getElementById("s").textContent =
        "waiting for display…"; }};
    }}
    reload();
    setInterval(reload, 3000);
  </script>
</body>
</html>
'''


def _make_handler(bmp_path: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?')[0]
            if path in ('/', '/live'):
                ts = int(time.time())
                body = _REFRESH_HTML.format(ts=ts).encode()
                self._respond(200, 'text/html; charset=utf-8', body)
            elif path in ('/display', '/snapshot'):
                self._serve_image()
            else:
                self._respond(404, 'text/plain', b'Not found')

        def _serve_image(self):
            try:
                from PIL import Image
                img = Image.open(bmp_path)
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                buf.seek(0)
                data = buf.read()
                self._respond(200, 'image/png', data)
            except FileNotFoundError:
                self._respond(503, 'text/plain', b'Display not yet rendered')
            except Exception as e:
                self._respond(500, 'text/plain', str(e).encode())

        def _respond(self, code, content_type, body):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(body))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # silence access logs

    return Handler


class PreviewServer:
    def __init__(self, port: int, bmp_path: str):
        self._port = port
        self._bmp_path = bmp_path
        self._server = None

    def start(self):
        handler = _make_handler(self._bmp_path)
        self._server = HTTPServer(('', self._port), handler)
        self._server.allow_reuse_address = True
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        logger.info('Preview server started on http://0.0.0.0:%d/', self._port)
        print(f'Preview: http://localhost:{self._port}/')


def start_if_enabled(config: dict, bmp_path: str) -> bool:
    """Start the preview server if preview_server_enabled is true. Returns True if started."""
    if not config.get('preview_server_enabled', False):
        return False
    port = config.get('preview_server_port', 8080)
    try:
        server = PreviewServer(port, bmp_path)
        server.start()
        return True
    except OSError as e:
        # Port already in use — another mode's server is still running; skip
        logger.debug('Preview server not started (port %d): %s', port, e)
        return False
