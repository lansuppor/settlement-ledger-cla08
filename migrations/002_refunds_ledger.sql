ALTER TABLE orders ADD COLUMN refunded_cents INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS refunds(
  tenant TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  PRIMARY KEY(tenant, refund_id)
);

CREATE TABLE IF NOT EXISTS ledger(
  tenant TEXT NOT NULL,
  seq INTEGER NOT NULL,
  order_id TEXT NOT NULL,
  op TEXT NOT NULL,
  biz_id TEXT,
  amount_cents INTEGER NOT NULL,
  paid_cents INTEGER NOT NULL,
  outstanding_cents INTEGER NOT NULL,
  refunded_cents INTEGER NOT NULL,
  PRIMARY KEY(tenant, seq)
);
