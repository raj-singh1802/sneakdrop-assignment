import asyncio
import hashlib
import hmac
import json
import os
import random
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
SECRET = os.environ["WEBHOOK_SECRET"].encode()
MAX_DELAY = float(os.environ.get("CHAOS_MAX_DELAY", "4"))

app = FastAPI(title="Fake Payment Provider")
payments: dict = {}
_tasks: set = set()  # keep references so tasks aren't garbage collected


class CreatePayment(BaseModel):
    payment_id: str
    amount_cents: int
    scenario: str = "random"
    late_delay: float = 10.0


def schedule_for(scenario: str, late_delay: float):
    """[(delay_seconds, event_type, seq, key)]. The same key listed twice means
    the SAME event (same event_id) is delivered again."""
    if scenario == "clean":
        return [(0.2, "payment.processing", 1, "a"), (0.6, "payment.succeeded", 2, "b")]
    if scenario == "duplicate":
        return [
            (0.2, "payment.processing", 1, "a"),
            (0.6, "payment.succeeded", 2, "b"),
            (0.6, "payment.succeeded", 2, "b"),  # simultaneous double delivery
            (1.8, "payment.succeeded", 2, "b"),  # late replay
        ]
    if scenario == "reordered":  # success arrives BEFORE processing
        return [(0.3, "payment.succeeded", 2, "b"), (1.2, "payment.processing", 1, "a")]
    if scenario == "late":  # success arrives after late_delay seconds
        return [(0.2, "payment.processing", 1, "a"), (late_delay, "payment.succeeded", 2, "b")]
    if scenario == "fail":
        return [(0.2, "payment.processing", 1, "a"), (0.6, "payment.failed", 2, "b")]
    if scenario == "random":  # independent random delays => natural reordering
        sched = [
            (random.uniform(0, MAX_DELAY), "payment.processing", 1, "a"),
            (random.uniform(0, MAX_DELAY), "payment.succeeded", 2, "b"),
        ]
        for d, t, s, k in list(sched):  # ~30% of events are delivered again
            if random.random() < 0.3:
                sched.append((d + random.uniform(0.2, MAX_DELAY), t, s, k))
        return sched
    raise ValueError(scenario)


def sign(body: bytes) -> str:
    return hmac.new(SECRET, body, hashlib.sha256).hexdigest()


async def deliver(event: dict, delay: float):
    await asyncio.sleep(delay)
    body = json.dumps(event, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-Signature": sign(body)}
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(4):  # like a real provider: retry on 5xx / network error
            try:
                r = await client.post(WEBHOOK_URL, content=body, headers=headers)
                if r.status_code < 500:
                    print(f"[provider] {event['type']} {event['event_id']} -> {r.status_code}", flush=True)
                    return
            except httpx.HTTPError as e:
                print(f"[provider] delivery error: {e!r}", flush=True)
            await asyncio.sleep(0.5 * 2**attempt)
    print(f"[provider] gave up on {event['event_id']}", flush=True)


@app.post("/payments", status_code=202)
async def create_payment(body: CreatePayment):
    try:
        plan = schedule_for(body.scenario, body.late_delay)
    except ValueError:
        raise HTTPException(400, f"unknown scenario {body.scenario!r}")

    events: dict = {}  # one event object per key => duplicates are byte-identical
    deliveries = []
    for delay, etype, seq, key in plan:
        ev = events.setdefault(key, {
            "event_id": "evt_" + uuid.uuid4().hex[:16],
            "type": etype,
            "seq": seq,
            "payment_id": body.payment_id,
            "amount_cents": body.amount_cents,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        deliveries.append((delay, ev))

    payments[body.payment_id] = {
        "scenario": body.scenario,
        "deliveries": [
            {"delay": round(d, 2), "event_id": e["event_id"], "type": e["type"], "seq": e["seq"]}
            for d, e in deliveries
        ],
    }
    for delay, ev in deliveries:
        t = asyncio.create_task(deliver(ev, delay))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
    return {"payment_id": body.payment_id, "status": "processing"}


@app.get("/payments/{payment_id}")
def inspect(payment_id: str):
    if payment_id not in payments:
        raise HTTPException(404)
    return payments[payment_id]
