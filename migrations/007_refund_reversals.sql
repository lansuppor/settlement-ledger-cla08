-- 退款单执行冲正（refund reversal）迁移
-- 已到账（succeeded）的退款单可执行冲正：把实退金额从原订单累计冲正中扣减，
-- 退款单进入 reversed（已冲正）终态并落库本次冲正的业务标识。

-- 退款单记录本次冲正的业务标识；未冲正为 NULL。
ALTER TABLE refund_orders ADD COLUMN reversal_biz_id TEXT;

-- 冲正记录：业务标识在租户内唯一，标识本次冲正操作本身，与退款单标识、原订单标识
-- 及订单侧冲正、作废、冲销、冲正修正、取消修正的业务标识独立，不得混用。
-- 冲正金额取退款单已落库的实退金额，调用方不可指定。
-- 同时落库冲正生效当时的原订单结果金额快照，使同一（租户, 业务标识）的重放
-- 返回首次的同一记录（含当时订单结果金额），不重复扣减、不新增流水。
CREATE TABLE IF NOT EXISTS refund_reversals(
  tenant TEXT NOT NULL,
  biz_id TEXT NOT NULL,                -- 本次冲正的业务标识，租户内唯一
  refund_id TEXT NOT NULL,             -- 被冲正的退款单标识
  order_id TEXT NOT NULL,              -- 原订单标识
  amount_cents INTEGER NOT NULL,       -- 冲正金额 = 退款单实退金额
  paid_result_cents INTEGER NOT NULL,        -- 冲正后原订单已收（不变，落库快照）
  outstanding_result_cents INTEGER NOT NULL, -- 冲正后原订单未收
  refunded_result_cents INTEGER NOT NULL,    -- 冲正后原订单累计冲正
  written_off_result_cents INTEGER NOT NULL, -- 冲正后原订单累计冲销（不变）
  order_status TEXT NOT NULL,                -- 冲正后按既有口径重判的原订单状态
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, biz_id)
);

-- 按退款单回查冲正记录
CREATE INDEX IF NOT EXISTS idx_refund_reversals_tenant_refund ON refund_reversals(tenant, refund_id);
