"""Async PostgreSQL connection pool (asyncpg)."""
from __future__ import annotations

import logging
import pathlib

import asyncpg

from bot import config

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

MIGRATIONS_FILE = pathlib.Path(__file__).with_name("migrations.sql")


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=10)
    log.info("PostgreSQL pool created")

    sql = MIGRATIONS_FILE.read_text(encoding="utf-8")
    async with _pool.acquire() as conn:
        await conn.execute(sql)
    log.info("Database migrations applied")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_pool() first")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        log.info("PostgreSQL pool closed")
