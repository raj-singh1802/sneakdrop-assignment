import os
import uuid

import httpx

from app.db import pool
from app.services.inventory import count_taken, lock_and_refresh, user_counts

PRICE_CENTS = 15000
PROVIDER_URL = os.environ.get("PROVIDER_URL", "http://provider:9000")
KNOWN_TYPES = {"payment.processing", "payment.succeeded", "payment.failed"}


# ---------- user clicks Pay ----------
def start_payment(user_id: int) -> dict:
    """Short transaction: validate and create a local PENDING payment.
    The provider is called AFTER commit, never while holding the lock."""
    with pool.connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
            return {"result": "UNKNOWN_USER"}
        lock_and_refresh(conn)  # expired holds are gone after this

        row = conn.execute(
            "SELECT id FROM reservations WHERE user_id = %s AND status = 'HELD'",
            (user_id,),
        ).fetchone()
        if row is None:
            return {"result": "NO_ACTIVE_HOLD"}
        rid = row[0]

        if conn.execute(
            "SELECT 1 FROM payments WHERE reservation_id = %s AND status = 'PENDING'",
            (rid,),
        ).fetchone():
            return {"result": "PAYMENT_ALREADY_PENDING"}

        pid = "pay_" + uuid.uuid4().hex[:16]
        conn.execute(
            "INSERT INTO payments (id, reservation_id, user_id, status, amount_cents) "
            "VALUES (%s, %s, %s, 'PENDING', %s)",
            (pid, rid, user_id, PRICE_CENTS),
        )
        return {"result": "CREATED", "payment_id": pid, "reservation_id": rid,
                "amount_cents": PRICE_CENTS}


def submit_to_provider(p: dict, scenario: str, late_delay: float) -> dict:
    try:
        r = httpx.post(
            f"{PROVIDER_URL}/payments",
            json={"payment_id": p["payment_id"], "amount_cents": p["amount_cents"],
                  "scenario": scenario, "late_delay": late_delay},
            timeout=5,
        )
        r.raise_for_status()
    except Exception:
        # Provider unreachable: release the pending slot so the user can retry.
        # AND status='PENDING' means a webhook that already settled it wins.
        with pool.connection() as conn:
            conn.execute(
                "UPDATE payments SET status = 'FAILED', settled_at = now() "
                "WHERE id = %s AND status = 'PENDING'",
                (p["payment_id"],),
            )
        return {"result": "PROVIDER_UNAVAILABLE"}
    return {"result": "PAYMENT_STARTED", "payment_id": p["payment_id"],
            "reservation_id": p["reservation_id"], "amount_cents": p["amount_cents"]}


# ---------- webhook ----------
def handle_webhook(evt: dict) -> dict:
    with pool.connection() as conn:
        cfg = lock_and_refresh(conn)  # same global lock as buy/expiry => no races

        pay = conn.execute(
            "SELECT id, reservation_id, user_id, status, last_seq "
            "FROM payments WHERE id = %s",
            (evt["payment_id"],),
        ).fetchone()
        if pay is None:
            return {"outcome": "unknown_payment"}

        # 1. Idempotency: an event we've already processed changes nothing.
        if conn.execute(
            "SELECT 1 FROM payment_events WHERE event_id = %s", (evt["event_id"],)
        ).fetchone():
            return {"outcome": "duplicate"}

        # 2. State machine. Effect + event record commit together.
        outcome = _apply(conn, cfg, evt, pay)
        conn.execute(
            "INSERT INTO payment_events (event_id, payment_id, type, seq, outcome) "
            "VALUES (%s, %s, %s, %s, %s)",
            (evt["event_id"], evt["payment_id"], evt["type"], evt["seq"], outcome),
        )
        return {"outcome": outcome}


def _apply(conn, cfg, evt, pay) -> str:
    pid, rid, uid, status, last_seq = pay
    etype, seq = evt["type"], evt["seq"]

    if etype == "payment.processing":
        # Informational. Never moves a settled payment backwards.
        if status != "PENDING" or seq <= last_seq:
            return "ignored_stale"
        conn.execute("UPDATE payments SET last_seq = %s WHERE id = %s", (seq, pid))
        return "applied"

    if etype == "payment.failed":
        if status != "PENDING":
            return "ignored_already_settled"
        conn.execute(
            "UPDATE payments SET status = 'FAILED', settled_at = now(), "
            "last_seq = GREATEST(last_seq, %s) WHERE id = %s",
            (seq, pid),
        )
        return "applied_failed"  # reservation stays HELD; user may retry within the hold

    # payment.succeeded
    if status == "SUCCEEDED":
        return "ignored_already_settled"  # same payment, new event id: still idempotent
    if status == "FAILED":
        _flag_refund(conn, pid)  # money moved but we had written it off
        return "succeeded_after_failed_refund_needed"

    conn.execute(
        "UPDATE payments SET status = 'SUCCEEDED', settled_at = now(), "
        "last_seq = GREATEST(last_seq, %s) WHERE id = %s",
        (seq, pid),
    )
    return _fulfil(conn, cfg, pid, rid, uid)


def _fulfil(conn, cfg, pid, rid, uid) -> str:
    total, _, max_per_user = cfg
    (res_status,) = conn.execute(
        "SELECT status FROM reservations WHERE id = %s", (rid,)
    ).fetchone()

    if res_status == "HELD":
        conn.execute(
            "UPDATE reservations SET status = 'PAID', paid_at = clock_timestamp() WHERE id = %s",
            (rid,),
        )
        return "applied_paid"

    if res_status == "PAID":
        _flag_refund(conn, pid)
        return "already_paid_refund_needed"

    # EXPIRED: the confirmation reached us after the hold ran out.
    # Honor it only if a pair is still free (which, by the queue invariant, also
    # means nobody is waiting) and the user is still under the limit.
    _, owned = user_counts(conn, uid)
    if total - count_taken(conn) > 0 and owned < max_per_user:
        conn.execute(
            "UPDATE reservations SET status = 'PAID', paid_at = clock_timestamp() WHERE id = %s",
            (rid,),
        )
        return "applied_paid_after_expiry"

    _flag_refund(conn, pid)  # pair already went to someone else
    return "late_refund_needed"


def _flag_refund(conn, pid):
    conn.execute("UPDATE payments SET refund_needed = true WHERE id = %s", (pid,))


# ---------- read-only debug views (admin only, used by tests and the video) ----------
def payment_debug(pid: str):
    with pool.connection() as conn:
        p = conn.execute(
            "SELECT id, reservation_id, user_id, status, amount_cents, refund_needed, last_seq "
            "FROM payments WHERE id = %s",
            (pid,),
        ).fetchone()
        if p is None:
            return None
        rstatus = conn.execute(
            "SELECT status FROM reservations WHERE id = %s", (p[1],)
        ).fetchone()[0]
        events = conn.execute(
            "SELECT event_id, type, seq, outcome FROM payment_events "
            "WHERE payment_id = %s ORDER BY received_at",
            (pid,),
        ).fetchall()
    return {
        "payment_id": p[0], "reservation_id": p[1], "user_id": p[2], "status": p[3],
        "amount_cents": p[4], "refund_needed": p[5], "last_seq": p[6],
        "reservation_status": rstatus,
        "events": [{"event_id": e[0], "type": e[1], "seq": e[2], "outcome": e[3]} for e in events],
    }


def list_refunds():
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, reservation_id, user_id, amount_cents FROM payments "
            "WHERE refund_needed ORDER BY created_at"
        ).fetchall()
    return [{"payment_id": r[0], "reservation_id": r[1], "user_id": r[2],
             "amount_cents": r[3]} for r in rows]
