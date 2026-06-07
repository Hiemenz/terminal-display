"""
Outbound push notifications — ntfy or Pushover. Stdlib-only (urllib), so no
new dependencies. Sends happen on a daemon thread so the render loop never
blocks on the network, and failures are logged, never raised.

Config keys (all optional; provider defaults to 'none' = disabled):

  notify_provider:      none | ntfy | pushover
  notify_min_interval:  per-key rate limit in seconds (default 60)
  # ntfy
  ntfy_server:          base URL (default https://ntfy.sh)
  ntfy_topic:           topic name (required for ntfy)
  ntfy_token:           bearer token for protected topics (optional)
  # pushover
  pushover_token:       application token (required for pushover)
  pushover_user:        user/group key (required for pushover)

Usage:
    import notifier
    notifier.configure(config)
    notifier.notify('HIGH CPU', '92% on host', priority='high', key='cpu')
"""
import logging
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

# ntfy uses 1..5 (5 = max urgency); Pushover uses -2..2.
_NTFY_PRIORITY = {'low': '2', 'default': '3', 'high': '4', 'urgent': '5'}
_PUSHOVER_PRIORITY = {'low': '-1', 'default': '0', 'high': '1', 'urgent': '2'}

_lock = threading.Lock()
_config: dict = {}
_last_sent: dict = {}   # key -> monotonic timestamp of last send


def configure(config: dict):
    """Install the active config. Safe to call again on reload."""
    global _config
    with _lock:
        _config = config or {}


def enabled() -> bool:
    return str(_config.get('notify_provider', 'none')).lower() in ('ntfy', 'pushover')


def notify(title: str, message: str = '', priority: str = 'default',
           tags: str = '', key: str = None):
    """Queue a push notification (non-blocking).

    `key` rate-limits repeats: a given key won't resend within
    notify_min_interval seconds (use it for recurring alerts like 'cpu').
    """
    if not enabled():
        return
    now = time.monotonic()
    rate = float(_config.get('notify_min_interval', 60) or 0)
    dedupe_key = key or title
    with _lock:
        last = _last_sent.get(dedupe_key, 0.0)
        if rate > 0 and (now - last) < rate:
            return
        _last_sent[dedupe_key] = now
        cfg = dict(_config)
    threading.Thread(
        target=_send, args=(cfg, title, message, priority, tags),
        daemon=True,
    ).start()


def _send(cfg: dict, title: str, message: str, priority: str, tags: str):
    provider = str(cfg.get('notify_provider', 'none')).lower()
    try:
        if provider == 'ntfy':
            _send_ntfy(cfg, title, message, priority, tags)
        elif provider == 'pushover':
            _send_pushover(cfg, title, message, priority)
    except Exception as e:   # network/DNS/HTTP — never let it escape
        logger.warning('notify (%s) failed: %s', provider, e)


def _send_ntfy(cfg: dict, title: str, message: str, priority: str, tags: str):
    topic = cfg.get('ntfy_topic', '')
    if not topic:
        logger.debug('ntfy: no topic configured')
        return
    server = str(cfg.get('ntfy_server', 'https://ntfy.sh')).rstrip('/')
    url = f'{server}/{topic}'
    headers = {
        'Title': title,
        'Priority': _NTFY_PRIORITY.get(priority, '3'),
    }
    if tags:
        headers['Tags'] = tags
    token = cfg.get('ntfy_token', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(
        url, data=(message or title).encode('utf-8'),
        headers=headers, method='POST',
    )
    urllib.request.urlopen(req, timeout=8).close()


def _send_pushover(cfg: dict, title: str, message: str, priority: str):
    token = cfg.get('pushover_token', '')
    user = cfg.get('pushover_user', '')
    if not (token and user):
        logger.debug('pushover: token/user not configured')
        return
    import urllib.parse
    data = urllib.parse.urlencode({
        'token': token,
        'user': user,
        'title': title,
        'message': message or title,
        'priority': _PUSHOVER_PRIORITY.get(priority, '0'),
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.pushover.net/1/messages.json', data=data, method='POST',
    )
    urllib.request.urlopen(req, timeout=8).close()
