# Test report

Generated for version 1.0.1 on Windows 11 Pro (26200), Python 3.14.6,
PySide6-Essentials 6.11.2, Playwright 1.63.0 (Chromium build 1243).

## Summary

```
1191 tests collected
1122 passed, 69 skipped in 174s
```

The 68 skips are all from one parametrised guard test
(`test_logging_contract.py::test_every_extra_key_survives_a_real_log_call`),
which skips per-module when a module does no structured logging. Nothing is
skipped for being broken or unfinished, and there are no `xfail`s: a defect
proof either describes something that is still broken (and would be listed
under "Still open" below) or has been rewritten as a regression test.

| Area | Tests |
|---|---|
| Theme, contrast, icons | 149 |
| Logging contract (static scan) | 104 |
| Widget library | 93 |
| Notifications | 78 |
| Purchase Guard | 71 |
| Repositories and duplicate protection | 62 |
| Service orchestration (purchase and monitor) | 53 |
| Money and price parsing | 50 |
| Cart and checkout (real browser) | 48 |
| Product parsing and page classification (real browser) | 41 |
| Health checks | 37 |
| Watch decisions, pacing, clock jumps | 36 |
| Redaction | 36 |
| Windows integration | 33 |
| Activity and settings pages | 30 |
| Database, migrations and diagnostics queries | 30 |
| Adversarial purchase-safety review | 29 + 7 (real browser) |
| Diagnostic report | 27 |
| Settings | 22 |
| Purchase state machine | 20 |
| Architecture review (static and behavioural) | 16 |
| Window behaviour and database recovery | 11 |
| Source hygiene and quantity wordings | 11 |
| Security: logging and export (real browser) | 7 |

28,500 lines of application code across 102 modules; 14,200 lines of tests.

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
| Packaged app launches | Window titled "Cartwright", 110 MB working set |
| Packaged app drives the browser | Process tree `Cartwright.exe → node.exe → 8× chrome.exe` |
| Packaged app after a hard kill | Restarted cleanly; no migration re-run; browser started again |
| Installer build | 56 MB `Setup.exe` |
| Unattended install | Exit 0 in 5.1 s, no admin prompt, 217 MB in `%LOCALAPPDATA%\Programs` |
| Start Menu AppUserModelID | `Get-StartApps` reports `Cartwright.Desktop` |
| Installed copy runs | Window created, AUMID registered, browser started, no errors |
| Unattended uninstall | Exit 0 in 8.1 s; program and shortcuts removed; **user data kept**; `AppUserModelId` and `Run` keys removed |
| Every screen and dialog, light and dark | 30 PNGs in `docs/screens/`, reviewed individually |
| Clean shutdown after the review fixes | Packaged app closed from its window: orderly shutdown, exit 0, **0** `chrome.exe` left |
| Inline error hint, both themes | Captured and reviewed: red and semibold, not the muted grey of ordinary guidance |

## Driven against live amazon.com

On 2026-09-16 the application was run end to end against the real site, in
Test Mode, on a signed-in account. Test Mode is enforced inside
`CheckoutManager.submit`: the order button is located and reported, never
clicked. **No order was placed at any point, and nothing was added to the
user's cart** -- the item offered Buy Now, which is a separate one-item
checkout.

The final run passed every check:

| Check | Result on the live page |
|---|---|
| Product | ASIN matched |
| Version | Colour and size read from the picker |
| Quantity | Not printed by Amazon at all; confirmed from the item subtotal |
| Condition | New, from the active buy-box row |
| Seller | Amazon.com |
| Item price | At the limit |
| Only your item | One line |
| Order total | Read, and within the limit |
| Delivery address | Read |
| Payment method | Read (card, masked by Amazon to its last four digits) |
| Order button | Located, and **not clicked** |

Getting there took six defects, each found by the run and fixed:

1. **Every new item was refused.** Amazon prints no condition for a new
   offer, and the parser's "no used wording anywhere" heuristic found the
   *used* offer Amazon advertises inside the same buy box. The condition now
   comes from the active accordion row -- Amazon's own statement of which
   offer is selected -- so "Buy New" reads as New.
2. **Variations were not read.** Today's inline picker has no
   `.a-form-label`; the label is a secondary-colour span and the value is
   `.inline-twister-dim-title-value`. The old chain used the value as the
   label, so it was discarded as an unrecognised dimension.
