# Task 5 — Future broker connection: design gate only

Use the system contract. This is a design and test task, not authorization to
trade. Do not add broker credentials, call a broker, create an account, install
an execution plugin, or enable `limited_live`/`live`.

Produce a broker-adapter design that supports a single venue first and remains
asset-class neutral. Specify credentials via secret references, per-account and
per-instrument allow-lists, order idempotency/client order IDs, canonical order
states, partial fills, cancels/replaces, rate limits, market status, order
preview, read-only reconciliation, manual emergency halt, and restart recovery.
Show the two-person/explicit-owner promotion controls and a configuration
diff/audit trail. Add contract tests using a fake broker only.

List the precise prerequisites before a real connection: broker suitability,
data permissions, tax/compliance review for the operating jurisdiction,
documented risk limits, disaster-recovery test, and explicit written owner
authorization. Stop after the design review and test evidence.
