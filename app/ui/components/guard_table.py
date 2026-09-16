"""The PASS/FAIL table the user reads before approving an order.

This is the most safety-critical widget in the application: it is the only
place a person can see *why* the program is willing to spend their money, and
the only place they can see why it refused. Three rules follow from that, and
none of them are cosmetic.

**Every outcome is a word.** ``CheckStatus.label`` gives "PASS", "FAIL", "Not
set" or "-". The left-edge colour from the stylesheet's ``#GuardRow[status]``
is reinforcement; it is never the only thing that distinguishes a pass from a
failure.

**A failure shows the two numbers that disagree.** "Price too high" is not
actionable. "Expected: at most $120.00 / Found: $149.99" is, and it is also
the form in which a user can spot that *the program* is wrong.

**Nothing internal is ever rendered.** ``check_id`` and ``error_code`` are
machine identifiers (``price_above_limit``); showing one turns a clear refusal
into something that looks like a crash. Only ``title``, ``detail``,
``expected`` and ``actual`` -- all written for a person -- reach the screen.

Rows are rebuilt from scratch on each :meth:`GuardTable.set_report`. Diffing
them would be faster and is exactly the kind of cleverness that leaves a stale
PASS on screen after a re-check failed.
"""

from __future__ import annotations

import logging
from typing import Final, Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.purchasing.validation import CheckStatus, GuardCheck, GuardReport
from app.ui.components.badge import StatusBadge
from app.ui.components.common import role_label
from app.ui.theme import StatusSeverity

_LOG: Final = logging.getLogger("app.ui.components.guard_table")

#: Text shown when there is no report at all. Not an error and not a pass:
#: "nothing has been checked" is its own state and has to look like one.
NOT_RUN_TEXT: Final[str] = "Checks have not run yet"

#: :class:`CheckStatus` -> the ``status`` property the stylesheet selects on.
#: "Not set" and "not applicable" are different reasons for the same visual
#: outcome -- neither was evaluated -- so both render as ``skipped``.
_ROW_STATUS: Final[Mapping[CheckStatus, str]] = {
    CheckStatus.PASS: "pass",
    CheckStatus.FAIL: "fail",
    CheckStatus.SKIPPED: "skipped",
    CheckStatus.NOT_APPLICABLE: "skipped",
}


class GuardTable(QWidget):
    """Renders a :class:`GuardReport` as a list of outcome rows."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._report: GuardReport | None = None

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        self._summary = role_label("", "sectionHeading", self)
        self._phase = role_label("", "caption", self)
        self._blocker = StatusBadge("", StatusSeverity.BLOCKED, self)
        self._blocker.setWordWrap(True)
        self._not_run = role_label(NOT_RUN_TEXT, "caption", self)

        column.addWidget(self._summary)
        column.addWidget(self._phase)
        column.addWidget(self._blocker)
        column.addWidget(self._not_run)

        self._rows_host = QWidget(self)
        self._rows = QVBoxLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 0, 0)
        # No gap between rows: the left-edge colour bars are meant to read as
        # one continuous list, the way a checklist does on paper.
        self._rows.setSpacing(1)
        column.addWidget(self._rows_host)
        column.addStretch(1)

        self.setAccessibleName("Purchase checks")
        self.set_report(None)

    # -- public API ---------------------------------------------------------

    def set_report(self, report: GuardReport | None) -> None:
        """Show ``report``, or the not-run state when it is ``None``."""
        self._report = report
        self._clear_rows()

        if report is None:
            self._summary.setVisible(False)
            self._phase.setVisible(False)
            self._blocker.setVisible(False)
            self._rows_host.setVisible(False)
            self._not_run.setVisible(True)
            self.setAccessibleDescription(NOT_RUN_TEXT)
            return

        self._not_run.setVisible(False)
        self._summary.setText(report.summary)
        self._summary.setVisible(True)
        self._phase.setText(report.phase.label)
        self._phase.setVisible(True)
        self._rows_host.setVisible(True)

        blockers = report.blocking_failures
        if blockers:
            first = blockers[0]
            self._blocker.set_status(
                f"Blocked by: {first.title}", StatusSeverity.BLOCKED
            )
            self._blocker.setVisible(True)
        else:
            self._blocker.setVisible(False)

        for check in report.checks:
            self._rows.addWidget(self._build_row(check))

        self.setAccessibleDescription(
            f"{report.summary}. {self._blocker.text()}"
            if blockers
            else report.summary
        )

    @property
    def report(self) -> GuardReport | None:
        """The report currently displayed."""
        return self._report

    # -- row construction ---------------------------------------------------

    def _build_row(self, check: GuardCheck) -> QFrame:
        """One row: title on the left, outcome on the right, detail beneath."""
        row = QFrame(self._rows_host)
        row.setObjectName("GuardRow")
        row.setFrameShape(QFrame.Shape.NoFrame)
        # Set before the row is ever shown, so no repolish is needed: the
        # property is in place by the time the stylesheet is first resolved.
        row.setProperty("status", _ROW_STATUS.get(check.status, "skipped"))
        row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        column = QVBoxLayout(row)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title = role_label(check.title, "title", row)
        title.setWordWrap(True)
        header.addWidget(title, 1)

        # A passing check shows what it actually found, compactly: the user
        # gets to confirm the number rather than trust the word "PASS".
        if check.status is CheckStatus.PASS and check.actual:
            header.addWidget(role_label(check.actual, "caption", row), 0)

        status = role_label(check.status.label, "title", row)
        status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(status, 0)
        column.addLayout(header)

        if check.status in {CheckStatus.FAIL, CheckStatus.SKIPPED} and check.detail:
            detail = role_label(check.detail, "caption", row)
            detail.setWordWrap(True)
            column.addWidget(detail)

        if check.status is CheckStatus.FAIL:
            for prefix, value in (
                ("Expected", check.expected),
                ("Found", check.actual),
            ):
                if value:
                    column.addWidget(
                        role_label(f"{prefix}: {value}", "caption", row)
                    )

        row.setAccessibleName(f"{check.title}: {check.status.label}")
        if check.error_code is not None:
            # The code goes to the log and nowhere else. It is what a support
            # question needs and the last thing a buyer should have to read.
            _LOG.debug(
                "Guard check %s failed with code %s",
                check.check_id,
                check.error_code.value,
            )
        return row

    def _clear_rows(self) -> None:
        """Remove every row widget.

        Reparenting to ``None`` detaches the row immediately -- ``deleteLater``
        alone would leave it visible until the event loop next runs, which in a
        modal purchase dialog can be a long time.
        """
        while self._rows.count():
            item = self._rows.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
