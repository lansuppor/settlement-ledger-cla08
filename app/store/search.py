import json
import sqlite3

from app.rules.order_rules import RANGE_FILTERS
from app.store.db import connect
from app.store.orders import _rollback_safe, _shape


def _build_where(filters: dict, cursor: str | None) -> tuple[str, list]:
    clauses = ["tenant=?"]
    params: list = []
    if "status" in filters:
        clauses.append("status=?")
        params.append(filters["status"])
    if "currency" in filters:
        clauses.append("currency=?")
        params.append(filters["currency"])
    for name, column in RANGE_FILTERS.items():
        if f"{name}_min_cents" in filters:
            clauses.append(f"{column}>=?")
            params.append(filters[f"{name}_min_cents"])
        if f"{name}_max_cents" in filters:
            clauses.append(f"{column}<=?")
            params.append(filters[f"{name}_max_cents"])
    if "has_refund" in filters:
        clauses.append("refunded_cents>0" if filters["has_refund"] else "refunded_cents=0")
    where = " AND ".join(clauses)
    # 租户放首位，游标放末位，保持参数顺序与占位符一致
    return where, params

def _execute(
    conn: sqlite3.Connection, tenant: str, request_id: str,
    filters: dict, size: int, cursor: str | None,
) -> dict:
    where, params = _build_where(filters, cursor)
    if cursor is not None:
        where += " AND order_id>?"
        params.append(cursor)
    # 按订单标识升序翻页：收付变动不改变排序键，无新受理时翻页不重不漏
    rows = conn.execute(
        f"SELECT tenant, order_id, amount_cents, paid_cents, refunded_cents, written_off_cents, currency, status"
        f" FROM orders WHERE {where} ORDER BY order_id ASC LIMIT ?",
        [tenant, *params, size],
    ).fetchall()
    orders = [_shape(row) for row in rows]
    return {
        "request_id": request_id,
        "orders": orders,
        "page": {
            "size": size,
            "next_cursor": orders[-1]["order_id"] if len(orders) == size else None,
        },
    }

def run_search(
    tenant: str, request_id: str, filters: dict, size: int, cursor: str | None
) -> tuple[dict, bool]:
    """条件检索。返回 (响应, 是否重放)。

    去重标识标识检索请求本身，与订单标识独立；同一（租户, 去重标识）重放
    返回首次落库的同一结果集，不重新查询、不重复写入。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT response_json FROM search_requests WHERE tenant=? AND request_id=?",
            (tenant, request_id),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return json.loads(existing["response_json"]), True
        response = _execute(conn, tenant, request_id, filters, size, cursor)
        conn.execute(
            "INSERT INTO search_requests(tenant, request_id, response_json) VALUES(?,?,?)",
            (tenant, request_id, json.dumps(response)),
        )
        conn.execute("COMMIT")
        return response, False
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()
