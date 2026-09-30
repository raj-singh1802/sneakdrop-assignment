CREATE TABLE users (
  id   SERIAL PRIMARY KEY,
  name TEXT NOT NULL
);
-- 5000 seeded users so the load test can use many distinct identities
INSERT INTO users (name)
SELECT 'user' || g FROM generate_series(1, 5000) AS g;

-- Single-row config. The row doubles as the lock we serialize buys on.
CREATE TABLE drop_config (
  id          INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  total_stock INT NOT NULL CHECK (total_stock >= 0),
  hold_seconds INT NOT NULL DEFAULT 300,
  max_per_user INT NOT NULL DEFAULT 2
);
INSERT INTO drop_config (total_stock) VALUES (20);

CREATE TABLE reservations (
  id         BIGSERIAL PRIMARY KEY,
  user_id    INT NOT NULL REFERENCES users(id),
  status     TEXT NOT NULL CHECK (status IN ('HELD','PAID','EXPIRED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  paid_at    TIMESTAMPTZ
);
-- DB-level guarantee: at most one active hold per user
CREATE UNIQUE INDEX one_hold_per_user
  ON reservations(user_id) WHERE status = 'HELD';
-- Fast lookups for the expiry sweeper and per-user counts
CREATE INDEX reservations_held_expiry
  ON reservations(expires_at) WHERE status = 'HELD';
CREATE INDEX reservations_user ON reservations(user_id);

CREATE TABLE waitlist (
  id        BIGSERIAL PRIMARY KEY,          -- id order = queue order
  user_id   INT NOT NULL REFERENCES users(id),
  joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status    TEXT NOT NULL CHECK (status IN ('WAITING','PROMOTED','SKIPPED','LEFT'))
);
CREATE UNIQUE INDEX one_wait_per_user
  ON waitlist(user_id) WHERE status = 'WAITING';
CREATE INDEX waitlist_queue ON waitlist(id) WHERE status = 'WAITING';

CREATE TABLE payments (
  id             TEXT PRIMARY KEY,            -- our reference, echoed by the provider
  reservation_id BIGINT NOT NULL REFERENCES reservations(id),
  user_id        INT NOT NULL REFERENCES users(id),
  status         TEXT NOT NULL CHECK (status IN ('PENDING','SUCCEEDED','FAILED')),
  amount_cents   INT NOT NULL,
  last_seq       INT NOT NULL DEFAULT 0,      -- highest event seq applied
  refund_needed  BOOLEAN NOT NULL DEFAULT false,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  settled_at     TIMESTAMPTZ
);
-- DB-level guarantees: one in-flight payment per reservation, one success per reservation
CREATE UNIQUE INDEX one_pending_payment_per_reservation
  ON payments(reservation_id) WHERE status = 'PENDING';
CREATE UNIQUE INDEX one_success_per_reservation
  ON payments(reservation_id) WHERE status = 'SUCCEEDED';
CREATE INDEX payments_reservation ON payments(reservation_id);

CREATE TABLE payment_events (
  event_id    TEXT PRIMARY KEY,               -- dedupe key
  payment_id  TEXT NOT NULL REFERENCES payments(id),
  type        TEXT NOT NULL,
  seq         INT NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  outcome     TEXT NOT NULL                   -- audit trail: what we did with it
);
