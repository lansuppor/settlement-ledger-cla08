-- 收款冲正修正与取消冲正修正迁移

-- 冲正修正记录：业务标识在租户内唯一，标识修正操作本身，
-- 与订单标识、冲正/作废/冲销业务标识独立，不得混用。
-- 修正减少已收、增加未收，不改订单金额与累计冲正/累计冲销；
-- cancelled 标记是否已被取消修正，取消金额取原始 amount_cents。
CREATE TABLE IF NOT EXISTS corrections(
  tenant TEXT NOT NULL,
  biz_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  cancelled INTEGER NOT NULL DEFAULT 0,
  cancelled_biz_id TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, biz_id)
);

-- 取消冲正修正记录：业务标识标识取消操作本身，与被取消修正的业务标识相互独立。
-- 取消金额取被取消修正的原始金额落库，请求不可指定。
CREATE TABLE IF NOT EXISTS correction_cancels(
  tenant TEXT NOT NULL,
  biz_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  correction_biz_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, biz_id)
);

-- 按订单回查修正/取消记录
CREATE INDEX IF NOT EXISTS idx_corrections_tenant_order ON corrections(tenant, order_id);
CREATE INDEX IF NOT EXISTS idx_correction_cancels_tenant_order ON correction_cancels(tenant, order_id);
