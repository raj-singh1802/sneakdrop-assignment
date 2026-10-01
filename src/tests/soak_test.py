import argparse
import asyncio
import random
import sys
import time
from collections import Counter

import httpx

BASE = "http://localhost:8000"
SCENARIOS = ["random", "clean", "duplicate", "reordered", "fail", "late"]
PAY_CHANCE = 0.007  # per loop while holding; low enough that stock keeps churning all run


async def post(c, uid, path, counters, **params):
    try:
        r = await c.post(path, headers={"X-User-Id": str(uid)}, params=params or None)
    except httpx.TransportError:
        counters["client_net_error"] += 1
        return None
    if r.status_code >= 500:
        counters["SERVER_5XX"] += 1
        return f"HTTP_{r.status_code}"
    try:
        return r.json().get("result", f"HTTP_{r.status_code}")
    except Exception:
        return f"HTTP_{r.status_code}"


async def get_status(c, uid, counters):
    try:
        r = await c.get("/status", headers={"X-User-Id": str(uid)})
        if r.status_code >= 500:
            counters["SERVER_5XX"] += 1
            return None
        return r.json()
    except (httpx.TransportError, ValueError):
        counters["client_net_error"] += 1
        return None


async def bot(c, uid, end, hold, seed, counters):
    rng = random.Random(seed * 100000 + uid)
    while time.monotonic() < end:
        await asyncio.sleep(rng.uniform(0.02, 0.4))
        s = await get_status(c, uid, counters)
        if s is None:
            continue
        roll = rng.random()
        if s["state"] == "NONE":
            res = await post(c, uid, "/buy", counters)
            counters[f"buy:{res}"] += 1
            if res == "SOLD_OUT" and rng.random() < 0.8:
                counters[f"join:{await post(c, uid, '/waitlist/join', counters)}"] += 1
        elif s["state"] == "HOLDING":
            if s["payment_status"] == "PENDING":
                continue
            if roll < PAY_CHANCE:
                scen = rng.choice(SCENARIOS)
                res = await post(c, uid, "/pay", counters, scenario=scen,
                                 late_delay=round(rng.uniform(1, hold + 3), 1))
                counters[f"pay[{scen}]:{res}"] += 1
            elif roll < PAY_CHANCE + 0.002:  # double-click on Pay
                rs = await asyncio.gather(*[post(c, uid, "/pay", counters, scenario="clean") for _ in range(2)])
                for res in rs:
                    counters[f"pay[double-click]:{res}"] += 1
            elif roll < 0.02:
                counters[f"buy-while-holding:{await post(c, uid, '/buy', counters)}"] += 1
        else:  # WAITING
            if roll < 0.02:
                counters[f"leave:{await post(c, uid, '/waitlist/leave', counters)}"] += 1
            elif roll < 0.05:
                counters[f"buy-while-waiting:{await post(c, uid, '/buy', counters)}"] += 1


async def reset(c, stock, hold):
    for attempt in range(3):
        try:
            (await c.post("/admin/reset", params={"total_stock": stock, "hold_seconds": hold})).raise_for_status()
            return
        except httpx.TransportError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.3)


async def auditor(c, stop, state):
    while not stop.is_set():
        try:
            r = await c.get("/admin/audit")
            if r.status_code != 200:
                state["audit_errors"] += 1
            else:
                a = r.json()
                state["samples"] += 1
                if not a["ok"]:
                    state["violations"].append(a["violations"])
        except (httpx.TransportError, ValueError):
            pass
        await asyncio.sleep(0.15)


async def main(a):
    limits = httpx.Limits(max_connections=150)
    async with httpx.AsyncClient(base_url=BASE, timeout=60, limits=limits) as c:
        await reset(c, a.stock, a.hold)
        counters = Counter()
        state = {"samples": 0, "violations": [], "audit_errors": 0}
        stop = asyncio.Event()
        end = time.monotonic() + a.seconds
        print(f"SOAK: {a.seconds}s, {a.users} users, {a.stock} pairs, {a.hold}s holds, seed {a.seed}")

        aud = asyncio.create_task(auditor(c, stop, state))
        await asyncio.gather(*[bot(c, u, end, a.hold, a.seed, counters) for u in range(1, a.users + 1)])

        print("bots stopped; waiting for in-flight payments to settle...")
        final = None
        for _ in range(60):
            r = await c.get("/admin/audit", params={"stats": 1})
            r.raise_for_status()
            final = r.json()
            if final["stats"]["payments_pending"] == 0:
                break
            await asyncio.sleep(0.5)
        stop.set()
        await aud

        s = final["stats"]
        print(f"\nauditor: {state['samples']} snapshots checked, {len(state['violations'])} with violations")
        for v in state["violations"][:3]:
            print("  VIOLATION:", {k: n for k, n in v.items() if n})
        print("\nclient actions:")
        for k in sorted(counters):
            print(f"  {k:42s} {counters[k]}")
        print("\nfinal state:", s)
        print("payment event outcomes:", final["outcomes"])

        checks = {
            "no invariant violated in any snapshot": not state["violations"] and final["ok"],
            "auditor ran (>10 snapshots, no audit errors)": state["samples"] > 10 and state["audit_errors"] == 0,
            "no 5xx from the server": counters["SERVER_5XX"] == 0,
            "inventory really turned over (run was not vacuous)": s["reservations_total"] > a.stock,
        }
        print()
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

        await reset(c, 20, 300)  # leave the DB in its real starting state
        sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=int, default=45)
    p.add_argument("--users", type=int, default=200)
    p.add_argument("--stock", type=int, default=20)
    p.add_argument("--hold", type=int, default=3)
    p.add_argument("--seed", type=int, default=1)
    asyncio.run(main(p.parse_args()))
