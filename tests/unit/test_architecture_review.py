"""Tests written during the architecture and code-quality review.

Two kinds of test live here.

**Defect proofs.** Each is marked ``xfail(strict=True)`` and asserts the
*correct* behaviour, so the suite stays green while the defect exists and
turns red (XPASS) the moment someone fixes it and forgets to remove the
marker. The reason string names the file and line of the defect.

**Invariants.** Mechanical checks of the layering rules ``docs/ARCHITECTURE.md``
claims. The ones that hold are plain tests; the ones that do not are marked
xfail with the offending lines listed, so the claim and the reality are both
written down.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import queue
import threading
from typing import Any

import pytest

from app.automation.browser_worker import (
    BrowserTask,
    BrowserWorker,
    Priority,
    WorkerState,
)
from app.config import SettingsService
from app.core.errors import AppError, ErrorCode
from app.core.money import Money
from app.database.database import Database
from app.database.repositories import (
    ActivityRepository,
    OrderRepository,
    ProductRepository,
    PurchaseRepository,
    RulesRepository,
    WatchRepository,
)
from app.paths import AppPaths
from app.purchasing.models import (
    Availability,
    ConditionPolicy,
    ItemCondition,
    ProductSnapshot,
    PurchaseMode,
    PurchaseRules,
    SellerPolicy,
    VariationSnapshot,
)
from app.purchasing.purchase_service import PurchaseService
from app.purchasing.states import PurchaseState

APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app"
ASIN = "B07XYZ1234"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def snapshot() -> ProductSnapshot:
    return ProductSnapshot(
        asin=ASIN,
        title="Klein Tools CL800 Clamp Meter",
        brand="Klein Tools",
        url=f"https://www.amazon.com/dp/{ASIN}",
        price=usd("109.97"),
        availability=Availability.IN_STOCK,
        availability_text="In Stock",
        seller="Amazon.com",
        ships_from="Amazon.com",
        condition=ItemCondition.NEW,
        variation=VariationSnapshot({"Color": "Black"}),
    )


@pytest.fixture
def rules() -> PurchaseRules:
    return PurchaseRules(
        expected_asin=ASIN,
        quantity=1,
        max_item_price=usd("120.00"),
        max_order_total=usd("135.00"),
        seller_policy=SellerPolicy.AMAZON_ONLY,
        condition_policy=ConditionPolicy.NEW_ONLY,
        expected_variation=VariationSnapshot({"Color": "Black"}),
        expected_seller="Amazon.com",
    )


@pytest.fixture
def repositories(database: Database) -> dict[str, Any]:
    return {
        "products": ProductRepository(database),
        "rules": RulesRepository(database),
        "purchases": PurchaseRepository(database),
        "orders": OrderRepository(database),
        "watches": WatchRepository(database),
        "activity": ActivityRepository(database),
    }


@pytest.fixture
def service(
    database: Database, app_paths: AppPaths, repositories: dict[str, Any]
) -> PurchaseService:
    """A real :class:`PurchaseService` over real repositories.

    The browser worker is real but never started, so nothing reaches a
    browser; these tests drive its signals by hand, which is exactly what
    ``BrowserWorker._execute`` does.
    """
    settings = SettingsService(database)
    settings.reload()
    return PurchaseService(
        worker=BrowserWorker(app_paths),
        products=repositories["products"],
        rules_repo=repositories["rules"],
        purchases=repositories["purchases"],
        orders=repositories["orders"],
        watches=repositories["watches"],
        activity=repositories["activity"],
        settings=settings,
    )


def _job_at(
    repos: dict[str, Any],
    snapshot: ProductSnapshot,
    rules: PurchaseRules,
    *states: PurchaseState,
) -> Any:
    """A purchase job walked forward through ``states``."""
    product = repos["products"].upsert_from_snapshot(snapshot)
    stored = repos["rules"].create(rules)
    job = repos["purchases"].create(
        product_id=product.id,
        rules_id=stored.rules_id,
        mode=PurchaseMode.ASSISTED,
        test_mode=False,
    )
    for state in states:
        job = repos["purchases"].transition(job.id, state)
    return job


_TO_FINAL = (
    PurchaseState.PRODUCT_CHECK,
    PurchaseState.RULE_VALIDATION,
    PurchaseState.CART_PREPARATION,
    PurchaseState.CHECKOUT,
    PurchaseState.FINAL_VALIDATION,
)


# ---------------------------------------------------------------------------
# Regression pin: recording a submission from SUBMITTING
# ---------------------------------------------------------------------------


def test_mark_submitted_accepts_the_state_the_submit_path_actually_uses(
    repositories: dict[str, Any], snapshot: ProductSnapshot, rules: PurchaseRules
) -> None:
    """``PurchaseService._submit`` is in SUBMITTING when it records.

    It transitions to ``SUBMITTING`` (purchase_service.py:537) and only then
    calls ``ADAPTER.submit_order``, which calls ``record_submission`` ->
    ``mark_submitted``. Released commit 2ef2699 checked the state against
    ``SUBMIT_ENTRY_STATES``, which excludes ``SUBMITTING``, so no order could
    ever be placed; this pins the working tree's ``SUBMIT_RECORD_STATES``.
    """
    purchases = repositories["purchases"]
    job = _job_at(
        repositories,
        snapshot,
        rules,
        *_TO_FINAL,
        PurchaseState.AWAITING_CONFIRMATION,
    )
    attempt = purchases.begin_attempt(job.id)
    purchases.transition(job.id, PurchaseState.SUBMITTING)

    purchases.mark_submitted(attempt)
    assert purchases.has_submitted(job.id)


# ---------------------------------------------------------------------------
# Defect 1 -- a BaseException kills the worker thread and orphans the queue
# ---------------------------------------------------------------------------


class _StubManager:
    """Stands in for BrowserManager: starts, stops, installs nothing."""

    def __init__(self, *, fail: AppError | None = None) -> None:
        self.fail = fail
        self.stopped = False

    def chromium_installed(self) -> bool:
        return True

    def install_chromium(self, on_progress: Any = None) -> None:  # pragma: no cover
        return None

    def start(self) -> None:
        if self.fail is not None:
            raise self.fail

    def stop(self) -> None:
        self.stopped = True

    def close_extra_pages(self) -> None:
        return None

    def page(self) -> Any:  # pragma: no cover - not used here
        raise AssertionError("no page in these tests")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: app/automation/browser_worker.py:341 catches Exception, not "
        "BaseException, so a BaseException raised inside a task escapes "
        "_execute and _loop, the thread dies, and every task queued "
        "afterwards is never served and never signalled."
    ),
)
def test_worker_survives_a_base_exception_in_a_task(app_paths: AppPaths) -> None:
    """One task raising a BaseException must not stop the queue."""
    worker = BrowserWorker(app_paths)
    worker._manager = _StubManager()  # type: ignore[assignment]

    served: list[str] = []
    second_ran = threading.Event()

    def explode(_session: Any) -> None:
        raise KeyboardInterrupt("not an Exception subclass")

    def ordinary(_session: Any) -> None:
        served.append("second")
        second_ran.set()

    worker.submit("explodes", explode)
    worker.submit("ordinary", ordinary)
    worker.shutdown()

    thread = threading.Thread(target=worker.run, name="worker-under-test")
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "worker thread did not finish"
    assert second_ran.is_set(), "the task queued after the failure never ran"
    assert served == ["second"]


def test_wrap_unexpected_is_typed_for_baseexception_but_never_given_one() -> None:
    """The helper's own signature shows the intent the except clause misses."""
    signature = inspect.signature(BrowserWorker._wrap_unexpected)
    assert signature.parameters["exc"].annotation == "BaseException"

    source = inspect.getsource(BrowserWorker._execute)
    assert "except Exception as exc" in source
    assert "except BaseException" not in source


