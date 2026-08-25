"""Notice when a Claude session in a local tab is waiting on you.

This device exists to have `claude` running on it and be glanced at from
across the room. `command_watch` already says when a long command finished,
but a Claude session never "finishes" — it stops, mid-task, needing an
approval or an answer, and nothing on the panel said so. Worse, the panel
deep-sleeps after a few minutes with no *keyboard* input, which is exactly
what a long agent turn looks like: the moment it needs you is the moment the
screen went to sleep showing something else.

The transcript on disk can answer this too (`claude_usage.session_state`),
but it only gains an entry when a turn completes, so it lags a live session
by however long the current step takes. A tmux pane's own text is immediate,
which is why this reads that instead — for tabs on this machine.

Pure bookkeeping over pane text, so it tests without tmux or a panel.
"""
from __future__ import annotations

from typing import Optional

# Claude Code prints this in its footer for as long as it is doing something.
# Its presence is the most reliable "busy" signal the pane offers.
BUSY_MARKERS = ('esc to interrupt',)
# ...and these are how it asks for a decision, as opposed to merely sitting at
# an empty composer. Worth separating: one of them can't proceed without you.
APPROVAL_MARKERS = ('do you want to', 'do you trust', 'would you like to')

WORKING = 'working'
APPROVAL = 'approval'
WAITING = 'waiting'


def pane_state(text: str) -> str:
    """Classify a `claude` pane's visible text: working / approval / waiting.

    Only ever called for panes already known to be running claude, so an
    empty screen means "at the composer", not "not claude".
    """
    if text is None:
        return ''
    low = text.lower()
    if any(marker in low for marker in BUSY_MARKERS):
        return WORKING
    if any(marker in low for marker in APPROVAL_MARKERS):
        return APPROVAL
    return WAITING


class AttentionWatcher:
    """Reports the moment a working session stops and wants a human.

    `min_seconds` keeps it quiet: a turn that resolves in four seconds was
    never something you walked away from, and announcing it would train you
    to ignore the status bar.
    """

    def __init__(self, min_seconds: float = 30.0):
        self.min_seconds = min_seconds
        # tab key → (last state, when it started)
        self._state: dict = {}

    def update(self, tab_key, state: str, label: str,
               now: float) -> Optional[str]:
        """Feed one tab's pane state. Returns a message worth showing, or None."""
        previous, since = self._state.get(tab_key, ('', now))
        if state == previous:
            return None
        self._state[tab_key] = (state, now)
        if previous != WORKING or state not in (WAITING, APPROVAL):
            return None
        if (now - since) < self.min_seconds:
            return None
        verb = 'needs you' if state == APPROVAL else 'is waiting'
        return 'claude %s (%s)' % (verb, label)

    def forget(self, tab_key) -> None:
        self._state.pop(tab_key, None)

    def tracked(self) -> dict:
        return dict(self._state)
