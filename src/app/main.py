import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from app.db import pool
from app.services.buy import buy
from app.services.inventory import lock_config, stock_snapshot
from app.services.status import user_status
from app.services.sweeper import sweeper_loop
from app.services.waitlist import join_waitlist, leave_waitlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


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


@app.post("/admin/reset")
def admin_reset(total_stock: int = 20, hold_seconds: int = 300):
    """Test-only. Disabled unless ENABLE_ADMIN=1."""
    if os.environ.get("ENABLE_ADMIN") != "1":
        raise HTTPException(status_code=404)
    with pool.connection() as conn:
        lock_config(conn)  # same lock order as everything else
        conn.execute("DELETE FROM payment_events")
        conn.execute("DELETE FROM waitlist")
        conn.execute("DELETE FROM reservations")
        conn.execute(
            "UPDATE drop_config SET total_stock = %s, hold_seconds = %s",
            (total_stock, hold_seconds),
        )
    return {"ok": True, "total_stock": total_stock, "hold_seconds": hold_seconds}
