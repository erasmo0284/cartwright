# Cartwright — User Guide

A small Windows app that watches a product on Amazon and, when the conditions
you set are met, either tells you or buys it.

It is not affiliated with Amazon. It automates *your own* Amazon account,
using its own private browser window on your PC.

---

## Installing

1. Run `Cartwright-1.0.0-Setup.exe`.
2. It installs for you only, so Windows will not ask for an administrator
   password.
3. Tick "Create a desktop icon" if you want one.
4. The first time you open the app it downloads a browser (about 430 MB).
   This happens once. You will see a progress message at the bottom of the
   window.

You need roughly 1 GB of free disk space and Windows 10 or later.

---

## First run, in order

### 1. Connect your Amazon account

Click **Connect Amazon** on the Overview screen. A normal browser window
opens at Amazon's sign-in page.

Sign in exactly as you normally would, including any code Amazon texts you.

**The app never sees your password, your codes or your passkeys.** You are
signing in directly with Amazon; the app just keeps the resulting session in
its own private browser folder, the same way your normal browser keeps you
signed in.

When it says **Connected**, close the browser window.

### 2. Leave Test Mode on

A new installation starts with **Test Mode on**, and there is an orange bar
across the top of the window saying so.

With Test Mode on, the app will do everything — check the product, add it to
an order, go through checkout, verify the total, and find Amazon's order
button — and then stop without clicking it. Nothing can be bought.

Leave it on until you have run at least one test and are happy with what you
see.

Test Mode also works as a stop button. If a confirmation is on screen and you
switch Test Mode on in Settings before pressing the button, the app checks the
setting again at the last moment, puts your cart back and orders nothing.

### 3. Check a product

Go to **New Purchase**, paste an Amazon product link, and click **Check
product**.

You will see what the app found: the title, the price, who is selling it,
where it ships from, the condition, the exact version (colour, size) and the
delivery estimate.

If something says "Not shown", the app genuinely could not read it. It will
not guess, and anything it could not read will stop a purchase rather than be
assumed.

---

## Setting your rules

Under the product you will find the rules. They are all limits: the app never
exceeds them.

### Maximum item price

The most you will pay for **one unit**.

### Maximum order total

The most the **whole order** may come to, including shipping, tax and any
fees.

These are two different promises, and both matter. An item at $109.97 with
$25 shipping is a $135 order. The order total is re-read from Amazon's own
checkout page and checked again in the seconds before the order is placed.

### Seller

| Option | What it means |
|---|---|
| **Amazon only** | Only when Amazon itself is the seller. The safest, and the default. |
| Amazon or the manufacturer | Also allows the brand's own storefront. |
| Only sellers I approve | Only names you type in. Matched exactly. |
| Any seller | Anyone. Your price and condition rules still apply. |

A seller called "Amazon.com Deals" or "AmazonBasics Store" is **not** Amazon —
it is a marketplace seller — and "Amazon only" correctly refuses it.

If the seller changes from the one that was there when you set the watch up,
the purchase stops even if your seller rule would otherwise allow it. A
different seller can mean a different item, price or returns policy.

### Condition

**New only** by default. "Used — Like New" is used, not new, and is refused.
If Amazon does not state the condition at all, the purchase stops.

### Versions (colour, size, style)

Amazon gives every version of a product its own item code, and the app buys
the item code you gave it. It will never switch to a different colour or size
to get a better price.

If the app cannot read which version the page was showing, it says so under
the rules: *"This item comes in several versions and the app could not read
which one is shown."* That is not an error. It means the purchase is pinned
to the item code alone, which is still the exact item you were looking at --
the app is telling you what it is relying on rather than letting you assume
it wrote down "Black".

### Advanced

- Only order to the delivery address recorded now
- Only order with the payment method recorded now
- Only order Prime-eligible offers
- Allow Amazon to add extras such as a protection plan *(off)*
- Allow this to be a repeating Subscribe & Save delivery *(off)*

The last two are off for a reason. Amazon sometimes pre-selects Subscribe &
Save on everyday items, and a protection plan can be attached at checkout. By
default, either one stops the order.

---

## The three ways to buy

### Buy with confirmation *(the default)*

The app prepares everything and then stops and shows you:

```
Ready to purchase

Klein Tools CL800 Clamp Meter
Quantity     1
Seller       Amazon.com
Condition    New

Item         $109.97
Shipping     $0.00
Tax          $7.97
Total        $117.94

Delivering to  John D., Raleigh, NC
Paying with    Visa ending in 1234

Purchase Guard   All checks passed

[ Cancel ]            [ Place order - $117.94 ]
```

Nothing is ordered until you click **Place order**. Pressing Enter or Escape
cancels — the order button is deliberately not the default.

### Watch and notify me

The app checks the product on a schedule and tells you when your rules are
met. You still decide whether to buy.

### Buy automatically

The app orders without asking, as soon as your rules are met.

This has to be switched on once, deliberately, in Settings. You will be shown
exactly what is still checked every time, and you have to tick "I understand"
before the button works. Test Mode must also be off before any real order can
happen.

---

## Watching a product

From **New Purchase**, click **Watch and notify me** after setting your rules.

