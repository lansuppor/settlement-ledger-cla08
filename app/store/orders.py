import sqlite3

from app.store.db import connect


def _rollback_safe(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # 事务已结束（如前置 ROLLBACK 后又进入 except）

def _shape(row: sqlite3.Row) -> dict:
    paid = row["paid_cents"]
    refunded = row["refunded_cents"]
    return {
        "tenant": row["tenant"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "paid_cents": paid,
        "outstanding_cents": row["amount_cents"] - paid,
        "refunded_cents": refunded,
        "refundable_cents": paid - refunded,
        "currency": row["currency"],
        "status": row["status"],
    }

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, refunded_cents, currency, status) VALUES(?,?,?,0,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
        conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, seq, op_type, biz_id, amount_cents, paid_result_cents, outstanding_result_cents, refunded_result_cents) VALUES(?,?,1,'accept',NULL,?,0,?,0)",
            (tenant, order_id, amount_cents, amount_cents),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, refunded_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    return _shape(row) if row is not None else None

def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            raise ValueError("payment exceeds outstanding amount")
        new_paid = row["paid_cents"] + amount_cents
        new_status = "settled" if new_paid >= row["amount_cents"] else "accepted"
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
        )
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM ledger_entries WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()["next_seq"]
        conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, seq, op_type, biz_id, amount_cents, paid_result_cents, outstanding_result_cents, refunded_result_cents) VALUES(?,?,?,'payment',NULL,?,?,?,?)",
            (
                tenant,
                order_id,
                seq,
                amount_cents,
                new_paid,
                row["amount_cents"] - new_paid,
                row["refunded_cents"],
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return get(tenant, order_id)

def add_refund(
    tenant: str, order_id: str, biz_id: str, amount_cents: int
) -> tuple[dict, bool] | None:
    """登记冲正。返回 (冲正结果, 是否重放)；订单不存在或跨租户返回 None。

    重放（同租户同业务标识）返回既有冲正记录，不重复扣减、不新增流水。
    金额非法或超过可退余额抛 ValueError，整笔回滚，不产生任何变更。
    """
    conn = connect()
    record = None
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, biz_id, order_id, amount_cents, created_at FROM refunds WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if existing is not None:
            # 重放：业务标识已存在，只返回同一记录
            if existing["order_id"] != order_id:
                raise ValueError("biz_id already used for another order")
            record = dict(existing)
            replayed = True
            conn.execute("COMMIT")
            return _build_refund_result(record, get(tenant, order_id), replayed), True

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        if amount_cents <= 0:
            raise ValueError("refund amount must be positive")
        refundable = row["paid_cents"] - row["refunded_cents"]
        if amount_cents > refundable:
            raise ValueError("refund exceeds refundable balance")

        new_refunded = row["refunded_cents"] + amount_cents
        # 可退余额归零：收付进入终态；其余情况保留原状态（accepted/settled）
        new_status = "completed" if row["paid_cents"] - new_refunded == 0 else row["status"]
        conn.execute(
            "UPDATE orders SET refunded_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_refunded, new_status, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO refunds(tenant, biz_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, biz_id, order_id, amount_cents),
        )
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM ledger_entries WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()["next_seq"]
        conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, seq, op_type, biz_id, amount_cents, paid_result_cents, outstanding_result_cents, refunded_result_cents) VALUES(?,?,?,'refund',?,?,?,?,?)",
            (
                tenant,
                order_id,
                seq,
                biz_id,
                amount_cents,
                row["paid_cents"],
                row["amount_cents"] - row["paid_cents"],
                new_refunded,
            ),
        )
        conn.execute("COMMIT")
        saved = conn.execute(
            "SELECT tenant, biz_id, order_id, amount_cents, created_at FROM refunds WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        record = dict(saved)
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return _build_refund_result(record, get(tenant, order_id), replayed), False

def _build_refund_result(record: dict, order: dict | None, replayed: bool) -> dict:
    result = dict(record)
    result["replayed"] = replayed
    if order is not None:
        result.update(
            paid_cents=order["paid_cents"],
            outstanding_cents=order["outstanding_cents"],
            refunded_cents=order["refunded_cents"],
            refundable_cents=order["refundable_cents"],
            status=order["status"],
        )
    return result

def list_ledger(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?", (tenant, order_id)
        ).fetchone()
        if exists is None:
            return None
        rows = conn.execute(
            "SELECT seq, op_type, biz_id, amount_cents, paid_result_cents, outstanding_result_cents, refunded_result_cents, created_at FROM ledger_entries WHERE tenant=? AND order_id=? ORDER BY seq ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