3. **A phantom list price.** `.a-text-price` is also the class on the
   per-unit price, so a $19.99 item reported a "was" price of $3.33. The
   per-unit markers are excluded, and a list price at or below the real
   price is now ignored.
4. **The checkout could not be read at all.** Buy Now goes to Amazon's
   current pipeline (`/checkout/p/...?pipelineType=Chewbacca`), whose summary
   is a list rather than a table, whose address has its own element, whose
   payment panel is mostly inline JSON, and whose line row is identified by a
   class with a base64 id and carries its ASIN on a descendant. Every
   selector for those was written for the older layout and matched nothing.
5. **One order read as six line items**, because the `checkout-item-block`
   ids wrap a promo panel, a title span and a gift-options link.
6. **The suggested order limit could not survive tax.** It was the item
   price exactly, so a $19.99 item with $1.45 of tax blocked on a limit the
   user never chose. The suggestion now carries a 20% allowance, shown in
   the editor where it can be lowered.

Two findings that were not defects:

* **Amazon asked for a security check during sign-in.** The app paused,
  showed the browser and waited for the human, which is what it is built to
  do. It then detected the signed-in session by itself.
* **Buy Now does not open the Turbo iframe on this account.** That layout
  exists and is still supported -- the tests drive a real cross-document
  frame -- but it is not what Amazon served.

The live layout is now covered by `TestCurrentCheckout` in
`tests/integration/test_cart_and_checkout.py`, against a fixture copied from
the real page, so none of the six can come back unnoticed.

### Two more products, and a seventh defect

One product proves one layout, so the rehearsal was repeated.

**A consumable sold by Amazon** (36 alkaline batteries, $13.70) passed every
check first time: a different variation dimension ("Size: 36 Count"),
Subscribe & Save present on the page and correctly reported as *not*
pre-selected, $0.99 of tax inside the suggested allowance, order button
located and not clicked.

**An item sold by a third party** (a case, $16.99, fulfilled by Amazon) was
correctly **blocked** by the seller rule -- but reported the seller as
**"Learn more about the seller"**. Signed in, `#sellerProfileTriggerId` is a
link with that label and the name sits in the offer-display feature instead;
signed out, the same id holds the name, which is why no earlier check caught
it. Under "Amazon only" this blocked either way, but under a manufacturer or
approved-seller rule that string would have been *stored as the expectation*
every future purchase was compared against.

Fixed in two ways, because reordering alone would not survive the next A/B
test: the chain now prefers the element that actually holds the name, and
`PageReader.text` gained an `accept` predicate so a candidate that matches an
element but yields a control's label is treated as a miss and the chain
continues. Re-run live, the same purchase now blocks with "expected Amazon
only, found Aproca Direct".

That change had a consequence worth recording: walking past a candidate that
yields nothing means the reader can walk into the *heading* above the value,
and a fixture caught it immediately -- an empty address element started
reading as "Shipping address". Headings are now rejected as values for both
the address and the payment method, with a test for each.

### A third-party purchase, and what it revealed about identity

The fourth rehearsal used an **approved-seller rule** -- the rule that exists
precisely so a non-Amazon seller can be bought from, and therefore the one
where a mis-read seller does real damage. It passed, but only after two
findings that change what the guard can claim.

**Amazon's checkout does not print the item code.** The third-party order
carried no `data-asin` anywhere in the line: not on the row, not on a
descendant, not in a product link. Only a line-item id, a quantity-update URL
and the seller's profile link. Worse, this means the earlier "full pass" was
luckier than it looked -- that order had an ASIN only because a Subscribe &
Save upsell inside the row happened to carry one. Without it, no purchase on
this layout could ever have been authorised.

The guard now identifies the line by **name** when there is no code, and the
fallback is deliberately narrow: exactly one line in the whole order, that
line carrying no code of its own, and its title equal to the product page's
title once normalised. A second line, a line bearing a different code, a
missing title or a title that merely *starts* the same all fail. The report
says which happened -- "matched by name; Amazon did not show the item code"
-- rather than letting the user assume the code was checked.

