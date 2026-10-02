-- 收款冲正修正与取消冲正修正迁移

-- 收款冲正修正记录：业务标识在租户内唯一，标识修正操作本身，
-- 与订单标识及冲正、作废、冲销、取消修正的业务标识独立，不得混用。
-- 修正生效后已收减少、未收增加；cancelled_at 标记是否已被取消修正。
CREATE TABLE IF NOT EXISTS payment_corrections(
  tenant TEXT NOT NULL,
  biz_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  cancelled_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, biz_id)
);

-- 取消修正记录：业务标识标识本次取消操作本身；取消金额取被取消修正的原始金额
CREATE TABLE IF NOT EXISTS payment_correction_cancels(
  tenant TEXT NOT NULL,
  biz_id TEXT NOT NULL,              -- 本次取消的业务标识
  order_id TEXT NOT NULL,
  correction_biz_id TEXT NOT NULL,   -- 被取消修正的业务标识
  amount_cents INTEGER NOT NULL,     -- 取被取消修正的原始金额，不可指定
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, biz_id)
);

-- 按订单回查修正/取消记录
CREATE INDEX IF NOT EXISTS idx_payment_corrections_tenant_order ON payment_corrections(tenant, order_id);
CREATE INDEX IF NOT EXISTS idx_payment_correction_cancels_tenant_order ON payment_correction_cancels(tenant, order_id);
