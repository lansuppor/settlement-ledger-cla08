"""订单批量受理导入：逐行校验、逐行独立生效。

每一行在自己的事务内受理（复用单笔受理的写路径），某行被拒绝不影响其他行，
也不产生流水；同一份提交内重复出现的行只按第一次生效，重复行拒绝；
订单标识已存在的行按重复受理拒绝，不修改已有订单与流水。
重放同一份提交时，已生效行因标识已存在而按重复受理拒绝，被拒行仍被拒绝，
既有订单与流水均不改变。
"""

from app.rules.order_rules import ALLOWED_CURRENCIES
from app.store import orders

# 行级拒绝原因（可区分）
REASON_MISSING_FIELD = "missing_field"          # 必填标识缺失或为空
REASON_INVALID_AMOUNT = "invalid_amount"        # 金额非正整数
REASON_UNSUPPORTED_CURRENCY = "unsupported_currency"  # 币种不支持
REASON_DUPLICATE = "duplicate"                  # 重复受理（批内重复或标识已存在）


def import_rows(rows: list) -> list[dict]:
    """逐行受理，返回与提交顺序一致的逐行结论。"""
    results = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        results.append(_import_row(row, seen, index))
    return results


def _reject(index: int, reason: str) -> dict:
    return {"index": index, "status": "rejected", "reason": reason}


def _import_row(row, seen: set[tuple[str, str]], index: int) -> dict:
    if not isinstance(row, dict):
        return _reject(index, REASON_MISSING_FIELD)
    tenant = row.get("tenant")
    order_id = row.get("order_id")
    if not isinstance(tenant, str) or not tenant or not isinstance(order_id, str) or not order_id:
        return _reject(index, REASON_MISSING_FIELD)
    amount = row.get("amount_cents")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return _reject(index, REASON_INVALID_AMOUNT)
    currency = row.get("currency")
    if not isinstance(currency, str) or currency not in ALLOWED_CURRENCIES:
        return _reject(index, REASON_UNSUPPORTED_CURRENCY)

    key = (tenant, order_id)
    if key in seen:
        return _reject(index, REASON_DUPLICATE)
    seen.add(key)
    try:
        orders.insert(tenant, order_id, amount, currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            return _reject(index, REASON_DUPLICATE)
        raise
    return {"index": index, "status": "accepted", "order": orders.get(tenant, order_id)}
