"""退款单（refund order）存储与生命周期。

退款单独立于订单收款冲正登记：它是登记在原订单之下的独立单据，带自身单据标识，
记录申请退款金额与实退金额。生命周期：

    accepted ──approve──▶ approved ──execute(succeeded)──▶ succeeded ──reverse──▶ reversed（终态）
        │                    │
        │                    └──execute(failed)──▶ failed ──execute──▶ succeeded/failed
        │
        ├──reject──▶ rejected
        └──────────▶ cancelled（终态）  （accepted/rejected/failed 均可撤销）

审核、执行、撤销、冲正均在单事务内完成；执行到账通过 orders.apply_refund_in_tx 与原订单
收付账联动（累计冲正、退款流水、状态重判同一口径），任一步失败整体回滚。
执行冲正把已到账退款单的实退金额从原订单累计冲正中扣减，追加一条原订单冲正流水
（业务标识记本次冲正的业务标识），按既有口径重判原订单状态，退款单进入 reversed 终态。
"""
import sqlite3

from app.store.db import connect
from app.store.orders import _assert_biz_id_free, _insert_ledger, _rejudge_status, apply_refund_in_tx

COLUMNS = (
    "tenant, refund_id, order_id, requested_amount_cents, refunded_amount_cents,"
    " status, failure_reason, reversal_biz_id, created_at, updated_at"
)

