from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.db import pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True)
    yield
    pool.close()


app = FastAPI(title="Sneaker Drop", lifespan=lifespan)


@app.get("/health")
def health():
    with pool.connection() as conn:
        row = conn.execute("SELECT total_stock FROM drop_config").fetchone()
    return {"ok": True, "total_stock": row[0]}
