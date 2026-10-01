# SneakDrop Implementation Notes

- Stock is computed as `total - (HELD + PAID)` under a row lock. There is no mutable counter that can drift.
- Max-2 counts `PAID + HELD`. Waiters who can't qualify are rejected at join and skipped at promotion.
- A promoted waiter gets a fresh 5-minute hold.
- Status endpoints are read-only and lock-free. Sweeper interval is 1s (`SWEEP_INTERVAL`).
- Payments are simulated by a separate provider container. It signs webhooks (HMAC-SHA256, shared secret) and retries on 5xx.
- Webhooks are idempotent on event_id. The effect and the event record commit in one transaction.
- Terminal events are self-sufficient. Non-terminal events can't regress a settled payment.
- Late confirmation: honored if the pair is still free and the user is under the limit. Otherwise the payment is flagged refund_needed, visible at `/admin/refunds`. There is no real refund call.
- Known limitation: no replay protection (timestamp window) on webhooks. If the provider accepted a request but the response was lost, the payment is written off locally, and a later success is flagged for refund.
- The page at `/` polls `/status` once a second. `/status` is read-only and takes no lock, so polling can't slow down buyers.
- The countdown is computed locally from the server's `seconds_left`, so there's no client clock skew.
- Users are seeded 1–5000, selected by the `X-User-Id` header (or `/?user=N` on the page). There's no login, by assumption.
- Buttons always stay enabled so the server's rule rejections are visible.

## How to run / Test commands

1. `pip install httpx`
2. `python src/tests/run_all.py` (ensure the stack is running via `docker compose up --build -d`)

## What each suite proves

- `load_test.py`: 2,000 simultaneous buyers get exactly 20 pairs, one user can't hold two, and the queue promotes in order.
- `payment_test.py`: duplicate, late and out-of-order webhooks, plus refund handling.
- `soak_test.py`: seven database invariants stay true under mixed concurrent chaos.

## Demo / Admin Controls

- Test-only endpoints: `/admin/*` and the payment scenarios (`/pay?scenario=...`) exist only with `ENABLE_ADMIN=1`. Turn it off outside demos.
