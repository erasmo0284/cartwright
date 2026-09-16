# Architecture

## The shape of the problem

This application spends a person's money on a website it does not control.
Two facts follow from that, and they drive every decision below.

**Amazon's pages change without notice.** Selectors break, layouts are
A/B-tested, and a page that looks right can be a CAPTCHA served with HTTP
200. So the automation layer is built to *stop and say so* rather than carry
on with a plausible-looking guess.

**A wrong decision costs real money.** So the code that decides whether to buy
is deterministic, pure, has no I/O, and is the only thing in the program that
can authorise a purchase. No language model, no heuristic, no "looks fine".

## Layers

```
                    ┌─────────────────────────────┐
                    │  UI (PySide6)               │  pages, dialogs, tray
                    └─────────────┬───────────────┘
                                  │ Qt signals
                    ┌─────────────▼───────────────┐
                    │  Services                   │  product, purchase,
                    │                             │  monitoring
                    └──────┬───────────────┬──────┘
                           │               │
         ┌─────────────────▼──┐     ┌──────▼──────────────────┐
         │  Purchase Guard    │     │  Browser worker thread  │
         │  (pure, no I/O)    │     │  owns Playwright        │
         └────────────────────┘     └──────┬──────────────────┘
                                           │
                                    ┌──────▼──────────────────┐
                                    │  Automation adapter     │
                                    │  parsers, cart, checkout│
                                    └──────┬──────────────────┘
                                           │
                                    ┌──────▼──────────────────┐
                                    │  selectors.py           │
                                    └─────────────────────────┘

              ┌──────────────────────────────────────────┐
              │  SQLite  (migrations, repositories)      │
              └──────────────────────────────────────────┘
```

Dependencies point downwards only. The guard does not know what a browser is;
the automation layer does not know what a rule is.

## The Purchase Guard

`app/purchasing/purchase_guard.py` is a pure function from *(rules, observed
state)* to a `GuardReport`. It has no imports from the automation layer, no
I/O and no randomness, which is why it can be tested exhaustively — the 68
tests in `tests/unit/test_purchase_guard.py` are written as an attack suite
that tries to get a bad purchase authorised.

Four rules govern it:

| Rule | Why |
|---|---|
| Absent data fails | A price that could not be read must not become "probably fine". |
| No warn-and-continue | There is no severity that lets a purchase through with a caveat. A required check that fails blocks. |
| The final total is re-checked last | Shipping, tax and fees only exist at checkout, so the order-total rule is enforced against the real total moments before submitting. |
| Nothing is substituted | A different ASIN, variation, seller or condition blocks rather than adapting. |

It runs in three phases — `PRE_CART`, `PRE_CHECKOUT`, `PRE_SUBMIT` — because
more becomes knowable as the flow progresses, and the last phase sees the
real order total.

**Assisted and automatic purchases call the same function with the same
rules.** There is no second, weaker path for automatic mode. This is the most
important structural property in the codebase.

## Purchase state machine

`app/purchasing/states.py` declares every legal transition in one table.

```
CREATED → PRODUCT_CHECK → RULE_VALIDATION → CART_PREPARATION → CHECKOUT
        → FINAL_VALIDATION → AWAITING_CONFIRMATION → SUBMITTING
        → CONFIRMING → CONFIRMED
```

Any live state may divert to `BLOCKED`, `FAILED`, `NEEDS_USER`, `CANCELLED`
or `UNKNOWN`.

Two properties are enforced by the table and asserted by tests:

- `SUBMITTING` is reachable **only** from `FINAL_VALIDATION` (automatic, after
  the guard passed) or `AWAITING_CONFIRMATION` (assisted, after a human acted).
- `UNKNOWN` — "submitted, outcome unreadable" — may transition **only** to
  `CONFIRMED` or `FAILED`, and only when a human says which. It can never
  return to `SUBMITTING`.

`test_states.py::test_resubmission_from_unknown_is_unreachable` walks the
whole reachability graph to prove the second one.

## Duplicate-order protection

Five independent mechanisms, at three different layers. Any one of them alone
would prevent a double order; they exist together because this is the failure
mode that actually costs money.

1. **Schema — one live purchase per product.** A partial unique index on
   `purchase_jobs(product_id)` over the non-terminal states. `UNKNOWN` counts
   as non-terminal on purpose, so an unresolved outcome blocks any new attempt
   on that product until a human resolves it.
2. **Schema — one submission per job.** A partial unique index on
   `purchase_attempts(purchase_job_id) WHERE submitted = 1`.
3. **Schema — one row per Amazon order number.** A partial unique index on
   `orders(amazon_order_number)`.
4. **Ordering — the submission is recorded *before* the click.** If the
   process dies mid-click, the record already exists, so startup recovery
   knows an order may exist. Recording afterwards would lose exactly the case
   that matters.
5. **Startup recovery.** Any job left in `SUBMITTING` or `CONFIRMING` becomes
   `UNKNOWN`, never resumed.

