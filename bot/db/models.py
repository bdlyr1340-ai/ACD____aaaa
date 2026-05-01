"""Database models — async PostgreSQL via asyncpg."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from bot import config
from bot.db.connection import get_pool


# ════════════════════════════════════════════════
# Users
# ════════════════════════════════════════════════

async def upsert_user(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    referred_by: Optional[int] = None,
) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as c:
        existing = await c.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        if existing:
            row = await c.fetchrow(
                """UPDATE users
                      SET username = $2, first_name = $3, last_seen_at = NOW()
                    WHERE user_id = $1
                RETURNING *""",
                user_id, username, first_name,
            )
            return dict(row)

        row = await c.fetchrow(
            """INSERT INTO users (user_id, username, first_name, credits, referred_by)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING *""",
            user_id, username, first_name, config.DEFAULT_CREDITS, referred_by,
        )

        if referred_by and referred_by != user_id:
            try:
                await c.execute(
                    "INSERT INTO referrals (referrer_id, referred_id) VALUES ($1, $2) "
                    "ON CONFLICT (referred_id) DO NOTHING",
                    referred_by, user_id,
                )
                await c.execute(
                    "UPDATE users SET credits = credits + $1 WHERE user_id = $2",
                    config.REFERRAL_BONUS, referred_by,
                )
            except Exception:
                pass
        return dict(row)


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        return dict(row) if row else None


async def add_credits(user_id: int, amount: int) -> int:
    pool = get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "UPDATE users SET credits = credits + $1 WHERE user_id = $2 RETURNING credits",
            amount, user_id,
        )
        return int(row["credits"]) if row else 0


async def deduct_credit(user_id: int, amount: int = 1) -> bool:
    pool = get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "UPDATE users SET credits = credits - $1 "
            "WHERE user_id = $2 AND credits >= $1 RETURNING credits",
            amount, user_id,
        )
        return row is not None


async def is_banned(user_id: int) -> bool:
    pool = get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT is_banned FROM users WHERE user_id = $1", user_id)
        return bool(row and row["is_banned"])


async def set_banned(user_id: int, banned: bool) -> None:
    pool = get_pool()
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET is_banned = $2 WHERE user_id = $1", user_id, banned)


async def list_banned() -> List[Dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT user_id, username, first_name FROM users WHERE is_banned = TRUE")
        return [dict(r) for r in rows]


async def checkin(user_id: int) -> bool:
    pool = get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT last_checkin FROM users WHERE user_id = $1", user_id)
        if not row:
            return False
        last = row["last_checkin"]
        if last and (datetime.utcnow().date() == last.date()):
            return False
        await c.execute(
            "UPDATE users SET credits = credits + $1, last_checkin = NOW() WHERE user_id = $2",
            config.CHECKIN_REWARD, user_id,
        )
        return True


async def all_user_ids() -> List[int]:
    pool = get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT user_id FROM users WHERE is_banned = FALSE")
        return [int(r["user_id"]) for r in rows]


async def stats() -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as c:
        users = await c.fetchval("SELECT COUNT(*) FROM users")
        banned = await c.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = TRUE")
        rotations = await c.fetchval("SELECT COUNT(*) FROM rotations")
        ok = await c.fetchval("SELECT COUNT(*) FROM rotations WHERE status = 'success'")
        return {"users": users, "banned": banned, "rotations": rotations, "successful": ok}


# ════════════════════════════════════════════════
# Rotations
# ════════════════════════════════════════════════

async def log_rotation_start(user_id: int, gmail: str) -> int:
    pool = get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "INSERT INTO rotations (user_id, gmail) VALUES ($1, $2) RETURNING id",
            user_id, gmail,
        )
        await c.execute(
            "UPDATE users SET total_rotations = total_rotations + 1 WHERE user_id = $1",
            user_id,
        )
        return int(row["id"])


async def log_rotation_finish(
    rot_id: int,
    user_id: int,
    *,
    success: bool,
    step: Optional[str] = None,
    new_password: Optional[str] = None,
    new_totp_secret: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    pool = get_pool()
    async with pool.acquire() as c:
        await c.execute(
            """UPDATE rotations
                  SET status = $2, step = $3,
                      new_password = $4, new_totp_secret = $5,
                      error_message = $6, finished_at = NOW()
                WHERE id = $1""",
            rot_id, "success" if success else "failed",
            step, new_password, new_totp_secret, error,
        )
        if success:
            await c.execute(
                "UPDATE users SET successful_rotations = successful_rotations + 1 WHERE user_id = $1",
                user_id,
            )


async def log_rotation_error(
    rot_id: Optional[int],
    user_id: int,
    *,
    gmail: str,
    step: str,
    error_text: str,
    screenshot_path: Optional[str],
    html_path: Optional[str],
) -> None:
    pool = get_pool()
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO rotation_errors
               (rotation_id, user_id, gmail, step, error_text, screenshot_path, html_path)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            rot_id, user_id, gmail, step, error_text, screenshot_path, html_path,
        )


# ════════════════════════════════════════════════
# Card keys
# ════════════════════════════════════════════════

async def create_card_key(
    code: str, credits: int, max_uses: int, expire_days: int, created_by: int,
) -> bool:
    pool = get_pool()
    async with pool.acquire() as c:
        try:
            if expire_days > 0:
                await c.execute(
                    """INSERT INTO card_keys (key_code, credits, max_uses, expire_at, created_by)
                       VALUES ($1, $2, $3, NOW() + ($4 || ' days')::interval, $5)""",
                    code, credits, max_uses, str(expire_days), created_by,
                )
            else:
                await c.execute(
                    """INSERT INTO card_keys (key_code, credits, max_uses, created_by)
                       VALUES ($1, $2, $3, $4)""",
                    code, credits, max_uses, created_by,
                )
            return True
        except Exception:
            return False


async def list_card_keys(limit: int = 30) -> List[Dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT * FROM card_keys ORDER BY created_at DESC LIMIT $1", limit
        )
        return [dict(r) for r in rows]


async def use_card_key(code: str, user_id: int) -> Optional[int]:
    """Return credits granted, or None/-1/-2/-3 for various failures."""
    pool = get_pool()
    async with pool.acquire() as c:
        key = await c.fetchrow("SELECT * FROM card_keys WHERE key_code = $1", code)
        if not key:
            return None
        if key["current_uses"] >= key["max_uses"]:
            return -1
        if key["expire_at"] and key["expire_at"] < datetime.utcnow().astimezone(key["expire_at"].tzinfo):
            return -2
        used = await c.fetchval(
            "SELECT 1 FROM card_key_usage WHERE key_code = $1 AND user_id = $2",
            code, user_id,
        )
        if used:
            return -3
        await c.execute(
            "UPDATE card_keys SET current_uses = current_uses + 1 WHERE key_code = $1",
            code,
        )
        await c.execute(
            "INSERT INTO card_key_usage (key_code, user_id) VALUES ($1, $2)",
            code, user_id,
        )
        await c.execute(
            "UPDATE users SET credits = credits + $1 WHERE user_id = $2",
            key["credits"], user_id,
        )
        return int(key["credits"])
