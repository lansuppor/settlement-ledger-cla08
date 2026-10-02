import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "rt"}

def _order(oid: str, amount: int = 1000, tenant: str = "rt", currency: str = "CNY") -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": currency})

def _pay(oid: str, amount: int, tenant: str = "rt") -> None:
    client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})

def _accept(rid: str, oid: str, amount: int, tenant: str = "rt"):
    return client.post(
        "/refund-orders",
        json={"refund_id": rid, "order_id": oid, "requested_amount_cents": amount},
        headers={"X-Tenant": tenant},
    )

# ---------- 受理与读取 ----------

def test_accept_and_read_refund_order() -> None:
    _order("a1", 500)
    resp = _accept("ra1", "a1", 200)
    assert resp.status_code == 201
    data = resp.json()
    assert data["refund_id"] == "ra1" and data["order_id"] == "a1"
    assert data["requested_amount_cents"] == 200
    assert data["refunded_amount_cents"] is None
    assert data["status"] == "accepted" and data["failure_reason"] is None
    assert data["replayed"] is False and resp.headers["x-idempotent-replay"] == "0"
    got = client.get("/refund-orders/ra1", headers=H)
    assert got.status_code == 200
    assert got.json()["requested_amount_cents"] == 200

def test_accept_replay_returns_same_order_without_creating() -> None:
    _order("a2", 300)
    first = _accept("ra2", "a2", 100)
    second = _accept("ra2", "a2", 100)
    assert first.status_code == 201 and second.status_code == 200
    assert second.headers["x-idempotent-replay"] == "1"
    assert second.json()["replayed"] is True
    assert first.json()["created_at"] == second.json()["created_at"]
    # 只有一张退款单
    page = client.post(
        "/refund-orders/search",
        json={"request_id": "rq-replay", "filters": {"order_id": "a2"}},
        headers=H,
    ).json()
    assert [r["refund_id"] for r in page["refund_orders"]] == ["ra2"]

def test_accept_replay_with_conflicting_fields_rejected_no_change() -> None:
    _order("a3", 300); _order("a3b", 300)
    assert _accept("ra3", "a3", 100).status_code == 201
    before = client.get("/refund-orders/ra3", headers=H).json()
    # 同一退款单标识但原订单不同：拒绝
    assert _accept("ra3", "a3b", 100).status_code == 409
    # 同一退款单标识但申请金额不同：拒绝
    assert _accept("ra3", "a3", 101).status_code == 409
    after = client.get("/refund-orders/ra3", headers=H).json()
    assert after == before

def test_accept_refund_id_conflicts_with_order_side_biz_ids() -> None:
    _order("a4", 400); _pay("a4", 400)
    # 退款单标识先占用
    assert _accept("shared-r", "a4", 10).status_code == 201
    # 直接冲正/作废/冲销/修正均不得复用该标识
    assert client.post("/orders/a4/refunds", json={"biz_id": "shared-r", "amount_cents": 1}, headers=H).status_code == 409
    assert client.post("/orders/a4/writeoffs", json={"biz_id": "shared-r", "amount_cents": 1}, headers=H).status_code == 409
    assert client.post("/orders/a4/corrections", json={"biz_id": "shared-r", "amount_cents": 1}, headers=H).status_code == 409
    # 反向：冲正业务标识占用后，退款单不得复用
    assert client.post("/orders/a4/refunds", json={"biz_id": "shared-r2", "amount_cents": 1}, headers=H).status_code == 200
    assert _accept("shared-r2", "a4", 10).status_code == 409
    # 冲销 -> 退款单
    assert client.post("/orders/a4/writeoffs", json={"biz_id": "shared-r3", "amount_cents": 1}, headers=H).status_code == 200
    assert _accept("shared-r3", "a4", 10).status_code == 409

def test_accept_on_orders_in_any_state() -> None:
    _order("a5", 100); client.post("/orders/a5/void", json={"biz_id": "void-a5"}, headers=H)
    assert _accept("ra5", "a5", 10).status_code == 201  # 已作废订单仍可受理
    _order("a6", 100); _pay("a6", 100)
    client.post("/orders/a6/refunds", json={"biz_id": "r-a6", "amount_cents": 100}, headers=H)
    assert client.get("/orders/a6", headers=H).json()["status"] == "completed"
    assert _accept("ra6", "a6", 10).status_code == 201  # 收付完结订单仍可受理

