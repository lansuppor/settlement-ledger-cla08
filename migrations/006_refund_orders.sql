-- 退款单（refund order）迁移
-- 退款单独立于订单收款冲正登记：登记在原订单之下的独立单据，带自身单据标识，
-- 记录申请退款金额与实退金额，生命周期：
-- accepted（已受理）→ approved（审核通过）/ rejected（审核驳回）
-- approved → succeeded（已到账）/ failed（执行失败，可再次执行）
-- accepted/rejected/failed → cancelled（已撤销终态）
CREATE TABLE IF NOT EXISTS refund_orders(
  tenant TEXT NOT NULL,
  refund_id TEXT NOT NULL,             -- 退款单标识，租户内唯一，与订单侧业务标识共用唯一性
  order_id TEXT NOT NULL,              -- 关联的原订单标识
  requested_amount_cents INTEGER NOT NULL,  -- 申请退款金额（最小货币单位正整数）
  refunded_amount_cents INTEGER,       -- 实退金额：执行到账后落库，此前为 NULL
  status TEXT NOT NULL,
  failure_reason TEXT,                 -- 最近一次执行失败原因；无失败或后又到账为 NULL
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, refund_id)
);

CREATE INDEX IF NOT EXISTS idx_refund_orders_tenant_order ON refund_orders(tenant, order_id);
CREATE INDEX IF NOT EXISTS idx_refund_orders_tenant_status ON refund_orders(tenant, status);

-- 退款单执行到账会在既有冲正登记表写入一条冲正（业务标识记退款单标识）。
-- 标记来源，使直接冲正接口将该标识识别为跨单据类型占用（命名空间冲突），
-- 而非误判为自身请求的重放；直接冲正登记该列恒为 NULL。
ALTER TABLE refunds ADD COLUMN refund_order_id TEXT;

-- 退款单条件检索请求：去重标识标识检索请求本身，与退款单标识相互独立，租户内唯一。
-- 与订单检索分表，两类检索的去重标识互不干扰。
CREATE TABLE IF NOT EXISTS refund_search_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, request_id)
);
