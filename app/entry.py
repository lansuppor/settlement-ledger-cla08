import argparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.rules.order_rules import ALLOWED_CURRENCIES
from app.store import orders
from app.store.db import connect, migrate
from app.usecase import batch_import

app = FastAPI(title="settlement-ledger")

ORDER_STATUSES = {"accepted", "settled", "completed"}

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class RefundIn(BaseModel):
    biz_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class BatchIn(BaseModel):
    rows: list = Field(min_length=1)

@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}

@app.post("/orders", status_code=201)
def create_order(body: OrderIn) -> dict:
    order_rules.assert_currency(body.currency)
    try:
        orders.insert(body.tenant, body.order_id, body.amount_cents, body.currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(status_code=409, detail="order already accepted")
        raise
    return orders.get(body.tenant, body.order_id)

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        return orders.add_payment(x_tenant, order_id, body.amount_cents)
    except LookupError:
        raise HTTPException(status_code=404, detail="order not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))

@app.post("/orders/{order_id}/refunds")
def add_refund(order_id: str, body: RefundIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        refund, replayed = orders.add_refund(x_tenant, order_id, body.biz_id, body.amount_cents)
    except LookupError:
        raise HTTPException(status_code=404, detail="order not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    return JSONResponse(  # 重放返回同一记录，保持 200 与首调一致
        refund,
        headers={"X-Idempotent-Replay": "1" if replayed else "0"},
    )

@app.get("/orders/{order_id}/ledger")
def read_ledger(order_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    entries = orders.list_ledger(x_tenant, order_id)
    if entries is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "entries": entries}

@app.post("/orders/batch")
def import_orders(body: BatchIn) -> dict:
    # 逐行校验、逐行独立生效：某行被拒绝不影响其他行，也不产生流水
    return {"results": batch_import.import_rows(body.rows)}

@app.get("/orders")
def search_orders(
    x_tenant: str = Header(default=""),
    request_id: str = "",
    status: str | None = None,
    currency: str | None = None,
    amount_min: int | None = None,
    amount_max: int | None = None,
    paid_min: int | None = None,
    paid_max: int | None = None,
    refunded_min: int | None = None,
    refunded_max: int | None = None,
    has_refund: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> JSONResponse:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id is required")
    filters: dict = {}
    if status is not None:
        if status not in ORDER_STATUSES:
            raise HTTPException(status_code=400, detail="unsupported status")
        filters["status"] = status
    if currency is not None:
        if currency not in ALLOWED_CURRENCIES:
            raise HTTPException(status_code=400, detail="unsupported currency")
        filters["currency"] = currency
    for key, low, high in (
        ("amount", amount_min, amount_max),
        ("paid", paid_min, paid_max),
        ("refunded", refunded_min, refunded_max),
    ):
        for label, value in (("min", low), ("max", high)):
            if value is not None and value < 0:
                raise HTTPException(status_code=400, detail=f"{key}_{label} must be non-negative")
        if low is not None and high is not None and low > high:
            raise HTTPException(status_code=400, detail=f"{key} range is inverted")
        filters[f"{key}_min"], filters[f"{key}_max"] = low, high
    if has_refund is not None:
        if has_refund in ("true", "1"):
            filters["has_refund"] = True
        elif has_refund in ("false", "0"):
            filters["has_refund"] = False
        else:
            raise HTTPException(status_code=400, detail="has_refund must be true or false")
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    response, replayed = orders.search(x_tenant, request_id, filters, cursor, limit)
    return JSONResponse(  # 重放返回首次的同一结果集，保持 200 与首调一致
        response,
        headers={"X-Idempotent-Replay": "1" if replayed else "0"},
    )

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    migrate()
    if args.migrate:
        print("migrated")
        return
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()
