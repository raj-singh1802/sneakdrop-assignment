import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from collections import Counter

import httpx

SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret-change-me").encode()
results = []


def H(u):
    return {"X-User-Id": str(u)}


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"   <- {detail}"))


async def reset(c, stock=5, hold=300):
    (await c.post("/admin/reset", params={"total_stock": stock, "hold_seconds": hold})).raise_for_status()


async def buy(c, u):
    return (await c.post("/buy", headers=H(u))).json()["result"]


async def pay(c, u, scenario="clean", late_delay=10):
    r = await c.post("/pay", headers=H(u), params={"scenario": scenario, "late_delay": late_delay})
    return r.json()


async def status(c, u):
    return (await c.get("/status", headers=H(u))).json()


async def info(c, pid):
    return (await c.get(f"/admin/payments/{pid}")).json()


async def wait_for(fn, timeout=15, every=0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = await fn()
        if v:
            return v
        await asyncio.sleep(every)
    return None


def outcomes(i):
    return {e["type"]: e["outcome"] for e in i["events"]}


async def owns(c, u, n=1):
    return (await status(c, u))["paid"] >= n


async def settled(c, pid):
    return (await info(c, pid))["status"] in ("SUCCEEDED", "FAILED")


async def main():
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30) as c, \
               httpx.AsyncClient(base_url="http://localhost:9000", timeout=30) as prov:

        print("1. clean payment")
        await reset(c)
        check("buy", await buy(c, 1) == "HELD")
        p = await pay(c, 1, "clean")
        check("pay accepted", p["result"] == "PAYMENT_STARTED", p)
        check("user ends up owning 1 pair", await wait_for(lambda: owns(c, 1)))
        i = await info(c, p["payment_id"])
        check("outcomes applied + applied_paid",
              outcomes(i) == {"payment.processing": "applied", "payment.succeeded": "applied_paid"}, outcomes(i))
        s = (await c.get("/stock")).json()
        check("stock paid=1 held=0", s["paid"] == 1 and s["held"] == 0, s)

        print("2. duplicate webhooks (succeeded delivered 3 times)")
        await reset(c)
        await buy(c, 2)
        p = await pay(c, 2, "duplicate")
        await wait_for(lambda: owns(c, 2))
        await asyncio.sleep(3)  # let the late replay land too
        i = await info(c, p["payment_id"])
        sent = (await prov.get(f"/payments/{p['payment_id']}")).json()["deliveries"]
        check("provider really sent 4 messages", len(sent) == 4, len(sent))
        check("app recorded only 2 distinct events", len(i["events"]) == 2, i["events"])
        s = (await c.get("/stock")).json()
        check("paid exactly once", s["paid"] == 1 and (await status(c, 2))["paid"] == 1, s)

        print("3. out-of-order (succeeded arrives before processing)")
        await reset(c)
        await buy(c, 3)
        p = await pay(c, 3, "reordered")
        await wait_for(lambda: owns(c, 3))
        await asyncio.sleep(1.5)
        i = await info(c, p["payment_id"])
        check("succeeded applied, late processing ignored as stale",
              outcomes(i) == {"payment.succeeded": "applied_paid", "payment.processing": "ignored_stale"},
              outcomes(i))
        check("payment stayed SUCCEEDED", i["status"] == "SUCCEEDED", i["status"])

        print("4. late success, hold expired, pair NOT reassigned -> honored")
        await reset(c, stock=5, hold=3)
        await buy(c, 4)
        p = await pay(c, 4, "late", late_delay=6)
        await asyncio.sleep(4.5)
        s4 = await status(c, 4)
        check("hold expired meanwhile", s4["state"] == "NONE" and s4["paid"] == 0, s4)
        check("late success still buys the pair", await wait_for(lambda: owns(c, 4), timeout=10))
        i = await info(c, p["payment_id"])
        check("outcome applied_paid_after_expiry",
              outcomes(i).get("payment.succeeded") == "applied_paid_after_expiry", outcomes(i))

        print("5. late success, pair already given to the next in line -> refund flagged")
        await reset(c, stock=1, hold=4)
        await buy(c, 5)
        p = await pay(c, 5, "late", late_delay=6.5)
        j = (await c.post("/waitlist/join", headers=H(6))).json()
        check("user 6 joined the queue", j["result"] == "WAITING", j)
        check("user 6 got promoted when 5's hold expired",
              await wait_for(lambda: _holding(c, 6), timeout=10))
        check("payment settles", await wait_for(lambda: settled(c, p["payment_id"]), timeout=15))
        i = await info(c, p["payment_id"])
        check("outcome late_refund_needed + refund flag",
              outcomes(i).get("payment.succeeded") == "late_refund_needed" and i["refund_needed"], outcomes(i))
        s5, s6 = await status(c, 5), await status(c, 6)
        check("user 5 did NOT get a pair, user 6 still holds it",
              s5["paid"] == 0 and s6["state"] == "HOLDING", (s5["paid"], s6["state"]))
        st = (await c.get("/stock")).json()
        check("never more than 1 pair out", st["held"] + st["paid"] <= 1, st)
        refunds = (await c.get("/admin/refunds")).json()
        check("listed in /admin/refunds", any(r["payment_id"] == p["payment_id"] for r in refunds), refunds)

        print("6. failed payment keeps the hold; retry succeeds")
        await reset(c)
        await buy(c, 7)
        p1 = await pay(c, 7, "fail")
        check("payment settles as FAILED",
              await wait_for(lambda: _is(c, p1["payment_id"], "FAILED")))
        s7 = await status(c, 7)
        check("still holding, payment_status FAILED",
              s7["state"] == "HOLDING" and s7["payment_status"] == "FAILED", s7)
        p2 = await pay(c, 7, "clean")
        check("retry accepted", p2["result"] == "PAYMENT_STARTED", p2)
        check("retry pays", await wait_for(lambda: owns(c, 7)))

        print("7. double-click on Pay")
        await reset(c)
        await buy(c, 8)
        rs = await asyncio.gather(pay(c, 8, "clean"), pay(c, 8, "clean"))
        cnt = Counter(r["result"] for r in rs)
        check("exactly one payment created", cnt == {"PAYMENT_STARTED": 1, "PAYMENT_ALREADY_PENDING": 1}, dict(cnt))
        await wait_for(lambda: owns(c, 8))
        check("paid once", (await status(c, 8))["paid"] == 1)

        print("8. webhook security / validation")
        body = json.dumps({"event_id": "evt_x", "type": "payment.succeeded", "seq": 2,
                           "payment_id": "pay_nope"}).encode()
        r = await c.post("/webhooks/payment", content=body, headers={"X-Signature": "deadbeef"})
        check("bad signature -> 401", r.status_code == 401, r.status_code)
        sig = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        r = await c.post("/webhooks/payment", content=body, headers={"X-Signature": sig})
        check("valid signature, unknown payment -> 404", r.status_code == 404, r.status_code)
        junk = b"not json"
        r = await c.post("/webhooks/payment", content=junk,
                         headers={"X-Signature": hmac.new(SECRET, junk, hashlib.sha256).hexdigest()})
        check("signed garbage -> 400", r.status_code == 400, r.status_code)

        await reset(c)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


async def _holding(c, u):
    return (await status(c, u))["state"] == "HOLDING"


async def _is(c, pid, st):
    return (await info(c, pid))["status"] == st


if __name__ == "__main__":
    asyncio.run(main())
