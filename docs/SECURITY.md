# Security review

Scope: what this application handles, what it deliberately cannot do, and the
checks that keep it that way. Every claim here is backed by a test or a
command whose output is shown.

## Threat model

The application runs on one person's PC, automates that person's own Amazon
account, and spends their money. The things that would actually hurt them:

| Risk | Consequence | Mitigation |
|---|---|---|
| Credentials captured or stored | Account takeover | The app never collects them; sign-in happens in Amazon's own page |
| Session cookies leaked via logs or a support bundle | Account takeover | Redaction at the handler boundary, plus an end-to-end test |
| Buying the wrong thing, or too much | Money lost | Purchase Guard, enforced identically in every mode |
| Buying the same thing twice | Money lost | Five independent protections, three in the schema |
| Buying unrelated cart contents | Money lost | Cart isolation, plus independent re-checks by the guard |
| Arbitrary code execution | Full compromise | No `eval`/`exec`/`pickle`, no remote code path, no update channel |
| Data exfiltration | Privacy | No telemetry, no analytics, no network access outside the browser |

## Credentials

**The application cannot collect an Amazon password.** There is no password
field anywhere in the UI, and no code path that types into one. Connecting an
account opens Amazon's own sign-in page in a visible browser window and waits
for the user; the resulting session lives in the browser profile, exactly as
it would in any browser.

Verified:

```
$ grep -rniE "(password|cvv|card_number|cardnumber|secret_key)\s*[:=]" app/ --include="*.py" \
    | grep -viE "(redact|sensitive|fragment|never|not store|_KEY_FRAGMENT)"
(no matches)
```

**The schema has nowhere to put a credential.** The only payment-related
column is `orders.payment_label`, which holds the masked description Amazon
itself renders (`Visa ending in 1234`). Checked mechanically:

```
$ python -c "... scan migration SQL for password|cvv|token|cookie|card_number|secret ..."
suspicious columns: ['submit_token    TEXT    NOT NULL UNIQUE,']
```

The single hit is `purchase_attempts.submit_token` — an idempotency token this
application generates itself with `secrets.token_hex(16)`. It grants no access
to anything and never leaves the machine.

## Redaction

`app/diagnostics/redaction.py` is the single implementation, used by the
logger, the automation diagnostics and the exported report. It is applied as a
**logging filter at the handler boundary**, not at each call site — so a
careless `logger.debug(page_content)` added later is still redacted.

It removes: values under any sensitive key name (`password`, `session-token`,
`at-main`, `ubid-main`, `csrf`, `cvv`, `otpcode`, `x-amz-*`, and 40 more),
JWT-shaped tokens, and card-shaped digit runs. URLs keep their scheme, host
and path; the **query string is always dropped**, because that is where Amazon
puts session and continuation material.

One deliberate exception: the Amazon order-number shape
(`NNN-NNNNNNN-NNNNNNN`) is preserved, because verifying an order is the entire
point of the confirmation step and the number grants no access on its own.

Tested at three levels:

- `tests/unit/test_redaction.py` — 36 tests, one per sensitive shape.
- `tests/integration/test_security_logging.py` — runs the real parser,
  classifier, cart reader and checkout reader, with the real logging
  configuration, against pages stuffed with credentials, then reads the log
  files off disk and asserts none of the planted values appear. It also
  asserts that useful information (the ASIN, the seller, the step names) *is*
  present, so the test cannot be satisfied by logging nothing.
- The same file asserts the exported diagnostic report contains none of them.

## The support bundle

`Settings → Diagnostics → Export diagnostic report` produces a zip. The report
is assembled and then passed through `redact_mapping` as a whole, so a field
added later cannot skip the filter. The browser profile directory is excluded
by path and cannot be reached even if the logs directory were redirected into
it. The report carries an `_excluded` key naming what was left out and why, so
the user can see the tool is not hiding anything.

## Code execution

No `eval`, no `exec`, no `pickle`, no `os.system`, no `shell=True`. The two
subprocess calls are fixed-argv lists with no shell:

- Playwright's own browser installer (`node.exe cli.js install chromium --no-shell`)
- `powershell -NoProfile -NonInteractive -Command <fixed script>` and
  `taskkill`, used only to find and end a browser process still holding the
  app's own profile directory after a crash.

There is no update channel, no plugin system and no path that loads code from
disk or the network at runtime.

## Network exposure

The only listener is a **named pipe** for single-instance activation
(`QLocalServer`), created with `UserAccessOption` so its ACL is restricted to
the current user, and with the pipe name salted with a hash of the username so
two users on one machine cannot collide. It is not a network socket.

