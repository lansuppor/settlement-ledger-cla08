import argparse
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import imports, orders, search
from app.store.db import connect, migrate

app = FastAPI(title="settlement-ledger")

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
    rows: list[Any] = Field(min_length=1)

class SearchIn(BaseModel):
    request_id: str = Field(min_length=1)
    filters: dict | None = None
    page: dict | None = None

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

@app.post("/orders/batch")
def accept_batch(body: BatchIn) -> dict:
    results = imports.accept_batch(body.rows)
    accepted = sum(1 for item in results if item["status"] == "accepted")
    return {"accepted": accepted, "rejected": len(results) - accepted, "results": results}

@app.post("/orders/search")
def search_orders(body: SearchIn, x_tenant: str = Header(default="")) -> JSONResponse:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        filters = order_rules.normalize_filters(body.filters)
        size, cursor = order_rules.normalize_page(body.page)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail=str(error))
    response, replayed = search.run_search(x_tenant, body.request_id, filters, size, cursor)
    response["replayed"] = replayed
    return JSONResponse(  # 重放返回首次的同一结果集，保持 200 与首调一致
        response,
        headers={"X-Idempotent-Replay": "1" if replayed else "0"},
    )

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
