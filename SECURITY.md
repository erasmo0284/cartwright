# Security policy

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue.

Use GitHub's private reporting on this repository:
**Security → Advisories → Report a vulnerability**
(<https://github.com/erasmo0284/cartwright/security/advisories/new>).

Please include what you found, how to reproduce it, and what you think the
impact is. I will acknowledge it as soon as I can. This is a personal project
maintained in spare time, so please do not expect a same-day reply — but a
report that could cost somebody money or expose their account will always be
looked at first.

**Please do not include** cookies, session tokens, passwords, card details,
order numbers or delivery addresses in a report. They are not needed to
explain a flaw, and this repository must never receive them.

## What counts as a security issue here

This application spends real money and holds a signed-in Amazon session, so
the interesting failures are not the usual ones. All of these are in scope:

- **Anything that could authorise a purchase that the user's rules should
  have refused** — a way past the Purchase Guard, a way to reach the order
  button without a valid authorisation, a check that can be made to pass
  without the underlying condition being true.
- **Anything that could cause a duplicate order**, including a path where an
  interrupted submission is retried automatically.
- **Any way a credential could be written down** — a password, session cookie,
  authentication token, card number or one-time code appearing in the log
  files, the activity feed, the database, a failure screenshot or the
  exported diagnostic report.
- **Any outbound network request the application makes of its own accord.**
  It is designed to make none: all traffic is the browser doing what the user
  can see it doing.
- Local privilege or path issues: writing outside the per-user data
  directory, following a path supplied by a stored value out of that
  directory, or the single-instance pipe being reachable by another user.

## What is already known, and not a vulnerability

- **The installer and executable are not code-signed.** SmartScreen warns on
  first run. This is a distribution decision, recorded in
  [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).
- **The database is not encrypted.** It holds a watch list, price history and
  masked payment labels — no credentials. Encrypting it with a key stored
  beside it would be theatre. The genuinely sensitive artefact is the browser
  profile, protected by the user's Windows account like any browser profile.
- **The application cannot defeat Amazon's protections, and will not try.**
  No CAPTCHA bypass, no fingerprint spoofing, no proxy rotation. Reports
  asking for those are not security reports; see
  [CONTRIBUTING.md](CONTRIBUTING.md).
- **Anyone at the unlocked Windows session can use the app**, exactly as they
  could use a signed-in browser. There is no separate app password.

## Supported versions

The latest release is the supported one. This is a young project; there are
no long-term support branches.

## The existing review

[docs/SECURITY.md](docs/SECURITY.md) is a full security review of the
application: the threat model, what is never requested or stored, how
redaction works and where it is enforced, the network surface, and the
residual risks. Reading it first will tell you what has already been
considered.
