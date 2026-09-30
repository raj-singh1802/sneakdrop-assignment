# SneakDrop Implementation Notes

- Stock is computed as `total - (HELD + PAID)` under a row lock. There is no mutable counter that can drift.
- Max-2 counts `PAID + HELD`. Waiters who can't qualify are rejected at join and skipped at promotion.
- A promoted waiter gets a fresh 5-minute hold.
- `/admin/reset` is test-only, enabled by `ENABLE_ADMIN=1`.
- Status endpoints are read-only and lock-free. Sweeper interval is 1s (`SWEEP_INTERVAL`).
- Payments are simulated by a separate provider container. It signs webhooks (HMAC-SHA256, shared secret) and retries on 5xx.
- Webhooks are idempotent on event_id. The effect and the event record commit in one transaction.
- Terminal events are self-sufficient. Non-terminal events can't regress a settled payment.
- Late confirmation: honored if the pair is still free and the user is under the limit. Otherwise the payment is flagged refund_needed, visible at `/admin/refunds`. There is no real refund call.
- Known limitation: no replay protection (timestamp window) on webhooks. If the provider accepted a request but the response was lost, the payment is written off locally, and a later success is flagged for refund.
- `/pay?scenario=...` and all `/admin/*` routes work only when `ENABLE_ADMIN=1`.
