-- 条件检索请求去重迁移

-- 检索请求：去重标识在租户内唯一，标识检索请求本身，与订单标识相互独立。
-- 首次检索落库响应快照，同一（租户, 去重标识）重放返回同一结果集，不重新查询、不重复写入。
CREATE TABLE IF NOT EXISTS search_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, request_id)
);
