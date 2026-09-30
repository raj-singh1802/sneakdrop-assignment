from app.db import pool
from app.services.inventory import count_taken, lock_and_refresh, user_counts


def join_waitlist(user_id: int) -> dict:
    with pool.connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
            return {"result": "UNKNOWN_USER"}

        total, _, max_per_user = lock_and_refresh(conn)

        held_now, owned = user_counts(conn, user_id)
        if held_now > 0:
            return {"result": "ALREADY_HOLDING"}
        if owned >= max_per_user:
            return {"result": "LIMIT_REACHED"}  # can never get another pair
        if conn.execute(
            "SELECT 1 FROM waitlist WHERE user_id = %s AND status = 'WAITING'",
            (user_id,),
        ).fetchone():
            return {"result": "ALREADY_WAITING"}
        if total - count_taken(conn) > 0:
            return {"result": "STOCK_AVAILABLE", "hint": "POST /buy"}

        wid = conn.execute(
            "INSERT INTO waitlist (user_id, status) VALUES (%s, 'WAITING') RETURNING id",
            (user_id,),
        ).fetchone()[0]
        position = conn.execute(
            "SELECT count(*) FROM waitlist WHERE status = 'WAITING' AND id <= %s",
            (wid,),
        ).fetchone()[0]
        return {"result": "WAITING", "position": position}


def leave_waitlist(user_id: int) -> dict:
    with pool.connection() as conn:
        lock_and_refresh(conn)  # take the lock so we can't race a promotion
        row = conn.execute(
            "UPDATE waitlist SET status = 'LEFT' "
            "WHERE user_id = %s AND status = 'WAITING' RETURNING id",
            (user_id,),
        ).fetchone()
        return {"result": "LEFT"} if row else {"result": "NOT_WAITING"}
