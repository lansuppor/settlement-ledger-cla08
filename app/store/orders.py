import sqlite3

from app.store.db import connect

# 业务标识命名空间：冲正、作废、冲销、冲正修正、取消修正、退款单及其执行冲正的业务标识在租户内共用唯一性，不得混用。
# 各表承载业务标识的列名（退款单以退款单标识作为其单据业务标识）。
BIZ_ID_TABLES = {
    "refunds": "biz_id",
    "voids": "biz_id",
    "writeoffs": "biz_id",
    "corrections": "biz_id",
    "correction_cancels": "biz_id",
    "refund_orders": "refund_id",
    "refund_reversals": "biz_id",
}

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
    for table, column in BIZ_ID_TABLES.items():
        if table == own_table:
            continue
        used = conn.execute(
            f"SELECT 1 FROM {table} WHERE tenant=? AND {column}=?",
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

def _rejudge_status(
    status: str, amount: int, paid: int, refunded: int, written_off: int
) -> str:
    """修正/取消改变已收后按既有口径重判订单状态（作废保持终态）。

    终态不可逆，故从终态进入修正/取消的路径已在前置校验拒绝；此处只需按
    accepted/settled/completed 的既有口径重判。
    """
    if status == "voided":
        return status
    if _is_terminal(paid, refunded, written_off):
        return "completed"
    return "settled" if paid >= amount else "accepted"

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

def apply_refund_in_tx(
    conn: sqlite3.Connection, tenant: str, order_id: str, biz_id: str,
    amount_cents: int, refund_order_id: str | None = None,
) -> None:
    """在调用方已开启的事务内登记一笔冲正并追加流水。

    供直接冲正与退款单执行到账共用，保证两者累计冲正、可退余额、终态口径一致。
    订单不存在/跨租户抛 LookupError；终态、非正数、超过可退余额抛 ValueError，
    由调用方整体回滚。biz_id 命名空间占用由调用方在前置校验保证。
    """
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
        "INSERT INTO refunds(tenant, biz_id, order_id, amount_cents, refund_order_id) VALUES(?,?,?,?,?)",
        (tenant, biz_id, order_id, amount_cents, refund_order_id),
    )
    _insert_ledger(
        conn, tenant, order_id, "refund", biz_id, amount_cents,
        row["paid_cents"], row["amount_cents"] - row["paid_cents"],
        new_refunded, row["written_off_cents"],
    )

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
            "SELECT tenant, biz_id, order_id, amount_cents, refund_order_id, created_at FROM refunds WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if existing is not None:
            # 退款单执行到账写入的冲正行属于退款单单据命名空间，直接冲正不得重放或复用
            if existing["refund_order_id"] is not None:
                raise ValueError("biz_id already used by another operation")
            # 重放：业务标识已存在，只返回同一记录
            if existing["order_id"] != order_id:
                raise ValueError("biz_id already used for another order")
            record = dict(existing)
            replayed = True
            conn.execute("COMMIT")
            return _build_op_result(record, get(tenant, order_id), replayed), True
        _assert_biz_id_free(conn, tenant, biz_id, "refunds")
        apply_refund_in_tx(conn, tenant, order_id, biz_id, amount_cents)
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

