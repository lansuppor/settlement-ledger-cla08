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
