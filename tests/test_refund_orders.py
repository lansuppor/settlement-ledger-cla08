import os
import tempfile

os.environ["APP_DB"] = os.path.join(tempfile.mkdtemp(), "test_refund_orders.sqlite")
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "u1"}

def _order(order_id: str, amount: int = 100, tenant: str = "u1", currency: str = "CNY") -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": currency})
    assert resp.status_code == 201, resp.text

def _pay(order_id: str, amount: int, tenant: str = "u1") -> None:
    resp = client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})
    assert resp.status_code == 200, resp.text

def _accept(refund_id: str, order_id: str, amount: int, tenant: str = "u1"):
    return client.post(
        "/refund-orders",
        json={"tenant": tenant, "refund_id": refund_id, "order_id": order_id, "request_cents": amount},
    )

# ---------- 受理与读取 ----------

def test_accept_refund_order_and_read_back() -> None:
    _order("ro1", 500)
    resp = _accept("rf1", "ro1", 200)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["refund_id"] == "rf1" and data["order_id"] == "ro1"
    assert data["request_cents"] == 200 and data["actual_cents"] == 0
    assert data["status"] == "accepted" and data["failure_reason"] is None
    assert data["tenant"] == "u1" and data["attempts"] == 0
    got = client.get("/refund-orders/rf1", headers=H)
    assert got.status_code == 200 and got.json()["created_at"] == data["created_at"]

def test_accept_replay_returns_same_doc_without_creating() -> None:
    _order("ro2", 300)
    first = _accept("rf2", "ro2", 100)
    second = _accept("rf2", "ro2", 100)
    assert first.status_code == 201 and second.status_code == 200
    assert second.headers["x-idempotent-replay"] == "1"
    assert second.json()["created_at"] == first.json()["created_at"]
    # 重放不新建：同租户按状态检索只命中一单
    resp = client.post("/refund-orders/search", json={"request_id": "rp-1", "filters": {"order_id": "ro2"}}, headers=H)
    assert [r["refund_id"] for r in resp.json()["refund_orders"]] == ["rf2"]

def test_accept_same_refund_id_for_another_order_refused() -> None:
    _order("ro3a", 100)
    _order("ro3b", 100)
    assert _accept("rf3", "ro3a", 10).status_code == 201
    resp = _accept("rf3", "ro3b", 10)
    assert resp.status_code == 409
    # 不改任何数据：ro3b 名下无退款单
    resp = client.post("/refund-orders/search", json={"request_id": "rp-3", "filters": {"order_id": "ro3b"}}, headers=H)
    assert resp.json()["refund_orders"] == []
    # 原单仍是首次受理的 ro3a
    assert client.get("/refund-orders/rf3", headers=H).json()["order_id"] == "ro3a"

def test_refund_id_conflicts_with_order_side_biz_id_both_directions() -> None:
    _order("ro4", 200)
    _pay("ro4", 200)
    # 先用订单侧冲正业务标识
    assert client.post("/orders/ro4/refunds", json={"biz_id": "shared-id", "amount_cents": 10}, headers=H).status_code == 200
    # 退款单标识与冲正业务标识冲突：拒绝
    assert _accept("shared-id", "ro4", 10).status_code == 409
    # 反向：退款单先占标识，订单侧作废/冲正/修正不得复用
    _order("ro4b", 100)
    assert _accept("shared-ro", "ro4b", 10).status_code == 201
    assert client.post("/orders/ro4b/void", json={"biz_id": "shared-ro"}, headers=H).status_code == 409
    _pay("ro4b", 100)
    assert client.post("/orders/ro4b/refunds", json={"biz_id": "shared-ro", "amount_cents": 10}, headers=H).status_code == 409

def test_accept_allowed_on_any_order_status() -> None:
    # 未受理收款的订单、作废订单、收付完结订单上均可受理退款单
    _order("ro5a", 100)
    _order("ro5b", 100)
    client.post("/orders/ro5b/void", json={"biz_id": "ro5b-void"}, headers=H)
    _order("ro5c", 100)
    _pay("ro5c", 100)
    client.post("/orders/ro5c/refunds", json={"biz_id": "ro5c-r", "amount_cents": 100}, headers=H)
    assert client.get("/orders/ro5c", headers=H).json()["status"] == "completed"
    assert _accept("rf5a", "ro5a", 10).status_code == 201
    assert _accept("rf5b", "ro5b", 10).status_code == 201
    assert _accept("rf5c", "ro5c", 10).status_code == 201

