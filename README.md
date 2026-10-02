# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款、登记冲正（退款）并查询收付流水；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/refunds`：登记冲正（退款）。请求字段 `refund_id`（租户内唯一的业务标识）、`amount_cents`。冲正金额不得超过可退余额（已收 − 累计冲正），超出返回 409；订单不存在或跨租户返回 404。首次生效返回 201 与冲正记录及订单金额汇总；同一 `refund_id` 重放返回 200 与同一冲正记录，不重复生效。可退余额归零后订单进入 `closed` 终态，后续冲正一律 409。
- `GET /orders/{order_id}/ledger`：查询该订单全部收付流水，按编号 `seq` 升序；每条含操作类型 `op`（`accept`/`payment`/`refund`）、业务标识 `biz_id`（若有）、金额与结果金额（`paid_cents`、`outstanding_cents`、`refunded_cents`）。跨租户查询返回 404。
- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账。
