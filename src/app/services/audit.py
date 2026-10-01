from app.db import pool

# One statement = one consistent snapshot. Every column must be 0.
AUDIT_SQL = """
WITH cfg AS (
    SELECT total_stock, max_per_user FROM drop_config WHERE id = 1
), taken AS (
    SELECT count(*) AS n FROM reservations WHERE status IN ('HELD', 'PAID')
)
SELECT
    GREATEST((SELECT n FROM taken) - (SELECT total_stock FROM cfg), 0) AS oversold_by,
    (SELECT count(*) FROM (
        SELECT 1 FROM reservations WHERE status = 'HELD'
         GROUP BY user_id HAVING count(*) > 1) q) AS users_with_multiple_holds,
    (SELECT count(*) FROM (
        SELECT 1 FROM reservations WHERE status IN ('HELD', 'PAID')
         GROUP BY user_id HAVING count(*) > (SELECT max_per_user FROM cfg)) q) AS users_over_limit,
    (SELECT count(*) FROM waitlist w
      WHERE w.status = 'WAITING'
        AND EXISTS (SELECT 1 FROM reservations r
                     WHERE r.user_id = w.user_id AND r.status = 'HELD')) AS waiting_while_holding,
    (CASE WHEN (SELECT n FROM taken) < (SELECT total_stock FROM cfg)
           AND EXISTS (SELECT 1 FROM waitlist WHERE status = 'WAITING')
          THEN 1 ELSE 0 END) AS idle_stock_with_queue,
    (SELECT count(*) FROM reservations r
      WHERE r.status = 'PAID'
        AND (SELECT count(*) FROM payments p
              WHERE p.reservation_id = r.id AND p.status = 'SUCCEEDED'
                AND NOT p.refund_needed) <> 1) AS paid_without_exactly_one_payment,
    (SELECT count(*) FROM payments p
       JOIN reservations r ON r.id = p.reservation_id
      WHERE p.status = 'SUCCEEDED' AND r.status <> 'PAID'
        AND NOT p.refund_needed) AS money_taken_nothing_delivered_no_refund
"""

STATS_SQL = """
SELECT
    (SELECT count(*) FROM reservations)                          AS reservations_total,
    (SELECT count(*) FROM reservations WHERE status = 'PAID')    AS paid,
    (SELECT count(*) FROM reservations WHERE status = 'EXPIRED') AS expired,
    (SELECT count(*) FROM reservations WHERE status = 'HELD')    AS held_now,
    (SELECT count(*) FROM waitlist WHERE status = 'WAITING')     AS waiting_now,
    (SELECT count(*) FROM waitlist WHERE status = 'PROMOTED')    AS promoted_total,
    (SELECT count(*) FROM waitlist WHERE status = 'SKIPPED')     AS skipped_total,
    (SELECT count(*) FROM payments)                              AS payments_total,
    (SELECT count(*) FROM payments WHERE status = 'PENDING')     AS payments_pending,
    (SELECT count(*) FROM payments WHERE refund_needed)          AS refunds_needed
"""


def audit(with_stats: bool = False) -> dict:
    """Read-only, takes no lock."""
    with pool.connection() as conn:
        cur = conn.execute(AUDIT_SQL)
        names = [d.name for d in cur.description]
        violations = dict(zip(names, cur.fetchone()))
        out = {"ok": not any(violations.values()), "violations": violations}
        if with_stats:
            cur = conn.execute(STATS_SQL)
            names = [d.name for d in cur.description]
            out["stats"] = dict(zip(names, cur.fetchone()))
            out["outcomes"] = {
                o: n for o, n in conn.execute(
                    "SELECT outcome, count(*) FROM payment_events GROUP BY outcome ORDER BY 2 DESC"
                ).fetchall()
            }
        return out
