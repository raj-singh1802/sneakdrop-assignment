import asyncio
import sys
import time
from collections import Counter

import httpx

BASE = "http://localhost:8000"


def hdr(uid):
    return {"X-User-Id": str(uid)}


async def buy(client, uid, sem, retries=2):
    async with sem:
        for attempt in range(retries + 1):
            try:
                r = await client.post("/buy", headers=hdr(uid))
                try:
                    return r.json().get("result", f"HTTP_{r.status_code}")
                except Exception:
                    return f"HTTP_{r.status_code}"
            except httpx.TransportError as e:
                if attempt == retries:
                    return f"ERR_{type(e).__name__}"
                await asyncio.sleep(0.2)


async def join(client, uid, sem):
    async with sem:
        r = await client.post("/waitlist/join", headers=hdr(uid))
        return uid, r.json()


async def status(client, uid):
    return (await client.get("/status", headers=hdr(uid))).json()


async def reset(client, **params):
    for attempt in range(3):
        try:
            (await client.post("/admin/reset", params=params)).raise_for_status()
            return
        except httpx.TransportError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.3)


async def main(n):
    limits = httpx.Limits(max_connections=500)
    async with httpx.AsyncClient(base_url=BASE, timeout=120, limits=limits) as c:
        sem = asyncio.Semaphore(500)
        ok = True

        # Test A: n users click Buy at once -> exactly 20 holds
        await reset(c)
        res = Counter(await asyncio.gather(*[buy(c, u, sem) for u in range(1, n + 1)]))
        s = (await c.get("/stock")).json()
        print(f"[A] {n} users, one click each: {dict(res)}")
        print(f"[A] stock: {s}")
        holders = res["HELD"] + res["ALREADY_HOLDING"]  # ALREADY_HOLDING only appears if a retry follows an attempt that did succeed
        errors = sum(v for k, v in res.items() if k.startswith(("ERR_", "HTTP_")))
        a_ok = (holders == 20 and res["SOLD_OUT"] == n - 20 and errors == 0
                and s["held"] == 20 and s["available"] == 0)
        print("[A]", "PASS" if a_ok else "FAIL")
        ok = ok and a_ok

        # Test B: one user clicks Buy 50 times at once -> exactly 1 hold
        await reset(c)
        res = Counter(await asyncio.gather(*[buy(c, 1, sem) for _ in range(50)]))
        print(f"[B] 1 user, 50 clicks: {dict(res)}")
        b_ok = res["HELD"] == 1 and res["ALREADY_HOLDING"] == 49
        print("[B]", "PASS" if b_ok else "FAIL")
        ok = ok and b_ok

        # Test C: queue + automatic promotion (8s holds, takes ~11s)
        await reset(c, total_stock=5, hold_seconds=8)
        res = Counter(await asyncio.gather(*[buy(c, u, sem) for u in range(1, 6)]))
        t0 = time.monotonic()
        waiters = list(range(101, 201))
        joins = await asyncio.gather(*[join(c, u, sem) for u in waiters])
        pos = {u: j.get("position") for u, j in joins}
        all_waiting = all(j["result"] == "WAITING" for _, j in joins)
        unique_pos = sorted(pos.values()) == list(range(1, 101))
        holder_join = (await c.post("/waitlist/join", headers=hdr(1))).json()["result"]
        print(f"[C] holds: {dict(res)} | 100 joined: {all_waiting} | "
              f"positions 1..100 unique: {unique_pos} | holder joins: {holder_join}")

        # first 5 holds expire at ~8s, sweeper promotes within ~1s
        await asyncio.sleep(max(0, t0 + 10.5 - time.monotonic()))
        stats = dict(zip(waiters, await asyncio.gather(*[status(c, u) for u in waiters])))
        old = await asyncio.gather(*[status(c, u) for u in range(1, 6)])
        holding = sorted(u for u, st in stats.items() if st["state"] == "HOLDING")
        first5 = sorted(u for u, p in pos.items() if p <= 5)
        shifted = all(st["queue_position"] == pos[u] - 5
                      for u, st in stats.items() if st["state"] == "WAITING")
        s = (await c.get("/stock")).json()
        print(f"[C] promoted users: {holding}")
        print(f"[C] stock: {s}")
        c_ok = (res["HELD"] == 5 and all_waiting and unique_pos
                and holder_join == "ALREADY_HOLDING"
                and holding == first5 and shifted
                and all(o["state"] != "HOLDING" for o in old)
                and s["held"] == 5 and s["available"] == 0)
        print("[C]", "PASS" if c_ok else "FAIL")
        ok = ok and c_ok

        await reset(c, total_stock=20)  # back to 20 pairs / 300s
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2000))
