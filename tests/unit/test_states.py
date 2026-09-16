"""Tests for the purchase state machine.

The tests named ``test_..._is_unreachable`` encode the safety properties of
the design. If one of them starts failing, the program has become capable of
placing a duplicate order.
"""

from __future__ import annotations

import re

import pytest

from app.database.migrations import m0001_initial
from app.purchasing.states import (
    ALLOWED_TRANSITIONS,
    LIVE_STATES,
    SUBMIT_ENTRY_STATES,
    TERMINAL_STATES,
    IllegalTransition,
    PurchaseState,
    assert_transition,
    can_transition,
    reachable_from,
)


class TestTable:
    def test_every_state_has_an_entry(self) -> None:
        for state in PurchaseState:
            assert state in ALLOWED_TRANSITIONS, state

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        for state in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[state] == frozenset(), state

    def test_no_state_transitions_to_itself(self) -> None:
        for state, targets in ALLOWED_TRANSITIONS.items():
            assert state not in targets, state

    def test_live_and_terminal_partition_all_states(self) -> None:
        # NEEDS_USER/UNKNOWN are live (they hold the product slot); the four
        # terminal states are not. Together they must cover everything.
        assert LIVE_STATES | TERMINAL_STATES == set(PurchaseState)
        assert not (LIVE_STATES & TERMINAL_STATES)

    def test_happy_path_is_walkable(self) -> None:
        path = [
            PurchaseState.CREATED,
            PurchaseState.PRODUCT_CHECK,
            PurchaseState.RULE_VALIDATION,
            PurchaseState.CART_PREPARATION,
            PurchaseState.CHECKOUT,
            PurchaseState.FINAL_VALIDATION,
            PurchaseState.AWAITING_CONFIRMATION,
            PurchaseState.SUBMITTING,
            PurchaseState.CONFIRMING,
            PurchaseState.CONFIRMED,
        ]
        for current, following in zip(path, path[1:], strict=False):
            assert can_transition(current, following), f"{current} -> {following}"

    def test_automatic_mode_skips_confirmation(self) -> None:
        assert can_transition(PurchaseState.FINAL_VALIDATION, PurchaseState.SUBMITTING)

    def test_every_state_has_a_label_and_progress_text(self) -> None:
        for state in PurchaseState:
            assert state.label
            assert state.progress_text
            # The UI must never surface a raw state identifier.
            assert "_" not in state.label

    def test_assert_transition_raises_with_a_helpful_message(self) -> None:
        with pytest.raises(IllegalTransition) as excinfo:
            assert_transition(PurchaseState.CREATED, PurchaseState.SUBMITTING)
        message = str(excinfo.value)
        assert "created" in message and "submitting" in message


class TestSubmissionSafety:
    def test_submitting_is_reachable_only_from_two_states(self) -> None:
        sources = {
            state
            for state, targets in ALLOWED_TRANSITIONS.items()
            if PurchaseState.SUBMITTING in targets
        }
        assert sources == SUBMIT_ENTRY_STATES

    def test_resubmission_from_unknown_is_unreachable(self) -> None:
        """The single most important property in the program."""
        assert not can_transition(PurchaseState.UNKNOWN, PurchaseState.SUBMITTING)
        assert PurchaseState.SUBMITTING not in reachable_from(PurchaseState.UNKNOWN)

    def test_unknown_resolves_only_to_confirmed_or_failed(self) -> None:
        assert ALLOWED_TRANSITIONS[PurchaseState.UNKNOWN] == frozenset(
            {PurchaseState.CONFIRMED, PurchaseState.FAILED}
        )

    def test_resubmission_after_confirming_is_unreachable(self) -> None:
        assert PurchaseState.SUBMITTING not in reachable_from(PurchaseState.CONFIRMING)

    def test_resubmission_after_submitting_is_unreachable(self) -> None:
        """Once submitted, no path leads back to submitting again."""
        assert PurchaseState.SUBMITTING not in reachable_from(PurchaseState.SUBMITTING)

    def test_needs_user_cannot_jump_straight_to_submitting(self) -> None:
        assert not can_transition(PurchaseState.NEEDS_USER, PurchaseState.SUBMITTING)

    def test_submitting_cannot_divert_to_needs_user(self) -> None:
        """An order may exist, so 'needs attention' is not an honest outcome.

        The correct destination is UNKNOWN, which blocks any retry.
        """
        assert not can_transition(PurchaseState.SUBMITTING, PurchaseState.NEEDS_USER)
        assert not can_transition(PurchaseState.SUBMITTING, PurchaseState.CANCELLED)
        assert not can_transition(PurchaseState.SUBMITTING, PurchaseState.BLOCKED)

    def test_confirmed_is_final(self) -> None:
        assert not reachable_from(PurchaseState.CONFIRMED)

    def test_order_may_exist_flag_is_set_from_submitting_onwards(self) -> None:
        assert PurchaseState.SUBMITTING.order_may_exist
        assert PurchaseState.CONFIRMING.order_may_exist
        assert PurchaseState.CONFIRMED.order_may_exist
        assert PurchaseState.UNKNOWN.order_may_exist
        for state in (
            PurchaseState.CREATED,
            PurchaseState.PRODUCT_CHECK,
            PurchaseState.RULE_VALIDATION,
            PurchaseState.CART_PREPARATION,
            PurchaseState.CHECKOUT,
            PurchaseState.FINAL_VALIDATION,
            PurchaseState.AWAITING_CONFIRMATION,
            PurchaseState.BLOCKED,
            PurchaseState.CANCELLED,
        ):
            assert not state.order_may_exist, state

    def test_blocked_is_reachable_from_every_pre_submit_state(self) -> None:
        for state in (
            PurchaseState.CREATED,
            PurchaseState.PRODUCT_CHECK,
            PurchaseState.RULE_VALIDATION,
            PurchaseState.CART_PREPARATION,
            PurchaseState.CHECKOUT,
            PurchaseState.FINAL_VALIDATION,
            PurchaseState.AWAITING_CONFIRMATION,
        ):
            assert can_transition(state, PurchaseState.BLOCKED), state


class TestSchemaAgreement:
    """The Python state sets must agree with the SQL that enforces them."""

    def test_state_check_constraint_matches_the_enum(self) -> None:
        match = re.search(
            r"CHECK \(state IN \((?P<body>.*?)\)\)",
            m0001_initial.SQL,
            re.DOTALL,
        )
        assert match, "purchase_jobs state CHECK constraint not found"
        sql_states = set(re.findall(r"'([a-z_]+)'", match.group("body")))
        assert sql_states == {state.value for state in PurchaseState}

    def test_inflight_index_matches_live_states(self) -> None:
        match = re.search(
            r"CREATE UNIQUE INDEX idx_purchase_jobs_single_inflight_product.*?"
            r"WHERE state IN \((?P<body>.*?)\);",
            m0001_initial.SQL,
            re.DOTALL,
        )
        assert match, "in-flight partial index not found"
        sql_states = set(re.findall(r"'([a-z_]+)'", match.group("body")))
        assert sql_states == {state.value for state in LIVE_STATES}
