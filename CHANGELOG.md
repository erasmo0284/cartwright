# Changelog

All notable changes to Cartwright are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-09-17

First public release.

### Added

- **Purchase Guard** — a deterministic gate every purchase must pass, with no
  I/O and no randomness: exact product and version, quantity, seller policy,
  condition, item price, whole order total, delivery address, payment method,
  no unexpected items and no attached add-ons. Assisted and automatic
  purchases call the same function; there is no weaker path.
- **Three ways to buy** — prepare and ask for confirmation (the default),
  watch and notify, or buy automatically behind an explicit consent dialog.
- **Test Mode**, on for every new installation: the whole sequence runs and
  stops at Amazon's order button without clicking it.
- **Cart isolation** — Buy Now where Amazon offers it, so the user's cart is
  never touched; otherwise the other items are set aside with permission and
  put back afterwards, journalled so an interruption can be undone.
- **Duplicate-order protection** — three partial unique indexes, the
  submission recorded before the click, and crash recovery that turns an
  interrupted order into "outcome unknown" and asks the user rather than
  retrying.
- **Human handoff** — CAPTCHA, one-time codes and unusual sign-in checks
  pause automation, show the browser and wait for the person. Nothing is
  bypassed.
- Watch list with price history, price-drop and back-in-stock triggers,
  paced checking, and sleep/wake recovery.
- Windows integration: Action Center notifications, a tray icon, start with
  Windows, single instance, and a per-user installer that needs no
  administrator rights.
- Product thumbnails, taken as a screenshot of the image the browser had
  already rendered, so the application still makes no network requests of its
  own.
- Documentation: user guide, architecture, security review, known
  limitations, test report and build instructions.

### Verified against the live site

Test Mode rehearsals were run end to end against amazon.com on a signed-in
account, and every defect they found is fixed and covered by a fixture copied
from the real page. **No order has been placed by this software.** The
details, including what the runs exposed, are in
[docs/TEST_REPORT.md](docs/TEST_REPORT.md).

### Known limitations

The honest list is [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).
The headline ones: no order has ever been placed, so the final click,
Amazon's confirmation page and reading an order number back out of order
history are unproven; Amazon's checkout does not print the item code, so the
order's contents are confirmed by name where it does not; the build is
unsigned; and it is Windows-only and written for `amazon.com`.

[1.0.0]: https://github.com/erasmo0284/cartwright/releases/tag/v1.0.0
