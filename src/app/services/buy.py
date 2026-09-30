from app.db import pool
from app.services.inventory import count_taken, lock_and_refresh, user_counts


def buy(user_id: int) -> dict:
    # Return results instead of raising, so the expiry/promotion work done
    # under the lock is committed even when this buy is rejected.
    with pool.connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
            return {"result": "UNKNOWN_USER"}

        total, hold_seconds, max_per_user = lock_and_refresh(conn)

        held_now, owned = user_counts(conn, user_id)
        if held_now > 0:
            return {"result": "ALREADY_HOLDING"}
        if owned >= max_per_user:
            return {"result": "LIMIT_REACHED"}

        # Invariant: after lock_and_refresh, free stock means an empty queue,
        # so a new buyer can never jump ahead of waiters.
        if total - count_taken(conn) <= 0:
            return {"result": "SOLD_OUT", "hint": "POST /waitlist/join"}

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
