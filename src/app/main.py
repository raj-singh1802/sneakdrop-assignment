import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from app.db import pool
from app.services.buy import buy


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True)
    yield
    pool.close()


app = FastAPI(title="Sneaker Drop", lifespan=lifespan)

BUY_STATUS = {
    "HELD": 201,
    "UNKNOWN_USER": 404,
    "ALREADY_HOLDING": 409,
    "LIMIT_REACHED": 409,
    "SOLD_OUT": 409,
    "QUEUE_HAS_PRIORITY": 409,
}


@app.get("/health")
def health():
    with pool.connection() as conn:
        row = conn.execute("SELECT total_stock FROM drop_config").fetchone()
    return {"ok": True, "total_stock": row[0]}


@app.post("/buy")
def buy_endpoint(x_user_id: int = Header(...)):
    result = buy(x_user_id)
    return JSONResponse(status_code=BUY_STATUS[result["result"]], content=result)


@app.get("/stock")
def stock():
    with pool.connection() as conn:
        total = conn.execute("SELECT total_stock FROM drop_config").fetchone()[0]
        held, paid = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'HELD' AND expires_at > clock_timestamp()),
                   count(*) FILTER (WHERE status = 'PAID')
              FROM reservations
            """
        ).fetchone()
    return {"total": total, "held": held, "paid": paid, "available": total - held - paid}


@app.post("/admin/reset")
def admin_reset():
    """Test-only: wipes reservations/waitlist. Disabled unless ENABLE_ADMIN=1."""
    if os.environ.get("ENABLE_ADMIN") != "1":
        raise HTTPException(status_code=404)
    with pool.connection() as conn:
        conn.execute("TRUNCATE payment_events, waitlist, reservations RESTART IDENTITY")
    return {"ok": True}