`SubmitAuthorization` ties it together: the only way to reach the click is to
hand the checkout manager an object carrying the approved total, proof the
guard passed, and that test mode is off. It re-validates all of them, re-reads
the checkout, compares the total, and only then calls `record_submission()`
and clicks.

## Cart isolation

Amazon's "Proceed to checkout" takes the **whole** active cart, so buying one
item out of a full cart is a real hazard. Three strategies, in order:

1. **Buy Now** — a separate one-item checkout that bypasses the cart. Preferred
   whenever available, because it makes the user's cart irrelevant.
2. **Empty cart** — nothing else is there.
3. **Set aside the others** — only with explicit permission, journalled before
   each move so an interruption can be undone, then restored afterwards.

If none applies, the purchase stops. The guard independently re-checks cart
and checkout contents, so a failure of the isolation logic still cannot
result in buying someone's groceries.

Two markup details that a naive implementation gets wrong, and which are
handled explicitly:

- The cart page renders an "Items you may like" strip whose elements also
  carry `data-asin`. A page-wide scrape returns products for a **completely
  empty cart**, so lines are read only inside the active-items container.
- On a genuinely empty cart the subtotal elements are absent from the DOM
  rather than present-and-blank, so a missing subtotal is an expected state.

## Threading

Playwright's synchronous API is not thread-safe and its objects belong to the
thread that created them, so **one dedicated thread owns the browser**, fed by
a priority queue (`app/automation/browser_worker.py`).

The technical reason is Playwright's constraint. The better reason is that it
makes concurrency impossible by construction: a scheduled price check cannot
interleave with a checkout, and two checkouts cannot overlap.

That thread has no Qt event loop — it is blocked in `queue.get()` or inside a
Playwright call — so it cannot be driven with slots. Work goes in through the
thread-safe `submit()`; results come back as Qt signals, which Qt delivers to
the GUI thread automatically.

Cancellation is cooperative: tasks check in at each named step (which is also
what produces the "Reading price…" progress messages). There is no safe way
to interrupt a blocked Playwright call from another thread, so per-action
timeouts are kept short (15 s) to bound the worst case.

SQLite is reached from several threads via a connection per thread, WAL mode
and a process-wide write lock. Contention is negligible (one browser
operation at a time), and serialising writes removes any chance of a lost
update on purchase state.

## Selectors and page classification

Every CSS string in the program lives in `app/automation/selectors.py`. Each
logical element is a `SelectorChain` of ordered candidates; the resolver tries
them in order and logs when a fallback was needed, so a layout change becomes
a visible signal rather than a silent wrong answer. This is the structure of
Stagehand's self-healing idea without the model: an ordered chain plus
telemetry when the preferred candidate stops matching.

**HTTP 200 does not mean success.** Amazon serves CAPTCHA pages, "something
went wrong" pages and sign-in redirects with a 200 status, so every page load
is classified from its content and URL by `login_detector.py` before anything
is read. Interruptions are checked *before* content types, because a CAPTCHA
can be served at a product URL.

## Money

Integer minor units throughout (`app/core/money.py`). No float ever touches an
amount.

Price parsing resolves every ambiguity in the direction that **blocks**:
`parse_money` refuses text containing two different prices;
`parse_money_ceiling` returns the largest value found, so a mis-read can only
make the program refuse to buy, never overspend.

The one bug this module exists to prevent: Amazon splits the visible price
across `<span class="a-price-whole">109.</span>` and
`<span class="a-price-fraction">97</span>`. Concatenating the text nodes gives
`10997` — a hundredfold error. The parser prefers the `.a-offscreen`
screen-reader span (one clean value) and only falls back to
`parse_split_price`, which handles the two nodes explicitly.

## Errors

`app/core/errors.py` is a closed catalogue: 39 codes, each with a
plain-English title, an explanation and concrete next actions. `describe()`
raises for an unregistered code, so a missing entry fails in tests rather than
degrading to a generic message in front of a user. There is no code path that
puts a raw exception message on screen.

## Data

Everything lives under `%LOCALAPPDATA%\AmazonPurchaseBot\`:

```
data/app.db                 SQLite, WAL
browser/amazon-profile/     the dedicated Chromium profile (the session)
browser/playwright/         the downloaded browser (~430 MB)
logs/                       rotating text + JSONL
screenshots/                failure diagnostics, user-deletable
backups/                    rolling database backups
config/                     reserved for user-exported configuration
```

Schema changes go through numbered migrations
(`app/database/migrations/`). Each runs in its own transaction, and because
`executescript` issues an implicit COMMIT, migrations are split with
`sqlite3.complete_statement` and executed statement by statement so a failure
rolls back rather than leaving half a schema.

No repository issues DDL.

## What is deliberately absent

- No CAPTCHA solving, anti-bot evasion, fingerprint spoofing or proxy
  rotation. When Amazon asks for a human, the app pauses, shows the browser
  and waits.
- No LLM anywhere in the purchase decision.
- No server, no account system, no telemetry, no crash upload.
- No storage of passwords, one-time codes, passkeys or card numbers. The only
  payment-related field in the schema is `payment_label`, which holds the
  masked description Amazon itself displays.