# ---------------------------------------------------------------------------
# Defect 2 -- the browser-unavailable state is immediately overwritten
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: app/automation/browser_worker.py:291 sets IDLE unconditionally "
        "after the NEEDS_BROWSER/FAILED branch at 281-285, so the status bar "
        "reports 'Ready' when the browser could not be prepared and "
        "MainWindow._on_worker_state's NEEDS_BROWSER message (main_window.py:"
        "481) is unreachable."
    ),
)
def test_worker_reports_a_browser_problem_rather_than_ready(
    app_paths: AppPaths,
) -> None:
    worker = BrowserWorker(app_paths)
    worker._manager = _StubManager(  # type: ignore[assignment]
        fail=AppError(ErrorCode.BROWSER_UNAVAILABLE)
    )
    states: list[str] = []
    worker.state_changed.connect(states.append)

    worker.shutdown()
    worker.run()

    assert WorkerState.NEEDS_BROWSER.value in states
    assert WorkerState.IDLE.value not in states


# ---------------------------------------------------------------------------
# Defect 3 -- the preparation error path runs twice
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: app/purchasing/purchase_service.py:386-388 handles the error and "
        "then re-raises, so BrowserWorker emits `failed` and _on_failed "
        "(line 949) handles the same job a second time, overwriting the "
        "NEEDS_USER the first pass chose with FAILED and making every "
        "NEEDS_USER resume transition unreachable."
    ),
)
def test_needs_user_survives_the_worker_failure_signal(
    service: PurchaseService,
    repositories: dict[str, Any],
    snapshot: ProductSnapshot,
    rules: PurchaseRules,
) -> None:
    """A verification challenge must leave the job waiting for the user."""
    from app.automation.cart_manager import IsolationJournal

    job = _job_at(repositories, snapshot, rules, PurchaseState.PRODUCT_CHECK)
    product = repositories["products"].get(job.product_id)
    error = AppError(ErrorCode.VERIFICATION_REQUIRED)
    assert error.needs_user

    service._handle_preparation_error(
        None,  # type: ignore[arg-type]
        job.id,
        product,
        error,
        IsolationJournal(),
    )
    assert repositories["purchases"].get(job.id).state is PurchaseState.NEEDS_USER

    # What BrowserWorker._execute does with the re-raised AppError.
    service._on_failed(1, error, {"kind": "purchase", "job_id": job.id})

    assert repositories["purchases"].get(job.id).state is PurchaseState.NEEDS_USER


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: the same double handling makes purchase_uncertain fire twice for "
        "one job, so MainWindow._on_purchase_uncertain (app/ui/main_window.py:"
        "677) shows the 'did the order get placed?' dialog twice and writes "
        "two activity entries."
    ),
)
def test_uncertain_outcome_is_raised_with_the_user_once(
    service: PurchaseService,
    repositories: dict[str, Any],
    snapshot: ProductSnapshot,
    rules: PurchaseRules,
) -> None:
    from app.automation.cart_manager import IsolationJournal

    job = _job_at(
        repositories, snapshot, rules, *_TO_FINAL, PurchaseState.SUBMITTING
    )
    product = repositories["products"].get(job.product_id)
    prompts: list[int] = []
    service.purchase_uncertain.connect(prompts.append)

    error = AppError(ErrorCode.TIMEOUT)
    service._handle_preparation_error(
        None,  # type: ignore[arg-type]
        job.id,
        product,
        error,
        IsolationJournal(),
    )
    service._on_failed(1, error, {"kind": "purchase", "job_id": job.id})

    assert prompts == [job.id]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: app/purchasing/purchase_service.py:842 and :967 pass "
        "`products.get(...)`, declared `ProductRecord | None`, into "
        "_mark_uncertain, which dereferences product.display_title at line "
        "661. The job is moved to UNKNOWN first, so the AttributeError leaves "
        "it unresolved and the product permanently unbuyable."
    ),
)
def test_an_uncertain_outcome_is_reported_even_with_no_product_row(
    service: PurchaseService,
    repositories: dict[str, Any],
    snapshot: ProductSnapshot,
    rules: PurchaseRules,
) -> None:
    job = _job_at(
        repositories, snapshot, rules, *_TO_FINAL, PurchaseState.SUBMITTING
    )
    prompts: list[int] = []
    service.purchase_uncertain.connect(prompts.append)

    service._mark_uncertain(job.id, None, "the outcome could not be read")

    assert repositories["purchases"].get(job.id).state is PurchaseState.UNKNOWN
    assert prompts == [job.id], "the user was never asked what happened"