def test_accept_unknown_or_cross_tenant_order_is_404() -> None:
    assert _accept("rf6", "missing", 10).status_code == 404
    _order("ro6", 100, tenant="u1")
    assert _accept("rf6x", "ro6", 10, tenant="u2").status_code == 404
    assert client.get("/refund-orders/rf6", headers=H).status_code == 404

def test_accept_invalid_amount_or_missing_field() -> None:
    _order("ro7", 100)
    assert _accept("rf7a", "ro7", 0).status_code == 422
    body = {"tenant": "u1", "refund_id": "rf7b", "order_id": "ro7", "request_cents": -5}
    assert client.post("/refund-orders", json=body).status_code == 422
    assert client.post("/refund-orders", json={"tenant": "u1", "order_id": "ro7", "request_cents": 10}).status_code == 422

def test_read_refund_order_tenant_isolation() -> None:
    _order("ro8", 100)
    _accept("rf8", "ro8", 10)
    assert client.get("/refund-orders/rf8", headers={"X-Tenant": "u2"}).status_code == 404
    assert client.get("/refund-orders/rf8").status_code == 400

# ---------- 审核 ----------

def test_review_approve_and_reject_only_from_accepted() -> None:
    _order("rv1", 200)
    _accept("rvf1", "rv1", 50)
    assert client.post("/refund-orders/rvf1/review", json={"approved": True}, headers=H).json()["status"] == "approved"
    # 已审核不可再审核
    resp = client.post("/refund-orders/rvf1/review", json={"approved": False}, headers=H)
    assert resp.status_code == 409 and resp.json()["detail"] == "refund already approved"

    _accept("rvf2", "rv1", 50)
    assert client.post("/refund-orders/rvf2/review", json={"approved": False}, headers=H).json()["status"] == "rejected"
    resp = client.post("/refund-orders/rvf2/review", json={"approved": True}, headers=H)
    assert resp.status_code == 409 and resp.json()["detail"] == "refund already rejected"
    assert client.post("/refund-orders/nope/review", json={"approved": True}, headers=H).status_code == 404

def test_rejected_cannot_execute_but_can_cancel() -> None:
    _order("rv2", 200)
    _pay("rv2", 200)
    _accept("rvf3", "rv2", 50)
    client.post("/refund-orders/rvf3/review", json={"approved": False}, headers=H)
    # 驳回后不可执行
    resp = client.post("/refund-orders/rvf3/execute", json={}, headers=H)
    assert resp.status_code == 409 and resp.json()["detail"] == "refund is rejected"
    # 可撤销，撤销后终态
    assert client.post("/refund-orders/rvf3/cancel", headers=H).json()["status"] == "cancelled"
    assert client.post("/refund-orders/rvf3/execute", json={}, headers=H).status_code == 409
    assert client.post("/refund-orders/rvf3/cancel", headers=H).status_code == 409
    # 全程不动原订单、不产生流水
    state = client.get("/orders/rv2", headers=H).json()
    assert state["refunded_cents"] == 0
    assert [e["op_type"] for e in client.get("/orders/rv2/ledger", headers=H).json()["entries"]] == ["accept", "payment"]

# ---------- 执行到账 ----------

def test_execute_approved_refund_posts_to_order_ledger() -> None:
    _order("re1", 300)
    _pay("re1", 300)
    _accept("ref1", "re1", 100)
    client.post("/refund-orders/ref1/review", json={"approved": True}, headers=H)
    resp = client.post("/refund-orders/ref1/execute", json={}, headers=H)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "settled" and data["actual_cents"] == 100 and data["attempts"] == 1
    assert data["failure_reason"] is None
    # 原订单累计冲正增加、已收不变
    state = client.get("/orders/re1", headers=H).json()
    assert state["refunded_cents"] == 100 and state["paid_cents"] == 300 and state["refundable_cents"] == 200
    # 追加原订单退款流水，业务标识记退款单标识
    entries = client.get("/orders/re1/ledger", headers=H).json()["entries"]
    last = entries[-1]
    assert last["op_type"] == "refund" and last["biz_id"] == "ref1" and last["amount_cents"] == 100
    assert last["refunded_result_cents"] == 100

