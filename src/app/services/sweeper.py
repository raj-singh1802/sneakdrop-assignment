import asyncio
import logging

from app.db import pool
from app.services.inventory import expire_and_promote, lock_config

log = logging.getLogger("sweeper")


def sweep_once():
    with pool.connection() as conn:
        cfg = lock_config(conn)
        return expire_and_promote(conn, cfg)


async def sweeper_loop(interval: float):
    while True:
        try:
            ev = await asyncio.to_thread(sweep_once)
            if ev["expired"] or ev["promoted"] or ev["skipped"]:
                log.info(
                    "expired=%s promoted=%s skipped=%s",
                    [u for _, u in ev["expired"]], ev["promoted"], ev["skipped"],
                )
        except Exception:
            log.exception("sweeper failed")  # never let the loop die
        await asyncio.sleep(interval)
