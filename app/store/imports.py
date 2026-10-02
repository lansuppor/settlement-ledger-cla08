from app.rules.order_rules import ALLOWED_CURRENCIES
from app.store import orders

# 行级拒绝原因（可区分）
REASON_INVALID_ROW = "invalid_row"                # 行不是对象
REASON_MISSING_FIELD = "missing_field"            # 必填标识缺失或为空
REASON_INVALID_AMOUNT = "invalid_amount"          # 金额非正整数
REASON_UNSUPPORTED_CURRENCY = "unsupported_currency"
REASON_DUPLICATE_IN_BATCH = "duplicate_in_batch"  # 同一份提交内重复
REASON_DUPLICATE_ACCEPTANCE = "duplicate_acceptance"  # 订单标识已存在

def _reject(line: int, reason: str, row: dict | None = None) -> dict:
    result = {"line": line, "status": "rejected", "reason": reason}
    if isinstance(row, dict):
        result["tenant"] = row.get("tenant")
        result["order_id"] = row.get("order_id")
    return result

def _accept_row(line: int, row: object, seen: set[tuple[str, str]]) -> dict:
    if not isinstance(row, dict):
        return _reject(line, REASON_INVALID_ROW)
    tenant = row.get("tenant")
    order_id = row.get("order_id")
    if (
        not isinstance(tenant, str) or not tenant.strip()
        or not isinstance(order_id, str) or not order_id.strip()
    ):
        return _reject(line, REASON_MISSING_FIELD, row)
    key = (tenant, order_id)
    if key in seen:
        # 同一份提交内重复出现：只按第一次生效，重复行拒绝
        return _reject(line, REASON_DUPLICATE_IN_BATCH, row)
    seen.add(key)
    amount = row.get("amount_cents")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return _reject(line, REASON_INVALID_AMOUNT, row)
    currency = row.get("currency")
    if not isinstance(currency, str) or currency not in ALLOWED_CURRENCIES:
        return _reject(line, REASON_UNSUPPORTED_CURRENCY, row)
    try:
        orders.insert(tenant, order_id, amount, currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            # 订单标识已存在：重复受理拒绝，不修改已有订单与流水
            return _reject(line, REASON_DUPLICATE_ACCEPTANCE, row)
        raise
    return {
        "line": line,
        "status": "accepted",
        "tenant": tenant,
        "order_id": order_id,
        "order": orders.get(tenant, order_id),
    }

def accept_batch(rows: list) -> list[dict]:
    """逐行校验并以行为单位独立生效；某行被拒绝不影响其他行，也不产生流水。"""
    seen: set[tuple[str, str]] = set()
    return [_accept_row(line, row, seen) for line, row in enumerate(rows, start=1)]
