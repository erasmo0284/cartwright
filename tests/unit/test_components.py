"""Tests for the reusable widget library.

These tests assert on the text a user would actually read, not just on
construction. A widget that builds without raising and then shows an empty row
where the price should be is exactly the defect that reaches a release, because
nothing in the process notices a blank label.

Three of them exist for safety rather than tidiness:

* the guard table is the last thing a person sees before money moves, so its
  rows are walked and their real ``QLabel`` text is compared against the
  report they were built from;
* no visible guard label may contain an underscore, which is the signature of
  a leaked ``check_id`` or ``ErrorCode`` -- a refusal that reads like a crash;
* the severity mappings are checked against *every* member of
  :class:`WatchStatus` and :class:`ActivitySeverity`, so adding a state to the
  database layer and forgetting the colour fails here rather than in the UI.

Everything runs under the offscreen platform, set before the first
:class:`QApplication` exists because Qt reads it exactly once at that point.
Nothing may open a window.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt  # noqa: E402  (must follow the platform setting)
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.core.errors import ErrorCode  # noqa: E402
from app.core.money import Money  # noqa: E402
from app.database.records import ActivitySeverity, WatchStatus  # noqa: E402
from app.purchasing.models import (  # noqa: E402
    Availability,
    ItemCondition,
    ProductSnapshot,
    VariationSnapshot,
)
from app.purchasing.validation import (  # noqa: E402
    CheckStatus,
    GuardPhase,
    GuardReport,
    check_fail,
    check_not_applicable,
    check_pass,
    check_skipped,
)
from app.ui.components import (  # noqa: E402
    BusyOverlay,
    ButtonRow,
    Card,
    DangerButton,
    ElidingLabel,
    EmptyState,
    GuardTable,
    HLine,
    IconButton,
    MetricTile,
    MoneyField,
    PrimaryButton,
    ProductSummaryCard,
    ProgressStrip,
    QuantityField,
    Sparkline,
    StatusBadge,
    StatusDot,
    StatusLabel,
    SubtleButton,
    VLine,
    apply_theme,
    elide,
    repolish,
    severity_for_activity,
    severity_for_watch_status,
    spacer,
    use_tokens,
)
from app.ui.components.guard_table import NOT_RUN_TEXT  # noqa: E402
from app.ui.theme import (  # noqa: E402
    DARK_TOKENS,
    LIGHT_TOKENS,
    StatusSeverity,
    build_qss,
)

COMPONENTS_DIR = Path(__file__).resolve().parents[2] / "app" / "ui" / "components"


@pytest.fixture(scope="module")
def qt_app() -> Iterator[QApplication]:
    """A single offscreen :class:`QApplication` for the whole module.

    Reused rather than recreated -- Qt allows one per process -- and never
    destroyed, because tearing it down while other Qt objects are alive
    crashes the interpreter.
    """
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    yield app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def visible_labels(widget: QWidget) -> list[QLabel]:
    """Every label under ``widget`` that the user would see.

    ``isHidden`` rather than ``isVisible``: none of these widgets is ever
    shown, so everything reports itself as not visible, while ``isHidden``
    still tells apart a label that was deliberately switched off.
    """
    return [
        label
        for label in widget.findChildren(QLabel)
        if not label.isHidden() and label.text()
    ]


def visible_texts(widget: QWidget) -> list[str]:
    """The text of every visible label under ``widget``."""
    return [label.text() for label in visible_labels(widget)]


def guard_rows(table: GuardTable) -> list[QFrame]:
    """The ``#GuardRow`` frames currently in ``table``, in order."""
    return [
        frame
        for frame in table.findChildren(QFrame)
        if frame.objectName() == "GuardRow"
    ]