No TCP listener, no HTTP server. The application makes no outbound network
requests of its own: `grep -rnE "requests\.|urllib\.request|httpx|aiohttp"` over
`app/` returns nothing. All network traffic is the browser, doing what the
user can see it doing.

Product thumbnails are worth spelling out, because showing a picture is the
sort of feature that usually smuggles in an HTTP client. It does not here:
the browser has already loaded the image in order to display the page, so
the thumbnail is a **screenshot of that element**
(`app/automation/image_capture.py`). No request is made, nothing is fetched
from an address the user did not visit, and the result is literally a picture
of what was on their screen.

## Anti-bot boundary

Deliberately absent, and this is a product decision rather than an
unimplemented feature:

- No CAPTCHA solving or bypass
- No fingerprint spoofing, user-agent forgery or `navigator.webdriver` masking
- No proxy rotation or address cycling
- No attempt to appear as anything other than browser automation

`selectors.py` and `login_detector.py` *detect* challenges so the program can
stop; when one appears, the browser window is brought forward and a human is
asked. `tests/integration/test_product_parser.py` asserts that a CAPTCHA
served at a product URL is classified as a CAPTCHA, not parsed for a price.

## Purchase safety

Covered in detail in [ARCHITECTURE.md](ARCHITECTURE.md). Summary of what is
enforced, and where:

| Protection | Enforced by |
|---|---|
| One live purchase per product | Partial unique index in SQLite |
| One submission per purchase job | Partial unique index in SQLite |
| One record per Amazon order number | Partial unique index in SQLite |
| Submission recorded *before* the click | Ordering in `CheckoutManager.submit` |
| Interrupted submission never resumed | `recover_interrupted()` at startup |
| Test mode cannot submit | `SubmitAuthorization.validate()` |
| A failed guard cannot submit | `SubmitAuthorization.validate()` |
| A changed total cannot submit | Re-read and compared inside `submit` |
| Automatic mode uses the same guard | One `check_final` call site |

The `TestSubmitBarriers` class in
`tests/integration/test_cart_and_checkout.py` drives the real checkout
manager against a real browser and asserts each barrier refuses, including
that `record_submission` was never called.

## Local data

Everything is under `%LOCALAPPDATA%\AmazonPurchaseBot\`, which is per-user and
not world-readable by default. The database is not encrypted: it contains a
watch list, a price history and masked payment labels, and encrypting it with
a key stored beside it would be theatre. The genuinely sensitive artefact is
the **browser profile**, which is protected exactly as any browser profile on
that machine is — by the user's Windows account.

`.gitignore` excludes `browser/`, `*amazon-profile*`, `**/Default/Cookies*`,
`data/`, `logs/` and the build output, so a profile cannot be committed even
if `APB_DATA_DIR` were pointed inside the repository.

Product thumbnails are cached in `images/`, one small PNG per product,
photographed from the page as described above. Deleting the folder costs a
placeholder until the next check. They are not included in the exported
diagnostic report.

Rolling backups of the database live in `backups/`, and a database that could
not be opened is moved aside as `app.db.damaged` when the user accepts the
offer to restore a backup, rather than being deleted. Both hold the same
content as the live database -- watch list, price history, masked payment
labels -- and neither holds a credential. They stay inside the same per-user
directory.

## Screenshots

Failure screenshots are opt-in (on by default, switchable off), stored under
`screenshots/`, listed in Settings and deletable from there. Their URL and
metadata are redacted before the row is written. They are never included in
the exported report — only their filenames are.

## Privacy

No telemetry, no analytics, no crash upload, no phone-home, no account, no
server. Nothing leaves the machine. Any future telemetry would have to be
explicit opt-in; there is no infrastructure for it and none is planned.

## Known residual risks

1. **Screenshots of a checkout page may contain the user's own delivery
   address and masked payment label.** They are local, opt-in and deletable,
   but a user who emails one has shared that. The export deliberately excludes
   them.
2. **The build is unsigned.** Windows SmartScreen will warn on first run.
   Signing with an Authenticode certificate is the fix and is a distribution
   decision, not a code change. A version resource is embedded to improve
   reputation slightly.
3. **A compromised machine is a compromised account.** The browser profile
   keeps the user signed in, by design. Anything with read access to that
   user's `%LOCALAPPDATA%` can use that session — which is equally true of
   Chrome, Edge and Firefox.
4. **`parse_money("-5")` returns +$5.00.** A leading minus is treated as
   punctuation, which is correct for scraped price text (a promotion row reads
   `-$10.00`) but means the parser is not a general-purpose numeric input
   validator. `MoneyField` rejects negative *user input* by checking the text
   before parsing.
