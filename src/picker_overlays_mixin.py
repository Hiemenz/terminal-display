"""EinkTerminal mixin: the SSH bookmarks / process-kill / service-manager /
power-menu overlays (F1, F3, F4, F5) — each follows the same
load-list -> toggle-open -> handle-key pattern."""
from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time

from terminal_state import _POWER_ITEMS

logger = logging.getLogger(__name__)


class PickerOverlaysMixin:
    """SSH picker, process kill, service manager, and power menu overlays."""

    def _load_ssh_bookmarks(self) -> tuple:
        bookmarks = self._config.get('terminal_ssh_bookmarks', [])
        strings, hosts = [], []
        for b in bookmarks:
            name = b.get('name', b.get('host', '?'))
            user = b.get('user', '')
            host = b.get('host', '')
            strings.append(f"  {name:<20}  {user+'@'+host if user else host}")
            hosts.append(b)
        return strings, hosts

    def _toggle_sshpick(self):
        if self._sshpick_active:
            self._sshpick_active = False
            self._render(); return
        items, hosts = self._load_ssh_bookmarks()
        if not items:
            self._new_tab(); return
        self._sshpick_items = items
        self._sshpick_hosts = hosts
        self._sshpick_idx = 0
        self._sshpick_active = True
        self._palette_active = self._clipboard_active = False
        self._prockill_active = self._svcmgr_active = self._power_active = False
        self._help_active = self._copy_active = False
        self._render()

    def _handle_sshpick_key(self, data: bytes) -> bytes:
        if not self._sshpick_active: return data
        if b'\x1b[A' in data:
            self._sshpick_idx = max(0, self._sshpick_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._sshpick_idx = min(len(self._sshpick_items) - 1, self._sshpick_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if self._sshpick_hosts:
                b = self._sshpick_hosts[self._sshpick_idx]
                parts = ['ssh']
                port = b.get('port', 22)
                if port and port != 22:
                    parts += ['-p', str(port)]
                user = b.get('user', '')
                host = b.get('host', '')
                parts.append(f"{user}@{host}" if user else host)
                self._sshpick_active = False
                self._new_tab(cmd=' '.join(parts))
            return b''
        if b'\x1b' in data:
            self._sshpick_active = False; self._render(); return b''
        return b''

    # ─── Process kill overlay (F3) ───────────────────────────────────────────

    def _load_process_list(self) -> tuple:
        import psutil
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda p: p.get('cpu_percent') or 0, reverse=True)
        strings, pids = [], []
        for p in procs[:15]:
            strings.append(f"{p.get('pid',0):>6}  {p.get('cpu_percent') or 0:>5.1f}%  {p.get('memory_percent') or 0:>4.1f}%  {(p.get('name') or '')[:22]}")
            pids.append(p.get('pid', 0))
        return strings, pids

    def _toggle_prockill(self):
        if self._prockill_active:
            self._prockill_active = False
        else:
            self._prockill_items, self._prockill_pids = self._load_process_list()
            self._prockill_idx = 0
            self._prockill_active = True
            self._palette_active = self._clipboard_active = False
            self._svcmgr_active = self._power_active = False
            self._help_active = self._copy_active = False
        self._render()

    def _handle_prockill_key(self, data: bytes) -> bytes:
        if not self._prockill_active:
            return data
        if b'\x1b[A' in data:
            self._prockill_idx = max(0, self._prockill_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._prockill_idx = min(len(self._prockill_items) - 1, self._prockill_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if self._prockill_pids:
                try:
                    import psutil
                    psutil.Process(self._prockill_pids[self._prockill_idx]).send_signal(signal.SIGTERM)
                except (ProcessLookupError, PermissionError, Exception):
                    pass
                self._prockill_items, self._prockill_pids = self._load_process_list()
                self._prockill_idx = min(self._prockill_idx, max(0, len(self._prockill_items) - 1))
                self._render()
            return b''
        if b'\x1b' in data:
            self._prockill_active = False; self._render(); return b''
        return b''

    # ─── Service manager overlay (F4) ────────────────────────────────────────

    def _load_service_list(self) -> tuple:
        names = self._config.get('terminal_services', [])
        strings = []
        for name in names:
            try:
                r = subprocess.run(['systemctl', 'is-active', name],
                                   capture_output=True, text=True, timeout=1)
                status = r.stdout.strip() or 'unknown'
            except Exception:
                status = 'unknown'
            strings.append(f"{'●' if status == 'active' else '○'}  {name:<30}  {status}")
        return strings, list(names)

    def _toggle_svcmgr(self):
        if self._svcmgr_active:
            self._svcmgr_active = False
        else:
            self._svcmgr_items, self._svcmgr_names = self._load_service_list()
            self._svcmgr_idx = 0
            self._svcmgr_active = True
            self._palette_active = self._clipboard_active = False
            self._prockill_active = self._power_active = False
            self._help_active = self._copy_active = False
        self._render()

    def _handle_svcmgr_key(self, data: bytes) -> bytes:
        if not self._svcmgr_active:
            return data
        if b'\x1b[A' in data:
            self._svcmgr_idx = max(0, self._svcmgr_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._svcmgr_idx = min(len(self._svcmgr_items) - 1, self._svcmgr_idx + 1)
            self._render(); return b''
        if data in (b'r', b'R'): self._svcmgr_action('restart'); return b''
        if data in (b's', b'S'): self._svcmgr_action('stop');    return b''
        if data in (b'a', b'A'): self._svcmgr_action('start');   return b''
        if b'\x1b' in data:
            self._svcmgr_active = False; self._render(); return b''
        return b''

    def _svcmgr_action(self, action: str):
        if not self._svcmgr_names:
            return
        name = self._svcmgr_names[self._svcmgr_idx]
        try:
            subprocess.Popen(['sudo', 'systemctl', action, name])
        except Exception as e:
            logger.warning('svcmgr %s %s: %s', action, name, e)
        def _reload():
            time.sleep(1.0)
            self._svcmgr_items, self._svcmgr_names = self._load_service_list()
            if self._svcmgr_active:
                self._render()
        threading.Thread(target=_reload, daemon=True).start()

    # ─── Power menu (F5) ─────────────────────────────────────────────────────

    def _toggle_power(self):
        if self._power_active:
            self._power_active = False
        else:
            self._power_active = True
            self._power_idx = 2  # Cancel default — safest
            self._palette_active = self._clipboard_active = False
            self._prockill_active = self._svcmgr_active = False
            self._help_active = self._copy_active = False
        self._render()

    def _handle_power_key(self, data: bytes) -> bytes:
        if not self._power_active:
            return data
        if b'\x1b[A' in data:
            self._power_idx = max(0, self._power_idx - 1)
            self._render(); return b''
        if b'\x1b[B' in data:
            self._power_idx = min(len(_POWER_ITEMS) - 1, self._power_idx + 1)
            self._render(); return b''
        if b'\r' in data or b'\n' in data:
            if self._power_idx == 0:
                subprocess.Popen(['sudo', 'shutdown', '-h', 'now'])
                self._running = False
            elif self._power_idx == 1:
                subprocess.Popen(['sudo', 'reboot'])
                self._running = False
            else:
                self._power_active = False
                self._render()
            return b''
        if b'\x1b' in data:
            self._power_active = False; self._render(); return b''
        return b''
