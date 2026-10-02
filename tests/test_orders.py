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

# ---------- 作废 ----------

def test_void_unpaid_order() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "v1", "amount_cents": 500, "currency": "CNY"})
    resp = client.post("/orders/v1/void", json={"biz_id": "void-1"}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["biz_id"] == "void-1" and data["replayed"] is False
    assert data["status"] == "voided"
    assert data["paid_cents"] == 0 and data["outstanding_cents"] == 500
    assert data["refunded_cents"] == 0 and data["written_off_cents"] == 0
    state = client.get("/orders/v1", headers={"X-Tenant": "t1"}).json()
    assert state["status"] == "voided" and state["amount_cents"] == 500  # 订单金额不变
    entries = client.get("/orders/v1/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "void"]
    assert entries[1]["biz_id"] == "void-1" and entries[1]["amount_cents"] == 0

def test_void_is_terminal_and_rejects_all_ops() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "v2", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/v2/void", json={"biz_id": "void-2"}, headers={"X-Tenant": "t1"})
    # 作废后收款、冲正、冲销、再作废一律拒绝且无任何变更
    assert client.post("/orders/v2/payments", json={"amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/v2/refunds", json={"biz_id": "v2-r", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/v2/writeoffs", json={"biz_id": "v2-w", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/v2/void", json={"biz_id": "void-2b"}, headers={"X-Tenant": "t1"}).status_code == 409
    state = client.get("/orders/v2", headers={"X-Tenant": "t1"}).json()
    assert state["status"] == "voided" and state["paid_cents"] == 0
    entries = client.get("/orders/v2/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "void"]  # 无新流水

def test_void_rejected_after_payment() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "v3", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/v3/payments", json={"amount_cents": 1}, headers={"X-Tenant": "t1"})
    # 已收大于零：拒绝作废，状态与流水不变
    assert client.post("/orders/v3/void", json={"biz_id": "void-3"}, headers={"X-Tenant": "t1"}).status_code == 409
    state = client.get("/orders/v3", headers={"X-Tenant": "t1"}).json()
    assert state["status"] == "accepted" and state["paid_cents"] == 1
    entries = client.get("/orders/v3/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment"]

def test_void_replay_returns_same_record_without_new_ledger() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "v4", "amount_cents": 100, "currency": "CNY"})
    first = client.post("/orders/v4/void", json={"biz_id": "void-4"}, headers={"X-Tenant": "t1"})
    second = client.post("/orders/v4/void", json={"biz_id": "void-4"}, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["created_at"] == second.json()["created_at"]
    assert second.json()["replayed"] is True
    assert second.headers["x-idempotent-replay"] == "1"
    entries = client.get("/orders/v4/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "void"]  # 不重复写流水

def test_void_biz_id_conflicts_and_tenant_isolation() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "v5a", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders", json={"tenant": "t1", "order_id": "v5b", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/v5a/void", json={"biz_id": "void-5"}, headers={"X-Tenant": "t1"})
    # 同租户将该标识用于另一订单的作废：拒绝且状态不变
    assert client.post("/orders/v5b/void", json={"biz_id": "void-5"}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.get("/orders/v5b", headers={"X-Tenant": "t1"}).json()["status"] == "accepted"
    # 同租户将该标识用于冲销：拒绝
    client.post("/orders", json={"tenant": "t1", "order_id": "v5c", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/v5c/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/v5c/writeoffs", json={"biz_id": "void-5", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 409
    # 不同标识各自生效
    assert client.post("/orders/v5b/void", json={"biz_id": "void-5b"}, headers={"X-Tenant": "t1"}).status_code == 200
    # 不同租户可复用同一标识
    client.post("/orders", json={"tenant": "t2", "order_id": "v5d", "amount_cents": 100, "currency": "CNY"})
    assert client.post("/orders/v5d/void", json={"biz_id": "void-5"}, headers={"X-Tenant": "t2"}).status_code == 200

def test_void_unknown_order_and_cross_tenant_are_404() -> None:
    assert client.post("/orders/nope/void", json={"biz_id": "void-x"}, headers={"X-Tenant": "t1"}).status_code == 404
    client.post("/orders", json={"tenant": "t1", "order_id": "v6", "amount_cents": 100, "currency": "CNY"})
    assert client.post("/orders/v6/void", json={"biz_id": "void-6"}, headers={"X-Tenant": "t2"}).status_code == 404
    # 跨租户按不存在处理：不产生流水，本租户仍可正常作废
    assert client.get("/orders/v6/ledger", headers={"X-Tenant": "t1"}).json()["entries"][0]["op_type"] == "accept"
    assert client.post("/orders/v6/void", json={"biz_id": "void-6"}, headers={"X-Tenant": "t1"}).status_code == 200

# ---------- 冲销 ----------

def test_writeoff_after_payment() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "w1", "amount_cents": 300, "currency": "CNY"})
    client.post("/orders/w1/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    resp = client.post("/orders/w1/writeoffs", json={"biz_id": "wo-1", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["biz_id"] == "wo-1" and data["amount_cents"] == 100 and data["replayed"] is False
    assert data["paid_cents"] == 300          # 已收不变
    assert data["outstanding_cents"] == 0     # 未收不变
    assert data["refunded_cents"] == 0        # 累计冲正不变
    assert data["written_off_cents"] == 100   # 累计冲销增加
    assert data["refundable_cents"] == 300    # 可退余额不受冲销影响
    assert data["status"] == "settled"        # 结清不等于终态

def test_writeoff_bounded_by_paid_minus_refund_minus_writeoff() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "w2", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/w2/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/w2/refunds", json={"biz_id": "w2-r", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    client.post("/orders/w2/writeoffs", json={"biz_id": "wo-2a", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    # 可冲销上限 = 200 - 50 - 100 = 50；超出拒绝且无任何变更
    assert client.post("/orders/w2/writeoffs", json={"biz_id": "wo-2b", "amount_cents": 51}, headers={"X-Tenant": "t1"}).status_code == 409
    # 非正数被模型拒绝
    assert client.post("/orders/w2/writeoffs", json={"biz_id": "wo-2c", "amount_cents": 0}, headers={"X-Tenant": "t1"}).status_code == 422
    state = client.get("/orders/w2", headers={"X-Tenant": "t1"}).json()
    assert state["written_off_cents"] == 100 and state["refunded_cents"] == 50
    # 边界值生效并进入终态
    assert client.post("/orders/w2/writeoffs", json={"biz_id": "wo-2d", "amount_cents": 50}, headers={"X-Tenant": "t1"}).status_code == 200
    state = client.get("/orders/w2", headers={"X-Tenant": "t1"}).json()
    assert state["written_off_cents"] == 150 and state["status"] == "completed"

def test_writeoff_is_idempotent_by_biz_id() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "w3", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/w3/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    first = client.post("/orders/w3/writeoffs", json={"biz_id": "wo-3", "amount_cents": 60}, headers={"X-Tenant": "t1"})
    second = client.post("/orders/w3/writeoffs", json={"biz_id": "wo-3", "amount_cents": 60}, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["created_at"] == second.json()["created_at"]
    assert second.json()["replayed"] is True
    assert second.headers["x-idempotent-replay"] == "1"
    state = client.get("/orders/w3", headers={"X-Tenant": "t1"}).json()
    assert state["written_off_cents"] == 60   # 不重复累计
    entries = client.get("/orders/w3/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "writeoff"]  # 不新增流水
    assert entries[2]["biz_id"] == "wo-3" and entries[2]["written_off_result_cents"] == 60
    # 不同标识各自生效
    assert client.post("/orders/w3/writeoffs", json={"biz_id": "wo-3b", "amount_cents": 40}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.get("/orders/w3", headers={"X-Tenant": "t1"}).json()["written_off_cents"] == 100

def test_writeoff_biz_id_not_shared_with_refund_or_void() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "w4", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/w4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/w4/refunds", json={"biz_id": "w4-shared", "amount_cents": 10}, headers={"X-Tenant": "t1"})
    # 冲正业务标识不得复用为冲销标识
    assert client.post("/orders/w4/writeoffs", json={"biz_id": "w4-shared", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 409
    # 冲销标识也不得复用为冲正标识
    client.post("/orders/w4/writeoffs", json={"biz_id": "w4-wo", "amount_cents": 10}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/w4/refunds", json={"biz_id": "w4-wo", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 409
    state = client.get("/orders/w4", headers={"X-Tenant": "t1"}).json()
    assert state["refunded_cents"] == 10 and state["written_off_cents"] == 10

def test_writeoff_unknown_order_and_cross_tenant_are_404() -> None:
    assert client.post("/orders/nope/writeoffs", json={"biz_id": "wo-x", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 404
    client.post("/orders", json={"tenant": "t1", "order_id": "w5", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/w5/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/w5/writeoffs", json={"biz_id": "wo-5", "amount_cents": 10}, headers={"X-Tenant": "t2"}).status_code == 404

# ---------- 终态不变式 ----------

def test_terminal_rejects_payment_refund_writeoff_void() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "t-term", "amount_cents": 500, "currency": "CNY"})
    client.post("/orders/t-term/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/t-term/refunds", json={"biz_id": "tt-r", "amount_cents": 60}, headers={"X-Tenant": "t1"})
    # 可退余额 40 冲销归零：已收>0 且 可退余额-累计冲销=0，进入终态（未收仍有 400）
    client.post("/orders/t-term/writeoffs", json={"biz_id": "tt-w", "amount_cents": 40}, headers={"X-Tenant": "t1"})
    state = client.get("/orders/t-term", headers={"X-Tenant": "t1"}).json()
    assert state["status"] == "completed" and state["outstanding_cents"] == 400
    # 终态不可逆：收款、冲正、冲销、作废一律拒绝且无任何变更
    assert client.post("/orders/t-term/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/t-term/refunds", json={"biz_id": "tt-r2", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/t-term/writeoffs", json={"biz_id": "tt-w2", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/t-term/void", json={"biz_id": "tt-v"}, headers={"X-Tenant": "t1"}).status_code == 409
    after = client.get("/orders/t-term", headers={"X-Tenant": "t1"}).json()
    assert after == state
    entries = client.get("/orders/t-term/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "refund", "writeoff"]

def test_settled_is_not_terminal_and_void_exclusive_with_terminal() -> None:
    # 结清后仍可在可退余额范围内冲正或冲销
    client.post("/orders", json={"tenant": "t1", "order_id": "t-st", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/t-st/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.get("/orders/t-st", headers={"X-Tenant": "t1"}).json()["status"] == "settled"
    assert client.post("/orders/t-st/writeoffs", json={"biz_id": "ts-w", "amount_cents": 30}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.get("/orders/t-st", headers={"X-Tenant": "t1"}).json()["status"] == "settled"
    # 已收款订单不可作废
    assert client.post("/orders/t-st/void", json={"biz_id": "ts-v"}, headers={"X-Tenant": "t1"}).status_code == 409
    # 无收款订单只能作废、不进终态
    client.post("/orders", json={"tenant": "t1", "order_id": "t-st2", "amount_cents": 100, "currency": "CNY"})
    assert client.post("/orders/t-st2/writeoffs", json={"biz_id": "ts-w2", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/t-st2/void", json={"biz_id": "ts-v2"}, headers={"X-Tenant": "t1"}).status_code == 200

def test_concurrent_writeoffs_never_exceed_writeoffable() -> None:
    import threading
    client.post("/orders", json={"tenant": "t1", "order_id": "c2", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/c2/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    results: list[int] = []
    def writeoff(biz: str) -> None:
        results.append(client.post("/orders/c2/writeoffs", json={"biz_id": biz, "amount_cents": 60}, headers={"X-Tenant": "t1"}).status_code)
    t1 = threading.Thread(target=writeoff, args=("cw-a",))
    t2 = threading.Thread(target=writeoff, args=("cw-b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [200, 409]   # 串行化后只有一笔生效
    state = client.get("/orders/c2", headers={"X-Tenant": "t1"}).json()
    assert state["written_off_cents"] == 60

def test_void_and_writeoff_survive_restart() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "p2", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/p2/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/p2/writeoffs", json={"biz_id": "p2-wo", "amount_cents": 80}, headers={"X-Tenant": "t1"})
    client.post("/orders", json={"tenant": "t1", "order_id": "p3", "amount_cents": 50, "currency": "CNY"})
    client.post("/orders/p3/void", json={"biz_id": "p3-void"}, headers={"X-Tenant": "t1"})
    # 模拟服务重启：重放同一冲销与作废请求
    replay_wo = client.post("/orders/p2/writeoffs", json={"biz_id": "p2-wo", "amount_cents": 80}, headers={"X-Tenant": "t1"})
    assert replay_wo.json()["replayed"] is True
    replay_void = client.post("/orders/p3/void", json={"biz_id": "p3-void"}, headers={"X-Tenant": "t1"})
    assert replay_void.json()["replayed"] is True
    state = client.get("/orders/p2", headers={"X-Tenant": "t1"}).json()
    assert state["written_off_cents"] == 80 and state["paid_cents"] == 200
    assert client.get("/orders/p3", headers={"X-Tenant": "t1"}).json()["status"] == "voided"
    assert len(client.get("/orders/p2/ledger", headers={"X-Tenant": "t1"}).json()["entries"]) == 3
    assert len(client.get("/orders/p3/ledger", headers={"X-Tenant": "t1"}).json()["entries"]) == 2

# ---------- 收款冲正修正 ----------

def test_payment_correction_reduces_paid_and_adds_ledger() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "pc1", "amount_cents": 500, "currency": "CNY"})
    client.post("/orders/pc1/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    resp = client.post("/orders/pc1/payment-corrections", json={"biz_id": "pc-1", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["biz_id"] == "pc-1" and data["amount_cents"] == 100
    assert data["correction_biz_id"] is None and data["replayed"] is False
    assert data["paid_cents"] == 200          # 已收减少
    assert data["outstanding_cents"] == 300   # 未收增加
    assert data["refunded_cents"] == 0        # 累计冲正不改
    assert data["written_off_cents"] == 0
    assert data["refundable_cents"] == 200    # 可退余额随已收下降
    assert data["status"] == "accepted"       # 结清后修正回退为已受理
    state = client.get("/orders/pc1", headers={"X-Tenant": "t1"}).json()
    assert state["amount_cents"] == 500       # 订单金额不改
    entries = client.get("/orders/pc1/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "correction"]
    assert entries[2]["biz_id"] == "pc-1" and entries[2]["amount_cents"] == 100
    assert entries[2]["paid_result_cents"] == 200 and entries[2]["outstanding_result_cents"] == 300

def test_payment_correction_is_idempotent_by_biz_id() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "pc2", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/pc2/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    body = {"biz_id": "pc-dup", "amount_cents": 50}
    first = client.post("/orders/pc2/payment-corrections", json=body, headers={"X-Tenant": "t1"})
    second = client.post("/orders/pc2/payment-corrections", json=body, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["created_at"] == second.json()["created_at"]
    assert second.json()["replayed"] is True
    assert second.headers["x-idempotent-replay"] == "1"
    state = client.get("/orders/pc2", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 150 and state["refundable_cents"] == 150  # 不重复扣减
    entries = client.get("/orders/pc2/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "correction"]

def test_payment_correction_distinct_biz_ids_apply_separately() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "pc3", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/pc3/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/pc3/payment-corrections", json={"biz_id": "pc-a", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    client.post("/orders/pc3/payment-corrections", json={"biz_id": "pc-b", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    state = client.get("/orders/pc3", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 100 and state["outstanding_cents"] == 100

def test_payment_correction_rejected_over_paid_balance() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "pc4", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/pc4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    # 未收款不得修正
    client.post("/orders", json={"tenant": "t1", "order_id": "pc4b", "amount_cents": 200, "currency": "CNY"})
    assert client.post("/orders/pc4b/payment-corrections", json={"biz_id": "pc-n1", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409
    # 超过已收
    assert client.post("/orders/pc4/payment-corrections", json={"biz_id": "pc-n2", "amount_cents": 101}, headers={"X-Tenant": "t1"}).status_code == 409
    # 非正数被模型拒绝
    assert client.post("/orders/pc4/payment-corrections", json={"biz_id": "pc-n3", "amount_cents": 0}, headers={"X-Tenant": "t1"}).status_code == 422
    state = client.get("/orders/pc4", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 100 and state["outstanding_cents"] == 100
    entries = client.get("/orders/pc4/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment"]

def test_payment_correction_respects_refund_and_writeoff_caps() -> None:
    # 已收 200、冲正 50、冲销 100：可修正上限 = 200-50-100 = 50
    client.post("/orders", json={"tenant": "t1", "order_id": "pc5", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/pc5/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/pc5/refunds", json={"biz_id": "pc5-r", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    client.post("/orders/pc5/writeoffs", json={"biz_id": "pc5-w", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/pc5/payment-corrections", json={"biz_id": "pc5-c1", "amount_cents": 51}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/pc5/payment-corrections", json={"biz_id": "pc5-c2", "amount_cents": 50}, headers={"X-Tenant": "t1"}).status_code == 200
    state = client.get("/orders/pc5", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 150 and state["refunded_cents"] == 50 and state["written_off_cents"] == 100
    # 累计冲正、累计冲销不越上限：此后冲正/冲销均无空间
    assert client.post("/orders/pc5/refunds", json={"biz_id": "pc5-r2", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/pc5/writeoffs", json={"biz_id": "pc5-w2", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409

def test_payment_correction_rejected_in_voided_and_completed() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "pc6v", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/pc6v/void", json={"biz_id": "pc6-void"}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/pc6v/payment-corrections", json={"biz_id": "pc6-c", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409
    # 收付完结终态：冲正将可退余额归零
    client.post("/orders", json={"tenant": "t1", "order_id": "pc6t", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/pc6t/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/pc6t/refunds", json={"biz_id": "pc6-r", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.get("/orders/pc6t", headers={"X-Tenant": "t1"}).json()["status"] == "completed"
    assert client.post("/orders/pc6t/payment-corrections", json={"biz_id": "pc6-ct", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409

def test_payment_correction_biz_id_not_shared_with_other_ops() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "pc7a", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders", json={"tenant": "t1", "order_id": "pc7b", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/pc7a/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/pc7b/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/pc7a/refunds", json={"biz_id": "pc7-shared", "amount_cents": 10}, headers={"X-Tenant": "t1"})
    # 冲正业务标识不得复用为修正标识
    assert client.post("/orders/pc7a/payment-corrections", json={"biz_id": "pc7-shared", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 409
    # 修正标识不得复用到另一订单
    assert client.post("/orders/pc7a/payment-corrections", json={"biz_id": "pc7-x", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/pc7b/payment-corrections", json={"biz_id": "pc7-x", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.get("/orders/pc7b", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 100
    # 不同租户可复用
    client.post("/orders", json={"tenant": "t2", "order_id": "pc7c", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/pc7c/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t2"})
    assert client.post("/orders/pc7c/payment-corrections", json={"biz_id": "pc7-x", "amount_cents": 10}, headers={"X-Tenant": "t2"}).status_code == 200

def test_payment_correction_unknown_order_and_cross_tenant_are_404() -> None:
    assert client.post("/orders/nope/payment-corrections", json={"biz_id": "pc-x", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 404
    client.post("/orders", json={"tenant": "t1", "order_id": "pc8", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/pc8/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/pc8/payment-corrections", json={"biz_id": "pc-8", "amount_cents": 10}, headers={"X-Tenant": "t2"}).status_code == 404
    # 跨租户不产生流水
    assert [e["op_type"] for e in client.get("/orders/pc8/ledger", headers={"X-Tenant": "t1"}).json()["entries"]] == ["accept", "payment"]

# ---------- 取消收款冲正修正 ----------

def test_cancel_payment_correction_restores_paid_and_adds_ledger() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "cc1", "amount_cents": 500, "currency": "CNY"})
    client.post("/orders/cc1/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    client.post("/orders/cc1/payment-corrections", json={"biz_id": "cc1-c", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    resp = client.post(
        "/orders/cc1/payment-correction-cancels",
        json={"biz_id": "cc1-x", "correction_biz_id": "cc1-c"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["biz_id"] == "cc1-x" and data["correction_biz_id"] == "cc1-c"
    assert data["amount_cents"] == 100 and data["replayed"] is False  # 金额取原始修正金额
    assert data["paid_cents"] == 300          # 已收恢复
    assert data["outstanding_cents"] == 200   # 未收减少
    assert data["refunded_cents"] == 0 and data["written_off_cents"] == 0
    assert data["refundable_cents"] == 300 and data["status"] == "accepted"
    entries = client.get("/orders/cc1/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "correction", "correction_cancel"]
    assert entries[3]["biz_id"] == "cc1-x" and entries[3]["amount_cents"] == 100
    assert entries[3]["paid_result_cents"] == 300

def test_cancel_payment_correction_restores_settled_status() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "cc2", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/cc2/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/cc2/payment-corrections", json={"biz_id": "cc2-c", "amount_cents": 40}, headers={"X-Tenant": "t1"})
    assert client.get("/orders/cc2", headers={"X-Tenant": "t1"}).json()["status"] == "accepted"
    resp = client.post(
        "/orders/cc2/payment-correction-cancels",
        json={"biz_id": "cc2-x", "correction_biz_id": "cc2-c"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 200 and resp.json()["status"] == "settled"

def test_cancel_payment_correction_is_idempotent_by_biz_id() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "cc3", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/cc3/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/cc3/payment-corrections", json={"biz_id": "cc3-c", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    body = {"biz_id": "cc3-x", "correction_biz_id": "cc3-c"}
    first = client.post("/orders/cc3/payment-correction-cancels", json=body, headers={"X-Tenant": "t1"})
    second = client.post("/orders/cc3/payment-correction-cancels", json=body, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["created_at"] == second.json()["created_at"]
    assert second.json()["replayed"] is True
    assert second.headers["x-idempotent-replay"] == "1"
    state = client.get("/orders/cc3", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 200         # 不重复恢复
    entries = client.get("/orders/cc3/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "correction", "correction_cancel"]

def test_cancel_payment_correction_rejected_cases() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "cc4", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/cc4/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/cc4/payment-corrections", json={"biz_id": "cc4-c", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    # 被取消修正不存在
    assert client.post(
        "/orders/cc4/payment-correction-cancels",
        json={"biz_id": "cc4-x0", "correction_biz_id": "missing"},
        headers={"X-Tenant": "t1"},
    ).status_code == 409
    # 取消成功
    assert client.post(
        "/orders/cc4/payment-correction-cancels",
        json={"biz_id": "cc4-x1", "correction_biz_id": "cc4-c"},
        headers={"X-Tenant": "t1"},
    ).status_code == 200
    # 已被取消：再次取消拒绝
    assert client.post(
        "/orders/cc4/payment-correction-cancels",
        json={"biz_id": "cc4-x2", "correction_biz_id": "cc4-c"},
        headers={"X-Tenant": "t1"},
    ).status_code == 409
    state = client.get("/orders/cc4", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 200         # 只恢复一次
    # 订单不匹配：修正属于 cc4，拿去 cc5 取消
    client.post("/orders", json={"tenant": "t1", "order_id": "cc5", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/cc5/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    assert client.post(
        "/orders/cc5/payment-correction-cancels",
        json={"biz_id": "cc4-x3", "correction_biz_id": "cc4-c"},
        headers={"X-Tenant": "t1"},
    ).status_code == 409
    assert client.get("/orders/cc5", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 200
    # 终态订单拒绝取消（构造 completed 后重放取消标识之外的新取消）
    client.post("/orders", json={"tenant": "t1", "order_id": "cc6", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/cc6/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/cc6/payment-corrections", json={"biz_id": "cc6-c", "amount_cents": 1}, headers={"X-Tenant": "t1"})
    client.post("/orders/cc6/refunds", json={"biz_id": "cc6-r", "amount_cents": 99}, headers={"X-Tenant": "t1"})
    assert client.get("/orders/cc6", headers={"X-Tenant": "t1"}).json()["status"] == "completed"
    assert client.post(
        "/orders/cc6/payment-correction-cancels",
        json={"biz_id": "cc6-x", "correction_biz_id": "cc6-c"},
        headers={"X-Tenant": "t1"},
    ).status_code == 409

def test_cancel_payment_correction_biz_id_namespace_and_404() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "cc7", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/cc7/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/cc7/payment-corrections", json={"biz_id": "cc7-c", "amount_cents": 30}, headers={"X-Tenant": "t1"})
    # 取消标识与修正标识不得混用
    dup = client.post(
        "/orders/cc7/payment-correction-cancels",
        json={"biz_id": "cc7-c", "correction_biz_id": "cc7-c"},
        headers={"X-Tenant": "t1"},
    )
    assert dup.status_code == 409
    # 取消标识重放跨订单拒绝
    assert client.post(
        "/orders/cc7/payment-correction-cancels",
        json={"biz_id": "cc7-x", "correction_biz_id": "cc7-c"},
        headers={"X-Tenant": "t1"},
    ).status_code == 200
    assert client.post(
        "/orders/cc7/payment-correction-cancels",
        json={"biz_id": "cc7-x", "correction_biz_id": "cc7-c"},
        headers={"X-Tenant": "t1"},
    ).status_code == 200  # 同订单重放仍 200
    # 订单不存在 / 跨租户
    assert client.post(
        "/orders/nope/payment-correction-cancels",
        json={"biz_id": "cc-x", "correction_biz_id": "whatever"},
        headers={"X-Tenant": "t1"},
    ).status_code == 404
    assert client.post(
        "/orders/cc7/payment-correction-cancels",
        json={"biz_id": "cc-x2", "correction_biz_id": "cc7-c"},
        headers={"X-Tenant": "t2"},
    ).status_code == 404

# ---------- 修正交错、并发与持久化 ----------

def test_interleaved_correction_cancel_with_payment_stays_closed() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "ic1", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/ic1/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})  # settled
    # 修正 60：已收 40、未收 60
    client.post("/orders/ic1/payment-corrections", json={"biz_id": "ic-c1", "amount_cents": 60}, headers={"X-Tenant": "t1"})
    # 再收款不得超过未收：收 70 拒绝，收 60 生效回到结清
    assert client.post("/orders/ic1/payments", json={"amount_cents": 70}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/ic1/payments", json={"amount_cents": 60}, headers={"X-Tenant": "t1"}).status_code == 200
    state = client.get("/orders/ic1", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 100 and state["outstanding_cents"] == 0 and state["status"] == "settled"
    # 取消修正恢复 60 会使已收 160 超过订单金额 100：拒绝
    assert client.post(
        "/orders/ic1/payment-correction-cancels",
        json={"biz_id": "ic-x1", "correction_biz_id": "ic-c1"},
        headers={"X-Tenant": "t1"},
    ).status_code == 409
    # 取消修正恢复 60 会使已收 160 超过订单金额 100：拒绝
    assert client.post(
        "/orders/ic1/payment-correction-cancels",
        json={"biz_id": "ic-x1", "correction_biz_id": "ic-c1"},
        headers={"X-Tenant": "t1"},
    ).status_code == 409
    # 只有再登记一笔修正把已收降下来，才有恢复空间：修正 60 后已收 40
    client.post("/orders/ic1/payment-corrections", json={"biz_id": "ic-c2", "amount_cents": 60}, headers={"X-Tenant": "t1"})
    assert client.get("/orders/ic1", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 40
    # 取消原修正：恢复后已收 100，不超订单金额，账面闭合回到结清
    assert client.post(
        "/orders/ic1/payment-correction-cancels",
        json={"biz_id": "ic-x2", "correction_biz_id": "ic-c1"},
        headers={"X-Tenant": "t1"},
    ).status_code == 200
    state = client.get("/orders/ic1", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 100 and state["outstanding_cents"] == 0 and state["status"] == "settled"

def test_concurrent_corrections_never_drive_paid_negative() -> None:
    import threading
    client.post("/orders", json={"tenant": "t1", "order_id": "ic2", "amount_cents": 100, "currency": "CNY"})
    client.post("/orders/ic2/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    results: list[int] = []
    def correction(biz: str) -> None:
        results.append(client.post("/orders/ic2/payment-corrections", json={"biz_id": biz, "amount_cents": 60}, headers={"X-Tenant": "t1"}).status_code)
    t1 = threading.Thread(target=correction, args=("ic-a",))
    t2 = threading.Thread(target=correction, args=("ic-b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [200, 409]   # 串行化后只有一笔生效
    state = client.get("/orders/ic2", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 40 and state["outstanding_cents"] == 60

def test_corrections_and_cancels_survive_restart() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "ic3", "amount_cents": 200, "currency": "CNY"})
    client.post("/orders/ic3/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/ic3/payment-corrections", json={"biz_id": "ic3-c1", "amount_cents": 80}, headers={"X-Tenant": "t1"})
    client.post("/orders/ic3/payment-corrections", json={"biz_id": "ic3-c2", "amount_cents": 20}, headers={"X-Tenant": "t1"})
    client.post(
        "/orders/ic3/payment-correction-cancels",
        json={"biz_id": "ic3-x1", "correction_biz_id": "ic3-c1"},
        headers={"X-Tenant": "t1"},
    )
    # 模拟服务重启：重放修正与取消请求
    replay_c = client.post("/orders/ic3/payment-corrections", json={"biz_id": "ic3-c2", "amount_cents": 20}, headers={"X-Tenant": "t1"})
    assert replay_c.json()["replayed"] is True
    replay_x = client.post(
        "/orders/ic3/payment-correction-cancels",
        json={"biz_id": "ic3-x1", "correction_biz_id": "ic3-c1"},
        headers={"X-Tenant": "t1"},
    )
    assert replay_x.json()["replayed"] is True
    state = client.get("/orders/ic3", headers={"X-Tenant": "t1"}).json()
    # 已收 = 200-80-20+80(取消) = 180
    assert state["paid_cents"] == 180 and state["outstanding_cents"] == 20
    entries = client.get("/orders/ic3/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["op_type"] for e in entries] == [
        "accept", "payment", "correction", "correction", "correction_cancel",
    ]
