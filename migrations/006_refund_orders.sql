-- 退款单（refund order）迁移
-- 退款单是登记在原订单之下的独立单据：带自身单据标识，记录申请退款金额与实退金额，
-- 生命周期：accepted（已受理）→ approved（审核通过待执行）/ rejected（审核驳回）
--   → settled（已到账）/ failed（执行失败可重试）→ cancelled（已撤销终态）。
-- 无需 ALTER 现有表：执行到账时复用 ledger_entries 对原订单累计冲正的既有口径。

-- 退款单：以（租户, 退款单标识）唯一；order_id 指向同一租户的原订单
CREATE TABLE IF NOT EXISTS refund_orders(
  tenant TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  request_cents INTEGER NOT NULL,          -- 申请退款金额（最小货币单位正整数）
  actual_cents INTEGER NOT NULL DEFAULT 0, -- 实退金额（执行成功时落申请金额）
  status TEXT NOT NULL,                    -- accepted / approved / rejected / settled / failed / cancelled
  failure_reason TEXT,                     -- 执行失败原因
  attempts INTEGER NOT NULL DEFAULT 0,     -- 已发起执行次数（失败累计、成功一次）
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, refund_id)
);

-- 按原订单回查退款单（执行时按订单加锁、检索按订单过滤）
CREATE INDEX IF NOT EXISTS idx_refund_orders_tenant_order ON refund_orders(tenant, order_id);

-- 退款单检索请求：去重标识标识检索请求本身，与退款单标识相互独立，租户内唯一。
-- 首次检索落库响应快照，同一（租户, 去重标识）重放返回同一结果集，不重新查询、不重复写入。
CREATE TABLE IF NOT EXISTS refund_search_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, request_id)
);