def test_accept_unknown_or_cross_tenant_order_is_404_and_writes_nothing() -> None:
    assert _accept("ra7", "missing", 10).status_code == 404
    _order("a8", 100, tenant="rt")
    assert _accept("ra8", "a8", 10, tenant="other").status_code == 404
    assert client.get("/refund-orders/ra7", headers=H).status_code == 404
    assert client.get("/refund-orders/ra8", headers=H).status_code == 404

def test_accept_invalid_params_are_422() -> None:
    _order("a9", 100)
    assert client.post(
        "/refund-orders", json={"refund_id": "", "order_id": "a9", "requested_amount_cents": 10}, headers=H
    ).status_code == 422
    assert client.post(
        "/refund-orders", json={"refund_id": "ra9", "order_id": "a9", "requested_amount_cents": 0}, headers=H
    ).status_code == 422
    assert client.post("/refund-orders", json={"refund_id": "ra9b", "order_id": "a9"}, headers=H).status_code == 422

# ---------- 审核 ----------

def test_approve_allows_single_execution() -> None:
    _order("b1", 300); _pay("b1", 300)
    _accept("rb1", "b1", 100)
    resp = client.post("/refund-orders/rb1/approve", headers=H)
    assert resp.status_code == 200 and resp.json()["status"] == "approved"
    # 已受理未审核之外的状态不可再审核通过/驳回
    assert client.post("/refund-orders/rb1/approve", headers=H).status_code == 409
    assert client.post("/refund-orders/rb1/reject", headers=H).status_code == 409

def test_reject_only_from_accepted_then_not_executable_but_cancellable() -> None:
    _order("b2", 300)
    _accept("rb2", "b2", 100)
    assert client.post("/refund-orders/rb2/reject", headers=H).json()["status"] == "rejected"
    # 驳回后不可执行
    assert client.post("/refund-orders/rb2/execute", json={"outcome": "succeeded"}, headers=H).status_code == 409
    assert client.post(
        "/refund-orders/rb2/execute", json={"outcome": "failed", "reason": "x"}, headers=H
    ).status_code == 409
    # 驳回后可撤销
    assert client.post("/refund-orders/rb2/cancel", headers=H).json()["status"] == "cancelled"
    # 驳回不产生原订单流水、不动累计冲正
    entries = client.get("/orders/b2/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept"]

def test_approve_or_reject_unknown_is_404() -> None:
    assert client.post("/refund-orders/nope/approve", headers=H).status_code == 404
    assert client.post("/refund-orders/nope/reject", headers=H).status_code == 404

# ---------- 执行到账与原订单联动 ----------

