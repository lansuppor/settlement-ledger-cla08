import sqlite3
from app.store.db import connect

def _write_ledger(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    op: str,
    biz_id: str | None,
    amount_cents: int,
    paid_cents: int,
    outstanding_cents: int,
    refunded_cents: int,
) -> None:
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM ledger WHERE tenant=?",
        (tenant,),
    ).fetchone()["next"]
    conn.execute(
        "INSERT INTO ledger(tenant, seq, order_id, op, biz_id, amount_cents, paid_cents, outstanding_cents, refunded_cents)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (tenant, seq, order_id, op, biz_id, amount_cents, paid_cents, outstanding_cents, refunded_cents),
    )

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, refunded_cents, currency, status)"
                " VALUES(?,?,?,0,0,?,'accepted')",
                (tenant, order_id, amount_cents, currency),
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        _write_ledger(conn, tenant, order_id, "accept", None, amount_cents, 0, amount_cents, 0)
        conn.execute("COMMIT")
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, refunded_cents, currency, status"
            " FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    outstanding = row["amount_cents"] - row["paid_cents"]
    refundable = row["paid_cents"] - row["refunded_cents"]
    return {**dict(row), "outstanding_cents": outstanding, "refundable_cents": refundable}

def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?,"
            " status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END"
            " WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        paid = row["paid_cents"] + amount_cents
        _write_ledger(
            conn, tenant, order_id, "payment", None, amount_cents,
            paid, row["amount_cents"] - paid, row["refunded_cents"],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)

def add_refund(tenant: str, order_id: str, refund_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT order_id, amount_cents FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return {
                "refund": {"refund_id": refund_id, "order_id": existing["order_id"], "amount_cents": existing["amount_cents"]},
                "order": get(tenant, existing["order_id"]),
                "replayed": True,
            }
        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        refundable = row["paid_cents"] - row["refunded_cents"]
        if amount_cents <= 0 or amount_cents > refundable:
            conn.execute("ROLLBACK")
            raise ValueError("refund exceeds refundable balance")
        conn.execute(
            "UPDATE orders SET refunded_cents = refunded_cents + ?,"
            " status = CASE WHEN paid_cents - (refunded_cents + ?) <= 0 THEN 'closed' ELSE status END"
            " WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, refund_id, order_id, amount_cents),
        )
        refunded = row["refunded_cents"] + amount_cents
        _write_ledger(
            conn, tenant, order_id, "refund", refund_id, amount_cents,
            row["paid_cents"], row["amount_cents"] - row["paid_cents"], refunded,
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {
        "refund": {"refund_id": refund_id, "order_id": order_id, "amount_cents": amount_cents},
        "order": get(tenant, order_id),
        "replayed": False,
    }

def ledger(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return None
        rows = conn.execute(
            "SELECT seq, op, biz_id, amount_cents, paid_cents, outstanding_cents, refunded_cents"
            " FROM ledger WHERE tenant=? AND order_id=? ORDER BY seq ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
