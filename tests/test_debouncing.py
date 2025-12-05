"""
tests/test_debouncing.py
~~~~~~~~~~~~~~~~~~~~~~~~
Test precipitation state debouncing/hysteresis logic.
"""

from __future__ import annotations

import pytest
from app.main import should_notify_state_change


def test_should_notify_no_history():
    """
    First check (no history) should NOT notify.
    Need at least 2 observations to confirm state is stable.
    """
    history = []
    prev_notified = None
    curr_state = "rain"

    should_notify, new_history = should_notify_state_change(
        history, prev_notified, curr_state
    )

    assert should_notify is False
    assert new_history == ["rain"]


def test_should_notify_stable_new_state():
    """
    State is stable for 2 checks AND different from last notified → notify.
    Example: none → rain → rain (should notify on second rain)
    """
    history = ["rain"]
    prev_notified = "none"
    curr_state = "rain"

    should_notify, new_history = should_notify_state_change(
        history, prev_notified, curr_state
    )

    assert should_notify is True
    assert new_history == ["rain", "rain"]


def test_should_not_notify_unstable_state():
    """
    State is flapping → do NOT notify.
    Example: rain → none → rain (unstable, don't notify)
    """
    history = ["none"]
    prev_notified = "rain"
    curr_state = "rain"

    should_notify, new_history = should_notify_state_change(
        history, prev_notified, curr_state
    )

    assert should_notify is False  # Not stable yet
    assert new_history == ["none", "rain"]


def test_should_not_notify_same_as_last_notified():
    """
    State is stable but same as last notified → do NOT notify.
    Prevents duplicate notifications.
    """
    history = ["rain", "rain"]
    prev_notified = "rain"
    curr_state = "rain"

    should_notify, new_history = should_notify_state_change(
        history, prev_notified, curr_state
    )

    assert should_notify is False
    assert new_history == ["rain", "rain"]


def test_should_notify_stable_different_state():
    """
    State changed from rain → snow and is stable for 2 checks → notify.
    """
    history = ["snow"]
    prev_notified = "rain"
    curr_state = "snow"

    should_notify, new_history = should_notify_state_change(
        history, prev_notified, curr_state
    )

    assert should_notify is True
    assert new_history == ["snow", "snow"]


def test_history_limited_to_two():
    """
    History should be limited to last 2 states (sliding window).
    """
    history = ["rain", "rain"]
    prev_notified = "rain"
    curr_state = "snow"

    should_notify, new_history = should_notify_state_change(
        history, prev_notified, curr_state
    )

    assert should_notify is False  # Not stable yet (only 1 snow)
    assert new_history == ["rain", "snow"]  # Oldest "rain" dropped
    assert len(new_history) == 2


def test_flapping_scenario_full_cycle():
    """
    Full scenario: weather flaps between rain/none several times.
    Should only notify when state is stable.
    """
    prev_notified = "none"
    history = []

    # Check 1: rain (first observation, no notify)
    should_notify, history = should_notify_state_change(history, prev_notified, "rain")
    assert should_notify is False
    assert history == ["rain"]

    # Check 2: rain (stable, notify!)
    should_notify, history = should_notify_state_change(history, prev_notified, "rain")
    assert should_notify is True
    assert history == ["rain", "rain"]
    prev_notified = "rain"  # Update after notification

    # Check 3: none (unstable, no notify)
    should_notify, history = should_notify_state_change(history, prev_notified, "none")
    assert should_notify is False
    assert history == ["rain", "none"]

    # Check 4: rain (back to rain, unstable, no notify)
    should_notify, history = should_notify_state_change(history, prev_notified, "rain")
    assert should_notify is False
    assert history == ["none", "rain"]

    # Check 5: none (flapping continues, no notify)
    should_notify, history = should_notify_state_change(history, prev_notified, "none")
    assert should_notify is False
    assert history == ["rain", "none"]

    # Check 6: none (stable at none, notify!)
    should_notify, history = should_notify_state_change(history, prev_notified, "none")
    assert should_notify is True
    assert history == ["none", "none"]
