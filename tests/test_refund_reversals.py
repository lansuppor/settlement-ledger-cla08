import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect as db_connect
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "rv"}


def _order(oid: str, amount: int = 1000, tenant: str = "rv", currency: str = "CNY") -> None:
    client.post(
        "/orders",
        json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": currency},
    )


def _pay(oid: str, amount: int, tenant: str = "rv") -> None:
    client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})


def _accept(rid: str, oid: str, amount: int, tenant: str = "rv"):
    return client.post(
        "/refund-orders",
        json={"refund_id": rid, "order_id": oid, "requested_amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def _settled_refund(
    rid: str, oid: str, requested: int, actual: int | None = None, tenant: str = "rv"
) -> None:
    """受理 → 审核通过 → 执行到账，得到一张已到账退款单。"""
    _accept(rid, oid, requested, tenant=tenant)
    th = {"X-Tenant": tenant}
    client.post(f"/refund-orders/{rid}/approve", headers=th)
    body = {"outcome": "succeeded"}
    if actual is not None:
        body["amount_cents"] = actual
    resp = client.post(f"/refund-orders/{rid}/execute", json=body, headers=th)
    assert resp.status_code == 200 and resp.json()["status"] == "succeeded"


def _reverse(rid: str, biz: str, tenant: str = "rv"):
    return client.post(
        f"/refund-orders/{rid}/reverse", json={"biz_id": biz}, headers={"X-Tenant": tenant}
    )


# ---------- 冲正生效与原订单联动 ----------

def test_reverse_succeeded_refund_rebooks_order_and_appends_ledger() -> None:
    _order("o1", 500); _pay("o1", 500)
    _settled_refund("r1", "o1", 200)
    order_before = client.get("/orders/o1", headers=H).json()
    assert order_before["refunded_cents"] == 200 and order_before["status"] == "settled"

    resp = _reverse("r1", "rev-1")
    assert resp.status_code == 200
    data = resp.json()
    assert resp.headers["x-idempotent-replay"] == "0" and data["replayed"] is False
    # 响应记本次冲正业务标识、退款单、原订单与实退金额，以及冲正后订单结果金额
    assert data["biz_id"] == "rev-1" and data["refund_id"] == "r1" and data["order_id"] == "o1"
    assert data["amount_cents"] == 200
    assert data["paid_cents"] == 500 and data["outstanding_cents"] == 0
    assert data["refunded_cents"] == 0 and data["refundable_cents"] == 500
    assert data["written_off_cents"] == 0 and data["order_status"] == "settled"

    # 退款单进入已冲正终态并落库冲正业务标识
    ro = client.get("/refund-orders/r1", headers=H).json()
    assert ro["status"] == "reversed" and ro["reversal_biz_id"] == "rev-1"
    # 实退金额仍保留
    assert ro["refunded_amount_cents"] == 200

    # 原订单：累计冲正扣减这次的量；订单金额、已收、累计冲销不改
    order = client.get("/orders/o1", headers=H).json()
    assert order["amount_cents"] == 500 and order["paid_cents"] == 500
    assert order["refunded_cents"] == 0 and order["written_off_cents"] == 0
    assert order["refundable_cents"] == 500 and order["status"] == "settled"

    # 追加一条冲正流水，业务标识记本次冲正业务标识、金额记实退金额；既有流水不改
    entries = client.get("/orders/o1/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "refund", "refund_reversal"]
    rev_entry = entries[3]
    assert rev_entry["biz_id"] == "rev-1" and rev_entry["amount_cents"] == 200
    assert rev_entry["refunded_result_cents"] == 0
    # 到账那条原有冲正流水保持不变
    assert entries[2]["biz_id"] == "r1" and entries[2]["amount_cents"] == 200
    assert entries[2]["refunded_result_cents"] == 200


def test_reverse_amount_is_stored_actual_not_requested() -> None:
    # 实退金额落库为 150（申请 200）：冲正金额取实退，调用方不可指定
    _order("o2", 500); _pay("o2", 500)
    _settled_refund("r2", "o2", 200, actual=150)
    data = _reverse("r2", "rev-2").json()
    assert data["amount_cents"] == 150
    assert client.get("/orders/o2", headers=H).json()["refunded_cents"] == 0


def test_reverse_rejudges_completed_order_back_to_settled() -> None:
    # 到账曾把订单送入收付完结终态；冲正重新入账后按既有口径回到非终态
    _order("o3", 100); _pay("o3", 100)
    _settled_refund("r3", "o3", 100)
    assert client.get("/orders/o3", headers=H).json()["status"] == "completed"
    resp = _reverse("r3", "rev-3")
    assert resp.json()["order_status"] == "settled"
    assert client.get("/orders/o3", headers=H).json()["status"] == "settled"


def test_reverse_deducts_only_this_refund_keeps_other_refunds() -> None:
    # 原订单上还有另一笔冲正：只扣减本该退款单的实退金额
    _order("o4", 500); _pay("o4", 500)
    client.post("/orders/o4/refunds", json={"biz_id": "direct-r4", "amount_cents": 100}, headers=H)
    _settled_refund("r4", "o4", 200)  # 累计冲正 300
    assert client.get("/orders/o4", headers=H).json()["refunded_cents"] == 300
    data = _reverse("r4", "rev-4").json()
    assert data["refunded_cents"] == 100  # 只剩直接冲正那笔
    assert data["refundable_cents"] == 400


# ---------- 仅已到账可冲正，其余状态拒绝且无变更 ----------

def test_reverse_only_allowed_from_succeeded() -> None:
    _order("o5", 500); _pay("o5", 500)
    _accept("r5a", "o5", 10)
    # accepted
    assert _reverse("r5a", "rev-x1").status_code == 409
    # approved
    client.post("/refund-orders/r5a/approve", headers=H)
    assert _reverse("r5a", "rev-x2").status_code == 409
    # failed
    client.post("/refund-orders/r5a/execute", json={"outcome": "failed", "reason": "boom"}, headers=H)
    assert _reverse("r5a", "rev-x3").status_code == 409

    _accept("r5b", "o5", 10)
    client.post("/refund-orders/r5b/reject", headers=H)
    assert _reverse("r5b", "rev-x4").status_code == 409  # rejected

    _accept("r5c", "o5", 10)
    client.post("/refund-orders/r5c/cancel", headers=H)
    assert _reverse("r5c", "rev-x5").status_code == 409  # cancelled

    # 全部拒绝无任何变更：累计冲正为 0，无冲正流水
    assert client.get("/orders/o5", headers=H).json()["refunded_cents"] == 0
    entries = client.get("/orders/o5/ledger", headers=H).json()["entries"]
    assert "refund_reversal" not in [e["op_type"] for e in entries]


def test_reversed_is_terminal_all_lifecycle_ops_rejected() -> None:
    _order("o6", 500); _pay("o6", 500)
    _settled_refund("r6", "o6", 100)
    assert _reverse("r6", "rev-6").status_code == 200
    # 此后审核、执行到账、执行失败、撤销、再冲正一律拒绝
    assert client.post("/refund-orders/r6/approve", headers=H).status_code == 409
    assert client.post("/refund-orders/r6/reject", headers=H).status_code == 409
    assert client.post(
        "/refund-orders/r6/execute", json={"outcome": "succeeded"}, headers=H
    ).status_code == 409
    assert client.post(
        "/refund-orders/r6/execute", json={"outcome": "failed", "reason": "x"}, headers=H
    ).status_code == 409
    assert client.post("/refund-orders/r6/cancel", headers=H).status_code == 409
    # 同一退款单以不同业务标识再次冲正也拒绝，且无任何变更
    assert _reverse("r6", "rev-6-other").status_code == 409
    order = client.get("/orders/o6", headers=H).json()
    assert order["refunded_cents"] == 0  # 未被二次扣减（首次已扣到 0，二次未生效）
    entries = client.get("/orders/o6/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries].count("refund_reversal") == 1


# ---------- 幂等重放 ----------

def test_reverse_replay_same_biz_id_applies_once() -> None:
    _order("o7", 500); _pay("o7", 500)
    _settled_refund("r7", "o7", 200)
    first = _reverse("r7", "rev-7")
    second = _reverse("r7", "rev-7")
    assert first.status_code == 200 and second.status_code == 200
    assert second.headers["x-idempotent-replay"] == "1"
    assert second.json()["replayed"] is True
    # 同一记录
    assert second.json()["created_at"] == first.json()["created_at"]
    assert second.json()["amount_cents"] == 200
    # 不重复扣减、不新增流水
    assert client.get("/orders/o7", headers=H).json()["refunded_cents"] == 0
    entries = client.get("/orders/o7/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries].count("refund_reversal") == 1
    # 只有一条冲正记录
    conn = db_connect()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM refund_reversals WHERE tenant=? AND biz_id=?", ("rv", "rev-7")
    ).fetchone()["c"]
    conn.close()
    assert count == 1


def test_reverse_replay_returns_first_snapshot_even_after_later_ledger_change() -> None:
    # 重放返回“当时订单结果金额”：冲正后账目再变化，重放仍返回首次快照
    _order("o8", 500); _pay("o8", 500)
    _settled_refund("r8", "o8", 200)
    first = _reverse("r8", "rev-8")
    assert first.json()["refunded_cents"] == 0
    # 冲正恢复了可退余额，再发生一笔直接冲正占用 150
    later = client.post(
        "/orders/o8/refunds", json={"biz_id": "direct-r8", "amount_cents": 150}, headers=H
    )
    assert later.status_code == 200
    assert client.get("/orders/o8", headers=H).json()["refunded_cents"] == 150
    replay = _reverse("r8", "rev-8")
    assert replay.json()["replayed"] is True
    # 仍是首次冲正当场的结果金额快照（累计冲正 0），不随后续变化
    assert replay.json()["refunded_cents"] == 0
    assert replay.json()["refundable_cents"] == 500


def test_same_biz_id_for_another_refund_order_conflicts_no_change() -> None:
    _order("o9", 500); _pay("o9", 500)
    _settled_refund("r9a", "o9", 100)
    _settled_refund("r9b", "o9", 100)
    assert _reverse("r9a", "rev-shared").status_code == 200
    # 同一冲正业务标识用于另一张退款单：409 且无变更（第二张仍 succeeded、未扣账）
    assert _reverse("r9b", "rev-shared").status_code == 409
    assert client.get("/refund-orders/r9b", headers=H).json()["status"] == "succeeded"
    assert client.get("/refund-orders/r9b", headers=H).json()["reversal_biz_id"] is None
    # r9b 的到账冲正仍在账上（累计冲正：两张各 100，r9a 冲正扣 100，余 100）
    assert client.get("/orders/o9", headers=H).json()["refunded_cents"] == 100


# ---------- 业务标识命名空间独立，不得混用 ----------

def test_reverse_biz_id_conflicts_with_order_side_and_refund_ids() -> None:
    _order("o10", 1000); _pay("o10", 1000)
    # 预置订单侧各操作标识
    client.post("/orders/o10/refunds", json={"biz_id": "b-refund", "amount_cents": 10}, headers=H)
    client.post("/orders/o10/writeoffs", json={"biz_id": "b-writeoff", "amount_cents": 10}, headers=H)
    client.post("/orders/o10/corrections", json={"biz_id": "b-corr", "amount_cents": 10}, headers=H)
    client.post(
        "/orders/o10/correction-cancels",
        json={"biz_id": "b-cancel", "correction_biz_id": "b-corr"},
        headers=H,
    )
    _settled_refund("r10a", "o10", 10)
    for biz in ("b-refund", "b-writeoff", "b-corr", "b-cancel"):
        assert _reverse("r10a", biz).status_code == 409
    # 冲正业务标识不得与退款单自身标识混用
    assert _reverse("r10a", "r10a").status_code == 409
    # 另一张退款单标识也占用命名空间
    _accept("r10b", "o10", 10)
    assert _reverse("r10a", "r10b").status_code == 409
    # 全部拒绝，r10a 仍已到账、未冲正
    assert client.get("/refund-orders/r10a", headers=H).json()["status"] == "succeeded"
    assert client.get("/refund-orders/r10a", headers=H).json()["reversal_biz_id"] is None


def test_reverse_biz_id_blocks_namespace_for_other_operations() -> None:
    _order("o11", 1000); _pay("o11", 1000)
    _settled_refund("r11", "o11", 100)
    assert _reverse("r11", "rev-ns").status_code == 200
    # 冲正后可退余额恢复（900）；冲正业务标识不得被订单侧任一操作复用
    assert client.post(
        "/orders/o11/refunds", json={"biz_id": "rev-ns", "amount_cents": 1}, headers=H
    ).status_code == 409
    assert client.post(
        "/orders/o11/writeoffs", json={"biz_id": "rev-ns", "amount_cents": 1}, headers=H
    ).status_code == 409
    assert client.post(
        "/orders/o11/corrections", json={"biz_id": "rev-ns", "amount_cents": 1}, headers=H
    ).status_code == 409
    # 不得作为新退款单标识
    assert _accept("rev-ns", "o11", 10).status_code == 409


# ---------- 404：不存在 / 跨租户 / 原订单已不存在 ----------

def test_reverse_unknown_refund_is_404() -> None:
    assert _reverse("nope", "rev-nope").status_code == 404


def test_reverse_cross_tenant_is_404() -> None:
    _order("o12", 500); _pay("o12", 500)
    _settled_refund("r12", "o12", 100)
    assert _reverse("r12", "rev-12", tenant="other").status_code == 404
    # 未产生冲正
    ro = client.get("/refund-orders/r12", headers=H).json()
    assert ro["status"] == "succeeded" and ro["reversal_biz_id"] is None


def test_reverse_when_original_order_gone_is_404_and_whole_tx_rolls_back() -> None:
    _order("o13", 500); _pay("o13", 500)
    _settled_refund("r13", "o13", 200)
    # 模拟原订单已被删除（系统无删除接口，直接删行）
    conn = db_connect()
    conn.execute("DELETE FROM orders WHERE tenant=? AND order_id=?", ("rv", "o13"))
    conn.close()
    resp = _reverse("r13", "rev-13")
    assert resp.status_code == 404
    # 整单回滚：退款单仍已到账、冲正标识为空、无冲正记录、无冲正流水
    ro = client.get("/refund-orders/r13", headers=H).json()
    assert ro["status"] == "succeeded" and ro["reversal_biz_id"] is None
    conn = db_connect()
    rev_count = conn.execute(
        "SELECT COUNT(*) AS c FROM refund_reversals WHERE tenant=? AND biz_id=?", ("rv", "rev-13")
    ).fetchone()["c"]
    led = conn.execute(
        "SELECT COUNT(*) AS c FROM ledger_entries WHERE tenant=? AND order_id=? AND op_type='refund_reversal'",
        ("rv", "o13"),
    ).fetchone()["c"]
    conn.close()
    assert rev_count == 0 and led == 0


# ---------- 读取与检索 ----------

def test_get_refund_includes_reversal_biz_id_empty_when_not_reversed() -> None:
    _order("o14", 500); _pay("o14", 500)
    _settled_refund("r14", "o14", 100)
    assert client.get("/refund-orders/r14", headers=H).json()["reversal_biz_id"] is None
    _reverse("r14", "rev-14")
    data = client.get("/refund-orders/r14", headers=H).json()
    assert data["reversal_biz_id"] == "rev-14" and data["status"] == "reversed"


def test_search_filter_by_reversed_status_and_flag() -> None:
    t = "rvsearch"
    th = {"X-Tenant": t}
    _order("s1", 1000, tenant=t); _pay("s1", 1000, tenant=t)
    _settled_refund("rs1", "s1", 100, tenant=t)  # 已到账，未冲正
    _settled_refund("rs2", "s1", 100, tenant=t)  # 已到账后冲正
    client.post("/refund-orders/rs2/reverse", json={"biz_id": "rev-s2"}, headers=th)
    _accept("rs3", "s1", 100, tenant=t)  # accepted，未冲正

    by_status = client.post(
        "/refund-orders/search", json={"request_id": "rq-rs1", "filters": {"status": "reversed"}}, headers=th
    )
    assert [r["refund_id"] for r in by_status.json()["refund_orders"]] == ["rs2"]

    only_reversed = client.post(
        "/refund-orders/search", json={"request_id": "rq-rs2", "filters": {"reversed": True}}, headers=th
    )
    assert [r["refund_id"] for r in only_reversed.json()["refund_orders"]] == ["rs2"]

    not_reversed = client.post(
        "/refund-orders/search", json={"request_id": "rq-rs3", "filters": {"reversed": False}}, headers=th
    )
    assert [r["refund_id"] for r in not_reversed.json()["refund_orders"]] == ["rs1", "rs3"]

    # 非法布尔值：可区分 400，不写入
    bad = client.post(
        "/refund-orders/search", json={"request_id": "rq-rs4", "filters": {"reversed": "yes"}}, headers=th
    )
    assert bad.status_code == 400 and bad.json()["detail"] == "invalid_reversed"
    ok = client.post(
        "/refund-orders/search", json={"request_id": "rq-rs4", "filters": {"reversed": True}}, headers=th
    )
    assert ok.status_code == 200 and ok.json()["replayed"] is False  # 非法请求未占用去重标识


def test_search_replay_snapshot_unaffected_by_reversal() -> None:
    t = "rvsearch2"
    th = {"X-Tenant": t}
    _order("s2", 1000, tenant=t); _pay("s2", 1000, tenant=t)
    _settled_refund("rr1", "s2", 100, tenant=t)
    body = {"request_id": "rq-rr1", "filters": {"status": "succeeded"}}
    first = client.post("/refund-orders/search", json=body, headers=th)
    assert [r["refund_id"] for r in first.json()["refund_orders"]] == ["rr1"]
    client.post("/refund-orders/rr1/reverse", json={"biz_id": "rev-rr1"}, headers=th)
    replay = client.post("/refund-orders/search", json=body, headers=th)
    assert replay.headers["x-idempotent-replay"] == "1"
    # 重放返回首次同一结果集，不随冲正后状态变化
    assert [r["refund_id"] for r in replay.json()["refund_orders"]] == ["rr1"]


# ---------- 并发与持久化 ----------

def test_concurrent_reverse_same_biz_id_applies_once() -> None:
    import threading
    _order("o15", 500); _pay("o15", 500)
    _settled_refund("r15", "o15", 200)
    statuses: list[int] = []

    def run() -> None:
        statuses.append(_reverse("r15", "rev-15").status_code)

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(statuses) == [200, 200]  # 一次生效 + 一次重放
    assert client.get("/orders/o15", headers=H).json()["refunded_cents"] == 0
    entries = client.get("/orders/o15/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries].count("refund_reversal") == 1


def test_concurrent_reverse_different_biz_ids_only_one_wins() -> None:
    import threading
    _order("o16", 500); _pay("o16", 500)
    _settled_refund("r16", "o16", 200)
    results: list[tuple[str, int]] = []

    def run(biz: str) -> None:
        results.append((biz, _reverse("r16", biz).status_code))

    t1 = threading.Thread(target=run, args=("rev-16a",))
    t2 = threading.Thread(target=run, args=("rev-16b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    statuses = sorted(code for _, code in results)
    assert statuses == [200, 409]  # 冲正只能一次，另一标识整体拒绝
    assert client.get("/orders/o16", headers=H).json()["refunded_cents"] == 0
    entries = client.get("/orders/o16/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries].count("refund_reversal") == 1
    assert client.get("/refund-orders/r16", headers=H).json()["status"] == "reversed"


def test_reversed_state_and_order_ledger_survive_restart() -> None:
    _order("o17", 400); _pay("o17", 400)
    _settled_refund("r17", "o17", 120)
    _reverse("r17", "rev-17")
    # 模拟服务重启后：状态、冲正标识、原订单累计冲正与流水仍一致
    ro = client.get("/refund-orders/r17", headers=H).json()
    assert ro["status"] == "reversed" and ro["reversal_biz_id"] == "rev-17"
    order = client.get("/orders/o17", headers=H).json()
    assert order["refunded_cents"] == 0 and order["refundable_cents"] == 400
    entries = client.get("/orders/o17/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "refund", "refund_reversal"]
    # 同一请求重放只生效一次
    replay = _reverse("r17", "rev-17")
    assert replay.json()["replayed"] is True
    assert [e["op_type"] for e in entries].count("refund_reversal") == 1
