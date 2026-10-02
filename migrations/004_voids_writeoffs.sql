-- 订单作废与收款冲销迁移

-- 订单增加累计冲销金额；可退余额仍 = paid_cents - refunded_cents（派生，不落地），
-- 终态判定另减累计冲销：paid > 0 且 paid - refunded - written_off 归零即收付完结
ALTER TABLE orders ADD COLUMN written_off_cents INTEGER NOT NULL DEFAULT 0;

-- 流水增加冲销结果金额，与既有结果金额连贯解释状态由来
ALTER TABLE ledger_entries ADD COLUMN written_off_result_cents INTEGER NOT NULL DEFAULT 0;

-- 作废记录：业务标识在租户内唯一，标识作废操作本身，与订单标识、冲正业务标识独立
CREATE TABLE IF NOT EXISTS voids(
  tenant TEXT NOT NULL,
  biz_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, biz_id)
);

-- 冲销记录：业务标识在租户内唯一，标识冲销操作本身
CREATE TABLE IF NOT EXISTS writeoffs(
  tenant TEXT NOT NULL,
  biz_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, biz_id)
);

-- 按业务标识回查作废/冲销记录（重放命中）；作废与冲销的业务标识不得互相混用
CREATE INDEX IF NOT EXISTS idx_voids_tenant_order ON voids(tenant, order_id);
CREATE INDEX IF NOT EXISTS idx_writeoffs_tenant_order ON writeoffs(tenant, order_id);
