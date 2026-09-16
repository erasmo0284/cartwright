"""Freeze the brand a rule set was created against.

``PurchaseRules.brand`` is what the ``amazon_or_manufacturer`` seller policy
compares a seller name to. Until this migration it was not stored: it was
re-derived on every check from ``products.brand``, which is refreshed from the
product page's byline each time the page is read.

That made the policy's allow-list page-controlled. A listing whose byline and
seller name both change -- which is what a listing takeover looks like --
would satisfy "Amazon or the manufacturer" for whatever the byline then said.

Storing the brand on the rule row freezes it at the moment the user set the
rule up, so a later byline change can no longer widen what the rule permits.
Existing rows are backfilled from the product their job points at, which is
the value they were already being evaluated with.
"""

from __future__ import annotations

SQL = """
ALTER TABLE purchase_rules ADD COLUMN brand TEXT;

-- Backfill from the product each rule set is used for. A rule set is owned by
-- exactly one watch job or one purchase job, so this cannot pick up a second
-- product's brand.
UPDATE purchase_rules
SET brand = (
    SELECT p.brand
    FROM products p
    WHERE p.id = (
        SELECT w.product_id FROM watch_jobs w WHERE w.rules_id = purchase_rules.id
        UNION ALL
        SELECT j.product_id FROM purchase_jobs j WHERE j.rules_id = purchase_rules.id
        LIMIT 1
    )
)
WHERE brand IS NULL;
"""
