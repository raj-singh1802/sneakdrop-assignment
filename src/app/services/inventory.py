def expire_overdue(conn):
    """Mark overdue holds EXPIRED. MUST be called while holding the
    drop_config row lock. Stage 3 will extend this to promote waiters."""
    return conn.execute(
        """
        UPDATE reservations
           SET status = 'EXPIRED'
         WHERE status = 'HELD' AND expires_at <= clock_timestamp()
        RETURNING id, user_id
        """
    ).fetchall()
