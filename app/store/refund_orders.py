import sqlite3

from app.store.db import connect
from app.store.orders import _insert_ledger, _is_terminal, _rollback_safe

# 订单侧业务标识表（列名均为 biz_id）：退款单标识与这些标识在租户内共用唯一性，不得混用
ORDER_BIZ_TABLES = ("refunds", "voids", "writeoffs", "corrections", "correction_cancels")

COLUMNS = (
    "tenant, refund_id, order_id, request_cents, actual_cents, status, "
    "failure_reason, attempts, created_at, updated_at"
)

def _shape(row: sqlite3.Row) -> dict:
    return {
        "tenant": row["tenant"],
        "refund_id": row["refund_id"],
        "order_id": row["order_id"],
        "request_cents": row["request_cents"],
        "actual_cents": row["actual_cents"],
        "status": row["status"],
        "failure_reason": row["failure_reason"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def _assert_refund_id_free_of_order_ops(conn: sqlite3.Connection, tenant: str, refund_id: str) -> None:
    """退款单标识不得与订单侧冲正/作废/冲销/修正/取消的业务标识混用。"""
    for table in ORDER_BIZ_TABLES:
        used = conn.execute(
            f"SELECT 1 FROM {table} WHERE tenant=? AND biz_id=?",
            (tenant, refund_id),
        ).fetchone()
        if used is not None:
            raise ValueError("refund_id conflicts with order operation biz_id")

def get(conn_like: sqlite3.Connection | None, tenant: str, refund_id: str) -> dict | None:
    """按标识读取退款单；可传入既有连接（事务内）或 None 新开连接。不存在返回 None。"""
    own_conn = conn_like is None
    conn = conn_like if conn_like is not None else connect()
    try:
        row = conn.execute(
            f"SELECT {COLUMNS} FROM refund_orders WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
    finally:
        if own_conn:
            conn.close()
    return _shape(row) if row is not None else None

def accept(
    tenant: str, refund_id: str, order_id: str, request_cents: int
) -> tuple[dict, bool]:
    """受理退款单。返回 (退款单, 是否重放)。

    同一（租户, 退款单标识）重复受理同一原订单返回首次的同一单据且不新建；
    标识已存在但指向另一原订单、或与订单侧业务标识冲突时拒绝且不改任何数据。
    退款单可受理在任意状态（含已作废/终态）的原订单上。
    """
    conn = connect()
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = get(conn, tenant, refund_id)
        if existing is not None:
            # 重放：标识已存在，同单返回首次的同一单据；指向另一订单则拒绝
            if existing["order_id"] != order_id:
                raise ValueError("refund_id already used for another order")
            conn.execute("COMMIT")
            return existing, True
        _assert_refund_id_free_of_order_ops(conn, tenant, refund_id)

        # 受理允许任意状态的原订单，但原订单必须存在且属于本租户
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            raise LookupError("order not found")

        conn.execute(
            "INSERT INTO refund_orders(tenant, refund_id, order_id, request_cents, status) "
            "VALUES(?,?,?,?,'accepted')",
            (tenant, refund_id, order_id, request_cents),
        )
        conn.execute("COMMIT")
        replayed = False
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    doc = get(None, tenant, refund_id)
    assert doc is not None
    return doc, replayed

def review(tenant: str, refund_id: str, approved: bool) -> dict:
    """审核：通过进入 approved，驳回进入 rejected；仅已受理未审核可审核。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {COLUMNS} FROM refund_orders WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            raise LookupError("refund order not found")
        if row["status"] != "accepted":
            raise ValueError(_not_awaiting_review_reason(row["status"]))
        new_status = "approved" if approved else "rejected"
        conn.execute(
            "UPDATE refund_orders SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE tenant=? AND refund_id=?",
            (new_status, tenant, refund_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    doc = get(None, tenant, refund_id)
    assert doc is not None
    return doc

def _not_awaiting_review_reason(status: str) -> str:
    if status == "approved":
        return "refund already approved"
    if status == "rejected":
        return "refund already rejected"
    if status == "settled":
        return "refund already settled"
    if status == "failed":
        return "refund already executed"
    return "refund already cancelled"

def execute(tenant: str, refund_id: str, failure_reason: str | None) -> tuple[dict, bool]:
    """执行退款单。返回 (退款单, 是否重放)。

    实退金额取退款单的申请金额。仅审核通过（approved）或执行失败待重试（failed）
    可执行；已到账重复执行返回同一结果不重复入账。成功：实退金额计入原订单累计
    冲正（不得超过当时可退余额），追加退款流水（业务标识记退款单标识）并按既有
    口径重判订单状态。失败（failure_reason 非空）：仅登记失败原因与次数，不动原
    订单、不产生流水。原订单已作废/收付完结/被删除/跨租户按不存在处理，整体拒绝，
    不产生流水与状态变化。
    """
    conn = connect()
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {COLUMNS} FROM refund_orders WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            raise LookupError("refund order not found")
        if row["status"] == "settled":
            # 重复执行：返回首次的同一结果，不重复入账
            conn.execute("COMMIT")
            return _shape(row), True
        if row["status"] not in ("approved", "failed"):
            raise ValueError(_not_executable_reason(row["status"]))

        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, row["order_id"]),
        ).fetchone()
        if order is None or order["status"] in ("voided", "completed"):
            # 已被删除、跨租户、已作废、已进入收付完结终态：按原订单不存在处理并整体拒绝
            raise LookupError("order not found")

        if failure_reason is not None:
            # 上报执行失败：只登记失败原因，可再次执行；不动原订单、不产生流水
            conn.execute(
                "UPDATE refund_orders SET status='failed', failure_reason=?, attempts=attempts+1, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE tenant=? AND refund_id=?",
                (failure_reason, tenant, refund_id),
            )
            conn.execute("COMMIT")
            replayed = False
        else:
            amount_cents = row["request_cents"]
            refundable = order["paid_cents"] - order["refunded_cents"]
            if amount_cents > refundable:
                # 超过当时可退余额：整体拒绝，退款单保持已通过待执行，无任何变更
                raise ValueError("refund exceeds refundable balance")

            new_refunded = order["refunded_cents"] + amount_cents
            # 可退余额减累计冲销归零且已收大于零：收付进入终态；其余情况保留原状态
            new_order_status = (
                "completed"
                if _is_terminal(order["paid_cents"], new_refunded, order["written_off_cents"])
                else order["status"]
            )
            conn.execute(
                "UPDATE orders SET refunded_cents=?, status=? WHERE tenant=? AND order_id=?",
                (new_refunded, new_order_status, tenant, row["order_id"]),
            )
            _insert_ledger(
                conn, tenant, row["order_id"], "refund", refund_id, amount_cents,
                order["paid_cents"], order["amount_cents"] - order["paid_cents"],
                new_refunded, order["written_off_cents"],
            )
            conn.execute(
                "UPDATE refund_orders SET status='settled', actual_cents=?, failure_reason=NULL, "
                "attempts=attempts+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE tenant=? AND refund_id=?",
                (amount_cents, tenant, refund_id),
            )
            conn.execute("COMMIT")
            replayed = False
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    doc = get(None, tenant, refund_id)
    assert doc is not None
    return doc, replayed

def _not_executable_reason(status: str) -> str:
    if status == "accepted":
        return "refund not approved"
    if status == "rejected":
        return "refund is rejected"
    # settled 已在调用前按重放处理，cancelled 为撤销终态
    return "refund already cancelled"

def cancel(tenant: str, refund_id: str) -> dict:
    """撤销退款单：仅已受理、已驳回、执行失败可撤销，撤销后进入已撤销终态。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {COLUMNS} FROM refund_orders WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            raise LookupError("refund order not found")
        if row["status"] not in ("accepted", "rejected", "failed"):
            raise ValueError(_not_cancellable_reason(row["status"]))
        conn.execute(
            "UPDATE refund_orders SET status='cancelled', "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    doc = get(None, tenant, refund_id)
    assert doc is not None
    return doc

def _not_cancellable_reason(status: str) -> str:
    if status == "approved":
        return "refund approved and awaiting execution"
    if status == "settled":
        return "refund already settled"
    return "refund already cancelled"
