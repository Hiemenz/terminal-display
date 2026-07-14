"""Rotating, ANSI-stripped session logs for terminal tabs.

Persists each tab's shell output to disk as plain text (escape sequences
stripped) so scrollback survives idle-reset / shell-exit and can be grepped
later. Off by default — see terminal_log_* in config.yaml.
"""
import os
import re
import time

# Matches a single ANSI escape sequence: CSI (`\x1b[...<final>`), OSC
# (`\x1b]...` terminated by BEL or ST), or a bare two-byte escape.
_CSI_OSC_RE = re.compile(
    rb'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])'
)


class _AnsiStripper:
    """Strips ANSI escapes from a byte stream fed in chunks.

    A chunk boundary can land mid-escape-sequence (PTY reads are 4096 bytes
    at a time); holding back a trailing partial sequence until the next
    feed() call keeps stray escape fragments out of the log.
    """

    def __init__(self):
        self._carry = b''

    def feed(self, chunk: bytes) -> bytes:
        data = self._carry + chunk
        self._carry = b''
        last_esc = data.rfind(b'\x1b')
        if last_esc != -1 and len(data) - last_esc < 128:
            tail = data[last_esc:]
            # match() (not fullmatch) — a complete escape followed by plain
            # text is fine to flush; only an escape that can't complete at
            # all within the tail needs to wait for more bytes.
            if _CSI_OSC_RE.match(tail) is None:
                self._carry = tail
                data = data[:last_esc]
        return _CSI_OSC_RE.sub(b'', data)


def _safe_label(label: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_-]+', '_', label).strip('_')
    return (safe or 'tab')[:40]


class TabLogger:
    """Appends one tab's de-escaped output to a rotating log file.

    Rotates to a fresh timestamped file once the active file passes
    max_bytes, and prunes the oldest *.log files across log_dir (shared by
    all tabs) down to max_files.
    """

    def __init__(self, log_dir: str, label: str, max_bytes: int = 1_000_000,
                 max_files: int = 40):
        self._dir = log_dir
        self._label = _safe_label(label)
        self._max_bytes = max(1, int(max_bytes))
        self._max_files = max(1, int(max_files))
        self._stripper = _AnsiStripper()
        self._fh = None
        self._size = 0
        self._file_seq = 0
        try:
            os.makedirs(self._dir, exist_ok=True)
        except OSError:
            return
        self._open_new_file()

    def _open_new_file(self):
        # Timestamp is only second-resolution; a burst of output can rotate
        # more than once per second, so a counter suffix guarantees each
        # rotation gets its own file instead of silently re-appending to
        # the previous one via a filename collision.
        self._file_seq += 1
        ts = time.strftime('%Y%m%d-%H%M%S')
        path = os.path.join(self._dir, f'{self._label}_{ts}_{self._file_seq:03d}.log')
        try:
            self._fh = open(path, 'ab', buffering=0)
        except OSError:
            self._fh = None
        self._size = 0
        self._prune()

    def write(self, chunk: bytes):
        if not chunk or self._fh is None:
            return
        text = self._stripper.feed(chunk)
        if not text:
            return
        try:
            self._fh.write(text)
        except OSError:
            return
        self._size += len(text)
        if self._size >= self._max_bytes:
            self.close()
            self._open_new_file()

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _prune(self):
        try:
            names = [f for f in os.listdir(self._dir) if f.endswith('.log')]
            files = sorted(
                (os.path.join(self._dir, f) for f in names),
                key=lambda p: os.path.getmtime(p),
            )
        except OSError:
            return
        excess = len(files) - self._max_files
        for path in files[:max(0, excess)]:
            try:
                os.remove(path)
            except OSError:
                pass
