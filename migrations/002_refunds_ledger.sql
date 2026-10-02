-- 冲正（退款）与收付流水迁移

-- 订单增加累计冲正金额；可退余额 = paid_cents - refunded_cents 派生，不落地
ALTER TABLE orders ADD COLUMN refunded_cents INTEGER NOT NULL DEFAULT 0;

-- 冲正记录：业务标识在租户内唯一，标识冲正操作本身，与订单标识相互独立
CREATE TABLE IF NOT EXISTS refunds(
  tenant TEXT NOT NULL,
  biz_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, biz_id)
);

-- 收付流水：按（租户, 订单）内发生顺序递增编号
CREATE TABLE IF NOT EXISTS ledger_entries(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  op_type TEXT NOT NULL,              -- accept / payment / refund
  biz_id TEXT,                        -- 业务标识（受理与收款无业务标识时为 NULL）
  amount_cents INTEGER NOT NULL,
  paid_result_cents INTEGER NOT NULL,
  outstanding_result_cents NOT NULL,
  refunded_result_cents NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, order_id, seq)
);

-- 按业务标识回查冲正记录（重放命中）
CREATE INDEX IF NOT EXISTS idx_refunds_tenant_order ON refunds(tenant, order_id);
