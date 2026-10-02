-- 批量受理导入与订单条件检索迁移

-- 检索去重：请求标识在租户内唯一，标识一次检索请求本身，与订单标识相互独立。
-- 首次检索把结果集快照落库；同一（租户, 请求标识）重放只返回首次结果，不再写入。
CREATE TABLE IF NOT EXISTS search_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  response TEXT NOT NULL,             -- 首次检索结果集快照（JSON）
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, request_id)
);