def passing_report() -> GuardReport:
    """A report where every check passed or was not evaluated."""
    return GuardReport(
        phase=GuardPhase.PRE_CHECKOUT,
        checks=(
            check_pass("asin_match", "Same item as the one you chose", actual="B0CX23V2ZK"),
            check_pass("price_limit", "Price within your limit", actual="$109.97"),
            check_skipped("order_total_limit", "Order total within your limit"),
            check_not_applicable("tax_known", "Tax is known"),
        ),
    )


def failing_report() -> GuardReport:
    """A report blocked by a price rule, with both numbers to compare."""
    return GuardReport(
        phase=GuardPhase.PRE_SUBMIT,
        checks=(
            check_pass("asin_match", "Same item as the one you chose", actual="B0CX23V2ZK"),
            check_fail(
                "price_above_limit",
                "Price within your limit",
                ErrorCode.PRICE_ABOVE_LIMIT,
                expected="at most $120.00",
                actual="$149.99",
                detail="The price rose after you set this limit.",
            ),
        ),
    )


def full_snapshot() -> ProductSnapshot:
    """A snapshot with every field the card can render."""
    return ProductSnapshot(
        asin="B0CX23V2ZK",
        title="Anker 737 Power Bank, 24,000mAh Portable Charger with Smart Display",
        price=Money.from_decimal("109.97"),
        availability=Availability.IN_STOCK,
        seller="Amazon.com",
        ships_from="Amazon.com",
        condition=ItemCondition.NEW,
        variation=VariationSnapshot({"Colour": "Black", "Size": "Large"}),
        prime_eligible=True,
        delivery_estimate="Tomorrow, 16 September",
    )


#: One factory per public widget. Anything added to the package belongs here,
#: which is the cheapest possible guard against a component that cannot even
#: be constructed.
WIDGET_FACTORIES: dict[str, Callable[[], QWidget]] = {
    "Card": lambda: Card("Title", "Subtitle", "cart"),
    "Card-bare": Card,
    "MetricTile": lambda: MetricTile("7", "Items watched"),
    "MetricTile-clickable": lambda: MetricTile("7", "Items watched", clickable=True),
    "StatusBadge": lambda: StatusBadge("Watching", StatusSeverity.SUCCESS),
    "StatusDot": StatusDot,
    "StatusLabel": lambda: StatusLabel("Paused", StatusSeverity.NEUTRAL),
    "EmptyState": lambda: EmptyState("watchlist", "Nothing yet", "Add one.", "Add"),
    "EmptyState-bare": EmptyState,
    "PrimaryButton": lambda: PrimaryButton("Buy now"),
    "SubtleButton": lambda: SubtleButton("Open on Amazon"),
    "DangerButton": lambda: DangerButton("Delete"),
    "IconButton": lambda: IconButton("refresh", "Check now"),
    "ButtonRow": lambda: ButtonRow(PrimaryButton("Save"), SubtleButton("Cancel")),
    "GuardTable": GuardTable,
    "ProductSummaryCard": ProductSummaryCard,
    "ProductSummaryCard-compact": lambda: ProductSummaryCard(compact=True),
    "MoneyField": MoneyField,
    "QuantityField": QuantityField,
    "ProgressStrip": ProgressStrip,
    "ProgressStrip-plain": lambda: ProgressStrip(cancellable=False),
    "BusyOverlay": BusyOverlay,
    "Sparkline": Sparkline,
    "ElidingLabel": lambda: ElidingLabel("A title", "title", 2),
    "HLine": HLine,
    "VLine": VLine,
    "spacer": spacer,
}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(WIDGET_FACTORIES))
def test_every_widget_constructs_with_a_valid_size_hint(
    qt_app: QApplication, name: str
) -> None:
    widget = WIDGET_FACTORIES[name]()
    hint = widget.sizeHint()
    assert isinstance(hint, QSize)
    # Qt returns (-1, -1) from sizeHint for a widget with no layout, meaning
    # "no preference"; the size a layout would then give it comes from the
    # widget's own bounds. That effective size is what has to be sane.
    effective = hint.expandedTo(widget.minimumSize()).boundedTo(
        widget.maximumSize()
    )
    assert effective.width() >= 0
    assert effective.height() >= 0


