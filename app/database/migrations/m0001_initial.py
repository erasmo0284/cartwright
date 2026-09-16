"""Migration 1: the initial schema.

Shipped. Do not edit -- corrections go in a later migration.

Modelling notes
---------------

*Money as integer cents.* Every amount is an ``INTEGER`` number of minor
units with an accompanying currency, mirroring :class:`app.core.money.Money`.
No amount is ever stored as a float.

*Timestamps as ISO-8601 UTC text.* Fixed-width, so text ordering equals
chronological ordering (see :mod:`app.core.timeutil`).

*Duplicate-order protection lives here, in the schema.* Three constraints do
the load-bearing work, so a logic bug in a service cannot cause a double
purchase:

1. ``idx_purchase_attempts_one_submission`` -- a partial unique index that
   permits at most one *submitted* attempt per purchase job, ever.
2. ``idx_purchase_jobs_single_inflight_product`` -- at most one purchase job
   per product may occupy a non-terminal state. ``unknown`` counts as
   non-terminal on purpose: an unresolved order blocks further attempts until
   a human says what happened.
3. ``idx_orders_amazon_number`` -- an Amazon order number can be recorded
   only once.

*No payment or credential data.* The only payment-related column is
``payment_label``, which holds the masked description Amazon itself displays
(``Visa ending 1234``). There is no column in which a card number, CVV,
password or session cookie could be stored.
"""

from __future__ import annotations

