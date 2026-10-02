import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "rv"}

def _order(oid: str, amount: int = 1000, tenant: str = "rv") -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"})

def _pay(oid: str, amount: int, tenant: str = "rv") -> None:
    client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})

def _accept(rid: str, oid: str, amount: int, tenant: str = "rv"):
    return client.post(
        "/refund-orders",
        json={"refund_id": rid, "order_id": oid, "requested_amount_cents": amount},
        headers={"X-Tenant": tenant},
    )

def _succeed(rid: str, oid: str, requested: int, actual: int | None = None, tenant: str = "rv"):
    """受理 → 审核通过 → 执行到账。"""
    _accept(rid, oid, requested, tenant)
    client.post(f"/refund-orders/{rid}/approve", headers={"X-Tenant": tenant})
    body = {"outcome": "succeeded"}
    if actual is not None:
        body["amount_cents"] = actual
    return client.post(f"/refund-orders/{rid}/execute", json=body, headers={"X-Tenant": tenant})

def _reverse(rid: str, biz_id: str, tenant: str = "rv"):
    return client.post(
        f"/refund-orders/{rid}/reversal", json={"biz_id": biz_id}, headers={"X-Tenant": tenant}
    )

# ---------- 执行冲正：生效与联动 ----------

def test_reverse_succeeded_refund_debits_order_and_appends_ledger() -> None:
    _order("v1", 500); _pay("v1", 500)
    _succeed("rv1", "v1", 200)
    assert client.get("/orders/v1", headers=H).json()["refunded_cents"] == 200
    resp = _reverse("rv1", "rev-0001")
    assert resp.status_code == 200 and resp.headers["x-idempotent-replay"] == "0"
    data = resp.json()
    assert data["replayed"] is False
    assert data["biz_id"] == "rev-0001" and data["refund_id"] == "rv1" and data["order_id"] == "v1"
    assert data["amount_cents"] == 200  # 冲正金额取实退金额
    # 当时订单结果金额随记录返回
    assert data["refunded_cents"] == 0 and data["paid_cents"] == 500
    assert data["outstanding_cents"] == 0 and data["status"] == "settled"
    # 退款单进入已冲正终态并落库冲正业务标识
    refund = client.get("/refund-orders/rv1", headers=H).json()
    assert refund["status"] == "reversed" and refund["reversal_biz_id"] == "rev-0001"
    # 原订单：累计冲正扣减，已收/累计冲销/订单金额不改
    order = client.get("/orders/v1", headers=H).json()
    assert order["refunded_cents"] == 0 and order["paid_cents"] == 500
    assert order["written_off_cents"] == 0 and order["amount_cents"] == 500
    assert order["refundable_cents"] == 500
    # 追加一条原订单冲正流水：业务标识记本次冲正的业务标识，金额记实退金额
    entries = client.get("/orders/v1/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries] == ["accept", "payment", "refund", "refund_reversal"]
    last = entries[-1]
    assert last["biz_id"] == "rev-0001" and last["amount_cents"] == 200
    assert last["refunded_result_cents"] == 0 and last["paid_result_cents"] == 500

def test_reverse_uses_actual_refunded_amount_not_requested() -> None:
    _order("v2", 500); _pay("v2", 500)
    _succeed("rv2", "v2", 200, actual=150)  # 实退 150 < 申请 200
    resp = _reverse("rv2", "rev-0002")
    assert resp.status_code == 200
    assert resp.json()["amount_cents"] == 150
    assert client.get("/orders/v2", headers=H).json()["refunded_cents"] == 0

def test_reverse_rejudges_order_out_of_completed() -> None:
    _order("v3", 100); _pay("v3", 100)
    _succeed("rv3", "v3", 100)
    assert client.get("/orders/v3", headers=H).json()["status"] == "completed"
    resp = _reverse("rv3", "rev-0003")
    assert resp.status_code == 200
    # 按既有口径重判：已收=订单金额且不再终态 → settled
    assert resp.json()["status"] == "settled"
    assert client.get("/orders/v3", headers=H).json()["status"] == "settled"

# ---------- 重放 ----------

def test_reverse_replay_returns_same_record_without_double_effect() -> None:
    _order("v4", 400); _pay("v4", 400)
    _succeed("rv4", "v4", 120)
    first = _reverse("rv4", "rev-0004")
    assert first.status_code == 200
    second = _reverse("rv4", "rev-0004")
    assert second.status_code == 200 and second.headers["x-idempotent-replay"] == "1"
    assert second.json()["replayed"] is True
    # 同一记录（含当时订单结果金额与落库时间）
    assert second.json() == first.json() | {"replayed": True}
    # 不重复扣减、不新增流水
    assert client.get("/orders/v4", headers=H).json()["refunded_cents"] == 0
    entries = client.get("/orders/v4/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries].count("refund_reversal") == 1

def test_reverse_same_refund_with_different_biz_id_rejected() -> None:
    _order("v5", 300); _pay("v5", 300)
    _succeed("rv5", "v5", 100)
    assert _reverse("rv5", "rev-0005").status_code == 200
    before = client.get("/orders/v5", headers=H).json()
    # 同一退款单以不同业务标识再次冲正：拒绝且无任何变更
    resp = _reverse("rv5", "rev-0005b")
    assert resp.status_code == 409
    assert client.get("/orders/v5", headers=H).json() == before
    entries = client.get("/orders/v5/ledger", headers=H).json()["entries"]
    assert [e["op_type"] for e in entries].count("refund_reversal") == 1
    # 冲正后再次执行到账：拒绝
    assert client.post(
        "/refund-orders/rv5/execute", json={"outcome": "succeeded"}, headers=H
    ).status_code == 409

# ---------- 状态约束 ----------

def test_reverse_rejected_for_non_succeeded_states_no_change() -> None:
    _order("v6", 300); _pay("v6", 300)
    # accepted
    _accept("rv6a", "v6", 10)
    assert _reverse("rv6a", "rev-006a").status_code == 409
    # approved
    _accept("rv6b", "v6", 10)
    client.post("/refund-orders/rv6b/approve", headers=H)
    assert _reverse("rv6b", "rev-006b").status_code == 409
    # rejected
    _accept("rv6c", "v6", 10)
    client.post("/refund-orders/rv6c/reject", headers=H)
    assert _reverse("rv6c", "rev-006c").status_code == 409
    # failed
    _accept("rv6d", "v6", 10)
    client.post("/refund-orders/rv6d/approve", headers=H)
    client.post("/refund-orders/rv6d/execute", json={"outcome": "failed", "reason": "x"}, headers=H)
    assert _reverse("rv6d", "rev-006d").status_code == 409
    # cancelled
    _accept("rv6e", "v6", 10)
    client.post("/refund-orders/rv6e/cancel", headers=H)
    assert _reverse("rv6e", "rev-006e").status_code == 409
    # 全部无任何变更：无冲正记录、无新流水、订单累计冲正不变
    assert client.get("/orders/v6", headers=H).json()["refunded_cents"] == 0
    entries = client.get("/orders/v6/ledger", headers=H).json()["entries"]
    assert "refund_reversal" not in [e["op_type"] for e in entries]
    for rid in ("rv6a", "rv6b", "rv6c", "rv6d", "rv6e"):
        assert client.get(f"/refund-orders/{rid}", headers=H).json()["reversal_biz_id"] is None

def test_reversed_is_terminal_for_all_lifecycle_ops() -> None:
    _order("v7", 300); _pay("v7", 300)
    _succeed("rv7", "v7", 100)
    assert _reverse("rv7", "rev-0007").status_code == 200
    assert client.post("/refund-orders/rv7/approve", headers=H).status_code == 409
    assert client.post("/refund-orders/rv7/reject", headers=H).status_code == 409
    assert client.post(
        "/refund-orders/rv7/execute", json={"outcome": "succeeded"}, headers=H
    ).status_code == 409
    assert client.post(
        "/refund-orders/rv7/execute", json={"outcome": "failed", "reason": "x"}, headers=H
    ).status_code == 409
    assert client.post("/refund-orders/rv7/cancel", headers=H).status_code == 409
    refund = client.get("/refund-orders/rv7", headers=H).json()
    assert refund["status"] == "reversed" and refund["reversal_biz_id"] == "rev-0007"

# ---------- 业务标识命名空间冲突 ----------

def test_reverse_biz_id_conflicts_with_other_operations() -> None:
    _order("v8", 500); _pay("v8", 500)
    _succeed("rv8", "v8", 100)
    # 订单侧冲正/作废/冲销/修正/取消修正的业务标识不得复用
    client.post("/orders/v8/refunds", json={"biz_id": "v8-ref", "amount_cents": 10}, headers=H)
    client.post("/orders/v8/writeoffs", json={"biz_id": "v8-wo", "amount_cents": 10}, headers=H)
    client.post("/orders/v8/corrections", json={"biz_id": "v8-corr", "amount_cents": 10}, headers=H)
    client.post(
        "/orders/v8/correction-cancels",
        json={"biz_id": "v8-cc", "correction_biz_id": "v8-corr"}, headers=H,
    )
    for used in ("v8-ref", "v8-wo", "v8-corr", "v8-cc"):
        assert _reverse("rv8", used).status_code == 409
    # 退款单标识（含自身与其他退款单）不得复用为冲正业务标识
    assert _reverse("rv8", "rv8").status_code == 409
    _accept("rv8b", "v8", 10)
    assert _reverse("rv8", "rv8b").status_code == 409
    # 均未生效
    assert client.get("/refund-orders/rv8", headers=H).json()["status"] == "succeeded"
    # 反向：冲正业务标识占用后，订单侧操作与退款单受理不得复用
    assert _reverse("rv8", "rev-0008").status_code == 200
    assert client.post(
        "/orders/v8/refunds", json={"biz_id": "rev-0008", "amount_cents": 1}, headers=H
    ).status_code == 409
    assert _accept("rev-0008", "v8", 10).status_code == 409
    # 同一冲正业务标识用于另一退款单：拒绝
    _order("v8x", 100, tenant="rv"); _pay("v8x", 100)
    _succeed("rv8x", "v8x", 50)
    assert _reverse("rv8x", "rev-0008").status_code == 409
    assert client.get("/refund-orders/rv8x", headers=H).json()["status"] == "succeeded"

# ---------- 不存在与租户隔离 ----------

def test_reverse_unknown_or_cross_tenant_is_404_and_writes_nothing() -> None:
    assert _reverse("nope", "rev-404a").status_code == 404
    _order("v9", 100, tenant="rv"); _pay("v9", 100)
    _succeed("rv9", "v9", 50)
    assert _reverse("rv9", "rev-404b", tenant="other").status_code == 404
    # 未产生任何变更
    refund = client.get("/refund-orders/rv9", headers=H).json()
    assert refund["status"] == "succeeded" and refund["reversal_biz_id"] is None
    assert client.get("/orders/v9", headers=H).json()["refunded_cents"] == 50
    # 404 不占用业务标识：同租户可正常使用该标识
    assert _reverse("rv9", "rev-404b").status_code == 200

def test_reverse_missing_tenant_header_is_400() -> None:
    assert client.post("/refund-orders/rv9/reversal", json={"biz_id": "x"}).status_code == 400

def test_reverse_invalid_params_are_422() -> None:
    assert client.post("/refund-orders/rv9/reversal", json={"biz_id": ""}, headers=H).status_code == 422
    assert client.post("/refund-orders/rv9/reversal", json={}, headers=H).status_code == 422

# ---------- 读取与检索 ----------

def test_read_exposes_reversal_biz_id() -> None:
    _order("va", 200); _pay("va", 200)
    _succeed("rva", "va", 80)
    before = client.get("/refund-orders/rva", headers=H).json()
    assert before["reversal_biz_id"] is None and before["status"] == "succeeded"
    _reverse("rva", "rev-000a")
    after = client.get("/refund-orders/rva", headers=H).json()
    assert after["status"] == "reversed" and after["reversal_biz_id"] == "rev-000a"
    assert after["refunded_amount_cents"] == 80  # 实退金额保留

def test_search_filters_by_reversal_state() -> None:
    _order("vb", 300); _pay("vb", 300)
    _succeed("rvb1", "vb", 50)
    _succeed("rvb2", "vb", 60)
    _accept("rvb3", "vb", 70)
    _reverse("rvb1", "rev-000b")
    # 按冲正状态过滤
    page = client.post(
        "/refund-orders/search",
        json={"request_id": "rvq-1", "filters": {"order_id": "vb", "has_reversal": True}},
        headers=H,
    ).json()
    assert [r["refund_id"] for r in page["refund_orders"]] == ["rvb1"]
    assert page["refund_orders"][0]["status"] == "reversed"
    page = client.post(
        "/refund-orders/search",
        json={"request_id": "rvq-2", "filters": {"order_id": "vb", "has_reversal": False}},
        headers=H,
    ).json()
    assert [r["refund_id"] for r in page["refund_orders"]] == ["rvb2", "rvb3"]
    # 状态过滤支持 reversed
    page = client.post(
        "/refund-orders/search",
        json={"request_id": "rvq-3", "filters": {"status": "reversed"}},
        headers=H,
    ).json()
    assert "rvb1" in [r["refund_id"] for r in page["refund_orders"]]
    # 非法 has_reversal：400 且不写入
    resp = client.post(
        "/refund-orders/search",
        json={"request_id": "rvq-4", "filters": {"has_reversal": "yes"}},
        headers=H,
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "invalid_has_reversal"
    # 重放语义不变：同一去重标识返回首次同一结果集
    first = client.post(
        "/refund-orders/search",
        json={"request_id": "rvq-5", "filters": {"has_reversal": True}},
        headers=H,
    )
    replay = client.post(
        "/refund-orders/search",
        json={"request_id": "rvq-5", "filters": {"has_reversal": True}},
        headers=H,
    )
    assert replay.headers["x-idempotent-replay"] == "1"
    assert replay.json()["refund_orders"] == first.json()["refund_orders"]
    # 跨租户检索不泄漏
    other = client.post(
        "/refund-orders/search",
        json={"request_id": "rvq-6", "filters": {"has_reversal": True}},
        headers={"X-Tenant": "other"},
    ).json()
    assert other["refund_orders"] == []