def test_no_component_module_sets_its_own_stylesheet() -> None:
    """The native-styling rule, enforced by reading the source.

    A widget with its own stylesheet is re-routed through
    ``QStyleSheetStyle``, loses the Windows 11 hover and press animations, and
    stops following the theme. The rule is only worth having if it cannot be
    broken quietly, so the package is checked as text.
    """
    modules = sorted(COMPONENTS_DIR.glob("*.py"))
    assert modules, f"No component modules found in {COMPONENTS_DIR}"
    offenders = [
        path.name
        for path in modules
        if "setStyleSheet" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_card_exposes_a_body_and_a_header_action(qt_app: QApplication) -> None:
    card = Card("Watch list", "Items being monitored", "watchlist")
    action = IconButton("refresh", "Check all now")
    card.set_header_action(action)
    card.add_widget(QLabel("Row one"))

    assert card.objectName() == "Card"
    assert "Watch list" in visible_texts(card)
    assert "Items being monitored" in visible_texts(card)
    assert action.parent() is not None
    assert card.body_layout.count() == 1

    card.set_title("Renamed")
    assert "Renamed" in visible_texts(card)
    assert card.accessibleName() == "Renamed"


def test_header_action_is_replaced_not_stacked(qt_app: QApplication) -> None:
    card = Card("Job")
    first = SubtleButton("Pause")
    second = SubtleButton("Resume")
    card.set_header_action(first)
    card.set_header_action(second)

    labels = [button.text() for button in card.findChildren(SubtleButton)]
    assert "Resume" in labels
    assert first.parent() is None


def test_buttons_carry_object_names_and_accessible_names(
    qt_app: QApplication,
) -> None:
    primary = PrimaryButton("Buy now")
    subtle = SubtleButton("Open on Amazon")
    danger = DangerButton("Delete watch")
    icon = IconButton("trash", "Delete this watch")

    assert primary.objectName() == "PrimaryButton"
    assert subtle.objectName() == "SubtleButton"
    assert danger.objectName() == "DangerButton"
    assert primary.accessibleName() == "Buy now"
    assert danger.minimumHeight() >= 32

    # An icon-only control must be announceable and hoverable.
    assert icon.toolTip() == "Delete this watch"
    assert icon.accessibleName() == "Delete this watch"
    assert not icon.icon().isNull()

    primary.setText("Place order")
    assert primary.accessibleName() == "Place order"


def test_button_row_orders_and_aligns(qt_app: QApplication) -> None:
    save, cancel = PrimaryButton("Save"), SubtleButton("Cancel")
    row = ButtonRow(save, cancel, align="left")
    assert row.buttons == (save, cancel)
    assert row.layout() is not None


def test_separators_use_the_stylesheet_names(qt_app: QApplication) -> None:
    assert HLine().objectName() == "Separator"
    # A vertical rule cannot be the one-pixel-high #Separator.
    assert VLine().objectName() == "SeparatorVertical"


def test_eliding_label_keeps_the_full_text(qt_app: QApplication) -> None:
    long_title = "Anker 737 Power Bank, 24,000mAh Portable Charger " * 4
    label = ElidingLabel(long_title, "title", 2)
    label.resize(120, 40)
    assert label.full_text == long_title
    # Something was dropped, so the whole string must remain reachable.
    assert label.text() != long_title
    assert label.toolTip() == long_title


def test_apply_theme_redraws_painted_colours(qt_app: QApplication) -> None:
    """Icons and hand-painted surfaces must follow a theme change.

    They cannot read the stylesheet, and Qt's own colour scheme is not a
    reliable stand-in -- the offscreen platform reports ``Unknown`` whatever
    ``ThemeManager`` last asked for -- so a dark card would otherwise keep the
    light theme's dark glyphs, which are close to invisible on it.
    """
    page = QWidget()
    column = QVBoxLayout(page)
    card = Card("Watch list", icon="watchlist")
    spark = Sparkline()
    spark.set_series(_series("10.00", "9.00"))
    column.addWidget(card)
    column.addWidget(spark)

    try:
        apply_theme(page, LIGHT_TOKENS)
        light_icon = card.findChildren(QLabel)[0].pixmap().toImage()
        light_paint = spark.grab().toImage()

        apply_theme(page, DARK_TOKENS)
        dark_icon = card.findChildren(QLabel)[0].pixmap().toImage()
        dark_paint = spark.grab().toImage()

        assert light_icon != dark_icon
        assert light_paint != dark_paint
    finally:
        # Leave automatic resolution in place for every other test.
        use_tokens(None)


def test_elide_helper_shortens_and_keeps_the_text_reachable(
    qt_app: QApplication,
) -> None:
    label = QLabel()
    label.resize(60, 20)
    long_text = "Anker 737 Power Bank with Smart Display and a long tail"
    shown = elide(label, long_text)
    assert shown == label.text()
    assert len(shown) < len(long_text)
    assert label.toolTip() == long_text

    # Nothing dropped means no tooltip to explain a truncation that did not
    # happen.
    elide(label, "Short")
    assert label.text() == "Short"
    assert label.toolTip() == ""


def test_repolish_does_not_raise_without_a_stylesheet(qt_app: QApplication) -> None:
    """The unpolish/polish dance is safe when no stylesheet is in force."""
    previous = qt_app.styleSheet()
    qt_app.setStyleSheet("")
    try:
        widget = QWidget()
        repolish(widget)
        badge = StatusBadge("Idle", StatusSeverity.NEUTRAL)
        repolish(badge)
    finally:
        qt_app.setStyleSheet(previous)


def test_property_change_survives_a_live_stylesheet(qt_app: QApplication) -> None:
    """A severity written after polish must still be the one in force."""
    previous = qt_app.styleSheet()
    qt_app.setStyleSheet(build_qss(LIGHT_TOKENS))
    try:
        badge = StatusBadge("Watching", StatusSeverity.SUCCESS)
        badge.grab()  # forces a polish and a paint
        badge.set_status("Blocked", StatusSeverity.BLOCKED)
        assert badge.property("severity") == "blocked"
        assert badge.text() == "Blocked"
        badge.grab()
    finally:
        qt_app.setStyleSheet(previous)


# ---------------------------------------------------------------------------
# Badges, dots and the severity mappings
# ---------------------------------------------------------------------------


def test_status_badge_sets_text_and_severity(qt_app: QApplication) -> None:
    badge = StatusBadge()
    badge.set_status("Needs sign-in", StatusSeverity.WARNING)
    assert badge.objectName() == "Badge"
    assert badge.text() == "Needs sign-in"
    assert badge.property("severity") == "warning"
    assert badge.severity is StatusSeverity.WARNING
    # Colour is never the only signal.
    assert badge.accessibleName() == "Needs sign-in"


def test_status_badge_falls_back_to_the_severity_word(qt_app: QApplication) -> None:
    badge = StatusBadge()
    badge.set_status("", StatusSeverity.ERROR)
    assert badge.text() == StatusSeverity.ERROR.label


def test_status_dot_is_always_paired_with_text(qt_app: QApplication) -> None:
    pair = StatusLabel("Out of stock", StatusSeverity.INFO)
    assert pair.text() == "Out of stock"
    dot = pair.findChild(StatusDot)
    assert dot is not None
    assert dot.objectName() == "StatusDot"
    assert dot.property("severity") == "info"

    pair.set_status("Error", StatusSeverity.ERROR)
    assert dot.property("severity") == "error"


@pytest.mark.parametrize("status", list(WatchStatus))
def test_every_watch_status_has_a_severity(status: WatchStatus) -> None:
    assert isinstance(severity_for_watch_status(status), StatusSeverity)


@pytest.mark.parametrize("severity", list(ActivitySeverity))
def test_every_activity_severity_has_a_severity(severity: ActivitySeverity) -> None:
    assert isinstance(severity_for_activity(severity), StatusSeverity)


def test_watch_severity_mapping_reads_sensibly() -> None:
    assert severity_for_watch_status(WatchStatus.ERROR) is StatusSeverity.ERROR
    assert severity_for_watch_status(WatchStatus.PAUSED) is StatusSeverity.NEUTRAL
    assert severity_for_watch_status(WatchStatus.EXPIRED) is StatusSeverity.NEUTRAL
    assert (
        severity_for_watch_status(WatchStatus.NEEDS_LOGIN) is StatusSeverity.WARNING
    )
    assert (
        severity_for_watch_status(WatchStatus.NEEDS_VERIFICATION)
        is StatusSeverity.WARNING
    )
    assert (
        severity_for_watch_status(WatchStatus.TARGET_REACHED)
        is StatusSeverity.SUCCESS
    )
    assert (
        severity_for_watch_status(WatchStatus.PURCHASE_COMPLETED)
        is StatusSeverity.SUCCESS
    )
    assert (
        severity_for_watch_status(WatchStatus.WAITING_FOR_PRICE)
        is StatusSeverity.INFO
    )
    assert severity_for_watch_status(WatchStatus.OUT_OF_STOCK) is StatusSeverity.INFO


# ---------------------------------------------------------------------------
# Metric tile
# ---------------------------------------------------------------------------


def test_metric_tile_shows_value_caption_and_severity(qt_app: QApplication) -> None:
    tile = MetricTile("0", "Items watched")
    tile.set_value("7")
    tile.set_caption("Items being watched")
    assert tile.value() == "7"
    assert tile.caption() == "Items being watched"
    assert "7" in visible_texts(tile)

    tile.set_severity(StatusSeverity.WARNING)
    assert StatusSeverity.WARNING.label in visible_texts(tile)
    assert StatusSeverity.WARNING.label in tile.accessibleName()

    tile.set_severity(None)
    assert StatusSeverity.WARNING.label not in visible_texts(tile)


def test_clickable_metric_tile_is_reachable_by_keyboard(
    qt_app: QApplication,
) -> None:
    tile = MetricTile("3", "Needs attention", clickable=True)
    fired: list[int] = []
    tile.clicked.connect(lambda: fired.append(1))

    assert tile.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert tile.accessibleName()

    tile.keyPressEvent(_key_event(Qt.Key.Key_Return))
    tile.keyPressEvent(_key_event(Qt.Key.Key_Space))
    assert len(fired) == 2


def test_non_clickable_metric_tile_ignores_keys(qt_app: QApplication) -> None:
    tile = MetricTile("3", "Items watched")
    fired: list[int] = []
    tile.clicked.connect(lambda: fired.append(1))
    tile.keyPressEvent(_key_event(Qt.Key.Key_Return))
    assert fired == []


def _key_event(key: Qt.Key):
    from PySide6.QtGui import QKeyEvent

    return QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_empty_state_renders_and_emits(qt_app: QApplication) -> None:
    state = EmptyState(
        icon="watchlist",
        heading="No products being watched",
        body="Add a product and the app can monitor its price and availability.",
        action_text="Add Product",
    )
    assert state.objectName() == "EmptyState"
    assert state.heading == "No products being watched"
    assert "monitor its price" in state.body
    assert state.accessibleName().startswith("No products being watched")

    fired: list[int] = []
    state.action_clicked.connect(lambda: fired.append(1))
    button = state.action_button
    assert button is not None
    button.click()
    assert fired == [1]


def test_empty_state_without_an_action_has_no_button(qt_app: QApplication) -> None:
    state = EmptyState(heading="Nothing here")
    assert state.action_button is None


# ---------------------------------------------------------------------------
# Guard table
# ---------------------------------------------------------------------------


def test_guard_table_not_run_state(qt_app: QApplication) -> None:
    table = GuardTable()
    table.set_report(None)
    assert NOT_RUN_TEXT in visible_texts(table)
    assert guard_rows(table) == []


def test_guard_table_passing_report(qt_app: QApplication) -> None:
    report = passing_report()
    table = GuardTable()
    table.set_report(report)

    rows = guard_rows(table)
    assert len(rows) == len(report.checks)

    expected_status = {
        CheckStatus.PASS: "pass",
        CheckStatus.SKIPPED: "skipped",
        CheckStatus.NOT_APPLICABLE: "skipped",
    }
    for row, check in zip(rows, report.checks):
        assert row.property("status") == expected_status[check.status]
        texts = visible_texts(row)
        assert check.title in texts
        assert check.status.label in texts

    # Two of the four checks were evaluated and both passed.
    assert report.summary in visible_texts(table)
    assert report.phase.label in visible_texts(table)
    # A passing check shows the value it actually read.
    assert "$109.97" in visible_texts(table)


def test_guard_table_failing_report_shows_expected_and_found(
    qt_app: QApplication,
) -> None:
    report = failing_report()
    table = GuardTable()
    table.set_report(report)

    rows = guard_rows(table)
    statuses = [row.property("status") for row in rows]
    assert statuses == ["pass", "fail"]

    failing_row = rows[1]
    texts = visible_texts(failing_row)
    assert "Price within your limit" in texts
    assert "FAIL" in texts
    assert "Expected: at most $120.00" in texts
    assert "Found: $149.99" in texts
    assert "The price rose after you set this limit." in texts

    # The blocking failure is named prominently, in words.
    table_texts = " | ".join(visible_texts(table))
    assert "Blocked by: Price within your limit" in table_texts
    assert report.summary in table_texts


def test_guard_table_never_shows_a_code_or_an_underscore(
    qt_app: QApplication,
) -> None:
    """A leaked ``check_id`` or ``ErrorCode`` turns a refusal into a crash."""
    table = GuardTable()
    codes = {code.value for code in ErrorCode}
    for report in (passing_report(), failing_report(), None):
        table.set_report(report)
        for text in visible_texts(table):
            assert "_" not in text, text
            assert text not in codes, text
            for code in codes:
                assert code not in text, text


def test_guard_table_rebuilds_rather_than_appends(qt_app: QApplication) -> None:
    """A stale PASS left behind by a re-check would be a safety defect."""
    table = GuardTable()
    table.set_report(failing_report())
    table.set_report(passing_report())
    assert len(guard_rows(table)) == len(passing_report().checks)
    assert "FAIL" not in visible_texts(table)

    table.set_report(None)
    assert guard_rows(table) == []
    assert table.report is None


# ---------------------------------------------------------------------------
# Product summary
# ---------------------------------------------------------------------------


def test_product_summary_renders_every_field(qt_app: QApplication) -> None:
    snapshot = full_snapshot()
    card = ProductSummaryCard()
    card.set_product(snapshot)

    assert card.title_text == snapshot.title
    assert card.field_text("Item code") == "B0CX23V2ZK"
    assert card.field_text("Version") == "Colour: Black, Size: Large"
    assert card.field_text("Price") == "$109.97"
    assert card.field_text("Availability") == "In stock"
    assert card.field_text("Sold by") == "Amazon.com"
    assert card.field_text("Ships from") == "Amazon.com"
    assert card.field_text("Condition") == "New"
    assert card.field_text("Delivery") == "Tomorrow, 16 September"
    assert card.field_text("Prime") == "Eligible"

    # Every value is labelled: an unlabelled "Amazon.com" says nothing about
    # whether it is the seller or the shipper.
    texts = visible_texts(card)
    for key in ("Item code", "Price", "Sold by", "Ships from", "Condition"):
        assert key in texts


def test_product_summary_shows_dashes_for_an_empty_snapshot(
    qt_app: QApplication,
) -> None:
    card = ProductSummaryCard()
    card.set_product(ProductSnapshot(asin="B0CX23V2ZK"))

    assert card.field_text("Price") == "Not shown"
    assert card.field_text("Version") == "No options"
    assert card.field_text("Availability") == "Unclear"
    assert card.field_text("Condition") == "Not stated"
    assert card.field_text("Sold by") == "-"
    assert card.field_text("Ships from") == "-"
    assert card.field_text("Delivery") == "-"
    # Prime is only ever stated when Amazon positively said so.
    assert card.field_text("Prime") is None

    # Nothing renders as an empty gap.
    for key in ("Price", "Version", "Availability", "Condition", "Sold by"):
        assert card.field_text(key)


def test_product_summary_compact_mode_is_a_list_row(qt_app: QApplication) -> None:
    card = ProductSummaryCard(compact=True)
    card.set_product(full_snapshot())
    assert card.field_text("Price") == "$109.97"
    assert card.field_text("Availability") == "In stock"
    assert card.field_text("Sold by") is None
    assert card.field_text("Item code") is None


def test_product_summary_tolerates_a_bad_image(qt_app: QApplication) -> None:
    card = ProductSummaryCard()
    card.set_product(full_snapshot())
    assert card.set_image_from_bytes(b"not an image") is False
    assert card.set_image_from_bytes(b"") is False

    # The neutral placeholder is still in place, so the thumbnail box is never
    # an empty hole the user reads as "still loading".
    image = next(
        label
        for label in card.findChildren(QLabel)
        if label.accessibleName() == "Product image"
    )
    assert not image.pixmap().isNull()
    card.grab()


def test_product_summary_accepts_a_real_pixmap(qt_app: QApplication) -> None:
    pixmap = QPixmap(QSize(200, 100))
    pixmap.fill(Qt.GlobalColor.darkGray)
    card = ProductSummaryCard()
    card.set_product(full_snapshot(), pixmap)
    card.grab()


# ---------------------------------------------------------------------------
# Money and quantity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "cents"),
    [
        ("109.97", 10997),
        ("$109.97", 10997),
        ("1,299.00", 129900),
        ("  109.97  ", 10997),
    ],
)
def test_money_field_accepts_what_a_person_types(
    qt_app: QApplication, typed: str, cents: int
) -> None:
    field = MoneyField()
    field.line_edit.setText(typed)
    assert field.is_valid()
    value = field.value()
    assert value is not None
    assert value.cents == cents


