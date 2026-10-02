# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、批量受理导入、按标识读取订单、条件检索订单、登记收款、登记收款冲正（退款）、订单作废、收款冲销、收款冲正修正与取消冲正修正，并按订单追溯收付流水，核对未收、累计冲正、累计冲销与可退余额；另支持登记在原订单之下的独立退款单（受理、审核通过/驳回、执行到账/执行失败、撤销）及其条件检索。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/batch`：批量受理导入。请求字段 `rows`，每行含 `tenant`、`order_id`、`amount_cents`、`currency`。逐行校验、以行为单位独立生效：某行被拒绝不影响其他行，也不产生流水；同一份提交内重复行只按第一次生效，重复行拒绝；订单标识已存在的行按重复受理拒绝，不修改已有订单与流水。返回 200 与逐行结论 `results`（成功行含新订单结果金额 `order`，拒绝行含可区分原因 `reason`：`duplicate_acceptance`、`duplicate_in_batch`、`invalid_amount`、`unsupported_currency`、`missing_field`、`invalid_row`）及 `accepted`/`rejected` 计数。同一份提交重复导入时，已生效行不重复受理、被拒行仍被拒绝，重放不改变既有订单与流水。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/search`：条件检索订单。租户通过请求头 `X-Tenant` 传入；请求字段 `request_id`（本次检索的去重标识，标识检索请求本身，与订单标识独立，租户内唯一）、`filters`（可选：`status`、`currency`、`amount_min_cents`/`amount_max_cents`、`paid_min_cents`/`paid_max_cents`、`refunded_min_cents`/`refunded_max_cents`、`has_refund`）、`page`（可选：`size` 默认 50、上限 500，`cursor` 为上一页返回的 `next_cursor`）。结果按订单标识升序稳定分页，无新收付变动时翻页不重不漏；`next_cursor` 为 null 表示已到末页。同一 `request_id` 重复检索返回首次的同一结果集（响应头 `X-Idempotent-Replay: 1`），不重新查询、不重复写入。无命中返回空列表；参数非法返回 400 与可区分原因（`invalid_status`、`unsupported_currency`、`invalid_amount_range`、`invalid_paid_range`、`invalid_refunded_range`、`invalid_has_refund`、`invalid_page_size`、`invalid_cursor`、`unknown_filter:*`）；跨租户或未提供租户不泄漏其他租户订单。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /orders/{order_id}/refunds`：登记收款冲正（退款）。请求字段 `biz_id`（本次冲正的业务标识，租户内唯一）、`amount_cents`；成功返回 200 与冲正记录（含 `biz_id`、`amount_cents`、`replayed` 及订单的 `paid_cents`、`outstanding_cents`、`refunded_cents`、`written_off_cents`、`refundable_cents`）。同一 `biz_id` 重放返回同一记录（响应头 `X-Idempotent-Replay: 1`），不重复累计、不新增流水；超过可退余额或非正数返回 409/422；订单不存在或跨租户返回 404。
- `POST /orders/{order_id}/void`：作废订单。请求字段 `biz_id`（本次作废的业务标识，租户内唯一，与订单标识、冲正/冲销业务标识独立，不得混用）。仅未发生收款的订单可作废，已收大于零返回 409；生效后进入 `voided` 终态，此后收款、冲正、冲销、再作废一律返回 409 且无任何变更；不改订单金额与既有流水，仅追加一条 `void` 流水。同一 `biz_id` 重放返回首次的同一记录（响应头 `X-Idempotent-Replay: 1`），不重复写流水、不改状态；同租户将该标识用于另一订单的作废或用于冲销返回 409 且状态不变；不同标识各自生效。订单不存在或跨租户返回 404，不泄漏是否存在，也不产生流水。
- `POST /orders/{order_id}/writeoffs`：登记收款冲销。请求字段 `biz_id`（本次冲销的业务标识，租户内唯一）、`amount_cents`（正整数，不得超过 已收 − 累计冲正 − 累计冲销，超出或非正数返回 409/422 且无任何变更）。冲销不改已收与累计冲正，累计冲销单调增加；未收与可退余额不受冲销影响。同一 `biz_id` 重放返回首次的同一记录（响应头 `X-Idempotent-Replay: 1`），不重复累计、不新增流水；不同标识各自生效；订单不存在或跨租户返回 404。
- `POST /orders/{order_id}/corrections`：登记收款冲正修正。请求字段 `biz_id`（本次修正的业务标识，租户内唯一，标识修正操作本身，与订单标识及冲正、作废、冲销、取消修正的业务标识独立，不得混用）、`amount_cents`（正整数，不得超过 已收 − 累计冲正 − 累计冲销；超过、非正数或金额非法返回 409/422 且无任何变更）。生效后已收减少、未收增加，订单金额与累计冲正、累计冲销及既有流水不改，追加一条 `correction` 修正流水；状态按既有口径重判（已收跌破订单金额回到 `accepted`，达到终态口径则进 `completed`）。同一 `biz_id` 重放返回首次的同一记录（含 `cancelled` 标记，响应头 `X-Idempotent-Replay: 1`），不重复扣减、不新增流水；不同标识各自生效；订单不存在或跨租户返回 404。
- `POST /orders/{order_id}/correction-cancels`：取消一笔收款冲正修正。请求字段 `biz_id`（本次取消的业务标识，租户内唯一，与被取消修正的业务标识相互独立）、`correction_biz_id`（被取消修正的业务标识；取消金额取该修正的原始金额，不可指定）。仅已生效且未被取消的修正可取消；生效后已收恢复、未收减少，订单金额与既有流水不改，追加一条 `correction_cancel` 取消流水。被取消修正不存在（`correction not found`）、已被取消（`correction already cancelled`）、订单不匹配（`correction belongs to another order`）或恢复后已收会超过订单金额（`correction cancel would exceed order amount`）时返回 409 且无任何变更；已作废或终态订单一律拒绝。同一 `biz_id` 重放返回首次的同一记录（响应头 `X-Idempotent-Replay: 1`），不重复恢复、不新增流水；不同标识各自生效；订单不存在或跨租户返回 404。
- `GET /orders/{order_id}/ledger`：按订单查询收付流水，按编号升序返回 `entries`，每条含 `seq`、`op_type`（accept/payment/refund/void/writeoff/correction/correction_cancel）、`biz_id`、`amount_cents` 与结果金额 `paid_result_cents`、`outstanding_result_cents`、`refunded_result_cents`、`written_off_result_cents`；订单不存在或跨租户返回 404。

