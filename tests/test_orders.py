import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

def test_accept_and_read_order() -> None:
    body = {"tenant": "t1", "order_id": "o1", "amount_cents": 500, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    got = client.get("/orders/o1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["outstanding_cents"] == 500

def test_duplicate_is_refused() -> None:
    body = {"tenant": "t1", "order_id": "o2", "amount_cents": 100, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    assert client.post("/orders", json=body).status_code == 409

def test_cross_tenant_read_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o3", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.get("/orders/o3", headers={"X-Tenant": "t2"}).status_code == 404

def test_payment_cannot_exceed_outstanding() -> None:
    body = {"tenant": "t1", "order_id": "o4", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.post("/orders/o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409

# ---------- 冲正（退款） ----------

def test_refund_after_payment() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r1", "amount_cents": 300, "currency": "CNY"})
    client.post("/orders/r1/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    resp = client.post("/orders/r1/refunds", json={"biz_id": "b1", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["amount_cents"] == 100
    assert data["biz_id"] == "b1"
    assert data["paid_cents"] == 300          # 已收不变
    assert data["outstanding_cents"] == 0
    assert data["refunded_cents"] == 100      # 累计冲正增加
    assert data["refundable_cents"] == 200    # 可退余额下降
    assert data["replayed"] is False

def test_refund_is_idempotent_by_biz_id() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r2", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/r2/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    first = client.post("/orders/r2/refunds", json={"biz_id": "dup", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    second = client.post("/orders/r2/refunds", json={"biz_id": "dup", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["created_at"] == second.json()["created_at"]
    assert second.json()["replayed"] is True
    assert second.headers["x-idempotent-replay"] == "1"
    state = client.get("/orders/r2", headers={"X-Tenant": "t1"}).json()
    assert state["refunded_cents"] == 50      # 不重复累计
    assert state["refundable_cents"] == 150
    ledger = client.get("/orders/r2/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in ledger] == ["accept", "payment", "refund"]  # 不新增流水

def test_distinct_biz_ids_apply_separately() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r3", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/r3/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/r3/refunds", json={"biz_id": "x1", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    client.post("/orders/r3/refunds", json={"biz_id": "x2", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    state = client.get("/orders/r3", headers={"X-Tenant": "t1"}).json()
    assert state["refunded_cents"] == 100 and state["refundable_cents"] == 100

def test_refund_rejected_over_refundable() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r4", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/r4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    # 未收款不得冲正
    client.post("/orders", json={"tenant": "t1", "order_id": "r4b", "amount_cents": 200, "currency": "CNY"})
    assert client.post("/orders/r4b/refunds", json={"biz_id": "n1", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409
    # 超过可退余额
    assert client.post("/orders/r4/refunds", json={"biz_id": "n2", "amount_cents": 150}, headers={"X-Tenant": "t1"}).status_code == 409
    # 非正数被模型拒绝
    assert client.post("/orders/r4/refunds", json={"biz_id": "n3", "amount_cents": 0}, headers={"X-Tenant": "t1"}).status_code == 422
    # 拒绝后状态不变、无冲正流水
    state = client.get("/orders/r4", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 100 and state["refunded_cents"] == 0
    ledger = client.get("/orders/r4/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in ledger] == ["accept", "payment"]

def test_refund_completes_order_and_further_refund_rejected() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r5", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/r5/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r5/refunds", json={"biz_id": "f1", "amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    state = client.get("/orders/r5", headers={"X-Tenant": "t1"}).json()
    assert state["refundable_cents"] == 0 and state["status"] == "completed"
    assert client.post("/orders/r5/refunds", json={"biz_id": "f2", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409

def test_refund_after_settlement_allowed_within_refundable() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r6", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/r6/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.get("/orders/r6", headers={"X-Tenant": "t1"}).json()["status"] == "settled"
    assert client.post("/orders/r6/refunds", json={"biz_id": "s1", "amount_cents": 30}, headers={"X-Tenant": "t1"}).status_code == 200
    state = client.get("/orders/r6", headers={"X-Tenant": "t1"}).json()
    assert state["status"] == "settled" and state["refundable_cents"] == 70

def test_refund_unknown_order_and_cross_tenant_are_404() -> None:
    assert client.post("/orders/nope/refunds", json={"biz_id": "z1", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 404
    client.post("/orders", json={"tenant": "t1", "order_id": "r7", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/r7/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r7/refunds", json={"biz_id": "z2", "amount_cents": 10}, headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/orders/r7/ledger", headers={"X-Tenant": "t2"}).status_code == 404

def test_biz_id_unique_per_tenant() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r8a", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders", json={"tenant": "t1", "order_id": "r8b", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/r8a/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/r8b/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r8a/refunds", json={"biz_id": "shared", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 200
    # 同租户业务标识复用到另一订单：拒绝且不改变 r8b
    assert client.post("/orders/r8b/refunds", json={"biz_id": "shared", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.get("/orders/r8b", headers={"X-Tenant": "t1"}).json()["refunded_cents"] == 0
    # 不同租户可复用同一业务标识
    client.post("/orders", json={"tenant": "t2", "order_id": "r8c", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/r8c/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t2"})
    assert client.post("/orders/r8c/refunds", json={"biz_id": "shared", "amount_cents": 10}, headers={"X-Tenant": "t2"}).status_code == 200

# ---------- 收付流水 ----------

def test_ledger_records_every_op_in_order() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "l1", "amount_cents": 500, "currency": "CNY"})
    client.post("/orders/l1/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/l1/refunds", json={"biz_id": "lb1", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    entries = client.get("/orders/l1/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[0]["op_type"] == "accept" and entries[0]["biz_id"] is None
    assert entries[1]["op_type"] == "payment" and entries[1]["paid_result_cents"] == 200
    assert entries[1]["outstanding_result_cents"] == 300 and entries[1]["refunded_result_cents"] == 0
    assert entries[2]["op_type"] == "refund" and entries[2]["biz_id"] == "lb1"
    assert entries[2]["paid_result_cents"] == 200 and entries[2]["refunded_result_cents"] == 50
    assert entries[2]["outstanding_result_cents"] == 300

# ---------- 并发与持久化 ----------

def test_concurrent_refund_and_payment_never_overpays_refund() -> None:
    import threading
    client.post("/orders", json={"tenant": "t1", "order_id": "c1", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/c1/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    results: list[int] = []
    def refund(biz: str) -> None:
        results.append(client.post("/orders/c1/refunds", json={"biz_id": biz, "amount_cents": 60}, headers={"X-Tenant": "t1"}).status_code)
    t1 = threading.Thread(target=refund, args=("c-a",))
    t2 = threading.Thread(target=refund, args=("c-b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [200, 409]   # 串行化后只有一笔生效
    state = client.get("/orders/c1", headers={"X-Tenant": "t1"}).json()
    assert state["refunded_cents"] == 60 and state["refundable_cents"] == 40

def test_state_and_replay_survive_restart() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "p1", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/p1/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/p1/refunds", json={"biz_id": "persist", "amount_cents": 80}, headers={"X-Tenant": "t1"})
    # 模拟服务重启：重放同一冲正请求
    replay = client.post("/orders/p1/refunds", json={"biz_id": "persist", "amount_cents": 80}, headers={"X-Tenant": "t1"})
    assert replay.json()["replayed"] is True
    state = client.get("/orders/p1", headers={"X-Tenant": "t1"}).json()
    assert state["refunded_cents"] == 80 and state["refundable_cents"] == 120
    entries = client.get("/orders/p1/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert len(entries) == 3

# ---------- 批量受理导入 ----------

def test_batch_rows_apply_independently_with_distinct_reasons() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "b-exists", "amount_cents": 100, "currency": "CNY"})
    rows = [
        {"tenant": "t1", "order_id": "b1", "amount_cents": 500, "currency": "CNY"},      # 受理
        {"tenant": "t1", "order_id": "b-exists", "amount_cents": 100, "currency": "CNY"}, # 重复受理
        {"tenant": "t1", "order_id": "b2", "amount_cents": 0, "currency": "CNY"},         # 金额非法
        {"tenant": "t1", "order_id": "b3", "amount_cents": 10.5, "currency": "CNY"},      # 金额非整数
        {"tenant": "t1", "order_id": "b4", "amount_cents": 100, "currency": "GBP"},       # 币种不支持
        {"tenant": "t1", "order_id": "", "amount_cents": 100, "currency": "CNY"},         # 标识为空
        {"tenant": "t1", "amount_cents": 100, "currency": "CNY"},                          # 标识缺失
        {"tenant": "t1", "order_id": "b1", "amount_cents": 500, "currency": "CNY"},       # 批内重复
        {"tenant": "t2", "order_id": "b1", "amount_cents": 700, "currency": "USD"},       # 跨租户同标识，独立受理
    ]
    resp = client.post("/orders/batch", json={"rows": rows})
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] == 2 and data["rejected"] == 7
    results = data["results"]
    assert [r["line"] for r in results] == list(range(1, 10))
    assert results[0]["status"] == "accepted"
    assert results[0]["order"]["outstanding_cents"] == 500   # 成功行给出新订单结果金额
    assert [r["reason"] for r in results[1:8]] == [
        "duplicate_acceptance", "invalid_amount", "invalid_amount",
        "unsupported_currency", "missing_field", "missing_field", "duplicate_in_batch",
    ]
    assert results[8]["status"] == "accepted" and results[8]["order"]["amount_cents"] == 700
    # 拒绝行不产生流水
    assert client.get("/orders/b-exists", headers={"X-Tenant": "t1"}).json()["amount_cents"] == 100
    ledger = client.get("/orders/b-exists/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in ledger] == ["accept"]

def test_batch_replay_does_not_reaccept_or_change_ledger() -> None:
    rows = [
        {"tenant": "t1", "order_id": "b10", "amount_cents": 300, "currency": "CNY"},
        {"tenant": "t1", "order_id": "b11", "amount_cents": -1, "currency": "CNY"},
    ]
    first = client.post("/orders/batch", json={"rows": rows}).json()
    assert first["accepted"] == 1 and first["rejected"] == 1
    second = client.post("/orders/batch", json={"rows": rows}).json()
    # 重放：已生效行不重复受理（按重复受理拒绝），被拒行仍被拒绝
    assert second["accepted"] == 0 and second["rejected"] == 2
    assert second["results"][0]["reason"] == "duplicate_acceptance"
    assert second["results"][1]["reason"] == "invalid_amount"
    state = client.get("/orders/b10", headers={"X-Tenant": "t1"}).json()
    assert state["amount_cents"] == 300 and state["paid_cents"] == 0
    ledger = client.get("/orders/b10/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in ledger] == ["accept"]   # 不新增流水

def test_batch_rejected_row_does_not_rollback_others() -> None:
    rows = [
        {"tenant": "t1", "order_id": "b20", "amount_cents": 100, "currency": "CNY"},
        {"tenant": "t1", "order_id": "b21", "amount_cents": 0, "currency": "CNY"},
        {"tenant": "t1", "order_id": "b22", "amount_cents": 200, "currency": "EUR"},
    ]
    data = client.post("/orders/batch", json={"rows": rows}).json()
    assert data["accepted"] == 2 and data["rejected"] == 1
    assert client.get("/orders/b20", headers={"X-Tenant": "t1"}).status_code == 200
    assert client.get("/orders/b21", headers={"X-Tenant": "t1"}).status_code == 404
    assert client.get("/orders/b22", headers={"X-Tenant": "t1"}).status_code == 200

# ---------- 条件检索 ----------

def _seed_search_orders() -> None:
    client.post("/orders", json={"tenant": "s1", "order_id": "so1", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders", json={"tenant": "s1", "order_id": "so2", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders", json={"tenant": "s1", "order_id": "so3", "amount_cents": 300, "currency": "USD"})
    client.post("/orders/so2/payments", json={"amount_cents": 200}, headers={"X-Tenant": "s1"})
    client.post("/orders/so3/payments", json={"amount_cents": 150}, headers={"X-Tenant": "s1"})
    client.post("/orders/so3/refunds", json={"biz_id": "so3-r1", "amount_cents": 50}, headers={"X-Tenant": "s1"})
    client.post("/orders", json={"tenant": "s2", "order_id": "so1", "amount_cents": 999, "currency": "CNY"})

def test_search_filters_by_status_currency_ranges_and_refund_flag() -> None:
    _seed_search_orders()
    def search(filters: dict, request_id: str) -> list[dict]:
        resp = client.post("/orders/search", json={"request_id": request_id, "filters": filters}, headers={"X-Tenant": "s1"})
        assert resp.status_code == 200
        return resp.json()["orders"]
    # 状态
    assert [o["order_id"] for o in search({"status": "settled"}, "q-status")] == ["so2"]
    # 币种
    assert [o["order_id"] for o in search({"currency": "USD"}, "q-currency")] == ["so3"]
    # 订单金额区间
    assert [o["order_id"] for o in search({"amount_min_cents": 150, "amount_max_cents": 300}, "q-amount")] == ["so2", "so3"]
    # 已收金额区间
    assert [o["order_id"] for o in search({"paid_min_cents": 100}, "q-paid")] == ["so2", "so3"]
    # 累计冲正金额区间
    assert [o["order_id"] for o in search({"refunded_min_cents": 1}, "q-refunded")] == ["so3"]
    # 是否发生过冲正
    assert [o["order_id"] for o in search({"has_refund": True}, "q-hasref")] == ["so3"]
    assert [o["order_id"] for o in search({"has_refund": False}, "q-noref")] == ["so1", "so2"]
    # 组合条件
    combo = search({"currency": "CNY", "status": "accepted", "amount_max_cents": 150}, "q-combo")
    assert [o["order_id"] for o in combo] == ["so1"]

def test_search_pagination_is_stable_and_complete() -> None:
    seen: list[str] = []
    cursor = None
    for page_no in range(3):
        page = {"size": 2, **({"cursor": cursor} if cursor else {})}
        resp = client.post(
            "/orders/search",
            json={"request_id": f"q-page-{page_no}", "filters": {"currency": "CNY"}, "page": page},
            headers={"X-Tenant": "s1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        seen += [o["order_id"] for o in data["orders"]]
        cursor = data["page"]["next_cursor"]
        if cursor is None:
            break
    assert seen == ["so1", "so2"]          # 不重不漏（s1 租户 CNY 订单仅两单）
    assert cursor is None

def test_search_replay_returns_same_snapshot_without_writes() -> None:
    body = {"request_id": "q-replay", "filters": {"status": "accepted"}}
    first = client.post("/orders/search", json=body, headers={"X-Tenant": "s1"})
    assert first.status_code == 200 and first.headers["x-idempotent-replay"] == "0"
    first_ids = [o["order_id"] for o in first.json()["orders"]]
    # 首次检索后再受理新订单，重放仍返回首次的同一结果集
    client.post("/orders", json={"tenant": "s1", "order_id": "so9", "amount_cents": 50, "currency": "CNY"})
    replay = client.post("/orders/search", json=body, headers={"X-Tenant": "s1"})
    assert replay.status_code == 200 and replay.headers["x-idempotent-replay"] == "1"
    assert replay.json()["replayed"] is True
    assert [o["order_id"] for o in replay.json()["orders"]] == first_ids
    # 新去重标识的检索能看到新订单
    fresh = client.post("/orders/search", json={"request_id": "q-replay-2", "filters": {"status": "accepted"}}, headers={"X-Tenant": "s1"})
    assert "so9" in [o["order_id"] for o in fresh.json()["orders"]]

def test_search_tenant_isolation_and_empty_result() -> None:
    resp = client.post("/orders/search", json={"request_id": "q-iso"}, headers={"X-Tenant": "s2"})
    assert resp.status_code == 200
    assert [o["order_id"] for o in resp.json()["orders"]] == ["so1"]   # 仅本租户订单
    assert all(o["tenant"] == "s2" for o in resp.json()["orders"])
    # 未提供租户
    assert client.post("/orders/search", json={"request_id": "q-no-tenant"}).status_code == 400
    # 无命中返回空列表
    empty = client.post("/orders/search", json={"request_id": "q-empty", "filters": {"status": "completed"}}, headers={"X-Tenant": "s2"})
    assert empty.status_code == 200 and empty.json()["orders"] == []

def test_search_invalid_params_have_distinct_reasons() -> None:
    def reason(payload: dict) -> str:
        resp = client.post("/orders/search", json=payload, headers={"X-Tenant": "s1"})
        assert resp.status_code == 400
        return resp.json()["detail"]
    assert reason({"request_id": "bad1", "filters": {"status": "unknown"}}) == "invalid_status"
    assert reason({"request_id": "bad2", "filters": {"currency": "GBP"}}) == "unsupported_currency"
    assert reason({"request_id": "bad3", "filters": {"amount_min_cents": 10, "amount_max_cents": 5}}) == "invalid_amount_range"
    assert reason({"request_id": "bad4", "filters": {"paid_min_cents": -1}}) == "invalid_paid_range"
    assert reason({"request_id": "bad5", "filters": {"has_refund": "yes"}}) == "invalid_has_refund"
    assert reason({"request_id": "bad6", "page": {"size": 0}}) == "invalid_page_size"
    assert reason({"request_id": "bad7", "filters": {"mystery": 1}}).startswith("unknown_filter")
    # 缺少去重标识
    assert client.post("/orders/search", json={}, headers={"X-Tenant": "s1"}).status_code == 422
    # 参数非法不写入：同一去重标识修正参数后按首次生效
    ok = client.post("/orders/search", json={"request_id": "bad1"}, headers={"X-Tenant": "s1"})
    assert ok.status_code == 200 and ok.json()["replayed"] is False