def test_money_field_treats_blank_as_no_limit(qt_app: QApplication) -> None:
    field = MoneyField()
    field.line_edit.setText("109.97")
    field.line_edit.setText("")
    assert field.value() is None
    assert field.is_valid()
    assert field.problem == ""


@pytest.mark.parametrize("typed", ["-5", "abc", "999999999", "(5.00)"])
def test_money_field_rejects_nonsense(qt_app: QApplication, typed: str) -> None:
    field = MoneyField()
    field.line_edit.setText(typed)
    assert not field.is_valid()
    assert field.value() is None
    # Explained in place, not in a dialog.
    assert field.problem


def test_money_field_emits_and_round_trips(qt_app: QApplication) -> None:
    field = MoneyField()
    seen: list[object] = []
    field.value_changed.connect(seen.append)

    original = Money.from_decimal("1299.00")
    field.set_value(original)
    assert field.value() == original
    assert seen and seen[-1] == original

    field.set_value(None)
    assert field.value() is None
    assert seen[-1] is None


def test_quantity_field_clamps_to_what_is_available(qt_app: QApplication) -> None:
    field = QuantityField()
    field.set_value(9)
    assert field.value() == 9

    field.set_maximum_available(2)
    assert field.value() == 2
    assert field.spin_box.maximum() == 2
    # The clamp is explained, not silent.
    assert "2 available" in visible_texts(field)

    field.set_maximum_available(None)
    assert field.spin_box.maximum() == 30
    field.set_value(50)
    assert field.value() == 30