**The check that compensates for it was skipping.** "Seller on the order"
exists because the buybox can change hands between reading the product page
and reaching the checkout, and it is the strongest control left when identity
rests on a name. It was reading `lines_for(asin)`, which is empty when there
is no ASIN, so it skipped -- and a fixture proved the consequence: an order
whose line named a seller **not** on the approved list passed the guard. It
now reads the line the identity check resolved, and that order blocks with
`SELLER_NOT_ALLOWED`.

Both are covered: nine unit tests on the boundary of the name fallback, and
three integration tests against a third-party fixture copied from the live
row -- including the seller substitution that used to pass.

Re-run live, the third-party purchase passes every check, identified by name,
with "Seller on the order: Aproca Direct" confirmed from the order itself.

### An order that charges postage

Every order so far had free delivery, so the shipping row had never carried a
value. The fifth rehearsal used a $26.99 item with **$4.49 delivery**. The
summary parsed exactly -- $26.99 + $4.49 + $2.29 tax = $33.77 -- and three
more things came out of it.

**The brand goes in front of the title.** The product page read "GQZMBM 16
Pcs/lot ..." and the checkout line read "JINSUO GQZMBM 16 Pcs/lot ...", so
the name match failed and the order was refused. The identity check now also
accepts the title with this product's brand prefixed -- a second exact form,
not a prefix rule, so "Case for <title>" and "Acme <title>" both still fail.

**The suggested order limit did not cover postage.** $33.77 against a
suggested $32.39: the 20% allowance covers tax but not tax plus delivery, so
a user accepting the defaults would have every such purchase refused. Amazon
states the delivery charge on the product page, so the suggestion now adds
it. The parsing is deliberately narrow -- a price immediately before the word
"delivery" or "shipping", and never when the text begins "FREE" -- because
the same field also says "FREE delivery ... on orders over $25", and reading
that $25 as postage would raise the limit by the one number in it that is not
a cost.

**Prime was reported for an item that plainly is not Prime.** A page-wide
`i.a-icon-prime` candidate matched an advert's icon, so a $4.49-postage item
arriving in a month read as Prime-eligible. Under the default rules nothing
uses that field, but a user who switches on "Prime only" would have had the
rule quietly satisfied by an advert. The page-wide candidates are marked
loose now, and a loose match reports "could not tell", which *blocks* when
Prime is required.

Re-run live, that order passes every check: identified by name against the
brand-prefixed title, postage inside the suggested limit, and Prime reported
as unknown rather than as yes.



## The Windows surface, checked on this machine

Three of the "never run on real hardware" items were driven for real. What
they establish, and what they do not:

| Check | Result |
|---|---|
| AUMID registration | `register_aumid()` returns True and writes `HKCU\SOFTWARE\Classes\AppUserModelId\Cartwright.Desktop`; the installed Start Menu shortcut carries the same id (read back with `System.AppUserModel.ID`) |
| Sending a toast | The real `Notifier` was run outside the test suite, against real WinRT. `self_test()` and a real `purchase_completed` toast were both accepted, and both are recorded in `notification_events` as delivered by toast |
| A toast **banner on screen** | **Not seen.** Eight screen captures at 1.2 s intervals show no banner, and `ToastNotificationManager.history` holds nothing for the app |
| Control experiment | The same library sent a toast under PowerShell's own well-known AppUserModelID -- an identity Windows already trusts. It behaved identically: accepted, no banner, nothing in history. **This machine is not displaying toast banners at all**, so the app's result says nothing about the app |
| Start with Windows | `startup.enable()` writes the `Run` value with the right command; `is_enabled()` reads it back; a `StartupApproved` blob of `03 00 …` is correctly read as "Turned off in Windows Settings" and `02 00 …` as "On"; `disable()` removes both. Done against the **real** `HKCU` keys, then cleaned up |
| Tray icon | Launching the app creates a real notification-area entry: `HKCU\Control Panel\NotifyIconSettings` gains a row whose `ExecutablePath` is the application's executable. It is not promoted out of the overflow flyout, which is Windows' default for a new application, so its **appearance** is still unconfirmed |
| Dark title bar and window chrome | **Confirmed.** The packaged build (the same folder the installer copies) was captured on real hardware in dark mode: the caption bar is dark with light text and the application icon, matching the window body (`docs/screens/real-installed-titlebar-dark.png`, `real-installed-dashboard-dark.png`) |

