"""The purchase state machine.

A purchase is modelled as an explicit state machine rather than a long
procedural function, so that:

* every legal move is declared in one table and can be reviewed;
* an illegal move raises instead of silently proceeding;
* the position survives a crash, because it is persisted on every change;
* the single most dangerous transition in the program -- resubmitting an
  order whose outcome is unknown -- is structurally impossible.

The happy path::

    CREATED
      -> PRODUCT_CHECK
      -> RULE_VALIDATION
      -> CART_PREPARATION
      -> CHECKOUT
      -> FINAL_VALIDATION
      -> AWAITING_CONFIRMATION     (assisted mode; skipped when automatic)
      -> SUBMITTING
      -> CONFIRMING
      -> CONFIRMED

Any live state may divert to ``BLOCKED``, ``FAILED``, ``NEEDS_USER``,
``CANCELLED`` or ``UNKNOWN``.

The two rules that matter most
------------------------------

``SUBMITTING`` is reachable **only** from ``AWAITING_CONFIRMATION`` (a
deliberate human action) or ``FINAL_VALIDATION`` (automatic mode, after the
guard has passed). Nothing else can start a submission.

``UNKNOWN`` -- "the order was submitted but we could not read the outcome" --
may transition only to ``CONFIRMED`` or ``FAILED``, and only when a human has
said which it was. It can never return to ``SUBMITTING``, so the program
cannot double-order after an ambiguous result.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Mapping


class PurchaseState(StrEnum):
    """Position of a purchase job. Values are persisted, so never rename."""

    CREATED = "created"
    PRODUCT_CHECK = "product_check"
    RULE_VALIDATION = "rule_validation"
    CART_PREPARATION = "cart_preparation"
    CHECKOUT = "checkout"
    FINAL_VALIDATION = "final_validation"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SUBMITTING = "submitting"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    BLOCKED = "blocked"
    FAILED = "failed"
    NEEDS_USER = "needs_user"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        """Plain-English status for the UI. No jargon, no state names."""
        return _LABELS[self]

    @property
    def progress_text(self) -> str:
        """Present-tense description shown while the step runs."""
        return _PROGRESS[self]

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def is_live(self) -> bool:
        """True while this job still occupies the product's purchase slot."""
        return self in LIVE_STATES

    @property
    def needs_human(self) -> bool:
        return self in {PurchaseState.NEEDS_USER, PurchaseState.UNKNOWN,
                        PurchaseState.AWAITING_CONFIRMATION}

    @property
    def order_may_exist(self) -> bool:
        """True when an order might have been created on Amazon.

        Drives the wording of every message from this point on: the program
        must never say "nothing was ordered" once this is true.
        """
        return self in {
            PurchaseState.SUBMITTING,
            PurchaseState.CONFIRMING,
            PurchaseState.CONFIRMED,
            PurchaseState.UNKNOWN,
        }


_LABELS: Final[Mapping[PurchaseState, str]] = {
    PurchaseState.CREATED: "Ready",
    PurchaseState.PRODUCT_CHECK: "Checking the product",
    PurchaseState.RULE_VALIDATION: "Checking your rules",
    PurchaseState.CART_PREPARATION: "Preparing the order",
    PurchaseState.CHECKOUT: "At checkout",
    PurchaseState.FINAL_VALIDATION: "Final checks",
    PurchaseState.AWAITING_CONFIRMATION: "Waiting for your confirmation",
    PurchaseState.SUBMITTING: "Placing the order",
    PurchaseState.CONFIRMING: "Verifying with Amazon",
    PurchaseState.CONFIRMED: "Purchased",
    PurchaseState.BLOCKED: "Blocked",
    PurchaseState.FAILED: "Did not complete",
    PurchaseState.NEEDS_USER: "Needs your attention",
    PurchaseState.UNKNOWN: "Result uncertain",
    PurchaseState.CANCELLED: "Stopped",
}

_PROGRESS: Final[Mapping[PurchaseState, str]] = {
    PurchaseState.CREATED: "Starting...",
    PurchaseState.PRODUCT_CHECK: "Opening Amazon and reading the product...",
    PurchaseState.RULE_VALIDATION: "Checking price, seller and condition...",
    PurchaseState.CART_PREPARATION: "Setting up the order for this item only...",
    PurchaseState.CHECKOUT: "Going through checkout...",
    PurchaseState.FINAL_VALIDATION: "Re-checking the total before ordering...",
    PurchaseState.AWAITING_CONFIRMATION: "Ready for you to review.",
    PurchaseState.SUBMITTING: "Submitting the order to Amazon...",
    PurchaseState.CONFIRMING: "Reading Amazon's confirmation...",
    PurchaseState.CONFIRMED: "Order confirmed.",
    PurchaseState.BLOCKED: "Stopped by your rules.",
    PurchaseState.FAILED: "Stopped without ordering.",
    PurchaseState.NEEDS_USER: "Waiting for you.",
    PurchaseState.UNKNOWN: "Waiting for you to check your Amazon orders.",
    PurchaseState.CANCELLED: "Stopped.",
}

#: States from which no further movement is possible.
TERMINAL_STATES: Final[frozenset[PurchaseState]] = frozenset(
    {
        PurchaseState.CONFIRMED,
        PurchaseState.BLOCKED,
        PurchaseState.FAILED,
        PurchaseState.CANCELLED,
    }
)