# ---------------------------------------------------------------------------
# Busy indicators
# ---------------------------------------------------------------------------


def test_progress_strip_hides_when_idle_and_emits_cancel(
    qt_app: QApplication,
) -> None:
    strip = ProgressStrip()
    assert strip.isHidden()

    strip.start("Opening Amazon...")
    assert not strip.isHidden()
    assert strip.is_running()
    assert strip.message == "Opening Amazon..."
    assert "Opening Amazon..." in visible_texts(strip)

    strip.set_message("Reading price...")
    assert strip.message == "Reading price..."

    fired: list[int] = []
    strip.cancel_requested.connect(lambda: fired.append(1))
    button = strip.cancel_button
    assert button is not None
    button.click()
    assert fired == [1]

    strip.stop()
    assert strip.isHidden()
    assert not strip.is_running()


def test_progress_strip_can_be_built_without_cancel(qt_app: QApplication) -> None:
    strip = ProgressStrip(cancellable=False)
    assert strip.cancel_button is None


def test_busy_overlay_covers_and_follows_its_host(qt_app: QApplication) -> None:
    host = QWidget()
    host.resize(400, 300)
    # Shown, not mapped: the offscreen platform renders into memory, so a
    # real resize event is delivered and nothing reaches a display.
    host.show()
    overlay = BusyOverlay("Re-reading the order...")

    overlay.show_over(host)
    assert overlay.parent() is host
    assert overlay.size() == host.size()
    assert overlay.is_showing()
    assert overlay.message == "Re-reading the order..."

    host.resize(500, 200)
    qt_app.processEvents()
    assert overlay.size() == host.size()
    overlay.grab()

    overlay.hide_overlay()
    assert not overlay.is_showing()
    # Resizing after hiding must not resurrect the sheet's geometry tracking.
    host.resize(600, 400)
    assert overlay.size() != host.size()