SQL = """
-- ---------------------------------------------------------------------------
-- Settings: typed application configuration, one JSON value per key.
-- ---------------------------------------------------------------------------
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,          -- JSON-encoded scalar or object
    updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Products: an Amazon item identity. One row per (marketplace, ASIN).
-- On Amazon each variation has its own ASIN, so this row *is* the variation.
-- ---------------------------------------------------------------------------
CREATE TABLE products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asin          TEXT NOT NULL,
    marketplace   TEXT NOT NULL DEFAULT 'www.amazon.com',
    title         TEXT,
    brand         TEXT,
    image_url     TEXT,
    canonical_url TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (marketplace, asin),
    CHECK (length(asin) BETWEEN 8 AND 14)
);

-- ---------------------------------------------------------------------------
-- Variation snapshots: the twister dimensions observed for a product, e.g.
-- {"Color": "Black", "Size": "Large"}. Compared by the Purchase Guard so a
-- silently substituted variation blocks the order.
-- ---------------------------------------------------------------------------
CREATE TABLE product_variations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    fingerprint     TEXT NOT NULL,      -- stable hash of dimensions_json
    dimensions_json TEXT NOT NULL,      -- {"Color": "Black"}
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (product_id, fingerprint)
);

-- ---------------------------------------------------------------------------
-- Purchase rules: everything the user decided must be true before buying.
-- Owned one-to-one by a watch job or a purchase job; never shared, so that
-- editing one job's rules can never silently change another's.
-- ---------------------------------------------------------------------------
CREATE TABLE purchase_rules (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    currency                TEXT    NOT NULL DEFAULT 'USD',
    quantity                INTEGER NOT NULL DEFAULT 1,
    max_item_price_cents    INTEGER,
    max_order_total_cents   INTEGER,
    seller_policy           TEXT    NOT NULL DEFAULT 'amazon_only',
    approved_sellers_json   TEXT    NOT NULL DEFAULT '[]',
    condition_policy        TEXT    NOT NULL DEFAULT 'new_only',
    expected_asin           TEXT    NOT NULL,
    expected_variation_json TEXT,
    expected_seller         TEXT,
    expected_ships_from     TEXT,
    expected_address_label  TEXT,
    expected_payment_label  TEXT,
    require_address_match   INTEGER NOT NULL DEFAULT 1,
    require_payment_match   INTEGER NOT NULL DEFAULT 1,
    allow_addons            INTEGER NOT NULL DEFAULT 0,
    allow_subscription      INTEGER NOT NULL DEFAULT 0,
    require_prime           INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL,
    CHECK (quantity BETWEEN 1 AND 30),
    CHECK (seller_policy IN (
        'amazon_only', 'amazon_or_manufacturer', 'approved_list', 'any')),
    CHECK (condition_policy IN (
        'new_only', 'allow_used', 'allow_refurbished')),
    CHECK (max_item_price_cents  IS NULL OR max_item_price_cents  >= 0),
    CHECK (max_order_total_cents IS NULL OR max_order_total_cents >= 0),
    CHECK (length(currency) = 3)
);

-- ---------------------------------------------------------------------------
-- Watch jobs: periodic monitoring of one product against one rule set.
-- ---------------------------------------------------------------------------
CREATE TABLE watch_jobs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id           INTEGER NOT NULL REFERENCES products (id)       ON DELETE CASCADE,
    rules_id             INTEGER NOT NULL REFERENCES purchase_rules (id) ON DELETE CASCADE,
    status               TEXT    NOT NULL DEFAULT 'watching',
    action               TEXT    NOT NULL DEFAULT 'notify',
    trigger_in_stock     INTEGER NOT NULL DEFAULT 1,
    trigger_target_price INTEGER NOT NULL DEFAULT 1,
    interval_seconds     INTEGER NOT NULL DEFAULT 300,
    expires_at           TEXT,
    next_check_at        TEXT    NOT NULL,
    last_checked_at      TEXT,
    last_check_summary   TEXT,
    last_error_code      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    paused_reason        TEXT,
    checks_performed     INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    CHECK (action IN ('notify', 'buy')),
    CHECK (interval_seconds >= 120),
    CHECK (status IN (
        'watching',
        'waiting_for_price',
        'target_reached',
        'out_of_stock',
        'paused',
        'needs_login',
        'needs_verification',
        'purchase_completed',
        'expired',
        'error'))
);

CREATE INDEX idx_watch_jobs_due     ON watch_jobs (status, next_check_at);
CREATE INDEX idx_watch_jobs_product ON watch_jobs (product_id);

-- ---------------------------------------------------------------------------
-- Price history: one row per successful observation. Drives the sparkline
-- and lets the guard show "seller changed since you set this up".
-- ---------------------------------------------------------------------------
CREATE TABLE price_history (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id            INTEGER NOT NULL REFERENCES products (id)   ON DELETE CASCADE,
    watch_job_id          INTEGER          REFERENCES watch_jobs (id) ON DELETE SET NULL,
    observed_at           TEXT    NOT NULL,
    price_cents           INTEGER,
    currency              TEXT,
    in_stock              INTEGER,
    availability_text     TEXT,
    seller                TEXT,
    ships_from            TEXT,
    item_condition        TEXT,
    variation_fingerprint TEXT,
    CHECK (price_cents IS NULL OR price_cents >= 0)
);

CREATE INDEX idx_price_history_product ON price_history (product_id, observed_at DESC);

-- ---------------------------------------------------------------------------
-- Purchase jobs: one intent to buy, carrying its state-machine position.
-- ---------------------------------------------------------------------------
CREATE TABLE purchase_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id       INTEGER NOT NULL REFERENCES products (id)       ON DELETE CASCADE,
    rules_id         INTEGER NOT NULL REFERENCES purchase_rules (id) ON DELETE CASCADE,
    watch_job_id     INTEGER          REFERENCES watch_jobs (id)     ON DELETE SET NULL,
    mode             TEXT    NOT NULL,
    test_mode        INTEGER NOT NULL DEFAULT 1,
    state            TEXT    NOT NULL DEFAULT 'created',
    idempotency_key  TEXT    NOT NULL UNIQUE,
    cart_strategy    TEXT,
    isolation_journal_json TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    state_changed_at TEXT    NOT NULL,
    terminal_at      TEXT,
    outcome_code     TEXT,
    outcome_detail   TEXT,
    CHECK (mode IN ('assisted', 'automatic')),
    CHECK (state IN (
        'created',
        'product_check',
        'rule_validation',
        'cart_preparation',
        'checkout',
        'final_validation',
        'awaiting_confirmation',
        'submitting',
        'confirming',
        'confirmed',
        'blocked',
        'failed',
        'needs_user',
        'unknown',
        'cancelled'))
);

CREATE INDEX idx_purchase_jobs_product ON purchase_jobs (product_id);
CREATE INDEX idx_purchase_jobs_state   ON purchase_jobs (state, updated_at DESC);
CREATE INDEX idx_purchase_jobs_watch   ON purchase_jobs (watch_job_id);

-- Safety constraint 2: at most one live purchase job per product. 'unknown'
-- and 'needs_user' are treated as live so an unresolved outcome blocks any
-- further attempt on that product until a human resolves it.
CREATE UNIQUE INDEX idx_purchase_jobs_single_inflight_product
    ON purchase_jobs (product_id)
    WHERE state IN (
        'created',
        'product_check',
        'rule_validation',
        'cart_preparation',
        'checkout',
        'final_validation',
        'awaiting_confirmation',
        'submitting',
        'confirming',
        'needs_user',
        'unknown');

-- ---------------------------------------------------------------------------
-- Purchase state transitions: an append-only audit trail.
-- ---------------------------------------------------------------------------
CREATE TABLE purchase_state_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_job_id INTEGER NOT NULL REFERENCES purchase_jobs (id) ON DELETE CASCADE,
    from_state      TEXT,
    to_state        TEXT    NOT NULL,
    reason          TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX idx_transitions_job ON purchase_state_transitions (purchase_job_id, id);

-- ---------------------------------------------------------------------------
-- Purchase attempts: one row per run of the checkout sequence.
-- ---------------------------------------------------------------------------
CREATE TABLE purchase_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_job_id INTEGER NOT NULL REFERENCES purchase_jobs (id) ON DELETE CASCADE,
    attempt_number  INTEGER NOT NULL,
    submit_token    TEXT    NOT NULL UNIQUE,
    submitted       INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT    NOT NULL,
    submitted_at    TEXT,
    finished_at     TEXT,
    outcome         TEXT,
    detail          TEXT,
    UNIQUE (purchase_job_id, attempt_number),
    CHECK (outcome IS NULL OR outcome IN (
        'not_submitted', 'confirmed', 'rejected', 'uncertain', 'blocked'))
);

-- Safety constraint 1: at most one attempt per job may ever be marked
-- submitted. This is the hard guarantee against a double order.
CREATE UNIQUE INDEX idx_purchase_attempts_one_submission
    ON purchase_attempts (purchase_job_id)
    WHERE submitted = 1;

-- ---------------------------------------------------------------------------
-- Purchase Guard results: a report per validation phase, with one row per
-- individual check so the UI can show the full PASS/FAIL table.
-- ---------------------------------------------------------------------------
CREATE TABLE guard_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_job_id INTEGER NOT NULL REFERENCES purchase_jobs (id) ON DELETE CASCADE,
    phase           TEXT    NOT NULL,
    passed          INTEGER NOT NULL,
    blocked_code    TEXT,
    created_at      TEXT    NOT NULL,
    CHECK (phase IN ('pre_cart', 'pre_checkout', 'pre_submit'))
);

CREATE INDEX idx_guard_reports_job ON guard_reports (purchase_job_id, id);

CREATE TABLE guard_checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id  INTEGER NOT NULL REFERENCES guard_reports (id) ON DELETE CASCADE,
    check_id   TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    status     TEXT    NOT NULL,
    expected   TEXT,
    actual     TEXT,
    error_code TEXT,
    required   INTEGER NOT NULL DEFAULT 1,
    position   INTEGER NOT NULL DEFAULT 0,
    CHECK (status IN ('pass', 'fail', 'skipped', 'not_applicable'))
);

CREATE INDEX idx_guard_checks_report ON guard_checks (report_id, position);

-- ---------------------------------------------------------------------------
-- Orders: only rows that Amazon actually confirmed, or that a human marked
-- as resolved after an uncertain outcome.
-- ---------------------------------------------------------------------------
CREATE TABLE orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_job_id     INTEGER          REFERENCES purchase_jobs (id) ON DELETE SET NULL,
    product_id          INTEGER          REFERENCES products (id)      ON DELETE SET NULL,
    amazon_order_number TEXT,
    quantity            INTEGER NOT NULL,
    item_price_cents    INTEGER,
    shipping_cents      INTEGER,
    tax_cents           INTEGER,
    total_cents         INTEGER NOT NULL,
    currency            TEXT    NOT NULL DEFAULT 'USD',
    seller              TEXT,
    item_condition      TEXT,
    shipping_label      TEXT,
    payment_label       TEXT,   -- masked, as Amazon displays it
    delivery_estimate   TEXT,
    confirmation_status TEXT    NOT NULL,
    placed_at           TEXT    NOT NULL,
    verified_at         TEXT,
    created_at          TEXT    NOT NULL,
    CHECK (quantity >= 1),
    CHECK (total_cents >= 0),
    CHECK (confirmation_status IN (
        'confirmed', 'confirmed_by_user', 'not_placed_per_user'))
);

-- Safety constraint 3: an Amazon order number may be recorded only once.
CREATE UNIQUE INDEX idx_orders_amazon_number
    ON orders (amazon_order_number)
    WHERE amazon_order_number IS NOT NULL;

CREATE INDEX idx_orders_placed ON orders (placed_at DESC);

-- ---------------------------------------------------------------------------
-- Activity events: the user-facing history feed.
-- ---------------------------------------------------------------------------
CREATE TABLE activity_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    severity        TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    detail          TEXT,
    product_id      INTEGER REFERENCES products (id)      ON DELETE SET NULL,
    watch_job_id    INTEGER REFERENCES watch_jobs (id)    ON DELETE SET NULL,
    purchase_job_id INTEGER REFERENCES purchase_jobs (id) ON DELETE SET NULL,
    order_id        INTEGER REFERENCES orders (id)        ON DELETE SET NULL,
    error_code      TEXT,
    amount_cents    INTEGER,
    currency        TEXT,
    metadata_json   TEXT,
    CHECK (category IN (
        'purchase', 'watch_check', 'warning', 'error', 'account', 'system')),
    CHECK (severity IN ('info', 'success', 'warning', 'blocked', 'error'))
);

CREATE INDEX idx_activity_created  ON activity_events (created_at DESC);
CREATE INDEX idx_activity_category ON activity_events (category, created_at DESC);
CREATE INDEX idx_activity_product  ON activity_events (product_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Notification events: a delivery log, used to suppress repeats.
-- ---------------------------------------------------------------------------
CREATE TABLE notification_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL,
    dedupe_key      TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    body            TEXT,
    delivered       INTEGER NOT NULL DEFAULT 0,
    delivery_method TEXT,
    suppressed_why  TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX idx_notifications_dedupe  ON notification_events (dedupe_key, created_at DESC);
CREATE INDEX idx_notifications_created ON notification_events (created_at DESC);

-- ---------------------------------------------------------------------------
-- Automation diagnostics: pointers to saved screenshots and page metadata.
-- The files themselves live under screenshots/ and are user-deletable.
-- ---------------------------------------------------------------------------
CREATE TABLE automation_diagnostics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    step            TEXT NOT NULL,
    error_code      TEXT,
    page_url        TEXT,              -- already redacted before storage
    screenshot_file TEXT,
    metadata_json   TEXT,
    purchase_job_id INTEGER REFERENCES purchase_jobs (id) ON DELETE CASCADE,
    watch_job_id    INTEGER REFERENCES watch_jobs (id)    ON DELETE CASCADE
);

CREATE INDEX idx_diagnostics_created ON automation_diagnostics (created_at DESC);
"""