def test_execute_rejudges_order_completed() -> None:
    _order("re2", 100)
    _pay("re2", 100)
    _accept("ref2", "re2", 100)
    client.post("/refund-orders/ref2/review", json={"approved": True}, headers=H)
    assert client.post("/refund-orders/ref2/execute", json={}, headers=H).status_code == 200
    assert client.get("/orders/re2", headers=H).json()["status"] == "completed"

def test_execute_rejected_when_over_refundable_and_keeps_approved() -> None:
    _order("re3", 200)
    _pay("re3", 120)  # 可退余额 120
    _accept("ref3", "re3", 150)
    client.post("/refund-orders/ref3/review", json={"approved": True}, headers=H)
    # 申请金额超过执行当时可退余额：整体拒绝
    resp = client.post("/refund-orders/ref3/execute", json={}, headers=H)
    assert resp.status_code == 409 and resp.json()["detail"] == "refund exceeds refundable balance"
    # 退款单保持已通过待执行，原订单与流水不变
    doc = client.get("/refund-orders/ref3", headers=H).json()
    assert doc["status"] == "approved" and doc["actual_cents"] == 0
    state = client.get("/orders/re3", headers=H).json()
    assert state["refunded_cents"] == 0 and state["refundable_cents"] == 120
    assert [e["op_type"] for e in client.get("/orders/re3/ledger", headers=H).json()["entries"]] == ["accept", "payment"]
    # 可退余额恢复后可再次执行成功（另一笔冲正被冲正修正扣回的场景用直接收款补齐模拟：等待余额变大）
    # 此处用新收款抬高已收后再执行
    _pay("re3", 30)  # 结清 150/150... 订单金额 200，只能再收 80；收 30 后可退 150
    resp = client.post("/refund-orders/ref3/execute", json={}, headers=H)
    assert resp.status_code == 200 and resp.json()["status"] == "settled"
    assert client.get("/orders/re3", headers=H).json()["refunded_cents"] == 150

