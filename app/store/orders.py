import sqlite3

from app.store.db import connect

# 终态：收付完结（completed）或已作废（voided），此后一切收付变更一律拒绝
TERMINAL_STATUSES = ("completed", "voided")

ORDER_COLUMNS = (
    "tenant, order_id, amount_cents, paid_cents, refunded_cents, "
    "written_off_cents, currency, status"
)


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
        "written_off_cents": row["written_off_cents"],
        "refundable_cents": paid - refunded,
        "currency": row["currency"],
        "status": row["status"],
    }

def _is_paid_out(paid: int, refunded: int, written_off: int) -> bool:
    # 终态不变式：已收大于零且可退余额减累计冲销归零（不越界为负时恰为零）
    return paid > 0 and paid - refunded - written_off <= 0

def _assert_not_terminal(row: sqlite3.Row) -> None:
    if row["status"] == "voided":
        raise ValueError("order is voided")
    if row["status"] == "completed":
        raise ValueError("order is completed")

def _append_ledger(
    conn: sqlite3.Connection, tenant: str, order_id: str, op_type: str,
    biz_id: str | None, amount_cents: int,
    paid: int, outstanding: int, refunded: int, written_off: int,
) -> None:
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM ledger_entries WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()["next_seq"]
    conn.execute(
        "INSERT INTO ledger_entries(tenant, order_id, seq, op_type, biz_id, amount_cents,"
        " paid_result_cents, outstanding_result_cents, refunded_result_cents, written_off_result_cents)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (tenant, order_id, seq, op_type, biz_id, amount_cents, paid, outstanding, refunded, written_off),
    )

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, refunded_cents, written_off_cents, currency, status) VALUES(?,?,?,0,0,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
        _append_ledger(conn, tenant, order_id, "accept", None, amount_cents, 0, amount_cents, 0, 0)
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
            f"SELECT {ORDER_COLUMNS} FROM orders WHERE tenant=? AND order_id=?",
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
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        _assert_not_terminal(row)
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            raise ValueError("payment exceeds outstanding amount")
        new_paid = row["paid_cents"] + amount_cents
        new_status = "settled" if new_paid >= row["amount_cents"] else "accepted"
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
        )
        _append_ledger(
            conn, tenant, order_id, "payment", None, amount_cents,
            new_paid, row["amount_cents"] - new_paid,
            row["refunded_cents"], row["written_off_cents"],
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
            return _build_op_result(record, get(tenant, order_id), replayed), True

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        _assert_not_terminal(row)
        if amount_cents <= 0:
            raise ValueError("refund amount must be positive")
        refundable = row["paid_cents"] - row["refunded_cents"]
        if amount_cents > refundable:
            raise ValueError("refund exceeds refundable balance")

        new_refunded = row["refunded_cents"] + amount_cents
        # 可退余额减累计冲销归零：收付进入终态；其余情况保留原状态（accepted/settled）
        if _is_paid_out(row["paid_cents"], new_refunded, row["written_off_cents"]):
            new_status = "completed"
        else:
            new_status = row["status"]
        conn.execute(
            "UPDATE orders SET refunded_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_refunded, new_status, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO refunds(tenant, biz_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, biz_id, order_id, amount_cents),
        )
        _append_ledger(
            conn, tenant, order_id, "refund", biz_id, amount_cents,
            row["paid_cents"], row["amount_cents"] - row["paid_cents"],
            new_refunded, row["written_off_cents"],
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
    return _build_op_result(record, get(tenant, order_id), replayed), False

def void_order(tenant: str, order_id: str, biz_id: str) -> tuple[dict, bool]:
    """作废订单。返回 (作废结果, 是否重放)；订单不存在或跨租户抛 LookupError。

    仅未发生收款的订单可作废；生效后进入 voided 终态，不改订单金额与既有流水，
    仅追加一条作废流水。重放（同租户同业务标识同订单）返回首次的同一记录，
    不重复写流水、不改状态；该标识用于另一订单的作废或用于冲销一律拒绝。
    """
    conn = connect()
    record = None
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, biz_id, order_id, created_at FROM voids WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if existing is not None:
            # 重放：业务标识已存在，只返回同一记录
            if existing["order_id"] != order_id:
                raise ValueError("biz_id already used for another order")
            record = dict(existing)
            replayed = True
            conn.execute("COMMIT")
            return _build_op_result(record, get(tenant, order_id), replayed), True
        clash = conn.execute(
            "SELECT 1 FROM writeoffs WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if clash is not None:
            # 作废与冲销的业务标识不得混用
            raise ValueError("biz_id already used for a writeoff")

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        _assert_not_terminal(row)
        if row["paid_cents"] > 0:
            raise ValueError("order has payments and cannot be voided")

        conn.execute(
            "UPDATE orders SET status='voided' WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        conn.execute(
            "INSERT INTO voids(tenant, biz_id, order_id) VALUES(?,?,?)",
            (tenant, biz_id, order_id),
        )
        _append_ledger(
            conn, tenant, order_id, "void", biz_id, 0,
            row["paid_cents"], row["amount_cents"] - row["paid_cents"],
            row["refunded_cents"], row["written_off_cents"],
        )
        conn.execute("COMMIT")
        saved = conn.execute(
            "SELECT tenant, biz_id, order_id, created_at FROM voids WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        record = dict(saved)
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return _build_op_result(record, get(tenant, order_id), replayed), False

def add_writeoff(
    tenant: str, order_id: str, biz_id: str, amount_cents: int
) -> tuple[dict, bool]:
    """登记收款冲销。返回 (冲销结果, 是否重放)；订单不存在或跨租户抛 LookupError。

    冲销不改已收、未收、累计冲正与可退余额，累计冲销单调增加；金额不得超过
    已收减累计冲正与累计冲销之差。重放返回首次的同一记录，不重复累计、不新增流水。
    """
    conn = connect()
    record = None
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, biz_id, order_id, amount_cents, created_at FROM writeoffs WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if existing is not None:
            # 重放：业务标识已存在，只返回同一记录
            if existing["order_id"] != order_id:
                raise ValueError("biz_id already used for another order")
            record = dict(existing)
            replayed = True
            conn.execute("COMMIT")
            return _build_op_result(record, get(tenant, order_id), replayed), True
        clash = conn.execute(
            "SELECT 1 FROM voids WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if clash is not None:
            # 冲销与作废的业务标识不得混用
            raise ValueError("biz_id already used for a void")

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        _assert_not_terminal(row)
        if amount_cents <= 0:
            raise ValueError("writeoff amount must be positive")
        writable = row["paid_cents"] - row["refunded_cents"] - row["written_off_cents"]
        if amount_cents > writable:
            raise ValueError("writeoff exceeds writable balance")

        new_written_off = row["written_off_cents"] + amount_cents
        # 可退余额减累计冲销归零：收付进入终态；其余情况保留原状态（accepted/settled）
        if _is_paid_out(row["paid_cents"], row["refunded_cents"], new_written_off):
            new_status = "completed"
        else:
            new_status = row["status"]
        conn.execute(
            "UPDATE orders SET written_off_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_written_off, new_status, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO writeoffs(tenant, biz_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, biz_id, order_id, amount_cents),
        )
        _append_ledger(
            conn, tenant, order_id, "writeoff", biz_id, amount_cents,
            row["paid_cents"], row["amount_cents"] - row["paid_cents"],
            row["refunded_cents"], new_written_off,
        )
        conn.execute("COMMIT")
        saved = conn.execute(
            "SELECT tenant, biz_id, order_id, amount_cents, created_at FROM writeoffs WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        record = dict(saved)
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return _build_op_result(record, get(tenant, order_id), replayed), False

def _build_op_result(record: dict, order: dict | None, replayed: bool) -> dict:
    result = dict(record)
    result["replayed"] = replayed
    if order is not None:
        result.update(
            paid_cents=order["paid_cents"],
            outstanding_cents=order["outstanding_cents"],
            refunded_cents=order["refunded_cents"],
            written_off_cents=order["written_off_cents"],
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
            "SELECT seq, op_type, biz_id, amount_cents, paid_result_cents, outstanding_result_cents, refunded_result_cents, written_off_result_cents, created_at FROM ledger_entries WHERE tenant=? AND order_id=? ORDER BY seq ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
