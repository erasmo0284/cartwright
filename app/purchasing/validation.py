"""Guard check results.

A guard run produces a :class:`GuardReport`: an ordered list of
:class:`GuardCheck` results, one per rule, plus an overall verdict. The report
is what the UI renders as the PASS/FAIL table, what the test-mode result
screen shows, and what is persisted for the audit trail.

The verdict is computed, never assigned: a report passes when no *required*
check failed. There is no way to construct a passing report that contains a
required failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable

from app.core.errors import ErrorCode


class CheckStatus(StrEnum):
    """Outcome of a single check."""

    PASS = "pass"
    FAIL = "fail"
    #: The rule was not configured, so there was nothing to check.
    SKIPPED = "skipped"
    #: The rule does not apply in this phase (e.g. tax before checkout).
    NOT_APPLICABLE = "not_applicable"

    @property
    def label(self) -> str:
        return {
            CheckStatus.PASS: "PASS",
            CheckStatus.FAIL: "FAIL",
            CheckStatus.SKIPPED: "Not set",
            CheckStatus.NOT_APPLICABLE: "-",
        }[self]


class GuardPhase(StrEnum):
    """When a guard run happened.

    Three phases, deliberately: the same rules are re-checked as more becomes
    knowable, and the last one runs against the real order total moments
    before submission.
    """

    PRE_CART = "pre_cart"
    PRE_CHECKOUT = "pre_checkout"
    PRE_SUBMIT = "pre_submit"

    @property
    def label(self) -> str:
        return {
            GuardPhase.PRE_CART: "Before adding to the order",
            GuardPhase.PRE_CHECKOUT: "Before checkout",
            GuardPhase.PRE_SUBMIT: "Immediately before ordering",
        }[self]


@dataclass(frozen=True)
class GuardCheck:
    """The result of validating one rule."""

    check_id: str
    title: str
    status: CheckStatus
    expected: str | None = None
    actual: str | None = None
    error_code: ErrorCode | None = None
    #: A failed required check blocks the purchase. A failed optional check is
    #: reported but does not block. Only checks the user did not configure are
    #: ever optional.
    required: bool = True
    #: Extra sentence shown under the row when it failed.
    detail: str | None = None

    @property
    def passed(self) -> bool:
        return self.status in {
            CheckStatus.PASS,
            CheckStatus.SKIPPED,
            CheckStatus.NOT_APPLICABLE,
        }

    @property
    def blocks(self) -> bool:
        return self.status is CheckStatus.FAIL and self.required


def check_pass(
    check_id: str, title: str, *, actual: str | None = None, expected: str | None = None
) -> GuardCheck:
    return GuardCheck(check_id, title, CheckStatus.PASS, expected=expected, actual=actual)


def check_fail(
    check_id: str,
    title: str,
    error_code: ErrorCode,
    *,
    expected: str | None = None,
    actual: str | None = None,
    detail: str | None = None,
    required: bool = True,
) -> GuardCheck:
    return GuardCheck(
        check_id,
        title,
        CheckStatus.FAIL,
        expected=expected,
        actual=actual,
        error_code=error_code,
        required=required,
        detail=detail,
    )


def check_skipped(check_id: str, title: str, *, detail: str | None = None) -> GuardCheck:
    return GuardCheck(
        check_id, title, CheckStatus.SKIPPED, required=False, detail=detail
    )


def check_not_applicable(check_id: str, title: str) -> GuardCheck:
    return GuardCheck(check_id, title, CheckStatus.NOT_APPLICABLE, required=False)


@dataclass(frozen=True)
class GuardReport:
    """The full outcome of one guard run."""

    phase: GuardPhase
    checks: tuple[GuardCheck, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        """True only when no required check failed.

        Computed on read, so a report can never claim to pass while holding a
        blocking failure.
        """
        return not any(check.blocks for check in self.checks)

    @property
    def failures(self) -> tuple[GuardCheck, ...]:
        return tuple(check for check in self.checks if check.status is CheckStatus.FAIL)

    @property
    def blocking_failures(self) -> tuple[GuardCheck, ...]:
        return tuple(check for check in self.checks if check.blocks)

    @property
    def blocked_code(self) -> ErrorCode | None:
        """The error code of the first blocking failure, for reporting."""
        for check in self.checks:
            if check.blocks and check.error_code is not None:
                return check.error_code
        return None

    @property
    def summary(self) -> str:
        """One-line summary, e.g. ``12 of 13 checks passed``."""
        total = sum(
            1
            for check in self.checks
            if check.status in {CheckStatus.PASS, CheckStatus.FAIL}
        )
        passed = sum(1 for check in self.checks if check.status is CheckStatus.PASS)
        if self.passed:
            return "All checks passed" if total else "No checks were needed"
        return f"{passed} of {total} checks passed"

    def find(self, check_id: str) -> GuardCheck | None:
        for check in self.checks:
            if check.check_id == check_id:
                return check
        return None

    def with_checks(self, extra: Iterable[GuardCheck]) -> GuardReport:
        return GuardReport(phase=self.phase, checks=self.checks + tuple(extra))
