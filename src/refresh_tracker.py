"""Track full and partial e-ink refresh counts to avoid burn-in and ghosting."""
import json
import os
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(_REPO_ROOT, 'data', 'refresh_state.json')

FULL_REFRESH_INTERVAL    = 3600  # force full refresh after this many seconds
PARTIAL_REFRESH_BEFORE_FULL = 30  # default; overridden at runtime via config

# In-memory partial counter — avoids a disk read+write on every partial update.
# Loaded from disk on first call so the count survives a restart.
_partial_count: int = -1  # -1 = not yet loaded


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def needs_full_refresh():
    """Return True if 1+ hour elapsed since last full refresh, or first run."""
    state = _load_state()
    last = state.get('last_full_refresh')
    if last is None:
        return True
    return (time.time() - last) >= FULL_REFRESH_INTERVAL


def record_full_refresh():
    """Save current timestamp as last full refresh and reset partial counter."""
    global _partial_count
    _partial_count = 0
    state = _load_state()
    state['last_full_refresh'] = time.time()
    state['partial_count'] = 0
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)


def record_partial_refresh(threshold=PARTIAL_REFRESH_BEFORE_FULL):
    """Increment partial refresh counter.

    Returns True when the count hits `threshold`, signalling that a full
    refresh should be done now to clear accumulated ghosting.
    The counter is NOT reset here — call record_full_refresh() to reset it.

    The count is kept in memory and only written to disk when the threshold
    is hit (to avoid a disk read+write on every single partial update)."""
    global _partial_count
    if _partial_count < 0:
        # First call: load persisted count so a restart doesn't reset the counter.
        _partial_count = _load_state().get('partial_count', 0)
    _partial_count += 1
    return _partial_count >= threshold