def _rollback_safe(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # 事务已结束

def _shape(row: sqlite3.Row) -> dict:
    return {
        "tenant": row["tenant"],
        "refund_id": row["refund_id"],
        "order_id": row["order_id"],
        "requested_amount_cents": row["requested_amount_cents"],
        "refunded_amount_cents": row["refunded_amount_cents"],
        "status": row["status"],
        "failure_reason": row["failure_reason"],
        "reversal_biz_id": row["reversal_biz_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def _fetch(conn: sqlite3.Connection, tenant: str, refund_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {COLUMNS} FROM refund_orders WHERE tenant=? AND refund_id=?",
        (tenant, refund_id),
    ).fetchone()

def _result(row: sqlite3.Row, replayed: bool) -> dict:
    result = _shape(row)
    result["replayed"] = replayed
    return result

def accept(
    tenant: str, refund_id: str, order_id: str, requested_amount_cents: int
) -> tuple[dict, bool]:
    """受理退款单。返回 (退款单, 是否重放)。

    同一（租户, 退款单标识）重复受理返回首次的同一单据且不新建，标记为重放；
    重放仅在原订单标识与申请金额完全一致时成立，否则按冲突拒绝。
    退款单标识已存在于订单侧业务标识命名空间，或原订单不存在/跨租户，一律拒绝
    且不写任何数据。退款单可受理在任意状态的订单上（收付联动只发生在执行到账时）。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _fetch(conn, tenant, refund_id)
        if existing is not None:
            if existing["order_id"] != order_id:
                raise ValueError("refund id already used for another order")
            if existing["requested_amount_cents"] != requested_amount_cents:
                raise ValueError("refund replay with different requested amount")
            conn.execute("COMMIT")
            return _result(existing, True), True
        # 与订单侧业务标识（冲正/作废/冲销/修正/取消修正）共用唯一性，不得混用
        _assert_biz_id_free(conn, tenant, refund_id, "refund_orders")
        # 原订单必须存在且属于本租户；订单可为任意状态（含已作废、已终态）
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            raise LookupError("order not found")
        conn.execute(
            "INSERT INTO refund_orders(tenant, refund_id, order_id, requested_amount_cents, status)"
            " VALUES(?,?,?,?,'accepted')",
            (tenant, refund_id, order_id, requested_amount_cents),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    # 重开连接读取，与既有写操作“提交后 get”的口径一致
    return get(tenant, refund_id)

def get(tenant: str, refund_id: str) -> tuple[dict, bool] | None:
    """按标识读取退款单；不存在或跨租户返回 None（调用方按 404 处理，不泄漏存在性）。

    返回 (退款单, False)：读取本身不是重放，重放标记只由受理/执行的幂等路径给出。
    """
    conn = connect()
    try:
        row = _fetch(conn, tenant, refund_id)
    finally:
        conn.close()
    return (_result(row, False), False) if row is not None else None

def review(
    tenant: str, refund_id: str, approve: bool
) -> tuple[dict, bool]:
    """审核：approve=True 通过（accepted→approved），False 驳回（accepted→rejected）。

    只允许对已受理未审核的退款单生效；其余状态拒绝且无变更。
    驳回事务内只改本单（无关联收付写入），此后不可执行、可撤销。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch(conn, tenant, refund_id)
        if row is None:
            raise LookupError("refund order not found")
        if row["status"] != "accepted":
            raise ValueError(f"refund order not awaiting review: {row['status']}")
        new_status = "approved" if approve else "rejected"
        conn.execute(
            "UPDATE refund_orders SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            " WHERE tenant=? AND refund_id=?",
            (new_status, tenant, refund_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return get(tenant, refund_id)

def execute(
    tenant: str, refund_id: str, succeed: bool,
    refunded_amount_cents: int | None = None, failure_reason: str | None = None,
) -> tuple[dict, bool]:
    """执行退款单。

    - succeed=True：执行到账。实退金额取请求传入值（缺省为申请金额），必须为正整数
      且不超过原订单当时可退余额；把实退金额计入原订单累计冲正、追加一条原订单退款
      流水（业务标识记退款单标识），并按既有口径重判原订单状态。成功后退款单进入
      succeeded，实退金额落库。超过可退余额、原订单已作废/已终态/被删除/跨租户时
      整体拒绝，不产生流水与状态变化，退款单保持 approved。
    - succeed=False：执行失败。记录失败原因，退款单进入 failed，可再次执行。
      仅 approved/failed 可执行（通过后的一次到账可经历多次失败重试）。
    已到账重复执行返回同一结果，不重复入账。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch(conn, tenant, refund_id)
        if row is None:
            raise LookupError("refund order not found")
        if row["status"] == "succeeded":
            # 到账结果只生效一次：重复执行返回同一结果
            conn.execute("COMMIT")
            return _result(row, True), True
        if row["status"] not in ("approved", "failed"):
            raise ValueError(f"refund order not executable: {row['status']}")
        # 原订单已作废、已进入收付完结终态、已被删除或跨租户：执行一律按不存在
        # 处理并整体拒绝（无论到账还是失败登记），不产生流水与状态变化。
        # LookupError 由调用方映射为 404。
        order_row = conn.execute(
            "SELECT status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, row["order_id"]),
        ).fetchone()
        if order_row is None or order_row["status"] in ("voided", "completed"):
            raise LookupError("order not found")
        if not succeed:
            reason = failure_reason if isinstance(failure_reason, str) and failure_reason else None
            conn.execute(
                "UPDATE refund_orders SET status='failed', failure_reason=?,"
                " updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE tenant=? AND refund_id=?",
                (reason, tenant, refund_id),
            )
            conn.execute("COMMIT")
        else:
            amount = (
                row["requested_amount_cents"]
                if refunded_amount_cents is None
                else refunded_amount_cents
            )
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
                raise ValueError("refunded amount must be a positive integer")
            # 与原订单收付账联动：超过当时可退余额在此抛 ValueError，由本事务整体
            # 回滚，退款单保持 approved/failed 不变。
            apply_refund_in_tx(
                conn, tenant, row["order_id"], refund_id, amount,
                refund_order_id=refund_id,
            )
            conn.execute(
                "UPDATE refund_orders SET status='succeeded', refunded_amount_cents=?,"
                " failure_reason=NULL, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                " WHERE tenant=? AND refund_id=?",
                (amount, tenant, refund_id),
            )
            conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return get(tenant, refund_id)

def _reversal_result(row: sqlite3.Row, replayed: bool) -> dict:
    """冲正结果：记录本身 + 冲正生效当时的原订单结果金额快照（重放返回同一记录）。"""
    return {
        "tenant": row["tenant"],
        "biz_id": row["biz_id"],
        "refund_id": row["refund_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "paid_cents": row["paid_result_cents"],
        "outstanding_cents": row["outstanding_result_cents"],
        "refunded_cents": row["refunded_result_cents"],
        "written_off_cents": row["written_off_result_cents"],
        "refundable_cents": row["paid_result_cents"] - row["refunded_result_cents"],
        "status": row["order_status"],
        "created_at": row["created_at"],
        "replayed": replayed,
    }

def reverse(tenant: str, refund_id: str, biz_id: str) -> tuple[dict, bool]:
    """对已到账的退款单执行冲正。返回 (冲正记录, 是否重放)。

    仅 succeeded 可冲正；其余状态（accepted/approved/rejected/failed/cancelled/
    reversed）一律拒绝且无任何变更。冲正金额取退款单已落库的实退金额，调用方不可
    指定。生效后在原订单累计冲正中扣减该实退金额，追加一条原订单 refund_reversal
    流水（业务标识记本次冲正的业务标识），按既有口径重判原订单状态；订单金额、
    已收、累计冲销及既有流水不改。退款单进入 reversed 终态并落库冲正业务标识，
    此后审核、执行、撤销、再冲正一律拒绝。
    同一（租户, 业务标识）重放返回首次的同一记录（含当时订单结果金额），不重复
    扣减、不新增流水；业务标识与订单侧任一操作或其他退款单/冲正冲突返回 409。
    退款单或原订单不存在、跨租户按不存在处理（LookupError → 404），不产生流水
    与状态变化。全部写入在单事务内完成，任一步失败整体回滚。
    """
    conn = connect()
    record = None
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM refund_reversals WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
        if existing is not None:
            # 重放判定以本次冲正的业务标识为准，与退款单标识互不干扰
            if existing["refund_id"] != refund_id:
                raise ValueError("biz_id already used for another refund order")
            record = existing
            replayed = True
            conn.execute("COMMIT")
            return _reversal_result(record, True), True
        # 与订单侧业务标识（冲正/作废/冲销/修正/取消修正）及退款单标识共用唯一性，不得混用
        _assert_biz_id_free(conn, tenant, biz_id, "refund_reversals")

        row = _fetch(conn, tenant, refund_id)
        if row is None:
            raise LookupError("refund order not found")
        if row["status"] != "succeeded":
            raise ValueError(f"refund order not reversible: {row['status']}")
        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, written_off_cents, status"
            " FROM orders WHERE tenant=? AND order_id=?",
            (tenant, row["order_id"]),
        ).fetchone()
        if order is None:
            raise LookupError("order not found")

        # 冲正金额取已落库的实退金额；累计冲正单调减少这次的量
        amount = row["refunded_amount_cents"]
        new_refunded = order["refunded_cents"] - amount
        new_status = _rejudge_status(
            order["status"], order["amount_cents"], order["paid_cents"],
            new_refunded, order["written_off_cents"],
        )
        conn.execute(
            "UPDATE orders SET refunded_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_refunded, new_status, tenant, row["order_id"]),
        )
        conn.execute(
            "UPDATE refund_orders SET status='reversed', reversal_biz_id=?,"
            " updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE tenant=? AND refund_id=?",
            (biz_id, tenant, refund_id),
        )
        conn.execute(
            "INSERT INTO refund_reversals(tenant, biz_id, refund_id, order_id, amount_cents,"
            " paid_result_cents, outstanding_result_cents, refunded_result_cents,"
            " written_off_result_cents, order_status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                tenant, biz_id, refund_id, row["order_id"], amount,
                order["paid_cents"], order["amount_cents"] - order["paid_cents"],
                new_refunded, order["written_off_cents"], new_status,
            ),
        )
        _insert_ledger(
            conn, tenant, row["order_id"], "refund_reversal", biz_id, amount,
            order["paid_cents"], order["amount_cents"] - order["paid_cents"],
            new_refunded, order["written_off_cents"],
        )
        conn.execute("COMMIT")
        record = conn.execute(
            "SELECT * FROM refund_reversals WHERE tenant=? AND biz_id=?",
            (tenant, biz_id),
        ).fetchone()
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return _reversal_result(record, replayed), replayed

def cancel(tenant: str, refund_id: str) -> tuple[dict, bool]:
    """撤销退款单。仅 accepted/rejected/failed 可撤销，撤销后进入 cancelled 终态，
    不可再执行。approved（审核通过待执行）与 succeeded/cancelled 不可撤销。

    这些状态均未与原订单收付账联动（到账才入账），故撤销只需整单改状态，
    无关联写入需要回滚；事务保证不出现半更新。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch(conn, tenant, refund_id)
        if row is None:
            raise LookupError("refund order not found")
        if row["status"] not in ("accepted", "rejected", "failed"):
            raise ValueError(f"refund order not cancellable: {row['status']}")
        conn.execute(
            "UPDATE refund_orders SET status='cancelled', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            " WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
    return get(tenant, refund_id)
