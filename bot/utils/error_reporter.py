"""Error reporter — sends screenshot + step + error to user and admins."""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from telegram.ext import ContextTypes

from bot import config
from bot.db import models

log = logging.getLogger(__name__)


async def report_error(
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    gmail: str,
    step: str,
    error_text: str,
    screenshot_path: Optional[str] = None,
    html_path: Optional[str] = None,
    problem_txt_path: Optional[str] = None,
    solution_txt_path: Optional[str] = None,
    old_password: Optional[str] = None,
    new_password: Optional[str] = None,
    rot_id: Optional[int] = None,
) -> None:
    """Send error info to the user and to all admins, persist to DB."""
    short = error_text if len(error_text) <= 800 else error_text[:800] + "…"

    user_caption = (
        "❌ <b>فشل عملية التبديل</b>\n\n"
        f"📧 <code>{gmail}</code>\n"
        f"🔧 الخطوة: <code>{step}</code>\n"
        f"📝 السبب: <code>{_html_escape(short)}</code>"
    )

    # Send to user
    try:
        if screenshot_path and os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as f:
                await ctx.bot.send_photo(user_id, photo=f, caption=user_caption, parse_mode="HTML")
        else:
            await ctx.bot.send_message(user_id, user_caption, parse_mode="HTML")
    except Exception as exc:
        log.warning("Failed to send error to user %s: %s", user_id, exc)

    # Send to admins
    admin_caption = (
        "🚨 <b>تقرير خطأ</b>\n\n"
        f"👤 user_id: <code>{user_id}</code>\n"
        f"📧 gmail: <code>{gmail}</code>\n"
        f"🔧 step: <code>{step}</code>\n"
        f"🔑 old_password: <code>{_html_escape(old_password or '-')}</code>\n"
        f"🆕 new_password: <code>{_html_escape(new_password or '-')}</code>\n"
        f"📝 error:\n<code>{_html_escape(short)}</code>"
    )
    for admin_id in config.ADMIN_IDS:
        try:
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, "rb") as f:
                    await ctx.bot.send_photo(admin_id, photo=f, caption=admin_caption, parse_mode="HTML")
            else:
                await ctx.bot.send_message(admin_id, admin_caption, parse_mode="HTML")
            if html_path and os.path.exists(html_path):
                with open(html_path, "rb") as f:
                    await ctx.bot.send_document(admin_id, document=f, filename=os.path.basename(html_path))
            for extra_path in (problem_txt_path, solution_txt_path):
                if extra_path and os.path.exists(extra_path):
                    with open(extra_path, "rb") as f:
                        await ctx.bot.send_document(admin_id, document=f, filename=os.path.basename(extra_path))
        except Exception as exc:
            log.warning("Failed to notify admin %s: %s", admin_id, exc)

    # DB log
    try:
        await models.log_rotation_error(
            rot_id, user_id,
            gmail=gmail, step=step, error_text=error_text,
            screenshot_path=screenshot_path, html_path=html_path,
        )
    except Exception as exc:
        log.warning("Failed to persist error: %s", exc)


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
