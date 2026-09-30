import math

from app.db import pool
from app.services.inventory import stock_snapshot


def user_status(user_id: int):
    """Read-only, takes no lock, so polling can never slow down buyers."""
    with pool.connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
            return None
        hold_id, expires_at, secs, position, paid = conn.execute(
            """
            WITH h AS (
              SELECT id, expires_at,
                     EXTRACT(EPOCH FROM (expires_at - clock_timestamp())) AS secs
                FROM reservations
               WHERE user_id = %(u)s AND status = 'HELD' AND expires_at > clock_timestamp()
            ), w AS (
              SELECT count(*) AS pos FROM waitlist
               WHERE status = 'WAITING'
                 AND id <= (SELECT id FROM waitlist
                             WHERE user_id = %(u)s AND status = 'WAITING')
            )
            SELECT (SELECT id FROM h), (SELECT expires_at FROM h), (SELECT secs FROM h),
                   (SELECT pos FROM w),
                   (SELECT count(*) FROM reservations
                     WHERE user_id = %(u)s AND status = 'PAID')
            """,
            {"u": user_id},
        ).fetchone()
        stock = stock_snapshot(conn)

    state = "HOLDING" if hold_id else ("WAITING" if position else "NONE")
    return {
        "state": state,
        "hold": {
            "reservation_id": hold_id,
            "expires_at": expires_at.isoformat(),
            "seconds_left": max(0, math.ceil(float(secs))),
        } if hold_id else None,
        "queue_position": position or None,
        "paid": paid,
        "stock": stock,
    }
