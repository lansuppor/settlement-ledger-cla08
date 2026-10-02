import sqlite3

from app.store.db import connect

# 业务标识命名空间：冲正、作废、冲销、收款冲正修正、取消冲正修正的业务标识
# 在租户内共用唯一性，不得混用
BIZ_ID_TABLES = (
    "refunds",
    "voids",
    "writeoffs",
    "payment_corrections",
    "payment_correction_cancels",
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

def _assert_biz_id_free(conn: sqlite3.Connection, tenant: str, biz_id: str, own_table: str) -> None:
    """业务标识不得跨操作类型混用：已被其他操作类型占用即拒绝。"""
    for table in BIZ_ID_TABLES:
        if table == own_table:
            continue
        used = conn.execute(
            f"SELECT 1 FROM {table} WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if used is not None:
            raise ValueError("biz_id already used by another operation")

def _assert_not_terminal(row: sqlite3.Row) -> None:
    """终态（completed/voided）不可逆：此后收款、冲正、冲销、作废一律拒绝。"""
    if row["status"] == "completed":
        raise ValueError("order is completed")
    if row["status"] == "voided":
        raise ValueError("order is voided")

def _is_terminal(paid: int, refunded: int, written_off: int) -> bool:
    """收付终态当且仅当已收大于零且可退余额减累计冲销归零。"""
    return paid > 0 and paid - refunded - written_off == 0

def _status_after_paid_change(amount: int, paid: int, refunded: int, written_off: int) -> str:
    """已收发生增减后按既有口径重判状态：终态优先，其次结清，其余为已受理。"""
    if _is_terminal(paid, refunded, written_off):
        return "completed"
    if paid >= amount:
        return "settled"
    return "accepted"

def _next_seq(conn: sqlite3.Connection, tenant: str, order_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM ledger_entries WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()["next_seq"]

def _insert_ledger(
    conn: sqlite3.Connection, tenant: str, order_id: str, op_type: str,
    biz_id: str | None, amount_cents: int,
    paid: int, outstanding: int, refunded: int, written_off: int,
) -> None:
    conn.execute(
        "INSERT INTO ledger_entries(tenant, order_id, seq, op_type, biz_id, amount_cents, paid_result_cents, outstanding_result_cents, refunded_result_cents, written_off_result_cents) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (tenant, order_id, _next_seq(conn, tenant, order_id), op_type, biz_id, amount_cents, paid, outstanding, refunded, written_off),
    )

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, refunded_cents, written_off_cents, currency, status) VALUES(?,?,?,0,0,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
        _insert_ledger(conn, tenant, order_id, "accept", None, amount_cents, 0, amount_cents, 0, 0)
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
            "SELECT tenant, order_id, amount_cents, paid_cents, refunded_cents, written_off_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
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
        _insert_ledger(
            conn, tenant, order_id, "payment", None, amount_cents,
            new_paid, row["amount_cents"] - new_paid, row["refunded_cents"], row["written_off_cents"],
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
        _assert_biz_id_free(conn, tenant, biz_id, "refunds")

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
        # 可退余额减累计冲销归零且已收大于零：收付进入终态；其余情况保留原状态
        new_status = (
            "completed"
            if _is_terminal(row["paid_cents"], new_refunded, row["written_off_cents"])
            else row["status"]
        )
        conn.execute(
            "UPDATE orders SET refunded_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_refunded, new_status, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO refunds(tenant, biz_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, biz_id, order_id, amount_cents),
        )
        _insert_ledger(
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

    仅未发生收款的订单可作废；作废后进入 voided 终态，此后收款、冲正、
    冲销、再作废一律拒绝。重放（同租户同业务标识同订单）返回首次的同一
    记录，不重复写流水、不改状态。
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
        _assert_biz_id_free(conn, tenant, biz_id, "voids")

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        if row["status"] == "voided":
            raise ValueError("order already voided")
        if row["status"] == "completed":
            raise ValueError("order is completed")
        if row["paid_cents"] > 0:
            raise ValueError("order has payments and cannot be voided")

        # 不改订单金额与既有流水，仅追加一条作废流水
        conn.execute(
            "UPDATE orders SET status='voided' WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        conn.execute(
            "INSERT INTO voids(tenant, biz_id, order_id) VALUES(?,?,?)",
            (tenant, biz_id, order_id),
        )
        _insert_ledger(
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

    冲销不改已收与累计冲正，累计冲销单调增加；金额不得超过
    已收 - 累计冲正 - 累计冲销。重放返回首次的同一记录，不重复累计、不新增流水。
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
        _assert_biz_id_free(conn, tenant, biz_id, "writeoffs")

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        _assert_not_terminal(row)
        if amount_cents <= 0:
            raise ValueError("writeoff amount must be positive")
        writeoffable = row["paid_cents"] - row["refunded_cents"] - row["written_off_cents"]
        if amount_cents > writeoffable:
            raise ValueError("writeoff exceeds writeoffable balance")

        new_written_off = row["written_off_cents"] + amount_cents
        # 可退余额减累计冲销归零且已收大于零：收付进入终态；其余情况保留原状态
        new_status = (
            "completed"
            if _is_terminal(row["paid_cents"], row["refunded_cents"], new_written_off)
            else row["status"]
        )
        conn.execute(
            "UPDATE orders SET written_off_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_written_off, new_status, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO writeoffs(tenant, biz_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, biz_id, order_id, amount_cents),
        )
        _insert_ledger(
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

def add_payment_correction(
    tenant: str, order_id: str, biz_id: str, amount_cents: int
) -> tuple[dict, bool]:
    """登记收款冲正修正。返回 (修正结果, 是否重放)；订单不存在或跨租户抛 LookupError。

    生效后已收减少、未收增加；订单金额与既有流水不改，追加一条修正流水。
    金额必须为正整数且不超过已收（已收 − 累计冲正 − 累计冲销）；
    作废或收付完结终态订单一律拒绝。重放返回首次的同一记录，不重复扣减、不新增流水。
    """
    conn = connect()
    record = None
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, biz_id, order_id, amount_cents, cancelled_at, created_at FROM payment_corrections WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if existing is not None:
            # 重放：业务标识已存在，只返回同一记录（无论该修正此后是否被取消）
            if existing["order_id"] != order_id:
                raise ValueError("biz_id already used for another order")
            record = dict(existing)
            replayed = True
            conn.execute("COMMIT")
            return _build_correction_result(record, get(tenant, order_id), replayed, None), True
        _assert_biz_id_free(conn, tenant, biz_id, "payment_corrections")

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        _assert_not_terminal(row)
        if amount_cents <= 0:
            raise ValueError("correction amount must be positive")
        # 修正不得使累计冲正（及累计冲销）越过上限：金额不超过 已收 − 累计冲正 − 累计冲销
        correctable = row["paid_cents"] - row["refunded_cents"] - row["written_off_cents"]
        if amount_cents > correctable:
            raise ValueError("correction exceeds paid balance")

        new_paid = row["paid_cents"] - amount_cents
        # 已收减少后按既有口径重判状态（终态/结清/已受理）
        new_status = _status_after_paid_change(
            row["amount_cents"], new_paid, row["refunded_cents"], row["written_off_cents"]
        )
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO payment_corrections(tenant, biz_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, biz_id, order_id, amount_cents),
        )
        _insert_ledger(
            conn, tenant, order_id, "correction", biz_id, amount_cents,
            new_paid, row["amount_cents"] - new_paid,
            row["refunded_cents"], row["written_off_cents"],
        )
        conn.execute("COMMIT")
        saved = conn.execute(
            "SELECT tenant, biz_id, order_id, amount_cents, cancelled_at, created_at FROM payment_corrections WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        record = dict(saved)
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return _build_correction_result(record, get(tenant, order_id), replayed, None), False

def cancel_payment_correction(
    tenant: str, order_id: str, biz_id: str, correction_biz_id: str
) -> tuple[dict, bool]:
    """取消收款冲正修正。返回 (取消结果, 是否重放)；订单不存在或跨租户抛 LookupError。

    取消金额取被取消修正的原始金额，不可指定；仅已生效且未被取消的修正可取消。
    生效后已收恢复、未收减少；订单金额与既有流水不改，追加一条取消流水。
    被取消修正不存在、已被取消、订单不匹配或恢复后已收会超过订单金额时拒绝。
    重放（同租户同取消业务标识）返回首次的同一记录，不重复恢复、不新增流水。
    """
    conn = connect()
    record = None
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, biz_id, order_id, correction_biz_id, amount_cents, created_at FROM payment_correction_cancels WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if existing is not None:
            # 重放：取消业务标识已存在，只返回同一记录
            if existing["order_id"] != order_id:
                raise ValueError("biz_id already used for another order")
            record = dict(existing)
            replayed = True
            conn.execute("COMMIT")
            return _build_correction_result(record, get(tenant, order_id), replayed, correction_biz_id), True
        _assert_biz_id_free(conn, tenant, biz_id, "payment_correction_cancels")

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        _assert_not_terminal(row)

        target = conn.execute(
            "SELECT order_id, amount_cents, cancelled_at FROM payment_corrections WHERE tenant=? AND biz_id=?",
            (tenant, correction_biz_id),
        ).fetchone()
        if target is None:
            raise ValueError("correction not found")
        if target["order_id"] != order_id:
            raise ValueError("correction order mismatch")
        if target["cancelled_at"] is not None:
            raise ValueError("correction already cancelled")
        cancel_amount = target["amount_cents"]

        # 恢复后已收不得超过订单金额（正常账目下恢复仅抵消原扣减，此处仍显式守界）
        new_paid = row["paid_cents"] + cancel_amount
        if new_paid > row["amount_cents"]:
            raise ValueError("recovery would exceed order amount")

        new_status = _status_after_paid_change(
            row["amount_cents"], new_paid, row["refunded_cents"], row["written_off_cents"]
        )
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
        )
        conn.execute(
            "UPDATE payment_corrections SET cancelled_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE tenant=? AND biz_id=?",
            (tenant, correction_biz_id),
        )
        conn.execute(
            "INSERT INTO payment_correction_cancels(tenant, biz_id, order_id, correction_biz_id, amount_cents) VALUES(?,?,?,?,?)",
            (tenant, biz_id, order_id, correction_biz_id, cancel_amount),
        )
        _insert_ledger(
            conn, tenant, order_id, "correction_cancel", biz_id, cancel_amount,
            new_paid, row["amount_cents"] - new_paid,
            row["refunded_cents"], row["written_off_cents"],
        )
        conn.execute("COMMIT")
        saved = conn.execute(
            "SELECT tenant, biz_id, order_id, correction_biz_id, amount_cents, created_at FROM payment_correction_cancels WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        record = dict(saved)
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return _build_correction_result(record, get(tenant, order_id), replayed, correction_biz_id), False

def _build_correction_result(
    record: dict, order: dict | None, replayed: bool, correction_biz_id: str | None
) -> dict:
    result = dict(record)
    result["replayed"] = replayed
    # 统一返回被取消修正的业务标识：取消记录回显被取消修正，修正操作为 None
    result["correction_biz_id"] = correction_biz_id
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
