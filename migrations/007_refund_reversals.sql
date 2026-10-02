-- 退款单执行冲正（refund reversal）迁移
--
-- 已到账（succeeded）退款单可凭独立的冲正业务标识执行冲正：把已落库的实退金额
-- 重新入账回原订单——在原订单累计冲正中扣减该实退金额（累计冲正单调减少这次的量），
-- 追加一条原订单冲正流水（op_type='refund_reversal'，业务标识记本次冲正的业务标识，
-- 金额记实退金额），并按既有口径重判原订单状态；订单金额、已收、累计冲销及既有流水不改。
--
-- 本次冲正的业务标识标识冲正操作本身：与退款单标识、原订单标识及订单侧冲正、作废、
-- 冲销、冲正修正、取消修正的业务标识独立，租户内唯一，不得混用。同一（租户, 业务标识）
-- 重复请求视为重放，返回首次的同一记录（含重放标记与当时订单结果金额快照），
-- 不重复扣减、不新增流水。
--
-- 冲正后退款单进入 reversed（已冲正）终态并落库冲正的业务标识；此后审核、执行到账、
-- 执行失败、撤销、再冲正一律拒绝。重放判定以本次冲正的业务标识为准，与退款单标识互不干扰。
CREATE TABLE IF NOT EXISTS refund_reversals(
  tenant TEXT NOT NULL,
  biz_id TEXT NOT NULL,                -- 本次冲正的业务标识，租户内唯一
  refund_id TEXT NOT NULL,            -- 被冲正的退款单标识
  order_id TEXT NOT NULL,             -- 关联的原订单标识
  amount_cents INTEGER NOT NULL,      -- 金额取退款单已落库的实退金额，请求不可指定
  -- 首次生效时的原订单结果金额快照：同标识重放返回首次同一记录与“当时订单结果金额”
  paid_result_cents INTEGER NOT NULL,
  outstanding_result_cents INTEGER NOT NULL,
  refunded_result_cents INTEGER NOT NULL,
  written_off_result_cents INTEGER NOT NULL,
  order_status TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, biz_id)
);

CREATE INDEX IF NOT EXISTS idx_refund_reversals_tenant_refund ON refund_reversals(tenant, refund_id);

-- 退款单新终态：已冲正。列存本次冲正的业务标识（未冲正为 NULL）。
ALTER TABLE refund_orders ADD COLUMN reversal_biz_id TEXT;
