import os, tempfile
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

def test_refund_keeps_totals_consistent() -> None:
    body = {"tenant": "t1", "order_id": "o5", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o5/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    got = client.post("/orders/o5/refunds", json={"refund_id": "r1", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    assert got.status_code == 201
    order = got.json()["order"]
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 100
    assert order["refunded_cents"] == 50 and order["refundable_cents"] == 150
    assert got.json()["refund"] == {"refund_id": "r1", "order_id": "o5", "amount_cents": 50}

def test_refund_replay_applies_once() -> None:
    body = {"tenant": "t1", "order_id": "o6", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o6/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    first = client.post("/orders/o6/refunds", json={"refund_id": "r2", "amount_cents": 80}, headers={"X-Tenant": "t1"})
    replay = client.post("/orders/o6/refunds", json={"refund_id": "r2", "amount_cents": 80}, headers={"X-Tenant": "t1"})
    assert first.status_code == 201 and replay.status_code == 200
    assert replay.json()["refund"] == first.json()["refund"]
    order = client.get("/orders/o6", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 80 and order["refundable_cents"] == 120
    other = client.post("/orders/o6/refunds", json={"refund_id": "r3", "amount_cents": 80}, headers={"X-Tenant": "t1"})
    assert other.status_code == 201
    order = client.get("/orders/o6", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 160

def test_refund_cannot_exceed_refundable() -> None:
    body = {"tenant": "t1", "order_id": "o7", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o7/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/o7/refunds", json={"refund_id": "r4", "amount_cents": 101}, headers={"X-Tenant": "t1"}).status_code == 409
    order = client.get("/orders/o7", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 0 and order["refundable_cents"] == 100

def test_refund_unknown_or_cross_tenant_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o8", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o8/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/o8/refunds", json={"refund_id": "r5", "amount_cents": 10}, headers={"X-Tenant": "t2"}).status_code == 404
    assert client.post("/orders/nope/refunds", json={"refund_id": "r6", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 404

def test_zero_refundable_closes_order() -> None:
    body = {"tenant": "t1", "order_id": "o9", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o9/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    got = client.post("/orders/o9/refunds", json={"refund_id": "r7", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert got.status_code == 201 and got.json()["order"]["status"] == "closed"
    assert client.post("/orders/o9/refunds", json={"refund_id": "r8", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409

def test_ledger_records_all_ops_in_order() -> None:
    body = {"tenant": "t1", "order_id": "o10", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o10/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/o10/refunds", json={"refund_id": "r9", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    entries = client.get("/orders/o10/ledger", headers={"X-Tenant": "t1"}).json()
    assert [e["op"] for e in entries] == ["accept", "payment", "refund"]
    assert [e["seq"] for e in entries] == sorted(e["seq"] for e in entries)
    refund = entries[2]
    assert refund["biz_id"] == "r9" and refund["amount_cents"] == 50
    assert refund["paid_cents"] == 200 and refund["outstanding_cents"] == 100 and refund["refunded_cents"] == 50

def test_ledger_cross_tenant_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o11", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.get("/orders/o11/ledger", headers={"X-Tenant": "t2"}).status_code == 404