You choose:

- **What triggers it** — in stock, at or below your price, or both
- **How often to check** — from 2 minutes to once a day, 5 minutes by default
- **When to stop** — never, or on a date you pick
- **What happens** — tell you, or buy

Checks are spaced out and given a little randomness so Amazon never sees a
regular burst of requests, and there is a minimum gap between any two page
loads no matter how many products you watch.

**Watching continues when you close the window.** The app keeps running in
the system tray. If you would rather it quit, change that in Settings.

### Watch statuses

| Status | Meaning |
|---|---|
| Watching | Checking normally |
| Waiting for price | In stock, but above your limit |
| Target reached | At or below your limit |
| Out of stock | Not available right now |
| Paused | You paused it |
| Needs sign-in | Amazon signed the app out; reconnect |
| Needs verification | Amazon wants a security check from you |
| Purchased | Bought. This watch has stopped for good. |
| Error | Something failed; it is trying again more slowly |

---

## When Amazon asks for a human

Sometimes Amazon shows a one-time code, an image challenge, or a "confirm
it's you" page.

The app **stops**, brings the browser window forward, and waits. It does not
try to answer the challenge, and it never types anything on your behalf.

Complete the check in the browser window and the app carries on from where it
stopped. Nothing is ordered while it is waiting.

---

## If an order's result is uncertain

Rarely, the order button gets clicked but Amazon's confirmation page cannot be
read — a network glitch at the worst moment, or a layout the app does not
recognise.

The app will tell you this plainly and **will not try again**. Trying again is
how people get charged twice.

You will be asked to:

1. Open your Amazon orders (there is a button that does it)
2. Look for the item and the total
3. Tell the app whether the order was placed

Until you answer, that product cannot be purchased again. That is deliberate.

The same question is asked if the app or the PC stops while an order is being
placed. On the next start it finds the interrupted purchase, says it cannot
tell whether Amazon accepted it, and asks you the same three things. It never
retries by itself.

---

## Your cart

The app never checks out your existing Amazon cart.

Where Amazon offers **Buy Now**, it uses that — a separate one-item order that
does not touch your cart at all.

Where Buy Now is not offered and your cart has other things in it, the app
asks permission to move them to "Saved for later", buys your item, then moves
them back. It lists exactly what it would move, and cancelling is the default.

If it cannot do that safely, it stops. It will never order something you did
not choose.

---

## Notifications

Windows notifications for:

- Target price reached
- Back in stock
- Seller changed
- Purchase ready for your confirmation
- Purchase completed
- Purchase blocked
- Amazon sign-in expired
- Amazon needs verification
- Monitoring error

You can turn most of these off in Settings. The three about purchases —
ready, completed, blocked — cannot be turned off, because they are how you
find out money was or nearly was spent.

Notifications never include your address, your card details or your order
number.

---

## Settings worth knowing about

**Test mode** — when on, no order can be placed. Turning it off asks you to
confirm.

**Close button** — minimise to the tray (default) or exit.

**Start with Windows** — starts the app hidden in the tray when you sign in.
If you later turn it off in Windows' own Startup settings, the app notices and
says so rather than claiming it is on.

**Clear browser session** — signs the app's private browser out of Amazon.
Your normal Chrome or Edge is untouched.

**Export diagnostic report** — a file you can send for support. It never
contains passwords, cookies or card details, and it says what it left out.

---

## Where your data is

```
%LOCALAPPDATA%\Cartwright\
```

Everything is there: the database, the private browser profile, the logs.
Nothing is sent anywhere. There is no telemetry and no account.

Uninstalling asks whether you want to keep it, so a reinstall does not need
to download the browser or sign in again.

---

## Troubleshooting

**"The browser could not be started"**
Settings → Browser → **Repair browser**. This re-downloads it.

**"Amazon sign-in has expired"**
Settings → Amazon → **Reconnect**, and sign in again.

**"Amazon's page looked different than expected"**
Amazon changed its layout. A diagnostic snapshot was saved. Nothing was
ordered. Try again later, or export a diagnostic report.

**Checks stopped happening**
Check the tray icon — monitoring may be paused. Also check whether a watch is
showing "Needs verification".

**Nothing gets bought even though the price looks right**
Open the watch and read its rules. The most common cause is an order-total
limit that is lower than the item price plus tax; the editor warns about this
when you set it.

**Notifications do not appear**
Settings → Notifications → **Send a test notification**. It will tell you
which channel it is using and what went wrong.

**"The app could not save its data" when starting**
The app's own database file is damaged or unreadable — usually after a power
cut or a disk problem. It keeps rolling backups, so it offers to restore the
most recent one and start. Anything recorded after that backup is lost, and
the damaged file is kept beside it (with a `.damaged` name) rather than
deleted. Your Amazon account and your real orders are not affected.

---

## Responsible use

You remain responsible for your Amazon account and for Amazon's Conditions of
Use. You are responsible for every order this software places, including
automatic ones.

The app operates transparently as browser automation controlled by the owner
of the account. It does not bypass CAPTCHAs, hide what it is, rotate network
addresses or falsify device information — by design, not by omission.
