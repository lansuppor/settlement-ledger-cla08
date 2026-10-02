import json
import sqlite3

from app.rules.refund_rules import RANGE_FILTERS
from app.store.db import connect
from app.store.refund_orders import COLUMNS, _rollback_safe, _shape


def _build_where(filters: dict) -> tuple[str, list]:
    clauses = ["tenant=?"]
    params: list = []
    if "status" in filters:
        clauses.append("status=?")
        params.append(filters["status"])
    if "order_id" in filters:
        clauses.append("order_id=?")
        params.append(filters["order_id"])
    if "has_reversal" in filters:
        # 按冲正状态过滤：已冲正（reversal_biz_id 落库）/ 未冲正
        clauses.append(
            "reversal_biz_id IS NOT NULL" if filters["has_reversal"] else "reversal_biz_id IS NULL"
        )
    for name, column in RANGE_FILTERS.items():
        if f"{name}_min_cents" in filters:
            clauses.append(f"{column}>=?")
            params.append(filters[f"{name}_min_cents"])
        if f"{name}_max_cents" in filters:
            clauses.append(f"{column}<=?")
            params.append(filters[f"{name}_max_cents"])
    return " AND ".join(clauses), params

def _execute(
    conn: sqlite3.Connection, tenant: str, request_id: str,
    filters: dict, size: int, cursor: str | None,
) -> dict:
    where, params = _build_where(filters)
    if cursor is not None:
        where += " AND refund_id>?"
        params.append(cursor)
    # 按退款单标识升序翻页：单据标识不随生命周期变动，无新受理时翻页不重不漏
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM refund_orders WHERE {where} ORDER BY refund_id ASC LIMIT ?",
        [tenant, *params, size],
    ).fetchall()
    refunds = [_shape(row) for row in rows]
    return {
        "request_id": request_id,
        "refund_orders": refunds,
        "page": {
            "size": size,
            "next_cursor": refunds[-1]["refund_id"] if len(refunds) == size else None,
        },
    }

def run_search(
    tenant: str, request_id: str, filters: dict, size: int, cursor: str | None
) -> tuple[dict, bool]:
    """退款单条件检索。返回 (响应, 是否重放)。

    去重标识标识检索请求本身，与退款单标识独立；同一（租户, 去重标识）重放
    返回首次落库的同一结果集，不重新查询、不重复写入。检索按租户隔离。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT response_json FROM refund_search_requests WHERE tenant=? AND request_id=?",
            (tenant, request_id),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return json.loads(existing["response_json"]), True
        response = _execute(conn, tenant, request_id, filters, size, cursor)
        conn.execute(
            "INSERT INTO refund_search_requests(tenant, request_id, response_json) VALUES(?,?,?)",
            (tenant, request_id, json.dumps(response)),
        )
        conn.execute("COMMIT")
        return response, False
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