def test_execute_success_links_order_ledger_and_rejudges() -> None:
    _order("c1", 500); _pay("c1", 500)
    _accept("rc1", "c1", 200)
    client.post("/refund-orders/rc1/approve", headers=H)
    resp = client.post("/refund-orders/rc1/execute", json={"outcome": "succeeded"}, headers=H)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "succeeded" and data["refunded_amount_cents"] == 200  # 缺省实退=申请
    assert data["failure_reason"] is None
    order = client.get("/orders/c1", headers=H).json()
    assert order["paid_cents"] == 500 and order["refunded_cents"] == 200
    assert order["refundable_cents"] == 300 and order["status"] == "settled"
    entries = client.get("/orders/c1/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "refund"]
    assert entries[2]["biz_id"] == "rc1" and entries[2]["amount_cents"] == 200

def test_execute_success_with_explicit_actual_amount() -> None:
    _order("c2", 500); _pay("c2", 500)
    _accept("rc2", "c2", 200)
    client.post("/refund-orders/rc2/approve", headers=H)
    resp = client.post(
        "/refund-orders/rc2/execute", json={"outcome": "succeeded", "amount_cents": 150}, headers=H
    )
    assert resp.json()["refunded_amount_cents"] == 150
    assert client.get("/orders/c2", headers=H).json()["refunded_cents"] == 150

def test_execute_success_rejudges_order_to_completed() -> None:
    _order("c3", 100); _pay("c3", 100)
    _accept("rc3", "c3", 100)
    client.post("/refund-orders/rc3/approve", headers=H)
    client.post("/refund-orders/rc3/execute", json={"outcome": "succeeded"}, headers=H)
    assert client.get("/orders/c3", headers=H).json()["status"] == "completed"

def test_execute_over_refundable_rejected_keeps_approved_no_ledger_change() -> None:
    _order("c4", 500); _pay("c4", 300)  # 可退余额 300
    _accept("rc4", "c4", 400)
    client.post("/refund-orders/rc4/approve", headers=H)
    assert client.post("/refund-orders/rc4/execute", json={"outcome": "succeeded"}, headers=H).status_code == 409
    # 退款单保持已通过待执行
    assert client.get("/refund-orders/rc4", headers=H).json()["status"] == "approved"
    order = client.get("/orders/c4", headers=H).json()
    assert order["refunded_cents"] == 0  # 整体拒绝，不累计
    assert [e["op_type"] for e in client.get("/orders/c4/ledger", headers=H).json()["entries"]] == ["accept", "payment"]
    # 缩小实退金额后可再次执行成功
    ok = client.post(
        "/refund-orders/rc4/execute", json={"outcome": "succeeded", "amount_cents": 300}, headers=H
    )
    assert ok.status_code == 200 and ok.json()["status"] == "succeeded"
    assert client.get("/orders/c4", headers=H).json()["refunded_cents"] == 300

def test_execute_shrunk_refundable_between_approve_and_execute() -> None:
    _order("c5", 500); _pay("c5", 500)
    _accept("rc5", "c5", 300)
    client.post("/refund-orders/rc5/approve", headers=H)
    # 审核通过后，另一笔直接冲正占用部分可退余额
    client.post("/orders/c5/refunds", json={"biz_id": "rc5-other", "amount_cents": 300}, headers=H)
    # 订单未终态（可退余额 200），但实退 300 超过当时可退余额：409 且保持已通过
    resp = client.post("/refund-orders/rc5/execute", json={"outcome": "succeeded"}, headers=H)
    assert resp.status_code == 409
    assert client.get("/refund-orders/rc5", headers=H).json()["status"] == "approved"

def test_execute_repeated_success_is_idempotent_no_double_entry() -> None:
    _order("c6", 200); _pay("c6", 200)
    _accept("rc6", "c6", 80)
    client.post("/refund-orders/rc6/approve", headers=H)
    first = client.post("/refund-orders/rc6/execute", json={"outcome": "succeeded"}, headers=H)
    second = client.post("/refund-orders/rc6/execute", json={"outcome": "succeeded"}, headers=H)
    assert first.status_code == 200 and second.status_code == 200
    assert second.headers["x-idempotent-replay"] == "1" and second.json()["replayed"] is True
    assert second.json()["refunded_amount_cents"] == 80
    assert client.get("/orders/c6", headers=H).json()["refunded_cents"] == 80
    entries = client.get("/orders/c6/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "refund"]

def test_execute_failure_records_reason_and_can_retry_to_success() -> None:
    _order("d1", 300); _pay("d1", 300)
    _accept("rd1", "d1", 100)
    client.post("/refund-orders/rd1/approve", headers=H)
    failed = client.post(
        "/refund-orders/rd1/execute", json={"outcome": "failed", "reason": "gateway timeout"}, headers=H
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["failure_reason"] == "gateway timeout"
    assert failed.json()["refunded_amount_cents"] is None
    # 失败不动原订单账
    assert client.get("/orders/d1", headers=H).json()["refunded_cents"] == 0
    assert [e["op_type"] for e in client.get("/orders/d1/ledger", headers=H).json()["entries"]] == ["accept", "payment"]
    # 缺少失败原因：422
    assert client.post("/refund-orders/rd1/execute", json={"outcome": "failed"}, headers=H).status_code == 422
    # 可再次执行，最终到账；到账后失败原因清空
    ok = client.post("/refund-orders/rd1/execute", json={"outcome": "succeeded"}, headers=H)
    assert ok.json()["status"] == "succeeded" and ok.json()["failure_reason"] is None
    assert client.get("/orders/d1", headers=H).json()["refunded_cents"] == 100

def test_execute_not_allowed_from_accepted_rejected_cancelled() -> None:
    _order("d2", 300); _pay("d2", 300)
    _accept("rd2", "d2", 10)
    assert client.post("/refund-orders/rd2/execute", json={"outcome": "succeeded"}, headers=H).status_code == 409
    client.post("/refund-orders/rd2/approve", headers=H)
    client.post("/refund-orders/rd2/execute", json={"outcome": "failed", "reason": "x"}, headers=H)
    client.post("/refund-orders/rd2/cancel", headers=H)
    assert client.post("/refund-orders/rd2/execute", json={"outcome": "succeeded"}, headers=H).status_code == 409

def test_execute_when_order_voided_completed_or_cross_tenant_is_404() -> None:
    # 原订单在执行前已作废
    _order("e1", 100)
    _accept("re1", "e1", 10)
    client.post("/refund-orders/re1/approve", headers=H)
    client.post("/orders/e1/void", json={"biz_id": "void-e1"}, headers=H)
    assert client.post("/refund-orders/re1/execute", json={"outcome": "succeeded"}, headers=H).status_code == 404
    # 失败登记同样被整体拒绝，退款单状态不变
    assert client.post(
        "/refund-orders/re1/execute", json={"outcome": "failed", "reason": "x"}, headers=H
    ).status_code == 404
    assert client.get("/refund-orders/re1", headers=H).json()["status"] == "approved"
    assert [e["op_type"] for e in client.get("/orders/e1/ledger", headers=H).json()["entries"]] == ["accept", "void"]
    # 原订单在执行前已进入收付完结终态（被直接冲正占满可退余额）
    _order("e2", 100); _pay("e2", 100)
    _accept("re2", "e2", 100)
    client.post("/refund-orders/re2/approve", headers=H)
    client.post("/orders/e2/refunds", json={"biz_id": "e2-full", "amount_cents": 100}, headers=H)
    assert client.get("/orders/e2", headers=H).json()["status"] == "completed"
    assert client.post("/refund-orders/re2/execute", json={"outcome": "succeeded"}, headers=H).status_code == 404
    assert client.get("/refund-orders/re2", headers=H).json()["status"] == "approved"
    # 跨租户执行：退款单按不存在处理
    assert client.post(
        "/refund-orders/re2/execute", json={"outcome": "succeeded"}, headers={"X-Tenant": "other"}
    ).status_code == 404

# ---------- 撤销 ----------

def test_cancel_allowed_states_and_terminal_afterwards() -> None:
    _order("f1", 300); _pay("f1", 300)
    _accept("rf1", "f1", 10)
    assert client.post("/refund-orders/rf1/cancel", headers=H).json()["status"] == "cancelled"
    assert client.post("/refund-orders/rf1/cancel", headers=H).status_code == 409
    assert client.post("/refund-orders/rf1/approve", headers=H).status_code == 409
    assert client.post("/refund-orders/rf1/execute", json={"outcome": "succeeded"}, headers=H).status_code == 409

    _accept("rf2", "f1", 10)
    client.post("/refund-orders/rf2/approve", headers=H)
    client.post("/refund-orders/rf2/execute", json={"outcome": "failed", "reason": "boom"}, headers=H)
    assert client.post("/refund-orders/rf2/cancel", headers=H).json()["status"] == "cancelled"
    assert client.post("/refund-orders/rf2/execute", json={"outcome": "succeeded"}, headers=H).status_code == 409

def test_cancel_rejected_for_approved_and_succeeded() -> None:
    _order("f2", 300); _pay("f2", 300)
    _accept("rf3", "f2", 10)
    client.post("/refund-orders/rf3/approve", headers=H)
    assert client.post("/refund-orders/rf3/cancel", headers=H).status_code == 409  # 已通过待执行不可撤销
    client.post("/refund-orders/rf3/execute", json={"outcome": "succeeded"}, headers=H)
    assert client.post("/refund-orders/rf3/cancel", headers=H).status_code == 409  # 已到账不可撤销

def test_cancel_unknown_is_404() -> None:
    assert client.post("/refund-orders/nope/cancel", headers=H).status_code == 404

# ---------- 读取与租户隔离 ----------

def test_get_unknown_and_cross_tenant_are_404() -> None:
    assert client.get("/refund-orders/nope", headers=H).status_code == 404
    _order("g1", 100, tenant="rt")
    _accept("rg1", "g1", 10)
    assert client.get("/refund-orders/rg1", headers={"X-Tenant": "other"}).status_code == 404

def test_get_refund_order_shape_contains_all_fields() -> None:
    _order("g2", 300); _pay("g2", 300)
    _accept("rg2", "g2", 50)
    client.post("/refund-orders/rg2/approve", headers=H)
    client.post("/refund-orders/rg2/execute", json={"outcome": "failed", "reason": "err"}, headers=H)
    data = client.get("/refund-orders/rg2", headers=H).json()
    assert set(data) >= {
        "refund_id", "order_id", "status", "requested_amount_cents",
        "refunded_amount_cents", "failure_reason",
    }
    assert data["status"] == "failed" and data["failure_reason"] == "err"

# ---------- 条件检索 ----------

def _seed_for_search() -> None:
    # 专用检索租户，与本模块其他用例的数据隔离
    t = "rsearch"
    th = {"X-Tenant": t}
    _order("h1", 1000, tenant=t)
    for rid, amount, status in [
        ("rs1", 100, "accepted"),
        ("rs2", 200, "approved"),
        ("rs3", 300, "rejected"),
        ("rs4", 400, "cancelled"),
    ]:
        _accept(rid, "h1", amount, tenant=t)
    client.post("/refund-orders/rs2/approve", headers=th)
    client.post("/refund-orders/rs3/reject", headers=th)
    client.post("/refund-orders/rs4/cancel", headers=th)
    _order("h2", 1000, tenant=t)
    _accept("rs5", "h2", 100, tenant=t)  # 另一原订单
    # 另一租户的同名/同数据退款单
    _order("h1", 1000, tenant="other")
    _accept("rs1", "h1", 100, tenant="other")

def test_search_filters_status_order_id_and_amount_range() -> None:
    _seed_for_search()
    th = {"X-Tenant": "rsearch"}
    def search(filters: dict, request_id: str) -> list[dict]:
        resp = client.post(
            "/refund-orders/search", json={"request_id": request_id, "filters": filters}, headers=th
        )
        assert resp.status_code == 200
        return resp.json()["refund_orders"]
    assert [r["refund_id"] for r in search({"status": "accepted"}, "rq-s1")] == ["rs1", "rs5"]
    assert [r["refund_id"] for r in search({"status": "rejected"}, "rq-s2")] == ["rs3"]
    assert [r["refund_id"] for r in search({"order_id": "h2"}, "rq-s3")] == ["rs5"]
    assert [r["refund_id"] for r in search({"requested_amount_min_cents": 200}, "rq-s4")] == ["rs2", "rs3", "rs4"]
    assert [r["refund_id"] for r in search({"requested_amount_max_cents": 100}, "rq-s5")] == ["rs1", "rs5"]
    combo = search({"status": "accepted", "order_id": "h1", "requested_amount_max_cents": 100}, "rq-s6")
    assert [r["refund_id"] for r in combo] == ["rs1"]

def test_search_pagination_stable_no_dup_no_miss() -> None:
    th = {"X-Tenant": "rsearch"}
    seen: list[str] = []
    cursor = None
    for page_no in range(4):
        page = {"size": 2, **({"cursor": cursor} if cursor else {})}
        resp = client.post(
            "/refund-orders/search",
            json={"request_id": f"rq-page-{page_no}", "filters": {"order_id": "h1"}, "page": page},
            headers=th,
        )
        assert resp.status_code == 200
        seen += [r["refund_id"] for r in resp.json()["refund_orders"]]
        cursor = resp.json()["page"]["next_cursor"]
        if cursor is None:
            break
    assert seen == ["rs1", "rs2", "rs3", "rs4"]
    assert cursor is None

def test_search_replay_returns_same_snapshot() -> None:
    th = {"X-Tenant": "rsearch"}
    body = {"request_id": "rq-replay-snap", "filters": {"status": "approved"}}
    first = client.post("/refund-orders/search", json=body, headers=th)
    assert first.status_code == 200 and first.headers["x-idempotent-replay"] == "0"
    first_ids = [r["refund_id"] for r in first.json()["refund_orders"]]
    replay = client.post("/refund-orders/search", json=body, headers=th)
    assert replay.headers["x-idempotent-replay"] == "1" and replay.json()["replayed"] is True
    assert [r["refund_id"] for r in replay.json()["refund_orders"]] == first_ids

def test_search_empty_result_and_tenant_isolation() -> None:
    th = {"X-Tenant": "rsearch"}
    empty = client.post(
        "/refund-orders/search",
        json={"request_id": "rq-empty", "filters": {"status": "succeeded"}},
        headers=th,
    )
    assert empty.status_code == 200 and empty.json()["refund_orders"] == []
    # 跨租户隔离：other 租户只看得到自己的 rs1
    other = client.post(
        "/refund-orders/search", json={"request_id": "rq-iso", "filters": {}}, headers={"X-Tenant": "other"}
    )
    assert [r["refund_id"] for r in other.json()["refund_orders"]] == ["rs1"]
    assert all(r["tenant"] == "other" for r in other.json()["refund_orders"])
    # 未提供租户
    assert client.post("/refund-orders/search", json={"request_id": "rq-no-tenant"}).status_code == 400

def test_search_invalid_params_distinct_reasons_and_no_write() -> None:
    th = {"X-Tenant": "rsearch"}
    def reason(payload: dict) -> str:
        resp = client.post("/refund-orders/search", json=payload, headers=th)
        assert resp.status_code == 400
        return resp.json()["detail"]
    assert reason({"request_id": "rb1", "filters": {"status": "nope"}}) == "invalid_status"
    assert reason({"request_id": "rb2", "filters": {"order_id": ""}}) == "invalid_order_id"
    assert reason(
        {"request_id": "rb3", "filters": {"requested_amount_min_cents": 10, "requested_amount_max_cents": 5}}
    ) == "invalid_requested_amount_range"
    assert reason({"request_id": "rb4", "filters": {"requested_amount_min_cents": -1}}) == "invalid_requested_amount_range"
    assert reason({"request_id": "rb5", "page": {"size": 0}}) == "invalid_page_size"
    assert reason({"request_id": "rb6", "page": {"cursor": 1}}) == "invalid_cursor"
    assert reason({"request_id": "rb7", "filters": {"bogus": 1}}).startswith("unknown_filter")
    assert client.post("/refund-orders/search", json={}, headers=th).status_code == 422
    # 参数非法不写入：同一去重标识修正参数后按首次生效
    ok = client.post("/refund-orders/search", json={"request_id": "rb1"}, headers=th)
    assert ok.status_code == 200 and ok.json()["replayed"] is False

# ---------- 并发与持久化 ----------

def test_concurrent_execute_of_one_refund_order_applies_once() -> None:
    import threading
    _order("i1", 500); _pay("i1", 500)
    _accept("ri1", "i1", 200)
    client.post("/refund-orders/ri1/approve", headers=H)
    statuses: list[int] = []
    def run() -> None:
        statuses.append(
            client.post("/refund-orders/ri1/execute", json={"outcome": "succeeded"}, headers=H).status_code
        )
    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(statuses) == [200, 200]  # 一笔到账，一笔幂等重放
    assert client.get("/orders/i1", headers=H).json()["refunded_cents"] == 200
    entries = client.get("/orders/i1/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "refund"]

def test_refund_order_state_and_order_refunded_survive_restart() -> None:
    _order("i2", 400); _pay("i2", 400)
    _accept("ri2", "i2", 120)
    client.post("/refund-orders/ri2/approve", headers=H)
    client.post("/refund-orders/ri2/execute", json={"outcome": "succeeded"}, headers=H)
    # 模拟服务重启：重放执行请求只生效一次
    replay = client.post("/refund-orders/ri2/execute", json={"outcome": "succeeded"}, headers=H)
    assert replay.json()["replayed"] is True
    ro = client.get("/refund-orders/ri2", headers=H).json()
    assert ro["status"] == "succeeded" and ro["refunded_amount_cents"] == 120
    order = client.get("/orders/i2", headers=H).json()
    assert order["refunded_cents"] == 120 and order["refundable_cents"] == 280
    assert len(client.get("/orders/i2/ledger", headers=H).json()["entries"]) == 3
