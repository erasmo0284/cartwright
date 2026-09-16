# Amazon Purchase Bot

A Windows desktop utility that watches a product on Amazon and — when the
conditions you set are met — tells you, or buys it.

It runs entirely on one PC. No server, no account, no telemetry. It automates
*your own* Amazon account in its own private browser window, and it never
stores your password, your one-time codes or your card details.

> Independent tool. Not affiliated with, produced by or endorsed by
> Amazon.com, Inc.

---

## What it does

- **Inspect** — paste a product link; read the price, seller, condition,
  exact variation, availability and delivery estimate.
- **Rules** — maximum item price, maximum complete order total, quantity,
  allowed sellers, acceptable condition, exact variation.
- **Watch** — check on a schedule and notify you, or buy automatically.
- **Purchase Guard** — a deterministic gate that must pass before any order.
  Identical in assisted and automatic mode.
- **Test Mode** — goes all the way to Amazon's order button and stops. On by
  default for a new installation.

For how to use it, see **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)**.

---

## Product philosophy

The purchasing engine is deterministic. There is no model, no heuristic and
no "looks about right" anywhere in the decision to spend money. Every
purchase must satisfy, explicitly:

```
Exact product (ASIN)          Allowed seller
Correct variation             Required condition
Correct quantity              Maximum item price
Correct delivery address      Maximum complete order total
Expected payment method       No unintended cart items
No unexpected add-ons         No unintended subscription
```

Anything that cannot be read is a failure, not an assumption. Anything that
changed is a block, not an adaptation.

---

## Requirements

- Windows 10 or later (built and tested on Windows 11)
- ~1 GB free disk: ~215 MB application, ~430 MB browser downloaded on first run

---

## Installing (for the person using it)

Run `AmazonPurchaseBot-1.0.0-Setup.exe`. It installs per-user, so Windows will
not ask for an administrator password. On first launch the app downloads the
browser it uses; this happens once.

---

## Development

```bash
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe scripts\install_browser.py
```

Run it:

```bash
.venv\Scripts\python.exe -m app.main
```

Run the tests:

```bash
.venv\Scripts\python.exe -m pytest tests
```

880 tests. The integration tests drive a real Chromium against local fixture
pages, with every request intercepted, so they cannot reach Amazon and cannot
place an order.

Capture every screen and dialog as PNGs, for visual review:

```bash
.venv\Scripts\python.exe scripts\capture_screens.py
```

### Building

```bash
.venv\Scripts\python.exe scripts\build.py --installer
```

See **[docs/BUILD.md](docs/BUILD.md)** for the details, including why the
browser is not bundled and why the build is onedir.

---

## Layout

```
app/
  main.py              entry point: crash capture, single instance, bootstrap
  branding.py          every product name, in one place
  config.py            settings, stored in SQLite
  core/                money, time, the error catalogue
  database/            migrations and repositories
  automation/          selectors, browser lifecycle, parsers, cart, checkout
  purchasing/          the Purchase Guard, the state machine, the services
  monitoring/          scheduler, per-check decisions, the monitor service
  notifications/       Windows toasts, with a tray fallback
  winint/              tray, startup, notifications identity, single instance
  diagnostics/         logging, redaction, health checks, support bundle
  ui/                  theme, components, pages, dialogs
tests/
  unit/                pure logic: guard, money, states, repositories, widgets
  integration/         real Chromium against local Amazon-like fixtures
  fixtures/            those fixtures
installer/             PyInstaller spec, Inno Setup script
scripts/               build, browser install, screenshot capture
docs/                  architecture, user guide, security, limitations, tests
```

---

## Documentation

| Document | What it covers |
|---|---|
| [USER_GUIDE.md](docs/USER_GUIDE.md) | How to use it |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it is built, and why |
| [SECURITY.md](docs/SECURITY.md) | The security review, with evidence |
| [BUILD.md](docs/BUILD.md) | Building the app and the installer |
| [TEST_REPORT.md](docs/TEST_REPORT.md) | What was tested, and what was not |
| [KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) | What it does not do |

---

## Responsible use

You remain responsible for your Amazon account and for Amazon's Conditions of
Use, including for orders placed automatically.

The software operates transparently as browser automation controlled by the
account owner. It does not bypass CAPTCHAs, spoof fingerprints, rotate
addresses or disguise itself — deliberately, not by omission. When Amazon asks
for a human, it stops and asks you.
