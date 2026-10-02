# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、批量受理导入、按标识读取订单、条件检索订单、登记收款、登记收款冲正（退款）与按订单追溯收付流水，并核对未收、累计冲正与可退余额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `POST /orders/batch`：批量受理导入。请求字段 `rows`，每行含 `tenant`、`order_id`、`amount_cents`、`currency`。逐行校验、逐行独立生效：某行被拒绝不影响其他行，也不产生流水；同一份提交内重复行只按第一次生效；订单标识已存在的行按重复受理拒绝，不修改已有订单与流水。返回 200 与逐行结论 `results`：成功行 `status=accepted` 并给出新订单结果金额，拒绝行 `status=rejected` 并给出可区分原因（`duplicate` 重复受理、`invalid_amount` 金额非法、`unsupported_currency` 币种不支持、`missing_field` 必填标识缺失或为空）。重放同一份提交时，已生效行不重复受理，被拒行仍被拒绝，既有订单与流水不变。
- `GET /orders`：条件检索订单。租户通过请求头 `X-Tenant` 传入；查询参数 `request_id`（必填，检索请求的去重标识，租户内唯一，与订单标识相互独立）、`status`、`currency`、`amount_min`/`amount_max`、`paid_min`/`paid_max`、`refunded_min`/`refunded_max`、`has_refund`（true/false）、`cursor`、`limit`（1–200，默认 50）。结果按订单标识升序稳定分页，返回 `items` 与分页位置 `next_cursor`（以其值作为下一页 `cursor`，为 `null` 表示末页）；无命中返回空列表。同一 `request_id` 重复检索只返回首次的同一结果集（响应头 `X-Idempotent-Replay: 1`）且不写入。参数非法返回 400 与可区分原因；跨租户或未提供租户不泄漏其他租户订单。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /orders/{order_id}/refunds`：登记收款冲正（退款）。请求字段 `biz_id`（本次冲正的业务标识，租户内唯一）、`amount_cents`；成功返回 200 与冲正记录（含 `biz_id`、`amount_cents`、`replayed` 及订单的 `paid_cents`、`outstanding_cents`、`refunded_cents`、`refundable_cents`）。同一 `biz_id` 重放返回同一记录（响应头 `X-Idempotent-Replay: 1`），不重复累计、不新增流水；超过可退余额或非正数返回 409/422；订单不存在或跨租户返回 404。
- `GET /orders/{order_id}/ledger`：按订单查询收付流水，按编号升序返回 `entries`，每条含 `seq`、`op_type`（accept/payment/refund）、`biz_id`、`amount_cents` 与结果金额 `paid_result_cents`、`outstanding_result_cents`、`refunded_result_cents`；订单不存在或跨租户返回 404。
- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优（写事务经 `BEGIN IMMEDIATE` 串行化）。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账。

## 简短调用示例

```bash
# 受理
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","order_id":"o1","amount_cents":500,"currency":"CNY"}'
# 收款（租户经 X-Tenant 头传入）
curl -s -X POST localhost:8000/orders/o1/payments -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"amount_cents":500}'
# 冲正：biz_id 标识本次冲正操作本身，与 order_id 独立，租户内唯一
curl -s -X POST localhost:8000/orders/o1/refunds -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"biz_id":"refund-0001","amount_cents":200}'
# 重放同一 biz_id：返回同一记录，不重复生效
curl -s -X POST localhost:8000/orders/o1/refunds -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"biz_id":"refund-0001","amount_cents":200}'
# 追溯流水
curl -s localhost:8000/orders/o1/ledger -H 'X-Tenant: t1'
# 批量受理导入：逐行独立生效，逐行返回结论
curl -s -X POST localhost:8000/orders/batch -H 'Content-Type: application/json' \
  -d '{"rows":[{"tenant":"t1","order_id":"o2","amount_cents":300,"currency":"CNY"},
               {"tenant":"t1","order_id":"o3","amount_cents":0,"currency":"CNY"}]}'
# 条件检索：request_id 标识这次检索请求本身，租户内唯一
curl -s 'localhost:8000/orders?request_id=q-0001&status=accepted&amount_min=100&limit=2' -H 'X-Tenant: t1'
# 翻页：以上一页返回的 next_cursor 作为 cursor
curl -s 'localhost:8000/orders?request_id=q-0002&cursor=o2' -H 'X-Tenant: t1'
```

金额口径（均为最小货币单位整数）：未收 = 订单金额 − 已收；累计冲正随冲正单调增加且不减少已收；可退余额 = 已收 − 累计冲正，单调下降，归零后订单进入 `completed` 终态。
