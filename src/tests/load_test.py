import asyncio
import sys
from collections import Counter

import httpx

BASE = "http://localhost:8000"


async def buy(client, uid, sem):
    async with sem:
        try:
            r = await client.post("/buy", headers={"X-User-Id": str(uid)})
            try:
                return r.json().get("result", f"HTTP_{r.status_code}")
            except Exception:
                return f"HTTP_{r.status_code}"
        except Exception as e:
            return f"ERR_{type(e).__name__}"


async def main(n):
    limits = httpx.Limits(max_connections=500)
    async with httpx.AsyncClient(base_url=BASE, timeout=120, limits=limits) as c:
        sem = asyncio.Semaphore(500)
        ok = True

        # Test A: n different users click Buy at once -> exactly 20 holds
        (await c.post("/admin/reset")).raise_for_status()
        res = Counter(await asyncio.gather(*[buy(c, u, sem) for u in range(1, n + 1)]))
        s = (await c.get("/stock")).json()
        print(f"[A] {n} users, one click each: {dict(res)}")
        print(f"[A] stock: {s}")
        a_ok = res["HELD"] == 20 and s["held"] == 20 and s["available"] == 0
        print("[A]", "PASS" if a_ok else "FAIL")
        ok = ok and a_ok

        # Test B: one user hammers Buy 50 times at once -> exactly 1 hold
        (await c.post("/admin/reset")).raise_for_status()
        res = Counter(await asyncio.gather(*[buy(c, 1, sem) for _ in range(50)]))
        print(f"[B] 1 user, 50 clicks: {dict(res)}")
        b_ok = res["HELD"] == 1 and res["ALREADY_HOLDING"] == 49
        print("[B]", "PASS" if b_ok else "FAIL")
        ok = ok and b_ok

        (await c.post("/admin/reset")).raise_for_status()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2000))
