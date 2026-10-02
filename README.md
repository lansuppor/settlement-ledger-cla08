# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款、登记收款冲正（退款）与按订单追溯收付流水，并核对未收、累计冲正与可退余额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
```

金额口径（均为最小货币单位整数）：未收 = 订单金额 − 已收；累计冲正随冲正单调增加且不减少已收；可退余额 = 已收 − 累计冲正，单调下降，归零后订单进入 `completed` 终态。
