# Contributing to Cartwright

Thank you for looking. This is a small project with one unusual property that
shapes everything about how changes are reviewed: **it spends real money on a
website it does not control.** Please read the short version below before
opening a pull request.

## The one rule

**A change that makes the Purchase Guard more permissive needs a test proving
it still refuses what it used to refuse.**

`app/purchasing/purchase_guard.py` is the only thing in the program that can
authorise a purchase. It is a pure function — no I/O, no randomness, no
language model — which is what makes it exhaustively testable, and
`tests/unit/test_purchase_guard.py` is written as an attack suite: each test
tries to get a bad purchase authorised. If you relax a check, add the test
that fails without your relaxation and passes with it.

The same applies to the barriers in
`app/automation/checkout_manager.py`, which is the only module that can click
Amazon's order button.

## Ground rules that the test suite enforces

These are not style preferences; the suite fails if you break them.

- **Absent data fails.** A value that could not be read never becomes a
  default. `None` and `UNKNOWN` mean "not established", and every required
  check treats them as a failure.
- **No warn-and-continue.** There is no severity that lets a purchase through
  with a caveat.
- **All DOM selectors live in `app/automation/selectors.py`.** A static scan
  in `tests/unit/test_architecture_review.py` checks this.
- **All SQL lives under `app/database/`.** Same scan.
- **No widget calls `setStyleSheet`.** Colour comes from
  `app/ui/theme/stylesheet.py` via an `objectName` or a
  `role`/`severity`/`status` property, so the app keeps the native Windows
  drawing and follows the theme.
- **Colour is never the only signal.** Every status carries words too.
- **Nothing is logged that could be a credential.** Redaction happens at the
  logging handler, and `tests/integration/test_security_logging.py` plants
  real credential shapes in fixture pages and reads the log files back off
  disk to prove none of them appear.
- **Tests never reach Amazon.** The integration suite intercepts every
  request and answers it from `tests/fixtures/amazon_pages.py`; a request to
  an unregistered address fails the suite.

## Things this project will not accept

Deliberately absent, and not for want of effort:

- CAPTCHA solving or bypassing
- Fingerprint spoofing, user-agent forgery, `navigator.webdriver` masking
- Proxy rotation or address cycling
- Anything else whose purpose is to make the automation harder for Amazon to
  detect

When Amazon asks for a human, the program stops and asks the human. That is
the product, not a limitation of it.

## Getting set up

Windows, Python 3.12 or newer:

```bash
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -m pytest tests
```

`docs/BUILD.md` covers running from source, capturing screenshots of every
screen, and producing the installer.

## Making a change

1. **Run the suite first**, so you know it was green before you started.
2. Keep the surrounding style: full-sentence docstrings and comments that
   explain *why* rather than what, and British spelling in prose.
3. Add tests next to the ones for the thing you changed.
4. If you touched anything user-visible, run
   `scripts/capture_screens.py` and **look at the images**. Several defects in
   this codebase were found only by looking at a screenshot.
5. Say in the pull request what you verified and how. "Tests pass" is not the
   same as "I checked this does what I think".

## Reporting Amazon changes

If Cartwright stops reading something correctly because Amazon changed a
page, that is the most useful kind of issue. Please include:

- what the app showed versus what the page showed;
- the relevant part of `%LOCALAPPDATA%\Cartwright\logs\app.log` — it records
  when a selector fallback was used, which usually points straight at the
  chain that needs a new candidate;
- the shape of the markup if you can see it, with any personal details
  removed.

Please do **not** include cookies, session tokens, order numbers or
addresses. The exported diagnostic report is already redacted and never
contains them; if you paste raw HTML, check it yourself first.

## Security

Please do not open a public issue for a vulnerability. See
[SECURITY.md](SECURITY.md).