### 退款单（refund order）

退款单独立于订单收款冲正登记：它是登记在原订单之下、带自身单据标识的独立单据，记录申请退款金额 `request_cents` 与实退金额 `actual_cents`，在生命周期内经历 `accepted`（已受理）、`approved`（审核通过待执行）、`rejected`（审核驳回）、`settled`（已到账）、`failed`（执行失败）、`cancelled`（已撤销）。退款单标识在租户内唯一，并与冲正/作废/冲销/修正/取消的业务标识共用命名空间，不得混用。除受理外租户均经 `X-Tenant` 请求头传入，金额为最小货币单位正整数。

- `POST /refund-orders`：受理退款单。请求字段 `tenant`、`refund_id`、`order_id`、`request_cents`。成功返回 201 与退款单对象；同一（租户, 退款单标识）重复受理同一原订单返回首次的同一单据、不新建且状态码为 200（响应头 `X-Idempotent-Replay: 1`）；退款单标识已存在但指向另一原订单（`refund_id already used for another order`）或与订单侧业务标识冲突（`refund_id conflicts with order operation biz_id`）返回 409 且不改任何数据；原订单不存在或跨租户返回 404。退款单可受理在任意状态（含已作废、收付完结）的订单上；申请金额非正或字段缺失返回 422。
- `GET /refund-orders/{refund_id}`：按标识读取退款单，含 `status`、`request_cents`、`actual_cents`、`failure_reason`、`attempts` 与关联的 `order_id`；不存在或跨租户返回 404（不泄漏是否存在）。
- `POST /refund-orders/{refund_id}/review`：审核。请求字段 `approved`（布尔）。仅 `accepted` 可审核：通过进入 `approved`，驳回进入 `rejected`；对其他状态审核返回 409（`refund already approved/rejected/...`）；驳回后不可执行、可撤销。
- `POST /refund-orders/{refund_id}/execute`：执行审核通过的退款单。请求字段 `failure_reason` 可选：缺省或 null 表示执行成功，非空字符串表示上报执行失败。成功一次：把实退金额（取申请金额）计入原订单累计冲正，要求不超过当时可退余额（`refund exceeds refundable balance` 返回 409 且退款单保持 `approved`、原订单与流水不变），成功后退款单进入 `settled`，追加原订单退款流水（`biz_id` 记退款单标识）并按既有口径重判原订单状态；重复执行返回首次的同一结果（响应头 `X-Idempotent-Replay: 1`），不重复入账、不新增流水。失败只登记失败原因与次数（`failed`），不动原订单、不产生流水，可再次执行。仅 `approved`/`failed` 可执行，`accepted`（`refund not approved`）、`rejected`、`cancelled` 返回 409；原订单已作废、已进入收付完结终态、被删除或跨租户时按不存在处理（404 `order not found`），不产生流水与状态变化。
- `POST /refund-orders/{refund_id}/cancel`：撤销退款单。仅 `accepted`、`rejected`、`failed` 可撤销，撤销后进入 `cancelled` 终态、不可再执行；`approved`（`refund approved and awaiting execution`）、`settled`、已撤销返回 409；退款单不存在或跨租户返回 404。任意撤销或驳回都不改原订单与流水，无半更新。
- `POST /refund-orders/search`：条件检索退款单。租户经请求头 `X-Tenant` 传入；请求字段 `request_id`（本次检索去重标识，租户内唯一，与退款单标识独立）、`filters`（可选：`status`、`order_id`、`request_min_cents`/`request_max_cents`）、`page`（可选：`size` 默认 50、上限 500，`cursor` 为上一页 `next_cursor`）。结果按退款单标识升序稳定分页，无新受理时翻页不重不漏；`next_cursor` 为 null 表示末页。同一 `request_id` 重放返回首次同一结果集快照（响应头 `X-Idempotent-Replay: 1`），不重新查询、不重复写入。无命中返回空列表；参数非法返回 400 与可区分原因（`invalid_status`、`invalid_order_id`、`invalid_request_range`、`invalid_page_size`、`invalid_cursor`、`unknown_filter:*` 等）且不写入；跨租户或未提供租户不泄漏其他租户退款单。

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
# 批量受理导入：逐行独立生效，拒绝行不影响其他行
curl -s -X POST localhost:8000/orders/batch -H 'Content-Type: application/json' \
  -d '{"rows":[
    {"tenant":"t1","order_id":"o2","amount_cents":300,"currency":"CNY"},
    {"tenant":"t1","order_id":"o1","amount_cents":500,"currency":"CNY"},
    {"tenant":"t1","order_id":"o3","amount_cents":0,"currency":"CNY"}
  ]}'