# ---------------------------------------------------------------------------
# Areas that hold up -- pinned so they stay that way
# ---------------------------------------------------------------------------


def test_close_all_really_closes_another_threads_connection(
    database: Database,
) -> None:
    """Shutdown does not leak the browser thread's SQLite connection."""
    opened: list[Any] = []

    def open_on_another_thread() -> None:
        opened.append(database.connection())

    thread = threading.Thread(target=open_on_another_thread)
    thread.start()
    thread.join()

    database.close_all()

    closed: list[bool] = []

    def probe() -> None:
        try:
            opened[0].execute("SELECT 1").fetchone()
            closed.append(False)
        except Exception:  # noqa: BLE001
            closed.append(True)

    probe_thread = threading.Thread(target=probe)
    probe_thread.start()
    probe_thread.join()
    assert closed == [True]


def test_close_current_thread_has_no_caller_in_the_application() -> None:
    """Dead code: the per-thread close helper is only ever used by tests."""
    callers = [
        path.name
        for path in APP_ROOT.rglob("*.py")
        if "close_current_thread" in path.read_text(encoding="utf-8")
        and path.name != "database.py"
    ]
    assert not callers, f"now called by {callers}; delete this test"


def test_worker_priority_queue_never_compares_tasks() -> None:
    """A priority tie is broken before the task object is reached.

    ``BrowserTask`` is not orderable, so a duplicate (priority, sequence) pair
    would raise inside ``PriorityQueue``. The monotonic sequence is what keeps
    that unreachable; this pins it.
    """
    with pytest.raises(TypeError):
        _ = BrowserTask(
            task_id=1, label="a", run=lambda _s: None, priority=Priority.INTERACTIVE
        ) < BrowserTask(
            task_id=2, label="b", run=lambda _s: None, priority=Priority.INTERACTIVE
        )

    pq: queue.PriorityQueue[tuple[int, int, Any]] = queue.PriorityQueue()
    pq.put((0, 1, object()))
    pq.put((0, 2, object()))
    assert pq.get()[1] == 1
    assert pq.get()[1] == 2


