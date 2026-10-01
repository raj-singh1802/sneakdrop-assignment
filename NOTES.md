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

1. Copy env template: `cp .env.example .env` (or set env vars directly, see `.env.example`).
2. Reset and build from scratch:
   ```
   docker compose down -v
   docker compose up --build -d
   ```
3. Verify services are healthy:
   ```
   curl -f http://localhost:8000/health
   curl -f http://localhost:8000/stock
   ```
   UI: `http://localhost:8000/?user=1` (seeded users 1-5000 via `X-User-Id` header).
4. Install test client and run full pipeline:
   ```
   pip install httpx
   python src/tests/run_all.py
   ```
   Individual suites: `python src/tests/load_test.py`, `python src/tests/payment_test.py`, `python src/tests/soak_test.py --seconds 45 --users 200 --stock 20 --hold 3`.

## Production warnings

- Set `ENABLE_ADMIN=0` outside demos. With `ENABLE_ADMIN=1`, `/admin/*` (reset, refunds, audit, payment debug) and `/pay?scenario=...&late_delay=...` chaos knobs are exposed.
- Change `WEBHOOK_SECRET` from `dev-secret-change-me` to a long random value in both `api` and `provider` (see `.env.example`). Webhooks are HMAC-SHA256 verified; mismatched secrets cause `401`.
- `SWEEP_INTERVAL` defaults to `1s`; `CHAOS_MAX_DELAY` defaults to `4s` for the fake provider.

## Architecture decisions

- Pessimistic row locking: `drop_config` single row is `SELECT ... FOR UPDATE` first in every mutating transaction (`lock_config`). Stock is never a mutable counter; it is computed as `total - count(HELD,PAID)` under that lock, so oversell is impossible.
- Lazy hold expiration + sweeper: `expire_and_promote()` runs inside `lock_and_refresh()` on every buy/join/leave/pay/webhook, plus a 1s background `sweeper_loop`. Expired `HELD→EXPIRED`, then FIFO `waitlist ORDER BY id` promotion with a fresh full hold. Free stock implies empty queue, so buyers can't jump waiters.
- Webhook idempotency via `event_id`: `payment_events(event_id PK)` dedupes; effect + event insert commit atomically under the same global lock. `payments.last_seq` drops stale/reordered events; terminal states never regress. Late `succeeded` after expiry is honored only if stock free and user under limit, else `refund_needed=true` (flag-only, listed at `/admin/refunds`; no real refund call).
- Lock-free status polling: `/status`, `/stock`, `/admin/audit` are single-statement read-only snapshots with no `FOR UPDATE`, so 1s UI polling can't slow buyers. Countdown is server `seconds_left` rendered locally to avoid clock skew.

## What each suite proves

- `load_test.py`: 2,000 simultaneous buyers get exactly 20 pairs, one user can't hold two, and the queue promotes in order.
- `payment_test.py`: duplicate, late and out-of-order webhooks, plus refund handling.
- `soak_test.py`: seven database invariants stay true under mixed concurrent chaos.

## Demo / Admin Controls

- Test-only endpoints: `/admin/*` and the payment scenarios (`/pay?scenario=...`) exist only with `ENABLE_ADMIN=1`. Turn it off outside demos.
