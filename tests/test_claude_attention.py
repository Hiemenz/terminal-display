"""Noticing when a Claude session in a tab has stopped for you.

The panel deep-sleeps after a few minutes with no *keyboard* input, which is
exactly what a long agent turn looks like — so the moment the session wants a
human is the moment the screen went to sleep showing something else. These
pin the two decisions that make that announcement trustworthy: what the pane
text means, and when it's worth interrupting for.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from claude_attention import (  # noqa: E402
    APPROVAL,
    WAITING,
    WORKING,
    AttentionWatcher,
    pane_state,
)


def test_the_busy_footer_means_working():
    assert pane_state('· Rummaging… (12s · esc to interrupt)') == WORKING


def test_an_approval_prompt_is_its_own_state():
    """It can't proceed without you, which is worth saying differently from
    'sitting at an empty composer'."""
    assert pane_state('Do you want to make this edit to render.py?\n 1. Yes') == APPROVAL
    assert pane_state('Do you trust the files in this folder?') == APPROVAL


def test_an_idle_composer_is_waiting():
    assert pane_state('╭─────────╮\n│ > ') == WAITING
    assert pane_state('') == WAITING


def test_busy_wins_over_a_prompt_still_on_screen():
    """An approval you already answered stays in the scrollback while the next
    step runs — the footer is the live signal, so it has to take precedence."""
    text = 'Do you want to make this edit?\n 1. Yes\n· Working… (esc to interrupt)'
    assert pane_state(text) == WORKING


def test_announced_when_a_long_turn_stops_for_you():
    w = AttentionWatcher(min_seconds=30)
    assert w.update('t1', WORKING, 'build', 0.0) is None
    message = w.update('t1', APPROVAL, 'build', 120.0)
    assert message is not None
    assert 'needs you' in message and 'build' in message


def test_waiting_and_approval_read_differently():
    w = AttentionWatcher(min_seconds=30)
    w.update('t1', WORKING, 'build', 0.0)
    assert 'is waiting' in w.update('t1', WAITING, 'build', 120.0)


def test_a_quick_turn_is_not_news():
    """Announcing four-second turns would train you to ignore the status bar."""
    w = AttentionWatcher(min_seconds=30)
    w.update('t1', WORKING, 'build', 0.0)
    assert w.update('t1', WAITING, 'build', 4.0) is None


def test_it_only_fires_on_the_transition():
    w = AttentionWatcher(min_seconds=30)
    w.update('t1', WORKING, 'build', 0.0)
    assert w.update('t1', WAITING, 'build', 120.0) is not None
    for t in (121.0, 200.0, 900.0):
        assert w.update('t1', WAITING, 'build', t) is None


def test_starting_work_is_never_announced():
    w = AttentionWatcher(min_seconds=30)
    w.update('t1', WAITING, 'build', 0.0)
    assert w.update('t1', WORKING, 'build', 120.0) is None


def test_a_closed_tab_is_forgotten():
    w = AttentionWatcher(min_seconds=30)
    w.update('t1', WORKING, 'build', 0.0)
    w.forget('t1')
    assert w.tracked() == {}
    # And a fresh tab reusing the key doesn't inherit the old clock.
    assert w.update('t1', WAITING, 'build', 120.0) is None


def test_panes_are_tracked_separately():
    """Two claude panes in one tmux session are two sessions — keyed by pane
    id, so one going quiet doesn't speak for the other."""
    w = AttentionWatcher(min_seconds=30)
    w.update('%1', WORKING, 'alpha', 0.0)
    w.update('%2', WORKING, 'beta', 0.0)
    assert w.update('%1', WAITING, 'alpha', 120.0) is not None
    assert w.update('%2', WORKING, 'beta', 120.0) is None


# ── condition() disambiguation ────────────────────────────────────────────────

def test_condition_single_waiting():
    w = AttentionWatcher(min_seconds=0)
    w.update('t1', WORKING, 'build', 0.0)
    w.update('t1', WAITING, 'build', 1.0)
    assert w.condition() == 'claude waiting (build)'


def test_condition_single_approval():
    w = AttentionWatcher(min_seconds=0)
    w.update('t1', WORKING, 'build', 0.0)
    w.update('t1', APPROVAL, 'build', 1.0)
    assert w.condition() == 'claude needs you (build)'


def test_condition_multiple_names_each_session():
    """With two pending sessions, both are named — no opaque count."""
    w = AttentionWatcher(min_seconds=0)
    w.update('%1', WORKING, 'alpha', 0.0)
    w.update('%2', WORKING, 'beta', 0.0)
    w.update('%1', WAITING, 'alpha', 1.0)
    w.update('%2', APPROVAL, 'beta', 1.0)
    cond = w.condition()
    assert 'alpha' in cond
    assert 'beta' in cond
    assert 'claude:' in cond


def test_condition_clears_when_answered():
    w = AttentionWatcher(min_seconds=0)
    w.update('t1', WORKING, 'build', 0.0)
    w.update('t1', WAITING, 'build', 1.0)
    assert w.condition() != ''
    w.update('t1', WORKING, 'build', 2.0)
    assert w.condition() == ''