# ---------------------------------------------------------------------------
# Sparkline
# ---------------------------------------------------------------------------


def _series(*amounts: str) -> list[Money]:
    return [Money.from_decimal(amount) for amount in amounts]


@pytest.mark.parametrize(
    "points",
    [
        [],
        _series("10.00"),
        _series("10.00", "9.00"),
        _series("10.00", "9.50", "9.50", "11.25", "8.00", "8.50", "12.00"),
        _series("9.99", "9.99", "9.99", "9.99"),
    ],
    ids=["empty", "single", "pair", "many", "flat"],
)
def test_sparkline_paints_every_series_shape(
    qt_app: QApplication, points: list[Money]
) -> None:
    spark = Sparkline()
    spark.resize(160, 40)
    spark.set_series(points)
    assert spark.point_count == len(points)
    # grab() runs a real paintEvent, which is where a divide-by-zero on a flat
    # series would surface.
    assert not spark.grab().isNull()


def test_sparkline_target_line_and_tooltip(qt_app: QApplication) -> None:
    spark = Sparkline()
    spark.resize(160, 40)
    spark.set_series(_series("10.00", "9.00", "12.00"))
    spark.set_target(Money.from_decimal("8.50"))

    tooltip = spark.toolTip()
    # The numbers are in the tooltip, so the shape is never the only clue.
    assert "Latest $12.00" in tooltip
    assert "Low $9.00" in tooltip
    assert "High $12.00" in tooltip
    assert "Target $8.50" in tooltip
    assert spark.accessibleDescription() == tooltip
    assert not spark.grab().isNull()

    spark.set_series([])
    assert "No price history yet" in spark.toolTip()
    spark.set_target(None)
    assert not spark.grab().isNull()


def test_sparkline_survives_a_mixed_currency_series(qt_app: QApplication) -> None:
    """Nonsense in, no exception out: a paint event must never raise."""
    spark = Sparkline()
    spark.resize(160, 40)
    spark.set_series([Money(1000, "USD"), Money(1200, "GBP")])
    assert not spark.grab().isNull()