What this does *not* prove: that a user will see a toast on a machine with
notifications enabled, that clicking a toast button reaches
`Notifier.action_invoked`, or that the tray icon looks right. The first two
need a machine that shows banners; the notifier's own wording already tells
the user to check Windows Settings when nothing appears, which is exactly the
situation this machine is in.

A real logon with "Start with Windows" enabled, and the tray icon's recovery
after an Explorer restart, were deliberately not performed: both disturb the
desktop session of whoever is at the machine.

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

8. **Every real purchase would have failed at the moment of submission.**
   The purchase service transitions a job to `SUBMITTING` *before* the click
   (which is what lets crash recovery detect an interrupted submission), but
   `PurchaseRepository.mark_submitted` only accepted the two pre-submit
   states, so the very first thing the real submit path did was raise
   "the app was not in a state where submitting is allowed". Nothing would
   ever have been ordered.

   This survived 880 tests because the integration tests exercise
   `CheckoutManager.submit` with their *own* `record_submission` callback (a
   list append), so the real repository call was never on the path. It was
   caught within minutes of writing `tests/unit/test_services.py`, whose
   whole purpose is to test the wiring rather than the parts. Fixed by adding
   `SUBMIT_RECORD_STATES = SUBMIT_ENTRY_STATES | {SUBMITTING}`, which weakens
   nothing because `SUBMITTING` is only reachable from those entry states in
   the first place.

   The lesson is recorded here deliberately: component tests with injected
   doubles can all pass while the assembled system cannot do its job.

9. **A test asserted the wrong safety semantics.** A test expected that
   turning the price *trigger* off would allow a purchase above the maximum
   item price. The code correctly refused. The test was wrong and is now
   replaced by one that documents the rule: a trigger decides *when*, a limit
   decides *whether*.

10. **Turning Test Mode on mid-flight did not stop the order.** `_submit`
    hard-coded `test_mode=False` in the `SubmitAuthorization` and `confirm()`
    never re-read the setting, so a user who switched Test Mode on while the
    confirmation was on screen still got a real order. Test mode is now read
    from the live settings at the moment of submission; the cart is restored
    and the job ends `CANCELLED`. Regression test:
    `test_switching_test_mode_on_before_confirming_stops_the_order`.

11. **Crash recovery could free the product slot after a recorded
    submission.** `recover_interrupted` moved such a job to `FAILED` — which
    says "nothing was ordered", is terminal, releases the one-live-purchase
    index for that product, and never prompts. It now moves to `UNKNOWN` and
    is returned in the uncertain list, and `UNKNOWN` was added to the
    diversions every live state may take so recovery can always reach it.
    Regression test:
    `test_recovery_never_says_nothing_was_ordered_after_a_submission`.

12. **The `PRE_CHECKOUT` guard phase was dead in production.** The guard
    exposed `check_cart` and the architecture claimed three phases, but the
    adapter only did an ad-hoc "any foreign lines?" check inline and no
    caller ever ran the phase, so its report was never persisted and its
    quantity check never ran. `prepare_purchase` now takes an `on_cart_read`
    callback, the service runs `GUARD.check_cart` there — after the item is
    added, before the checkout is entered — and a failure blocks with the
    named rule and restores the cart. Four regression tests in
    `TestPreCheckoutGuard`.

13. **A mangled escape silently disabled every checkout quantity pattern.**
    An earlier patch applied through a shell heredoc turned the `\b` word
    boundaries in `CHECKOUT_QUANTITY_PATTERNS` into literal backspace
    characters (`0x08`). The module imported, the regexes compiled, and every
    one of them stopped matching — so a checkout line reading "Qty: 2"
    reported *no readable quantity*, which the guard correctly refuses to
    validate. Found when an integration test that had been asserting the
    broken behaviour was re-examined. Fixed at the byte level, and
    `tests/unit/test_source_hygiene.py` now scans every source file for stray
    control characters and asserts each pattern still matches the wording it
    exists for.

14. **`sum(line.quantity …)` over a nullable quantity.** Three call sites
    summed `CartLine.quantity`, which is deliberately `int | None`, so an
    unreadable quantity raised `TypeError` — in one case while recording an
    order that had just been placed. All three now use `CartLine.units`,
    which treats unknown as zero for arithmetic only.