#: States that hold the product's single purchase slot. Must stay in step with
#: ``idx_purchase_jobs_single_inflight_product`` in migration 1 -- there is a
#: test that compares the two.
LIVE_STATES: Final[frozenset[PurchaseState]] = frozenset(
    {
        PurchaseState.CREATED,
        PurchaseState.PRODUCT_CHECK,
        PurchaseState.RULE_VALIDATION,
        PurchaseState.CART_PREPARATION,
        PurchaseState.CHECKOUT,
        PurchaseState.FINAL_VALIDATION,
        PurchaseState.AWAITING_CONFIRMATION,
        PurchaseState.SUBMITTING,
        PurchaseState.CONFIRMING,
        PurchaseState.NEEDS_USER,
        PurchaseState.UNKNOWN,
    }
)

#: Diversions available from any state that has not finished.
_DIVERSIONS: Final[frozenset[PurchaseState]] = frozenset(
    {
        PurchaseState.BLOCKED,
        PurchaseState.FAILED,
        PurchaseState.NEEDS_USER,
        PurchaseState.CANCELLED,
    }
)

_TRANSITIONS: Mapping[PurchaseState, frozenset[PurchaseState]] = {
    PurchaseState.CREATED: _DIVERSIONS | {PurchaseState.PRODUCT_CHECK},
    PurchaseState.PRODUCT_CHECK: _DIVERSIONS | {PurchaseState.RULE_VALIDATION},
    PurchaseState.RULE_VALIDATION: _DIVERSIONS | {PurchaseState.CART_PREPARATION},
    PurchaseState.CART_PREPARATION: _DIVERSIONS | {PurchaseState.CHECKOUT},
    PurchaseState.CHECKOUT: _DIVERSIONS | {PurchaseState.FINAL_VALIDATION},
    # Assisted mode pauses for the user; automatic mode submits directly, but
    # only ever after FINAL_VALIDATION has passed the guard.
    PurchaseState.FINAL_VALIDATION: _DIVERSIONS
    | {PurchaseState.AWAITING_CONFIRMATION, PurchaseState.SUBMITTING},
    # A user who steps away can still cancel; re-validation is also allowed
    # because the total may have gone stale while the dialog was open.
    PurchaseState.AWAITING_CONFIRMATION: _DIVERSIONS
    | {PurchaseState.SUBMITTING, PurchaseState.FINAL_VALIDATION},
    # Once submitting, the only honest outcomes are: we read a result, or we
    # did not. NEEDS_USER is absent on purpose -- an order may exist, so the
    # correct destination for "we do not know" is UNKNOWN.
    PurchaseState.SUBMITTING: frozenset(
        {PurchaseState.CONFIRMING, PurchaseState.UNKNOWN, PurchaseState.FAILED}
    ),
    PurchaseState.CONFIRMING: frozenset(
        {PurchaseState.CONFIRMED, PurchaseState.UNKNOWN, PurchaseState.FAILED}
    ),
    # NEEDS_USER means nothing was submitted, so the run may resume.
    PurchaseState.NEEDS_USER: _DIVERSIONS
    | {
        PurchaseState.PRODUCT_CHECK,
        PurchaseState.RULE_VALIDATION,
        PurchaseState.CART_PREPARATION,
        PurchaseState.CHECKOUT,
        PurchaseState.FINAL_VALIDATION,
        PurchaseState.AWAITING_CONFIRMATION,
    },
    # The critical one. A human tells us what happened; we never retry.
    PurchaseState.UNKNOWN: frozenset(
        {PurchaseState.CONFIRMED, PurchaseState.FAILED}
    ),
    PurchaseState.CONFIRMED: frozenset(),
    PurchaseState.BLOCKED: frozenset(),
    PurchaseState.FAILED: frozenset(),
    PurchaseState.CANCELLED: frozenset(),
}

#: The transition table, with self-transitions removed. Re-entering the same
#: state is a no-op for callers (see :func:`can_transition`), not a recorded
#: move, so that a second verification prompt does not litter the audit trail.
ALLOWED_TRANSITIONS: Final[Mapping[PurchaseState, frozenset[PurchaseState]]] = {
    state: frozenset(targets) - {state} for state, targets in _TRANSITIONS.items()
}

#: The only states from which a submission may begin.
SUBMIT_ENTRY_STATES: Final[frozenset[PurchaseState]] = frozenset(
    {PurchaseState.FINAL_VALIDATION, PurchaseState.AWAITING_CONFIRMATION}
)


class IllegalTransition(Exception):
    """Raised when code attempts a move the state machine forbids.

    This is a programming error, not a user-facing condition: it means a
    service tried to do something the design prohibits.
    """

    def __init__(self, current: PurchaseState, requested: PurchaseState) -> None:
        self.current = current
        self.requested = requested
        allowed = sorted(state.value for state in ALLOWED_TRANSITIONS[current])
        super().__init__(
            f"Cannot move a purchase from {current.value!r} to {requested.value!r}. "
            f"Allowed from {current.value!r}: {allowed or ['(terminal)']}"
        )


def can_transition(current: PurchaseState, requested: PurchaseState) -> bool:
    """Whether moving from ``current`` to ``requested`` is permitted."""
    return requested in ALLOWED_TRANSITIONS[current]


def assert_transition(current: PurchaseState, requested: PurchaseState) -> None:
    """Raise :class:`IllegalTransition` unless the move is permitted."""
    if not can_transition(current, requested):
        raise IllegalTransition(current, requested)


def reachable_from(start: PurchaseState) -> frozenset[PurchaseState]:
    """Every state reachable from ``start``. Used by the safety tests."""
    seen: set[PurchaseState] = set()
    frontier = [start]
    while frontier:
        state = frontier.pop()
        for target in ALLOWED_TRANSITIONS[state]:
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return frozenset(seen)
