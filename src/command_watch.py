"""Notice when a long-running command finishes.

This device is something you glance at: you start a build, or leave `claude`
working, and look at the panel from across the room. The status bar already
flags *unseen output* in a background tab, but not the thing you actually
want to know — that the long thing you were waiting on is done, and how long
it took.

Pure bookkeeping over foreground-process names, so it can be tested without
tmux, a PTY, or a panel: feed it what each tab is running and it tells you
which tabs just went back to a prompt after a while.
"""
from __future__ import annotations

from typing import Optional, Tuple

# Names that mean "sitting at a prompt", not "running something".
SHELL_COMMANDS = {'bash', 'zsh', 'sh', 'dash', 'fish', 'tmux', 'login'}


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return '%ds' % seconds
    if seconds < 3600:
        return '%dm%02ds' % (seconds // 60, seconds % 60)
    return '%dh%02dm' % (seconds // 3600, (seconds % 3600) // 60)


class CommandWatcher:
    """Tracks what each tab is running and reports long commands finishing.

    `min_seconds` keeps the noise out: an `ls` that finishes instantly is not
    news, and neither is the shell you just typed into.
    """

    def __init__(self, min_seconds: float = 30.0):
        self.min_seconds = min_seconds
        # tab key → (command name, when it started)
        self._running: dict = {}

    def update(self, tab_key, command: str,
               now: float) -> Optional[Tuple[str, float]]:
        """Feed one tab's current foreground command.

        Returns ('name', duration) when a command that ran at least
        `min_seconds` has just finished, else None.
        """
        command = (command or '').strip()
        at_prompt = not command or command in SHELL_COMMANDS
        previous = self._running.get(tab_key)

        if at_prompt:
            if previous is None:
                return None
            name, started = previous
            del self._running[tab_key]
            duration = now - started
            if duration >= self.min_seconds:
                return (name, duration)
            return None

        if previous is None or previous[0] != command:
            # A new command, or one command replacing another without the
            # shell showing up in between (`make && ./run`).
            finished = None
            if previous is not None:
                name, started = previous
                duration = now - started
                if duration >= self.min_seconds:
                    finished = (name, duration)
            self._running[tab_key] = (command, now)
            return finished
        return None

    def forget(self, tab_key) -> None:
        """Drop a closed tab, so its key can't leak or fire later."""
        self._running.pop(tab_key, None)

    def running(self) -> dict:
        return dict(self._running)
