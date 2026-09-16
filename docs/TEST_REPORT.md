# Test report

Generated for version 1.0.0 on Windows 11 Pro (26200), Python 3.14.6,
PySide6-Essentials 6.11.2, Playwright 1.63.0 (Chromium build 1243).

## Summary

```
948 tests collected
880 passed, 68 skipped in 109s
```

The 68 skips are all from one parametrised guard test
(`test_logging_contract.py::test_every_extra_key_survives_a_real_log_call`),
which skips per-module when a module does no structured logging. Nothing is
skipped for being broken or unfinished.

| Area | Tests |
|---|---|
| Widget library | 93 |
| Theme, contrast, icons | 145 |
| Logging contract (static scan) | 102 |
| Notifications | 78 |
| Purchase Guard | 68 |
| Repositories and duplicate protection | 58 |
| Money and price parsing | 49 |
| Cart and checkout (real browser) | 46 |
| Product parsing and page classification (real browser) | 38 |
| Health checks | 37 |
| Redaction | 36 |
| Watch decisions, pacing, clock jumps | 36 |
| Windows integration | 33 |
| Diagnostic report | 27 |
| Database and migrations | 23 |
| Settings | 22 |
| Purchase state machine | 20 |
| Activity and settings pages | 30 |
| Security: logging and export (real browser) | 7 |

27,591 lines of application code across 100 modules; 9,647 lines of tests.

## How the risky things are tested

### Integration tests cannot reach Amazon

`tests/integration/conftest.py` installs a route handler on the browser
context that answers **every** request from
`tests/fixtures/amazon_pages.py`. A request to an address a test did not
register is answered with a 404 and recorded;
`test_every_request_is_intercepted` asserts the recorded list is empty.

These tests therefore drive real Chromium, real Playwright and the real
parsers, while being structurally incapable of placing an order.

### The Purchase Guard is tested as an attack suite

`tests/unit/test_purchase_guard.py` (68 tests) is written as attempts to get
a bad purchase authorised, not as coverage of happy paths:

- wrong ASIN, missing ASIN, changed variation, extra variation dimension
- seller names built to defeat "Amazon only": `Amazon.com Deals`,
  `AmazonBasics Store`, `Amazon Warehouse`, `Amazonn.com`, `Best Amazon Deals`
- `Used - Like New` against a New-only rule
- price exactly at the limit (passes) and one cent over (blocks)
- an item within its limit whose shipping pushes the order over its limit
- a missing price, a missing seller, a missing total, a currency mismatch
- a pre-selected Subscribe & Save, an attached protection plan
- a changed delivery address, a changed payment method
- Prime required but undeterminable

### The submit barriers are tested against a real browser

`tests/integration/test_cart_and_checkout.py::TestSubmitBarriers` drives the
real `CheckoutManager` against real Chromium and asserts the order button is
**not** clicked when:

- test mode is on
- the guard did not pass
- the order total differs from what was approved — higher *or lower*
- the total cannot be read at all
- the order button cannot be found

Two of those tests instrument the real DOM button with a click listener and
assert the ordering: `record_submission` fires **before** the click, and a
`record_submission` that raises prevents the click entirely.

### Duplicate protection is tested at the schema level

`tests/unit/test_database.py` and `test_repositories.py` attempt, through the
repository API:

- a second live purchase job for one product → refused
- a new purchase while a previous one is `UNKNOWN` → refused, with the
  "unconfirmed result" message
- a second submitted attempt for one job → refused
- marking the same attempt submitted twice → refused (this was a real bug:
  the `UPDATE` matched no rows and returned silently, so the repository now
  checks `rowcount` and raises)
- the same Amazon order number recorded twice → the existing row is returned
- a submission from a state where submitting is not allowed → refused

`TestCrashRecovery` moves jobs into `SUBMITTING` and `CONFIRMING`, runs
`recover_interrupted()`, and asserts they become `UNKNOWN`, that the product
stays blocked, and that only an explicit human resolution frees it.

`test_states.py` walks the whole transition graph to prove
`SUBMITTING` is unreachable from `UNKNOWN`, from `CONFIRMING`, and from
`SUBMITTING` itself.

### Redaction is tested end to end, not just as a string function

`tests/integration/test_security_logging.py` plants eight real credential
shapes (an `Atza|` session token, `at-main`, `ubid-main`, a password, a card
number, a CVV, a CSRF token, an OTP) into product, cart, checkout and
sign-in fixture pages, runs the real parsers and classifier with the real
logging configuration, then reads the log files off disk and asserts none of
the planted values appear.

It also asserts that the ASIN, the seller and the step names **do** appear —
so the test cannot be satisfied by logging nothing.

## Verified by running it, not by reading it

