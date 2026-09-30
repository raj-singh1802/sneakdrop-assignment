# SneakDrop Implementation Notes

- Stock is computed as `total - (HELD + PAID)` under a row lock. There is no mutable counter that can drift.
- Max-2 counts `PAID + HELD`. Waiters who can't qualify are rejected at join and skipped at promotion.
- A promoted waiter gets a fresh 5-minute hold.
- `/admin/reset` is test-only, enabled by `ENABLE_ADMIN=1`.
- Status endpoints are read-only and lock-free. Sweeper interval is 1s (`SWEEP_INTERVAL`).
