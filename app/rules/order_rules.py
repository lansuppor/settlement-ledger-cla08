ALLOWED_CURRENCIES = {"CNY", "USD", "EUR", "JPY"}

ORDER_STATUSES = {"accepted", "settled", "completed", "voided"}

# 金额区间筛选字段：筛选参数名 -> 订单列名
RANGE_FILTERS = {
    "amount": "amount_cents",
    "paid": "paid_cents",
    "refunded": "refunded_cents",
}

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 500

def assert_currency(currency: str) -> None:
    if currency not in ALLOWED_CURRENCIES:
        raise ValueError("unsupported currency")

def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)

def normalize_filters(filters: object) -> dict:
    """校验并规范化检索条件；非法条件抛 ValueError/TypeError（可区分原因）。"""
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise TypeError("invalid_filters")
    known = {"status", "currency", "has_refund"} | {
        f"{name}_{bound}_cents" for name in RANGE_FILTERS for bound in ("min", "max")
    }
    unknown = set(filters) - known
    if unknown:
        raise ValueError(f"unknown_filter:{min(unknown)}")
    out: dict = {}
    status = filters.get("status")
    if status is not None:
        if status not in ORDER_STATUSES:
            raise ValueError("invalid_status")
        out["status"] = status
    currency = filters.get("currency")
    if currency is not None:
        if currency not in ALLOWED_CURRENCIES:
            raise ValueError("unsupported_currency")
        out["currency"] = currency
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
    has_refund = filters.get("has_refund")
    if has_refund is not None:
        if not isinstance(has_refund, bool):
            raise ValueError("invalid_has_refund")
        out["has_refund"] = has_refund
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
