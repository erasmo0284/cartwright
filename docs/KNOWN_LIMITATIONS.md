# Known limitations

An honest list. Where something is untested rather than broken, it says so.

## Not verified against the real Amazon

This is the most important item on the page.

**No order has been placed on amazon.com.** A full Test Mode rehearsal *has*
now been run against the live site on a signed-in account (2026-09-16): the
app read the real product page, opened Buy Now, read the real checkout --
totals, tax, delivery address, payment method -- ran the guard, located the
order button and stopped. Every check passed. See
[TEST_REPORT.md](TEST_REPORT.md) for what that exposed and fixed.

What remains unproven is the last click and everything after it: the order
being accepted, Amazon's confirmation page, and reading the order number back
out of order history. Those cannot be verified without spending money.

Specifically unverified against the live site:

| Flow | Status |
|---|---|
| Product page parsing | **Verified live** |
| Cart reading and isolation | Empty cart verified live; a cart with items, and setting items aside, are fixtures only |
| Checkout totals, address, payment | **Verified live** on the current pipeline |
| Buy Now | **Verified live** (it navigates to a checkout page on this account) |
| Turbo Checkout modal | Fixtures only, against a real cross-document iframe; Amazon did not serve this layout |
| Placing a real order | **Never run** |
| Order confirmation reading | Fixtures only |
| Order-history verification | Fixtures only |
| Sign-in and security checks | **Verified live**: Amazon asked for a check, the app paused and waited for the human, then detected the session |
| CAPTCHA handoff | Detection tested on fixtures; no real CAPTCHA has appeared |

Verifying what is left requires spending real money. **The intended first run
is still: connect the account, then run a test in Test Mode on a cheap item,
and read the result.** That exercises the entire path except the final click,
which is exactly what Test Mode exists for -- and it is how the six defects
above were found.

The selectors were compiled from current public research (see
`app/automation/selectors.py`), each as an ordered fallback chain, and the
resolver logs whenever a fallback is used. Expect to need to update that one
file when Amazon changes.

## Amazon-specific limitations

- **US marketplace focus.** URLs, seller names and summary-row labels are
  written for `amazon.com`. Other storefronts will parse partially: prices and
  ASINs should work, but localised labels ("Sold by", "Order total") will not
  match, and the guard will block rather than guess. Not a supported
  configuration.
- **Buy Now is not always offered.** Where Amazon does not offer it and the
  cart holds other items, the app asks permission to set them aside, or stops.
- **Turbo Checkout** (the Buy Now modal in an iframe) is implemented against
  documented identifiers but has never been exercised. If it misbehaves, the
  guard's "order button located" check fails and nothing is ordered.
- **Prime eligibility is not reliably detectable.** The parser returns
  "unknown" rather than guessing. A rule requiring Prime therefore *blocks*
  when eligibility cannot be established, which is safe but may be
  surprising. It is off by default.
- **Digital goods, subscriptions, pre-orders and add-on items** are untested
  and likely to be refused by one check or another.
- **A variation dimension with an unfamiliar name carries no expectation of
  its own.** Amazon puts non-dimension labels in the same markup, so only
  recognised dimension names (Colour, Size, Style, …) become part of the
  variation the guard compares; anything else is dropped and logged at INFO.
  The bound on this is the ASIN check: a different variation is a different
  ASIN, and that comparison is exact. A new dimension name therefore weakens
  the *description* of what is being bought, not the identity of it. Since
  1.0.0 the app notices this case and says so on the rules screen, rather
  than letting the user assume the version was recorded.
- **Amazon does not print the item code on its current checkout**, so the
  app confirms the order holds the right item by matching its *name* against
  the product page it read moments earlier -- exactly one line, no code on
  it, and an exact title match, or it refuses. The item code is still checked
  everywhere Amazon does publish it, and the seller on the order line, the
  item price, the order total and the delivery address are all still checked
  independently. It is weaker than a code comparison, and it is the honest
  limit of what that page offers.
- **The `Amazon or the manufacturer` seller rule trusts the brand name the
  product page showed when you created the rule.** It is stored at that
  moment and never refreshed, so a later change to the page cannot widen what
  the rule allows -- but if the page was already showing a misleading brand,
  the rule was created around that. `Amazon only` and the approved-seller list
  do not have this property, and `Amazon only` is the default.
- **Subscribe & Save is detected by element ids first, wording second.** If
  Amazon renames the accordion rows, the app falls back to looking for
  subscription wording inside the buy box, and reports "subscription" unless
  a one-time option can be shown to be selected. That is the safe direction,
  but on a renamed buy box it can refuse a purchase that was in fact a
  one-time order.

