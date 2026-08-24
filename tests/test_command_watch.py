"""Long-running commands announce themselves when they finish."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from command_watch import CommandWatcher, format_duration  # noqa: E402


def test_long_command_reports_when_it_finishes():
    w = CommandWatcher(min_seconds=30)
    assert w.update('tab1', 'make', now=0.0) is None
    assert w.update('tab1', 'make', now=60.0) is None      # still running
    assert w.update('tab1', 'bash', now=120.0) == ('make', 120.0)


def test_quick_command_is_not_news():
    w = CommandWatcher(min_seconds=30)
    w.update('tab1', 'ls', now=0.0)
    assert w.update('tab1', 'bash', now=2.0) is None


def test_sitting_at_a_prompt_reports_nothing():
    w = CommandWatcher(min_seconds=30)
    assert w.update('tab1', 'bash', now=0.0) is None
    assert w.update('tab1', 'bash', now=999.0) is None


def test_empty_command_counts_as_a_prompt():
    w = CommandWatcher(min_seconds=1)
    w.update('tab1', 'make', now=0.0)
    assert w.update('tab1', '', now=10.0) == ('make', 10.0)


def test_back_to_back_commands_still_report():
    """`make && ./run` never shows the shell in between."""
    w = CommandWatcher(min_seconds=30)
    w.update('tab1', 'make', now=0.0)
    assert w.update('tab1', 'run', now=100.0) == ('make', 100.0)
    assert w.update('tab1', 'bash', now=140.0) == ('run', 40.0)


def test_tabs_are_tracked_independently():
    w = CommandWatcher(min_seconds=30)
    w.update('tab1', 'make', now=0.0)
    w.update('tab2', 'claude', now=10.0)
    assert w.update('tab1', 'bash', now=100.0) == ('make', 100.0)
    assert w.update('tab2', 'bash', now=110.0) == ('claude', 100.0)


def test_a_closed_tab_is_forgotten():
    w = CommandWatcher(min_seconds=1)
    w.update('tab1', 'make', now=0.0)
    w.forget('tab1')
    assert w.update('tab1', 'bash', now=100.0) is None
    assert w.running() == {}


def test_duration_formatting():
    assert format_duration(9) == '9s'
    assert format_duration(75) == '1m15s'
    assert format_duration(3700) == '1h01m'
