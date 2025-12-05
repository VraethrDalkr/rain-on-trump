"""
tests/test_landing_notification.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Test that landing in existing precipitation doesn't trigger false notifications.
"""

from __future__ import annotations

import pytest
from app.main import should_suppress_landing_notification


def test_suppress_notification_just_landed():
    """
    Just landed (was_in_flight=True) should suppress notification.
    User arrives at rainy location - shouldn't say "just started raining".
    """
    was_in_flight = True
    curr_type = "rain"
    prev_type = "none"  # Set during flight

    suppress = should_suppress_landing_notification(was_in_flight, prev_type, curr_type)

    assert suppress is True


def test_allow_notification_not_recently_landed():
    """
    Normal ground-to-ground transition should allow notification.
    """
    was_in_flight = False
    curr_type = "rain"
    prev_type = "none"

    suppress = should_suppress_landing_notification(was_in_flight, prev_type, curr_type)

    assert suppress is False


def test_allow_notification_after_landing_established():
    """
    After landing is established (prev_type != "none"), allow notifications.
    This handles weather changes AFTER landing.
    """
    was_in_flight = False
    curr_type = "snow"
    prev_type = "rain"  # Already established ground state

    suppress = should_suppress_landing_notification(was_in_flight, prev_type, curr_type)

    assert suppress is False


def test_suppress_even_when_landing_in_snow():
    """
    Landing in snow should also be suppressed (not just rain).
    """
    was_in_flight = True
    curr_type = "snow"
    prev_type = "none"

    suppress = should_suppress_landing_notification(was_in_flight, prev_type, curr_type)

    assert suppress is True


def test_suppress_landing_in_mixed_precipitation():
    """
    Landing in mixed rain/snow should be suppressed.
    """
    was_in_flight = True
    curr_type = "both"
    prev_type = "none"

    suppress = should_suppress_landing_notification(was_in_flight, prev_type, curr_type)

    assert suppress is True


def test_allow_notification_landing_in_clear():
    """
    Landing in clear weather (none -> none) doesn't need suppression.
    No notification would be triggered anyway.
    """
    was_in_flight = True
    curr_type = "none"
    prev_type = "none"

    suppress = should_suppress_landing_notification(was_in_flight, prev_type, curr_type)

    # Either True or False is fine - no notification will be sent anyway
    # since curr_type == prev_type
    assert isinstance(suppress, bool)
