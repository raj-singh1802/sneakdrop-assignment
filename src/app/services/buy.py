from app.db import pool
from app.services.inventory import expire_overdue


def buy(user_id: int) -> dict:
    # pool.connection() opens a transaction and commits on normal exit.
    # We RETURN results instead of raising, so the lazy-expiry work
    # is committed even when the buy itself is rejected.
    with pool.connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
            return {"result": "UNKNOWN_USER"}

        # 1. Serialize all buyers on the single config row
        total, hold_seconds, max_per_user = conn.execute(
            "SELECT total_stock, hold_seconds, max_per_user "
            "FROM drop_config WHERE id = 1 FOR UPDATE"
        ).fetchone()

        # 2. Lazy expiry (inside the lock)
        expire_overdue(conn)

        # 3. Per-user rules
        held_now, owned = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'HELD'),
                   count(*) FILTER (WHERE status IN ('HELD','PAID'))
              FROM reservations WHERE user_id = %s
            """,
            (user_id,),
        ).fetchone()
        if held_now > 0:
            return {"result": "ALREADY_HOLDING"}
        if owned >= max_per_user:
            return {"result": "LIMIT_REACHED"}

        # 4. Stock = total - (live holds + paid), counted under the lock
        taken = conn.execute(
            "SELECT count(*) FROM reservations WHERE status IN ('HELD','PAID')"
        ).fetchone()[0]
        if total - taken <= 0:
            return {"result": "SOLD_OUT"}

        # Free stock while people are waiting belongs to the queue, not to new buyers
        if conn.execute(
            "SELECT EXISTS (SELECT 1 FROM waitlist WHERE status = 'WAITING')"
        ).fetchone()[0]:
            return {"result": "QUEUE_HAS_PRIORITY"}

        # 5. Create the hold
        rid, expires_at = conn.execute(
            """
            INSERT INTO reservations (user_id, status, expires_at)
            VALUES (%s, 'HELD', clock_timestamp() + make_interval(secs => %s))
            RETURNING id, expires_at
            """,
            (user_id, hold_seconds),
        ).fetchone()
        return {
            "result": "HELD",
            "reservation_id": rid,
            "expires_at": expires_at.isoformat(),
            "hold_seconds": hold_seconds,
        }
