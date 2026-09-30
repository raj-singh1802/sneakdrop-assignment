import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.db import pool
from app.services.buy import buy
from app.services.inventory import lock_config, stock_snapshot
from app.services.payments import (
    KNOWN_TYPES, handle_webhook, list_refunds, payment_debug,
    start_payment, submit_to_provider,
)
from app.services.status import user_status
from app.services.sweeper import sweeper_loop
from app.services.waitlist import join_waitlist, leave_waitlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"].encode()


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True)
    task = asyncio.create_task(sweeper_loop(float(os.environ.get("SWEEP_INTERVAL", "1"))))
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    pool.close()


app = FastAPI(title="Sneaker Drop", lifespan=lifespan)

BUY_STATUS = {
    "HELD": 201, "UNKNOWN_USER": 404, "ALREADY_HOLDING": 409,
    "LIMIT_REACHED": 409, "SOLD_OUT": 409,
}
JOIN_STATUS = {
    "WAITING": 201, "UNKNOWN_USER": 404, "ALREADY_HOLDING": 409,
    "LIMIT_REACHED": 409, "ALREADY_WAITING": 409, "STOCK_AVAILABLE": 409,
}
PAY_STATUS = {
    "PAYMENT_STARTED": 202, "UNKNOWN_USER": 404, "NO_ACTIVE_HOLD": 409,
    "PAYMENT_ALREADY_PENDING": 409, "PROVIDER_UNAVAILABLE": 502,
}


def _require_admin():
    if os.environ.get("ENABLE_ADMIN") != "1":
        raise HTTPException(status_code=404)


@app.get("/health")
def health():
    with pool.connection() as conn:
        row = conn.execute("SELECT total_stock FROM drop_config").fetchone()
    return {"ok": True, "total_stock": row[0]}


@app.post("/buy")
def buy_endpoint(x_user_id: int = Header(...)):
    r = buy(x_user_id)
    return JSONResponse(status_code=BUY_STATUS[r["result"]], content=r)


@app.post("/waitlist/join")
def join_endpoint(x_user_id: int = Header(...)):
    r = join_waitlist(x_user_id)
    return JSONResponse(status_code=JOIN_STATUS[r["result"]], content=r)


@app.post("/waitlist/leave")
def leave_endpoint(x_user_id: int = Header(...)):
    r = leave_waitlist(x_user_id)
    return JSONResponse(status_code=200 if r["result"] == "LEFT" else 409, content=r)


@app.post("/pay")
def pay_endpoint(x_user_id: int = Header(...), scenario: str = "random", late_delay: float = 10.0):
    if os.environ.get("ENABLE_ADMIN") != "1":  # demo knobs are test-only
        scenario, late_delay = "random", 10.0
    r = start_payment(x_user_id)
    if r["result"] == "CREATED":
        r = submit_to_provider(r, scenario, late_delay)
    return JSONResponse(status_code=PAY_STATUS[r["result"]], content=r)


def _valid(evt) -> bool:
    return (
        isinstance(evt, dict)
        and isinstance(evt.get("event_id"), str)
        and isinstance(evt.get("payment_id"), str)
        and evt.get("type") in KNOWN_TYPES
        and isinstance(evt.get("seq"), int)
    )


@app.post("/webhooks/payment")
async def payment_webhook(request: Request):
    body = await request.body()
    expected = hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("x-signature", "")):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        evt = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="malformed json")
    if not _valid(evt):
        raise HTTPException(status_code=400, detail="malformed event")
    result = await run_in_threadpool(handle_webhook, evt)
    # 2xx for everything we understood (duplicates, stale, late...) so the provider
    # stops retrying. 404 for an unknown payment. A crash becomes 500 => provider retries.
    return JSONResponse(
        status_code=404 if result["outcome"] == "unknown_payment" else 200,
        content=result,
    )


@app.get("/status")
def status_endpoint(x_user_id: int = Header(...)):
    s = user_status(x_user_id)
    if s is None:
        raise HTTPException(status_code=404, detail="unknown user")
    return s


@app.get("/stock")
def stock():
    with pool.connection() as conn:
        return stock_snapshot(conn)


# ---------- test/demo only (ENABLE_ADMIN=1) ----------
@app.post("/admin/reset")
def admin_reset(total_stock: int = 20, hold_seconds: int = 300):
    _require_admin()
    with pool.connection() as conn:
        lock_config(conn)  # same lock order as everything else
        conn.execute("DELETE FROM payment_events")
        conn.execute("DELETE FROM payments")
        conn.execute("DELETE FROM waitlist")
        conn.execute("DELETE FROM reservations")
        conn.execute(
            "UPDATE drop_config SET total_stock = %s, hold_seconds = %s",
            (total_stock, hold_seconds),
        )
    return {"ok": True, "total_stock": total_stock, "hold_seconds": hold_seconds}


@app.get("/admin/payments/{payment_id}")
def admin_payment(payment_id: str):
    _require_admin()
    d = payment_debug(payment_id)
    if d is None:
        raise HTTPException(status_code=404)
    return d


@app.get("/admin/refunds")
def admin_refunds():
    _require_admin()
    return list_refunds()