# 条件检索：request_id 标识本次检索本身，重放返回首次的同一结果集
curl -s -X POST localhost:8000/orders/search -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"q-0001","filters":{"status":"accepted","currency":"CNY","amount_min_cents":100},"page":{"size":20}}'
# 翻页：带上上一页返回的 next_cursor
curl -s -X POST localhost:8000/orders/search -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"q-0002","filters":{"status":"accepted"},"page":{"size":20,"cursor":"o2"}}'
# 收款（租户经 X-Tenant 头传入）
curl -s -X POST localhost:8000/orders/o1/payments -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"amount_cents":500}'
# 冲正：biz_id 标识本次冲正操作本身，与 order_id 独立，租户内唯一
curl -s -X POST localhost:8000/orders/o1/refunds -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"biz_id":"refund-0001","amount_cents":200}'
# 重放同一 biz_id：返回同一记录，不重复生效
curl -s -X POST localhost:8000/orders/o1/refunds -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"biz_id":"refund-0001","amount_cents":200}'
# 冲销：金额不得超过 已收 − 累计冲正 − 累计冲销；不改已收与可退余额
curl -s -X POST localhost:8000/orders/o1/writeoffs -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"biz_id":"wo-0001","amount_cents":100}'
# 作废：仅未收款订单可作废；biz_id 标识作废操作本身，重放返回同一记录
curl -s -X POST localhost:8000/orders/o2/void -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"biz_id":"void-0001"}'
# 收款冲正修正：减少已收、增加未收；金额不得超过 已收 − 累计冲正 − 累计冲销
curl -s -X POST localhost:8000/orders/o1/corrections -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"biz_id":"corr-0001","amount_cents":100}'
# 取消冲正修正：取消金额取被取消修正的原始金额，无需也不可指定
curl -s -X POST localhost:8000/orders/o1/correction-cancels -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"biz_id":"cancel-0001","correction_biz_id":"corr-0001"}'
# 追溯流水
curl -s localhost:8000/orders/o1/ledger -H 'X-Tenant: t1'
# 退款单：受理（租户在请求体，沿用受理订单的习惯），标识独立于订单与冲正业务标识
curl -s -X POST localhost:8000/refund-orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","refund_id":"ro-0001","order_id":"o1","request_cents":200}'
# 审核：通过或驳回（驳回后不可执行、可撤销）
curl -s -X POST localhost:8000/refund-orders/ro-0001/review -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"approved":true}'
# 执行到账：实退金额取申请金额，计入原订单累计冲正并追加退款流水
curl -s -X POST localhost:8000/refund-orders/ro-0001/execute -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{}'
# 执行失败上报（可再次执行）；成功到账后重复执行为重放，不重复入账
curl -s -X POST localhost:8000/refund-orders/ro-0001/execute -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"failure_reason":"gateway timeout"}'
# 撤销：仅已受理、已驳回、执行失败可撤销
curl -s -X POST localhost:8000/refund-orders/ro-0001/cancel -H 'X-Tenant: t1'
# 按标识读取
curl -s localhost:8000/refund-orders/ro-0001 -H 'X-Tenant: t1'
# 条件检索：按状态、原订单、申请金额区间过滤，按退款单标识升序稳定分页
curl -s -X POST localhost:8000/refund-orders/search -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"rq-0001","filters":{"order_id":"o1","request_min_cents":100},"page":{"size":20}}'
```

金额口径（均为最小货币单位整数）：未收 = 订单金额 − 已收；累计冲正随冲正单调增加且不减少已收；累计冲销随冲销单调增加且不改已收与累计冲正；可退余额 = 已收 − 累计冲正，不受冲销影响。冲正修正减少已收、增加未收，不改订单金额与累计冲正、累计冲销；取消冲正修正按原始金额恢复已收、减少未收；修正与取消后按既有口径重判状态，可退余额与冲销上限随已收同步变化。进入 `completed` 终态当且仅当已收大于零且 可退余额 − 累计冲销 归零；终态不可逆，此后收款、冲正、冲销、作废、修正与取消修正一律拒绝。结清（`settled`）不等于终态，结清后仍可在可退余额范围内冲正或冲销。作废与终态互斥：无收款订单只能作废（`voided`）、不进终态；进终态订单必已收款、不可作废。
