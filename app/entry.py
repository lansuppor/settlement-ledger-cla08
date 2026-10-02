import argparse
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.rules import order_rules, refund_rules
from app.store import imports, orders, refund_orders, refund_search, search
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

class VoidIn(BaseModel):
    biz_id: str = Field(min_length=1)

class WriteoffIn(BaseModel):
    biz_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class CorrectionIn(BaseModel):
    biz_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class CorrectionCancelIn(BaseModel):
    biz_id: str = Field(min_length=1)
    correction_biz_id: str = Field(min_length=1)

class BatchIn(BaseModel):
    rows: list[Any] = Field(min_length=1)

class SearchIn(BaseModel):
    request_id: str = Field(min_length=1)
    filters: dict | None = None
    page: dict | None = None

class RefundOrderIn(BaseModel):
    refund_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    requested_amount_cents: int = Field(gt=0)

class RefundExecuteIn(BaseModel):
    outcome: Literal["succeeded", "failed"]
    amount_cents: int | None = Field(default=None, gt=0)  # 实退金额，缺省取申请金额
    reason: str | None = None

    @model_validator(mode="after")
    def _check_failure_reason(self) -> "RefundExecuteIn":
        if self.outcome == "failed" and not (isinstance(self.reason, str) and self.reason.strip()):
            raise ValueError("failure reason is required")
        return self

class RefundReverseIn(BaseModel):
    # 本次冲正的业务标识：租户内唯一，标识冲正操作本身，与退款单标识、原订单标识
    # 及订单侧冲正、作废、冲销、冲正修正、取消修正的业务标识独立，不得混用。
    biz_id: str = Field(min_length=1)

class RefundSearchIn(BaseModel):
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

@app.post("/orders/{order_id}/void")
def void_order(order_id: str, body: VoidIn, x_tenant: str = Header(default="")) -> JSONResponse:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        void, replayed = orders.void_order(x_tenant, order_id, body.biz_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="order not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    return JSONResponse(  # 重放返回同一记录，保持 200 与首调一致
        void,
        headers={"X-Idempotent-Replay": "1" if replayed else "0"},
    )

@app.post("/orders/{order_id}/writeoffs")
def add_writeoff(order_id: str, body: WriteoffIn, x_tenant: str = Header(default="")) -> JSONResponse:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        writeoff, replayed = orders.add_writeoff(x_tenant, order_id, body.biz_id, body.amount_cents)
    except LookupError:
        raise HTTPException(status_code=404, detail="order not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    return JSONResponse(  # 重放返回同一记录，保持 200 与首调一致
        writeoff,
        headers={"X-Idempotent-Replay": "1" if replayed else "0"},
    )

@app.post("/orders/{order_id}/corrections")
def add_correction(order_id: str, body: CorrectionIn, x_tenant: str = Header(default="")) -> JSONResponse:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        correction, replayed = orders.add_correction(x_tenant, order_id, body.biz_id, body.amount_cents)
    except LookupError:
        raise HTTPException(status_code=404, detail="order not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    return JSONResponse(  # 重放返回同一记录，保持 200 与首调一致
        correction,
        headers={"X-Idempotent-Replay": "1" if replayed else "0"},
    )

@app.post("/orders/{order_id}/correction-cancels")
def cancel_correction(order_id: str, body: CorrectionCancelIn, x_tenant: str = Header(default="")) -> JSONResponse:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        cancel, replayed = orders.cancel_correction(
            x_tenant, order_id, body.biz_id, body.correction_biz_id
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="order not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    return JSONResponse(  # 重放返回同一记录，保持 200 与首调一致
        cancel,
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

# ---------- 退款单（refund order） ----------

@app.post("/refund-orders", status_code=201)
def accept_refund_order(body: RefundOrderIn, x_tenant: str = Header(default="")) -> JSONResponse:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        refund, replayed = refund_orders.accept(
            x_tenant, body.refund_id, body.order_id, body.requested_amount_cents
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="order not found")
    except ValueError as error:
        # 退款单标识冲突（本单据重复但参数不一致，或与订单侧业务标识冲突）：拒绝且不改任何数据
        raise HTTPException(status_code=409, detail=str(error))
    # 重放返回首次的同一单据，保持 200 与首调 201 可区分并以响应头标记重放
    return JSONResponse(
        refund,
        status_code=200 if replayed else 201,
        headers={"X-Idempotent-Replay": "1" if replayed else "0"},
    )

@app.get("/refund-orders/{refund_id}")
def read_refund_order(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    found = refund_orders.get(x_tenant, refund_id)
    if found is None:
        raise HTTPException(status_code=404, detail="refund order not found")
    refund, _ = found
    refund.pop("replayed", None)  # 读取非幂等动作，响应不带重放标记
    return refund

def _refund_lifecycle_call(
    x_tenant: str, refund_id: str, func, *args
) -> JSONResponse:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        refund, replayed = func(x_tenant, refund_id, *args)
    except LookupError:
        raise HTTPException(status_code=404, detail="refund order not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    return JSONResponse(  # 重放（重复执行到账）返回同一结果，保持 200 与首调一致
        refund,
        headers={"X-Idempotent-Replay": "1" if replayed else "0"},
    )

@app.post("/refund-orders/{refund_id}/approve")
def approve_refund_order(refund_id: str, x_tenant: str = Header(default="")) -> JSONResponse:
    return _refund_lifecycle_call(x_tenant, refund_id, refund_orders.review, True)

@app.post("/refund-orders/{refund_id}/reject")
def reject_refund_order(refund_id: str, x_tenant: str = Header(default="")) -> JSONResponse:
    return _refund_lifecycle_call(x_tenant, refund_id, refund_orders.review, False)

@app.post("/refund-orders/{refund_id}/execute")
def execute_refund_order(
    refund_id: str, body: RefundExecuteIn, x_tenant: str = Header(default="")
) -> JSONResponse:
    return _refund_lifecycle_call(
        x_tenant, refund_id, refund_orders.execute,
        body.outcome == "succeeded", body.amount_cents, body.reason,
    )

@app.post("/refund-orders/{refund_id}/cancel")
def cancel_refund_order(refund_id: str, x_tenant: str = Header(default="")) -> JSONResponse:
    return _refund_lifecycle_call(x_tenant, refund_id, refund_orders.cancel)

@app.post("/refund-orders/{refund_id}/reverse")
def reverse_refund_order(
    refund_id: str, body: RefundReverseIn, x_tenant: str = Header(default="")
) -> JSONResponse:
    # 执行冲正：仅已到账退款单可冲正，金额取已落库实退金额，业务标识由请求给出。
    # 重放（同租户同冲正业务标识）返回首次同一记录，404/409 与重放头沿用既有口径。
    return _refund_lifecycle_call(x_tenant, refund_id, refund_orders.reverse, body.biz_id)

@app.post("/refund-orders/search")
def search_refund_orders(
    body: RefundSearchIn, x_tenant: str = Header(default="")
) -> JSONResponse:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        filters = refund_rules.normalize_filters(body.filters)
        size, cursor = refund_rules.normalize_page(body.page)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail=str(error))
    response, replayed = refund_search.run_search(
        x_tenant, body.request_id, filters, size, cursor
    )
    response["replayed"] = replayed
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
