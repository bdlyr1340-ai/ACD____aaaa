-- Database schema for Google Account Rotator Bot
-- Idempotent: safe to run multiple times.

-- ════════════════════════════════════════════════
-- 1. Users
-- ════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS users (
    user_id        BIGINT PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    credits        INTEGER NOT NULL DEFAULT 3,
    total_rotations     INTEGER NOT NULL DEFAULT 0,
    successful_rotations INTEGER NOT NULL DEFAULT 0,
    referred_by    BIGINT,
    is_banned      BOOLEAN NOT NULL DEFAULT FALSE,
    last_checkin   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Compatibility for upgrades from the old schema
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_rotations INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS successful_rotations INTEGER NOT NULL DEFAULT 0;

-- ════════════════════════════════════════════════
-- 2. Rotations log (each Google account run)
-- ════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS rotations (
    id               SERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    gmail            TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending|success|failed
    step             TEXT,                              -- last step reached
    new_password     TEXT,
    new_totp_secret  TEXT,
    error_message    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_rotations_user    ON rotations(user_id);
CREATE INDEX IF NOT EXISTS idx_rotations_created ON rotations(created_at DESC);

-- ════════════════════════════════════════════════
-- 3. Errors (screenshots + html for debugging)
-- ════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS rotation_errors (
    id           SERIAL PRIMARY KEY,
    rotation_id  INTEGER REFERENCES rotations(id) ON DELETE CASCADE,
    user_id      BIGINT,
    gmail        TEXT,
    step         TEXT,
    error_text   TEXT,
    screenshot_path TEXT,
    html_path    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ════════════════════════════════════════════════
-- 4. Referrals
-- ════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS referrals (
    id          SERIAL PRIMARY KEY,
    referrer_id BIGINT NOT NULL,
    referred_id BIGINT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);

-- ════════════════════════════════════════════════
-- 5. Card Keys / Redeem Codes
-- ════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS card_keys (
    id           SERIAL PRIMARY KEY,
    key_code     TEXT UNIQUE NOT NULL,
    credits      INTEGER NOT NULL,
    max_uses     INTEGER NOT NULL DEFAULT 1,
    current_uses INTEGER NOT NULL DEFAULT 0,
    expire_at    TIMESTAMPTZ,
    created_by   BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_card_keys_code ON card_keys(key_code);

CREATE TABLE IF NOT EXISTS card_key_usage (
    id       SERIAL PRIMARY KEY,
    key_code TEXT NOT NULL,
    user_id  BIGINT NOT NULL,
    used_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_card_key_usage_key  ON card_key_usage(key_code);
CREATE INDEX IF NOT EXISTS idx_card_key_usage_user ON card_key_usage(user_id);