15. **Three ways the shell could lose the user.** Declining "Close anyway"
    still accepted the close event (window gone, process alive, no tray icon
    on that path); a `--tray` start returned before connecting the
    single-instance activation signal, so a second launch could not raise the
    only window that was hidden; and a corrupt or read-only database escaped
    as a raw `sqlite3` error into the crash handler. All three are fixed, the
    last by typing the failure as `AppError(DATABASE_ERROR)` and offering the
    newest rolling backup at startup (the damaged file is kept, not deleted).
    Ten regression tests in `tests/unit/test_window_and_recovery.py`.

16. **The cart reader fabricated a quantity of 1.** `_row_quantity` returned
    1 whenever the control was missing or unreadable, on the reasoning that
    the guard would then compare it against the expectation. That holds only
    when the user asked for a different number: against the common rule
    "quantity 1" a fabricated 1 is reported as a **PASS on a number nobody
    observed** -- in the `PRE_CHECKOUT` phase that had just been brought into
    use. It now returns `int | None`, tries the form control then the
    wording, and `None` fails the check. Found by the review pass that was
    converting the defect proofs; two regression tests in
    `test_cart_and_checkout.py`.

17. **The manufacturer seller policy followed a page-supplied brand.**
    `purchase_rules` had no `brand` column, so `PurchaseRules.brand` -- what
    `amazon_or_manufacturer` compares a seller name to -- was re-derived on
    every check from `products.brand`, which is refreshed from the product
    page's byline. A listing whose byline *and* seller name both change (what
    a listing takeover looks like) would satisfy "Amazon or the manufacturer"
    for whatever the byline then claimed. Migration 2 stores the brand on the
    rule row, backfilled from the product each rule set belongs to, so the
    value is frozen when the user sets the rule up. Verified by upgrading a
    real v1 database created by the packaged application: version 1 → 2,
    integrity `ok`, and the packaged app then performed the same upgrade on
    its own data on next launch, taking a pre-migration backup first.

## Still open, and deliberately so

These were found by the review passes, are understood, and are **not** fixed.
They are here rather than in a backlog because each one is a limit on what
the guard can promise.

- **An unrecognised variation label becomes no expectation at all.**
  `ProductParser._add_dimension` drops labels outside
  `selectors.KNOWN_VARIATION_LABELS`, because Amazon puts non-dimension
  labels in the same markup and treating those as dimensions would block
  ordinary purchases. If Amazon renames a dimension *before* the user sets a
  rule up, the stored expectation is empty and `_check_variation` then skips
  for the life of that rule. The bound is the ASIN: each Amazon variation has
  its own ASIN and that comparison is exact, so the identity of the item is
  still pinned -- only the description of it is weakened. Pinned by
  `test_an_unknown_variation_label_becomes_no_expectation_at_all`, which is a
  green test asserting today's behaviour, not a wish.
- **`amazon_or_manufacturer` still trusts a name the page supplies**, just no
  longer a refreshed one. Freezing the brand (fix 17) closes the "byline
  changes later" hole; it cannot close "the byline was already wrong when the
  rule was created". `amazon_only` and `approved_list` do not have this
  property, and the rules editor recommends `amazon_only`.
- **Two bare HTML element names live outside `selectors.py`** (`"body"` in
  `page_reader.py`, `"option"` in `product_parser.py`). Neither is Amazon
  markup, so the layering scan allows them by name and its docstring says so.

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
- **`BrowserWorker` queueing, priority and cancellation, and `AppContext`
  startup, have no direct unit tests.** `PurchaseService` and
  `MonitorService` now do (`tests/unit/test_services.py`, 47 tests, against a
  fake worker that runs tasks synchronously). The worker's own threading and
  the context's startup order were verified by running the packaged
  application, which is observation rather than assertion.

See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) for the full list.

## Reproducing

```bash
.venv\Scripts\python.exe -m pytest tests                       # everything
.venv\Scripts\python.exe -m pytest tests/unit                   # fast, no browser
.venv\Scripts\python.exe -m pytest tests/integration            # needs Chromium
.venv\Scripts\python.exe scripts\capture_screens.py             # visual review
.venv\Scripts\python.exe scripts\build.py --clean --installer   # build both
```
