"""Bot configuration — all values from environment variables."""
from __future__ import annotations

import os
import sys


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]


# ── Telegram ──
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS: list[int] = _int_list(os.environ.get("ADMIN_IDS", ""))

# ── Database (PostgreSQL on Railway) ──
DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

# ── Credits ──
DEFAULT_CREDITS: int = int(os.environ.get("DEFAULT_CREDITS", "3"))
ROTATE_COST: int = int(os.environ.get("ROTATE_COST", "1"))
CHECKIN_REWARD: int = int(os.environ.get("CHECKIN_REWARD", "1"))
REFERRAL_BONUS: int = int(os.environ.get("REFERRAL_BONUS", "2"))

# ── Proxy ──
PROXY_URL: str = os.environ.get("PROXY_URL", "")
PROXY_LIST: str = os.environ.get("PROXY_LIST", "")

# ── Cloud Browser (optional) ──
BROWSER_PROVIDER: str = os.environ.get("BROWSER_PROVIDER", "")
BROWSERBASE_API_KEY: str = os.environ.get("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID: str = os.environ.get("BROWSERBASE_PROJECT_ID", "")
BROWSERLESS_TOKEN: str = os.environ.get("BROWSERLESS_TOKEN", "")
BROWSERLESS_URL: str = os.environ.get("BROWSERLESS_URL", "wss://production-sfo.browserless.io")
BROWSERLESS_PROXY: str = os.environ.get("BROWSERLESS_PROXY", "residential")
BROWSERLESS_PROXY_COUNTRY: str = os.environ.get("BROWSERLESS_PROXY_COUNTRY", "us")

# ── Bulk processing ──
MAX_BULK_ACCOUNTS: int = int(os.environ.get("MAX_BULK_ACCOUNTS", "30"))
ROTATE_TIMEOUT_SEC: int = int(os.environ.get("ROTATE_TIMEOUT_SEC", "300"))

# ── Logging ──
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")


def validate() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not ADMIN_IDS:
        missing.append("ADMIN_IDS")
    if missing:
        print(f"[FATAL] Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