def test_no_qtimer_is_created_outside_the_gui_thread_modules() -> None:
    """The browser thread has no event loop, so it must own no QTimer.

    ``app/automation/`` is the only package that runs on that thread.
    """
    offenders: list[str] = []
    for path in sorted((APP_ROOT / "automation").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "QTimer" in text:
            offenders.append(path.name)
    assert not offenders, f"QTimer on a thread with no event loop: {offenders}"


# ---------------------------------------------------------------------------
# Layering invariants from docs/ARCHITECTURE.md
# ---------------------------------------------------------------------------


def _code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Source lines with comments and docstrings removed.

    A product name or a selector mentioned in prose is documentation, not a
    layering violation, so only real code is examined.
    """
    text = path.read_text(encoding="utf-8")
    lines = dict(enumerate(text.splitlines(), start=1))
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for number in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                lines.pop(number, None)
    return [
        (number, line)
        for number, line in sorted(lines.items())
        if not line.strip().startswith("#")
    ]


def _python_files() -> list[pathlib.Path]:
    return sorted(APP_ROOT.rglob("*.py"))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "INCONSISTENCY: docs/ARCHITECTURE.md says every CSS string lives in "
        "app/automation/selectors.py. Two modules build their own: "
        "checkout_manager.py:267 hard-codes ('.a-price .a-offscreen', "
        "'.a-price', '.a-color-price') and cart_manager.py:560 concatenates "
        "an unescaped [data-asin=...] attribute selector."
    ),
)
def test_only_the_selectors_module_holds_dom_selectors() -> None:
    markers = ("[data-", "a-price", "a-offscreen", ":has(", "nth-child", "css=")
    allowed = {APP_ROOT / "automation" / "selectors.py"}
    offenders: list[str] = []
    for path in _python_files():
        if path in allowed:
            continue
        for number, line in _code_lines(path):
            if any(marker in line for marker in markers):
                offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{number}")
    assert not offenders, f"DOM selectors outside selectors.py: {offenders}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "INCONSISTENCY: docs/ARCHITECTURE.md puts all SQL behind "
        "app/database/. SettingsService issues its own (config.py:249, :364) "
        "and the support bundle runs ad-hoc introspection queries with "
        "f-string table and column names (diagnostics/report.py:316, :333)."
    ),
)
def test_only_the_database_package_issues_sql() -> None:
    statements = (
        "SELECT ",
        "INSERT INTO",
        "UPDATE ",
        "DELETE FROM",
        "CREATE TABLE",
        "CREATE INDEX",
    )
    offenders: list[str] = []
    for path in _python_files():
        if "database" in path.parts:
            continue
        for number, line in _code_lines(path):
            if any(statement in line for statement in statements):
                offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{number}")
    assert not offenders, f"SQL outside app/database: {offenders}"


def test_the_product_name_appears_only_in_branding() -> None:
    """``BRAND`` is the only source of the product name in application code."""
    from app.branding import BRAND

    literals = (
        BRAND.display_name,
        BRAND.short_name,
        BRAND.data_folder_name,
        BRAND.aumid,
        BRAND.single_instance_key,
    )
    offenders: list[str] = []
    for path in _python_files():
        if path == APP_ROOT / "branding.py":
            continue
        for number, line in _code_lines(path):
            for literal in literals:
                if literal in line:
                    offenders.append(
                        f"{path.relative_to(APP_ROOT.parent)}:{number} -> {literal}"
                    )
    assert not offenders, f"hard-coded product name: {offenders}"


def test_automation_never_imports_the_decision_layer() -> None:
    """app/automation/ may not reach the guard, the services or the database.

    This is the claim that matters most in ``docs/ARCHITECTURE.md``: the
    automation layer does not know what a rule is, so it cannot decide to buy.
    """
    forbidden = (
        "app.purchasing.purchase_guard",
        "app.purchasing.purchase_service",
        "app.purchasing.product_service",
        "app.purchasing.validation",
        "app.purchasing.states",
        "app.monitoring",
        "app.database",
        "app.config",
        "app.ui",
    )
    offenders: list[str] = []
    for path in sorted((APP_ROOT / "automation").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            for name in names:
                if any(name.startswith(item) for item in forbidden):
                    offenders.append(f"{path.name}:{node.lineno} -> {name}")
    assert not offenders, f"automation reaches upwards: {offenders}"


def test_no_service_layer_imports_a_widget() -> None:
    """The non-UI layers stay free of Qt widgets and painting."""
    offenders: list[str] = []
    for package in ("purchasing", "monitoring", "automation", "database", "core"):
        for path in sorted((APP_ROOT / package).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if "QtWidgets" in node.module or "QtGui" in node.module:
                        offenders.append(f"{path.name}:{node.lineno} -> {node.module}")
    assert not offenders, f"widgets imported by a service: {offenders}"