def add_correction(
    tenant: str, order_id: str, biz_id: str, amount_cents: int
) -> tuple[dict, bool]:
    """登记收款冲正修正。返回 (修正结果, 是否重放)；订单不存在或跨租户抛 LookupError。

    生效后已收减少、未收增加，订单金额与累计冲正/累计冲销及既有流水不改，
    仅追加一条修正流水。扣减上限为 已收 − 累计冲正 − 累计冲销，保证交错后
    累计冲正不越过已收、累计冲销不越过既有口径；已作废或已进入收付完结
    终态的订单拒绝。重放返回首次的同一记录，不重复扣减、不新增流水。
    """
    conn = connect()
    record = None
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, biz_id, order_id, amount_cents, cancelled, cancelled_biz_id, created_at FROM corrections WHERE tenant=? AND biz_id=?",
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
        _assert_biz_id_free(conn, tenant, biz_id, "corrections")

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        _assert_not_terminal(row)
        if amount_cents <= 0:
            raise ValueError("correction amount must be positive")
        # 修正后累计冲正、累计冲销仍不得越过既有上限：
        # paid' >= refunded + written_off（即可退余额减累计冲销不归负）。
        # 无冲正/冲销时该上限即已收本身，等价于“超过已收拒绝”。
        corr_cap = row["paid_cents"] - row["refunded_cents"] - row["written_off_cents"]
        if amount_cents > corr_cap:
            raise ValueError("correction exceeds reducible paid balance")

        new_paid = row["paid_cents"] - amount_cents
        new_status = _rejudge_status(
            row["status"], row["amount_cents"], new_paid, row["refunded_cents"], row["written_off_cents"]
        )
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO corrections(tenant, biz_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, biz_id, order_id, amount_cents),
        )
        _insert_ledger(
            conn, tenant, order_id, "correction", biz_id, amount_cents,
            new_paid, row["amount_cents"] - new_paid,
            row["refunded_cents"], row["written_off_cents"],
        )
        conn.execute("COMMIT")
        saved = conn.execute(
            "SELECT tenant, biz_id, order_id, amount_cents, cancelled, cancelled_biz_id, created_at FROM corrections WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        record = dict(saved)
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return _build_op_result(record, get(tenant, order_id), replayed), False

def cancel_correction(
    tenant: str, order_id: str, biz_id: str, correction_biz_id: str
) -> tuple[dict, bool]:
    """取消一笔收款冲正修正。返回 (取消结果, 是否重放)；订单不存在或跨租户抛 LookupError。

    取消金额取被取消修正的原始金额，不可指定；仅已生效且未被取消的修正可取消。
    生效后已收恢复、未收减少，订单金额与既有流水不改，仅追加一条取消流水。
    被取消修正不存在、订单不匹配、已取消或恢复后已收超过订单金额一律拒绝。
    重放返回首次的同一记录，不重复恢复、不新增流水。
    """
    conn = connect()
    record = None
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, biz_id, order_id, correction_biz_id, amount_cents, created_at FROM correction_cancels WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if existing is not None:
            # 重放：取消业务标识已存在，只返回同一记录
            if existing["order_id"] != order_id:
                raise ValueError("biz_id already used for another order")
            record = dict(existing)
            replayed = True
            conn.execute("COMMIT")
            return _build_op_result(record, get(tenant, order_id), replayed), True
        _assert_biz_id_free(conn, tenant, biz_id, "correction_cancels")

        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            raise LookupError("order not found")
        _assert_not_terminal(row)

        target = conn.execute(
            "SELECT order_id, amount_cents, cancelled FROM corrections WHERE tenant=? AND biz_id=?",
            (tenant, correction_biz_id),
        ).fetchone()
        if target is None:
            raise ValueError("correction not found")
        if target["order_id"] != order_id:
            raise ValueError("correction belongs to another order")
        if target["cancelled"]:
            raise ValueError("correction already cancelled")
        amount_cents = target["amount_cents"]

        new_paid = row["paid_cents"] + amount_cents
        if new_paid > row["amount_cents"]:
            raise ValueError("correction cancel would exceed order amount")
        new_status = _rejudge_status(
            row["status"], row["amount_cents"], new_paid, row["refunded_cents"], row["written_off_cents"]
        )
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
        )
        conn.execute(
            "UPDATE corrections SET cancelled=1, cancelled_biz_id=? WHERE tenant=? AND biz_id=?",
            (biz_id, tenant, correction_biz_id),
        )
        conn.execute(
            "INSERT INTO correction_cancels(tenant, biz_id, order_id, correction_biz_id, amount_cents) VALUES(?,?,?,?,?)",
            (tenant, biz_id, order_id, correction_biz_id, amount_cents),
        )
        _insert_ledger(
            conn, tenant, order_id, "correction_cancel", biz_id, amount_cents,
            new_paid, row["amount_cents"] - new_paid,
            row["refunded_cents"], row["written_off_cents"],
        )
        conn.execute("COMMIT")
        saved = conn.execute(
            "SELECT tenant, biz_id, order_id, correction_biz_id, amount_cents, created_at FROM correction_cancels WHERE tenant=? AND biz_id=?",
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
    if "cancelled" in result:
        result["cancelled"] = bool(result["cancelled"])
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