| What | Result |
|---|---|
| Headed Chromium launch through `BrowserManager` | Launched, parsed a fixture page, closed cleanly |
| Real first-run browser install | 437 MB downloaded into the app's own directory |
| Clean shutdown leaves no orphaned browser | 8 `chrome.exe` while running → **0** after `shutdown()`, in 0.1 s |
| PyInstaller build | 213 MB onedir, `--version` smoke test passed |
| Packaged app launches | Window titled "Amazon Purchase Bot", 110 MB working set |
| Packaged app drives the browser | Process tree `AmazonPurchaseBot.exe → node.exe → 8× chrome.exe` |
| Packaged app after a hard kill | Restarted cleanly; no migration re-run; browser started again |
| Installer build | 55 MB `Setup.exe` |
| Unattended install | Exit 0 in 5.1 s, no admin prompt, 217 MB in `%LOCALAPPDATA%\Programs` |
| Start Menu AppUserModelID | `Get-StartApps` reports `AmazonPurchaseBot.Desktop` |
| Installed copy runs | Window created, AUMID registered, browser started, no errors |
| Unattended uninstall | Exit 0 in 8.1 s; program and shortcuts removed; **user data kept**; `AppUserModelId` and `Run` keys removed |
| Every screen and dialog, light and dark | 28 PNGs in `docs/screens/`, reviewed individually |

## Bugs found by testing, and fixed

1. **Migration crashed the first real run.**
   `logger.info("Applying migration", extra={..., "name": ...})` — `name` is a
   reserved `LogRecord` attribute, so `makeRecord` raises `KeyError`. It was
   invisible because the default root level is WARNING, so `logger.info` never
   built a record; `AppContext` configures INFO *before* migrating, so a
   genuine first launch would have died. Fixed, and
   `tests/unit/test_logging_contract.py` now scans every `extra={...}` in
   `app/` for reserved keys and additionally makes a real log call per module.
   Found independently by the end-to-end security test and by a review pass.

2. **Backup connections were never closed.** `with sqlite3.connect(...)`
   commits but does not close, so on Windows the open handle made the file
   undeletable and backup pruning silently failed. Found by
   `test_backup_retention_prunes_old_files`.

3. **Chromium was never detected in a packaged install.** The detection glob
   looked for `chrome-win/chrome.exe`; current Playwright builds ship
   `chrome-win64/chrome.exe`. Found by the first real integration run.

4. **`extract_asin` accepted non-Amazon URLs.** An eBay link containing a
   10-character path segment yielded an "ASIN". Now the host must be an Amazon
   storefront, and `amazon.com.evil.net` is rejected.

5. **Ghost rows in the dashboard and watch list.** `takeAt()` +
   `deleteLater()` leaves a widget parented and painting at its stale
   geometry, so a list rebuilt twice before the event loop ran drew its old
   rows on top of the new ones. Found by looking at a screenshot, confirmed by
   measuring geometry and by a `grabWindow` capture of real composited pixels.
   Fixed with a shared `clear_layout()` that reparents before deleting.

6. **Navigation icons were invisible in dark mode.** `IconSet()` with no
   arguments fell back to the light token set, so glyphs were near-black on a
   dark background. Found by zooming into a dark-mode screenshot. Fixed at the
   source and by rebuilding the icon set on `theme_changed`.

7. **A silent uninstall hung forever.** The `[Code]` section used Inno's
   plain `MsgBox`, which ignores `/SUPPRESSMSGBOXES`; an unattended uninstall
   waited on a dialog nobody could click. Found by running the uninstall
   unattended. Fixed with `SuppressibleMsgBox` and re-verified.

8. **A test asserted the wrong safety semantics.** A test expected that
   turning the price *trigger* off would allow a purchase above the maximum
   item price. The code correctly refused. The test was wrong and is now
   replaced by one that documents the rule: a trigger decides *when*, a limit
   decides *whether*.

## What is NOT tested

Stated plainly, because it is the honest limit of this report.

- **No order has ever been placed on amazon.com.** Every checkout test runs
  against local fixtures. The selectors are drawn from current public research
  and arranged as fallback chains, but whether they match Amazon's live markup
  today is unverified.
- **The Buy Now / Turbo Checkout iframe path has never executed.**
- **No real Amazon sign-in, one-time code or CAPTCHA has been driven.**
  Detection is tested on fixtures; the handoff loop has never waited on a real
  challenge.
- **No real Windows toast has been shown.** The notifier is tested against a
  fake. `Settings → Notifications → Send a test notification` exists for this
  reason.
- **"Start with Windows" has never survived a real logon.** The registry
  read/write is tested against a scratch key.
- **The tray icon has never been seen in a notification area**, and the
  explorer-restart recovery has not been exercised.
- **Sleep/wake has not been exercised on real hardware.** The clock-jump
  detector is tested with simulated clocks.
- **High-DPI (125% / 150%) was not visually reviewed**, nor Windows 10.
- **`MonitorService`, `PurchaseService` orchestration, `BrowserWorker`
  queueing and `AppContext` startup have no direct unit tests.** Their parts
  are tested (the guard, the watcher decisions, the repositories, the state
  machine) and their integration was exercised by running the packaged
  application, but the wiring itself is covered by observation rather than by
  assertion.

See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) for the full list.

## Reproducing

```bash
.venv\Scripts\python.exe -m pytest tests                       # everything
.venv\Scripts\python.exe -m pytest tests/unit                   # fast, no browser
.venv\Scripts\python.exe -m pytest tests/integration            # needs Chromium
.venv\Scripts\python.exe scripts\capture_screens.py             # visual review
.venv\Scripts\python.exe scripts\build.py --clean --installer   # build both
```
