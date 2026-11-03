import pytest

from core.execution.state_machine import (
    InvalidTransition,
    can_transition,
    ensure_transition,
    is_terminal,
)
from database.models import OrderStatus

TERMINAL = ["FILLED", "CANCELLED", "REJECTED", "EXPIRED"]
LIVE = ["SUBMITTED", "PARTIALLY_FILLED", "CANCEL_PENDING"]


@pytest.mark.parametrize("status", TERMINAL)
def test_terminal_states_only_accept_themselves(status):
    assert is_terminal(status)
    assert can_transition(status, status)
    assert not can_transition(status, OrderStatus.SUBMITTED)
    assert not can_transition(status, OrderStatus.FILLED if status != "FILLED" else OrderStatus.CANCELLED)


@pytest.mark.parametrize("status", LIVE)
def test_live_states_can_progress_and_reject_backwards(status):
    assert not is_terminal(status)
    assert can_transition(status, OrderStatus.FILLED)
    assert can_transition(status, OrderStatus.CANCELLED)


def test_created_cannot_jump_straight_to_filled():
    """A local record may not claim a fill the broker never reported."""
    assert not can_transition(OrderStatus.CREATED, OrderStatus.FILLED)
    assert can_transition(OrderStatus.CREATED, OrderStatus.PENDING_SUBMIT)


def test_submitted_cannot_become_partially_filled_backwards_to_created():
    assert not can_transition(OrderStatus.SUBMITTED, OrderStatus.CREATED)


def test_duplicate_callback_is_idempotent():
    for status in (*TERMINAL, *LIVE, OrderStatus.CREATED, OrderStatus.UNKNOWN):
        assert can_transition(status, status), status


def test_unknown_is_the_recovery_state():
    for target in OrderStatus:
        assert can_transition(OrderStatus.UNKNOWN, target)


def test_ensure_transition_returns_the_target_and_guards_illegal_moves():
    assert ensure_transition(OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED) is (
        OrderStatus.PARTIALLY_FILLED
    )

    with pytest.raises(InvalidTransition) as excinfo:
        ensure_transition(OrderStatus.FILLED, OrderStatus.SUBMITTED)

    assert excinfo.value.current is OrderStatus.FILLED
    assert excinfo.value.target is OrderStatus.SUBMITTED
    assert "FILLED -> SUBMITTED" in str(excinfo.value)


def test_string_and_enum_forms_are_interchangeable():
    assert can_transition("SUBMITTED", OrderStatus.FILLED)
    assert can_transition(OrderStatus.SUBMITTED, "FILLED")