## Functional gaps

- **One marketplace per product, no variation switching.** The app buys the
  exact ASIN you gave it. It will not select a different colour or size to get
  a better price, by design.
- **No multi-item orders.** One product per purchase.
- **No price-drop prediction, no deal discovery, no recommendations.** It
  watches what you tell it to watch.
- **The activity feed is capped** at 5,000 events and price history at 500
  points per product; older entries are pruned.
- **No scheduled quiet hours.** Monitoring runs whenever the app is running.
- **Watch intervals are per-job**, but the global request floor (15 s between
  any two page loads) means a very large watch list will drift later than its
  nominal interval. That is the intended trade-off.

## Platform

- **Windows only.** The modules import cleanly elsewhere and the unit tests
  pass, but the tray, notifications, startup registration and profile-lock
  recovery are Windows-specific.
- **The build is unsigned.** SmartScreen will warn on first run
  ("Windows protected your PC" → More info → Run anyway). Fixing this needs an
  Authenticode certificate; it is a distribution decision, not a code change.
- **High-DPI at 125% / 150% has not been visually verified.** Qt 6 handles
  scaling automatically and the layout uses no pixel constants for text, but
  it was reviewed only at 100%.
- **Windows 10** is expected to work — the code paths are the same, and the
  `windows11` Qt style simply falls back to `windowsvista` — but it has not
  been tested there.

## Untested at runtime

Honest inventory of code that exists and is unit-tested but has never run
against the real thing:

- **A toast banner has never been seen.** The notifier was run for real
  against WinRT on the development machine: the AUMID registers, the toast is
  accepted and recorded as delivered. No banner appeared -- but a control
  toast sent under an identity Windows already trusts (PowerShell's own)
  behaved identically, so that machine simply does not display banners. What
  remains unproven is the part that needs a machine with notifications on:
  the banner itself, its name and icon, and a click on a toast button
  reaching the application. `Settings → Notifications → Send a test
  notification` exists precisely for this.
- **"Start with Windows" has not survived a real logon.** The real `HKCU`
  entry was created, read back, correctly reported as "Turned off in Windows
  Settings" when Windows' own disable flag was set, and removed again -- but
  no one has signed out and back in to watch it start.
- **The tray icon** creates a real notification-area entry (Windows records
  it under `HKCU\Control Panel\NotifyIconSettings` with the application's
  executable path), but it sits in the hidden overflow flyout by default, so
  its appearance has not been seen, and the recovery after an Explorer
  restart has not been exercised.
- **Sleep/wake.** The clock-jump detector is unit-tested with simulated
  clocks; the machine has not actually been suspended.
- **The installer's interactive wizard.** The unattended path is tested; see below.

## Installer

`AmazonPurchaseBot-1.0.0-Setup.exe` was built with Inno Setup 6.7.3 and
tested unattended: install (exit 0, 5.1 s, no administrator prompt, 217 MB in
`%LOCALAPPDATA%\Programs`), launch of the installed copy, and uninstall
(exit 0, program and shortcuts removed, **user data kept**, the
`AppUserModelId` and `Run` registry keys removed). The Start Menu shortcut was
confirmed to carry the AppUserModelID via `Get-StartApps`.

Still unverified in the installer:

- the **interactive** wizard -- every run so far was `/VERYSILENT`, so the
  page layout, the desktop-shortcut and start-with-Windows checkboxes, and
  the "keep your data?" uninstall prompt have been read in the script but not
  clicked;
- **upgrade over a running copy** (`AppMutex` should ask the user to close the
  app first);
- the **low-disk-space warning**;
- installing as a **different Windows user**, and on a machine where
  `%LOCALAPPDATA%` is redirected to a network path.

See [TEST_REPORT.md](TEST_REPORT.md) for what was verified and how.

## Operational

- **Recovering from an uncertain order needs the user.** By design: the app
  refuses to guess or retry. Until the user answers, that product cannot be
  purchased again.
- **A crash during cart isolation leaves items in "Saved for later"** until
  the next start, when the journal is replayed and they are moved back. If the
  app is never started again, they stay there.
- **Clearing the browser session signs you out** and requires reconnecting.
- **Two copies cannot run at once.** Chromium refuses to share a profile, so
  the second launch raises the first window and exits.

## Deliberately out of scope

Not missing — excluded on purpose:

- CAPTCHA solving, anti-bot evasion, fingerprint spoofing, proxy rotation
- Any AI or language model in the purchase decision
- A cloud service, user accounts, or multi-user support
- Telemetry, analytics or crash reporting
- Other retailers
- An automatic updater