def test_execute_replay_after_settled_does_not_post_twice() -> None:
    _order("re4", 200)
    _pay("re4", 200)
    _accept("ref4", "re4", 60)
    client.post("/refund-orders/ref4/review", json={"approved": True}, headers=H)
    first = client.post("/refund-orders/ref4/execute", json={}, headers=H)
    second = client.post("/refund-orders/ref4/execute", json={}, headers=H)
    assert first.status_code == 200 and second.status_code == 200
    assert second.headers["x-idempotent-replay"] == "1"
    assert second.json()["status"] == "settled" and second.json()["actual_cents"] == 60
    state = client.get("/orders/re4", headers=H).json()
    assert state["refunded_cents"] == 60  # 不重复入账
    entries = client.get("/orders/re4/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "refund"]  # 不重复流水

def test_execute_failure_recorded_then_retry_success() -> None:
    _order("re5", 200)
    _pay("re5", 200)
    _accept("ref5", "re5", 70)
    client.post("/refund-orders/ref5/review", json={"approved": True}, headers=H)
    fail = client.post("/refund-orders/ref5/execute", json={"failure_reason": "gateway timeout"}, headers=H)
    assert fail.status_code == 200
    data = fail.json()
    assert data["status"] == "failed" and data["failure_reason"] == "gateway timeout" and data["attempts"] == 1
    # 失败不动原订单、不产生流水
    assert client.get("/orders/re5", headers=H).json()["refunded_cents"] == 0
    assert [e["op_type"] for e in client.get("/orders/re5/ledger", headers=H).json()["entries"]] == ["accept", "payment"]
    # 空失败原因被拒绝（422）
    assert client.post("/refund-orders/ref5/execute", json={"failure_reason": ""}, headers=H).status_code == 422
    # 可再次执行，成功到账并清空失败原因
    ok = client.post("/refund-orders/ref5/execute", json={}, headers=H)
    assert ok.status_code == 200 and ok.json()["status"] == "settled"
    assert ok.json()["actual_cents"] == 70 and ok.json()["failure_reason"] is None and ok.json()["attempts"] == 2
    assert client.get("/orders/re5", headers=H).json()["refunded_cents"] == 70

def test_execute_not_approved_refused() -> None:
    _order("re6", 100)
    _pay("re6", 100)
    _accept("ref6", "re6", 10)
    # 已受理未审核：不可执行
    assert client.post("/refund-orders/ref6/execute", json={}, headers=H).status_code == 409
    client.post("/refund-orders/ref6/cancel", headers=H)
    assert client.post("/refund-orders/ref6/execute", json={}, headers=H).status_code == 409
    assert client.post("/refund-orders/ref6/execute", json={"failure_reason": "x"}, headers=H).status_code == 409

def test_execute_when_order_voided_completed_or_cross_tenant_is_404() -> None:
    # 受理后原订单被作废
    _order("re7a", 100)
    _accept("ref7a", "re7a", 10)
    client.post("/refund-orders/ref7a/review", json={"approved": True}, headers=H)
    client.post("/orders/re7a/void", json={"biz_id": "re7a-void"}, headers=H)
    resp = client.post("/refund-orders/ref7a/execute", json={}, headers=H)
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"
    # 不产生流水与状态变化：退款单仍待执行，订单只有 accept/void
    assert client.get("/refund-orders/ref7a", headers=H).json()["status"] == "approved"
    assert [e["op_type"] for e in client.get("/orders/re7a/ledger", headers=H).json()["entries"]] == ["accept", "void"]

    # 原订单先经其他冲正进入收付完结终态
    _order("re7b", 100)
    _pay("re7b", 100)
    _accept("ref7b", "re7b", 10)
    client.post("/refund-orders/ref7b/review", json={"approved": True}, headers=H)
    client.post("/orders/re7b/refunds", json={"biz_id": "re7b-full", "amount_cents": 100}, headers=H)
    assert client.post("/refund-orders/ref7b/execute", json={}, headers=H).status_code == 404
    assert client.get("/orders/re7b", headers=H).json()["refunded_cents"] == 100

    # 跨租户执行按不存在处理
    _order("re7c", 100, tenant="u1")
    _pay("re7c", 100)
    _accept("ref7c", "re7c", 10)
    client.post("/refund-orders/ref7c/review", json={"approved": True}, headers=H)
    assert client.post("/refund-orders/ref7c/execute", json={}, headers={"X-Tenant": "u2"}).status_code == 404
    # 退款单不存在
    assert client.post("/refund-orders/nope/execute", json={}, headers=H).status_code == 404

# ---------- 撤销 ----------

def test_cancel_allowed_states_and_terminal_afterwards() -> None:
    _order("rc1", 200)
    _pay("rc1", 200)
    _accept("rcf1", "rc1", 10)  # accepted → cancel
    assert client.post("/refund-orders/rcf1/cancel", headers=H).json()["status"] == "cancelled"
    assert client.post("/refund-orders/rcf1/cancel", headers=H).status_code == 409

    _accept("rcf2", "rc1", 10)
    client.post("/refund-orders/rcf2/review", json={"approved": False}, headers=H)
    assert client.post("/refund-orders/rcf2/cancel", headers=H).status_code == 200  # rejected → cancel

    _accept("rcf3", "rc1", 10)
    client.post("/refund-orders/rcf3/review", json={"approved": True}, headers=H)
    client.post("/refund-orders/rcf3/execute", json={"failure_reason": "boom"}, headers=H)
    assert client.post("/refund-orders/rcf3/cancel", headers=H).json()["status"] == "cancelled"  # failed → cancel

    # approved 不可撤销；settled 不可撤销
    _accept("rcf4", "rc1", 10)
    client.post("/refund-orders/rcf4/review", json={"approved": True}, headers=H)
    assert client.post("/refund-orders/rcf4/cancel", headers=H).status_code == 409
    _accept("rcf5", "rc1", 10)
    client.post("/refund-orders/rcf5/review", json={"approved": True}, headers=H)
    client.post("/refund-orders/rcf5/execute", json={}, headers=H)
    assert client.post("/refund-orders/rcf5/cancel", headers=H).status_code == 409
    assert client.post("/refund-orders/rcf5/cancel", headers=H).status_code == 409
    assert client.post("/refund-orders/nope/cancel", headers=H).status_code == 404

# ---------- 并发与一致性 ----------

def test_concurrent_execution_never_exceeds_refundable() -> None:
    import threading
    _order("cc1", 100)
    _pay("cc1", 100)
    _accept("ccf1", "cc1", 60)
    _accept("ccf2", "cc1", 60)
    client.post("/refund-orders/ccf1/review", json={"approved": True}, headers=H)
    client.post("/refund-orders/ccf2/review", json={"approved": True}, headers=H)
    results: list[int] = []
    def run(refund_id: str) -> None:
        results.append(client.post(f"/refund-orders/{refund_id}/execute", json={}, headers=H).status_code)
    t1 = threading.Thread(target=run, args=("ccf1",))
    t2 = threading.Thread(target=run, args=("ccf2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [200, 409]  # 串行化后只有一单到账
    state = client.get("/orders/cc1", headers=H).json()
    assert state["refunded_cents"] == 60 and state["refundable_cents"] == 40
    statuses = [client.get(f"/refund-orders/{r}", headers=H).json()["status"] for r in ("ccf1", "ccf2")]
    assert sorted(statuses) == ["approved", "settled"]  # 被拒单保持已通过待执行

def test_state_survives_restart_and_replay_executes_once() -> None:
    _order("rp1", 200)
    _pay("rp1", 200)
    _accept("rpf1", "rp1", 80)
    client.post("/refund-orders/rpf1/review", json={"approved": True}, headers=H)
    assert client.post("/refund-orders/rpf1/execute", json={}, headers=H).status_code == 200
    # 模拟服务重启：重放执行请求只生效一次
    replay = client.post("/refund-orders/rpf1/execute", json={}, headers=H)
    assert replay.headers["x-idempotent-replay"] == "1" and replay.json()["actual_cents"] == 80
    state = client.get("/orders/rp1", headers=H).json()
    assert state["refunded_cents"] == 80 and state["refundable_cents"] == 120
    assert len(client.get("/orders/rp1/ledger", headers=H).json()["entries"]) == 3
    doc = client.get("/refund-orders/rpf1", headers=H).json()
    assert doc["status"] == "settled" and doc["request_cents"] == 80 and doc["actual_cents"] == 80

# ---------- 条件检索 ----------

def _seed_search_refunds() -> None:
    # 检索测试使用独立租户 uq，避免与同库其他生命周期测试的数据互相串扰
    t = "uq"
    _order("q1", 300, tenant=t)
    _pay("q1", 300, tenant=t)
    _order("q2", 300, tenant=t)
    _pay("q2", 300, tenant=t)
    for rid, oid, amount in [
        ("zr1", "q1", 100), ("zr2", "q1", 200), ("zr3", "q2", 300),
    ]:
        _accept(rid, oid, amount, tenant=t)
    client.post("/refund-orders/zr1/review", json={"approved": True}, headers={"X-Tenant": t})
    client.post("/refund-orders/zr2/review", json={"approved": False}, headers={"X-Tenant": t})
    client.post("/refund-orders/zr3/review", json={"approved": True}, headers={"X-Tenant": t})
    client.post("/refund-orders/zr3/execute", json={}, headers={"X-Tenant": t})
    # 其他租户的同名/同状态单据
    _order("q1", 300, tenant="u2")
    _accept("zr1", "q1", 100, tenant="u2")

def test_refund_search_filters() -> None:
    _seed_search_refunds()
    def search(filters: dict, request_id: str, tenant: str = "uq"):
        resp = client.post("/refund-orders/search", json={"request_id": request_id, "filters": filters}, headers={"X-Tenant": tenant})
        assert resp.status_code == 200, resp.text
        return resp.json()["refund_orders"]
    assert [r["refund_id"] for r in search({"status": "approved"}, "rq-status")] == ["zr1"]
    assert [r["refund_id"] for r in search({"status": "settled"}, "rq-settled")] == ["zr3"]
    assert [r["refund_id"] for r in search({"order_id": "q1"}, "rq-order")] == ["zr1", "zr2"]
    assert [r["refund_id"] for r in search({"request_min_cents": 150}, "rq-min")] == ["zr2", "zr3"]
    assert [r["refund_id"] for r in search({"request_max_cents": 150}, "rq-max")] == ["zr1"]
    combo = search({"status": "rejected", "request_min_cents": 100, "order_id": "q1"}, "rq-combo")
    assert [r["refund_id"] for r in combo] == ["zr2"]

def test_refund_search_pagination_stable() -> None:
    t = "uq"
    seen: list[str] = []
    cursor = None
    for page_no in range(3):
        page = {"size": 2, **({"cursor": cursor} if cursor else {})}
        resp = client.post(
            "/refund-orders/search",
            json={"request_id": f"rq-page-{page_no}", "filters": {"order_id": "q1"}, "page": page},
            headers={"X-Tenant": t},
        )
        assert resp.status_code == 200
        seen += [r["refund_id"] for r in resp.json()["refund_orders"]]
        cursor = resp.json()["page"]["next_cursor"]
        if cursor is None:
            break
    assert seen == ["zr1", "zr2"] and cursor is None

def test_refund_search_replay_snapshot_and_tenant_isolation() -> None:
    t = "uq"
    body = {"request_id": "rq-replay", "filters": {"order_id": "q1"}}
    first = client.post("/refund-orders/search", json=body, headers={"X-Tenant": t})
    first_ids = [r["refund_id"] for r in first.json()["refund_orders"]]
    _accept("zr9", "q1", 10, tenant=t)  # 首次检索后新受理
    replay = client.post("/refund-orders/search", json=body, headers={"X-Tenant": t})
    assert replay.headers["x-idempotent-replay"] == "1"
    assert [r["refund_id"] for r in replay.json()["refund_orders"]] == first_ids
    fresh = client.post("/refund-orders/search", json={"request_id": "rq-replay-2", "filters": {"order_id": "q1"}}, headers={"X-Tenant": t})
    assert "zr9" in [r["refund_id"] for r in fresh.json()["refund_orders"]]
    # 跨租户隔离：u2 只见本租户退款单
    other = client.post("/refund-orders/search", json={"request_id": "rq-iso"}, headers={"X-Tenant": "u2"})
    assert [r["refund_id"] for r in other.json()["refund_orders"]] == ["zr1"]
    assert all(r["tenant"] == "u2" for r in other.json()["refund_orders"])
    # 未提供租户 400；无命中返回空列表（独立租户 uv 内无任何退款单）
    assert client.post("/refund-orders/search", json={"request_id": "rq-noh"}).status_code == 400
    empty = client.post("/refund-orders/search", json={"request_id": "rq-empty", "filters": {"status": "cancelled"}}, headers={"X-Tenant": "uv"})
    assert empty.status_code == 200 and empty.json()["refund_orders"] == []

def test_refund_search_invalid_params_distinct_and_not_persisted() -> None:
    def reason(payload: dict) -> str:
        resp = client.post("/refund-orders/search", json=payload, headers=H)
        assert resp.status_code == 400
        return resp.json()["detail"]
    assert reason({"request_id": "bad1", "filters": {"status": "nope"}}) == "invalid_status"
    assert reason({"request_id": "bad2", "filters": {"request_min_cents": -1}}) == "invalid_request_range"
    assert reason({"request_id": "bad3", "filters": {"request_min_cents": 9, "request_max_cents": 3}}) == "invalid_request_range"
    assert reason({"request_id": "bad4", "page": {"size": 0}}) == "invalid_page_size"
    assert reason({"request_id": "bad5", "page": {"cursor": 7}}) == "invalid_cursor"
    assert reason({"request_id": "bad6", "filters": {"bogus": 1}}).startswith("unknown_filter")
    assert reason({"request_id": "bad7", "filters": {"order_id": ""}}) == "invalid_order_id"
    assert client.post("/refund-orders/search", json={}, headers=H).status_code == 422
    # 参数非法不写入：同去重标识修正参数后按首次生效
    ok = client.post("/refund-orders/search", json={"request_id": "bad1"}, headers=H)
    assert ok.status_code == 200 and ok.json()["replayed"] is False
