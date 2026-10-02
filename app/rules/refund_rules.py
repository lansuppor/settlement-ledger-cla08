"""退款单条件检索参数校验与规范化。"""

# 退款单生命周期状态
REFUND_ORDER_STATUSES = {
    "accepted",   # 已受理，待审核
    "approved",   # 审核通过，待执行
    "rejected",   # 审核驳回，不可执行、可撤销
    "succeeded",  # 执行到账（终态）
    "failed",     # 执行失败，可再次执行、可撤销
    "cancelled",  # 已撤销（终态）
}

# 申请金额区间筛选字段：筛选参数名 -> 退款单列名
RANGE_FILTERS = {
    "requested_amount": "requested_amount_cents",
}

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 500

def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)

def normalize_filters(filters: object) -> dict:
    """校验并规范化检索条件；非法条件抛 ValueError/TypeError（可区分原因）。"""
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise TypeError("invalid_filters")
    known = {"status", "order_id"} | {
        f"{name}_{bound}_cents" for name in RANGE_FILTERS for bound in ("min", "max")
    }
    unknown = set(filters) - known
    if unknown:
        raise ValueError(f"unknown_filter:{min(unknown)}")
    out: dict = {}
    status = filters.get("status")
    if status is not None:
        if not isinstance(status, str) or status not in REFUND_ORDER_STATUSES:
            raise ValueError("invalid_status")
        out["status"] = status
    order_id = filters.get("order_id")
    if order_id is not None:
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("invalid_order_id")
        out["order_id"] = order_id
    for name in RANGE_FILTERS:
        lo = filters.get(f"{name}_min_cents")
        hi = filters.get(f"{name}_max_cents")
        for value in (lo, hi):
            if value is not None and (not _is_int(value) or value < 0):
                raise ValueError(f"invalid_{name}_range")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"invalid_{name}_range")
        if lo is not None:
            out[f"{name}_min_cents"] = lo
        if hi is not None:
            out[f"{name}_max_cents"] = hi
    return out

def normalize_page(page: object) -> tuple[int, str | None]:
    """校验并规范化分页参数，返回 (size, cursor)；非法抛 ValueError/TypeError。"""
    if page is None:
        return PAGE_SIZE_DEFAULT, None
    if not isinstance(page, dict):
        raise TypeError("invalid_page")
    unknown = set(page) - {"size", "cursor"}
    if unknown:
        raise ValueError(f"unknown_page_field:{min(unknown)}")
    size = page.get("size", PAGE_SIZE_DEFAULT)
    if not _is_int(size) or size < 1 or size > PAGE_SIZE_MAX:
        raise ValueError("invalid_page_size")
    cursor = page.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError("invalid_cursor")
    return size, cursor
