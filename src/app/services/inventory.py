def lock_config(conn):
    """Take the global lock (the drop_config row). Every transaction that
    changes stock, holds or the queue must call this FIRST."""
    return conn.execute(
        "SELECT total_stock, hold_seconds, max_per_user "
        "FROM drop_config WHERE id = 1 FOR UPDATE"
    ).fetchone()


def count_taken(conn):
    return conn.execute(
        "SELECT count(*) FROM reservations WHERE status IN ('HELD','PAID')"
    ).fetchone()[0]


def user_counts(conn, user_id):
    """Returns (active holds, holds + paid) for one user."""
    return conn.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'HELD'),
               count(*) FILTER (WHERE status IN ('HELD','PAID'))
          FROM reservations WHERE user_id = %s
        """,
        (user_id,),
    ).fetchone()


def expire_and_promote(conn, cfg):
    """Call only while holding the drop_config lock.
    1) expire overdue holds  2) give every free pair to the queue, in order."""
    total, hold_seconds, max_per_user = cfg

    expired = conn.execute(
        """
        UPDATE reservations SET status = 'EXPIRED'
         WHERE status = 'HELD' AND expires_at <= clock_timestamp()
        RETURNING id, user_id
        """
    ).fetchall()

    free = total - count_taken(conn)
    promoted, skipped = [], []
    while free > 0:
        nxt = conn.execute(
            "SELECT id, user_id FROM waitlist WHERE status = 'WAITING' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if nxt is None:
            break
        wid, uid = nxt
        held_now, owned = user_counts(conn, uid)
        if held_now > 0 or owned >= max_per_user:
            # Defensive second check (join also blocks this). Mark SKIPPED
            # so we never look at them again; the next person gets the pair.
            conn.execute("UPDATE waitlist SET status = 'SKIPPED' WHERE id = %s", (wid,))
            skipped.append(uid)
            continue
        conn.execute("UPDATE waitlist SET status = 'PROMOTED' WHERE id = %s", (wid,))
        conn.execute(
            """
            INSERT INTO reservations (user_id, status, expires_at)
            VALUES (%s, 'HELD', clock_timestamp() + make_interval(secs => %s))
            """,
            (uid, hold_seconds),  # fresh, full hold for the promoted user
        )
        promoted.append(uid)
        free -= 1

    return {"expired": expired, "promoted": promoted, "skipped": skipped}


def lock_and_refresh(conn):
    cfg = lock_config(conn)
    expire_and_promote(conn, cfg)
    return cfg


def stock_snapshot(conn):
    """Read-only, single statement (one consistent snapshot), takes no lock."""
    total, held, paid = conn.execute(
        """
        SELECT (SELECT total_stock FROM drop_config WHERE id = 1),
               count(*) FILTER (WHERE status = 'HELD' AND expires_at > clock_timestamp()),
               count(*) FILTER (WHERE status = 'PAID')
          FROM reservations
        """
    ).fetchone()
    return {"total": total, "held": held, "paid": paid, "available": total - held - paid}
