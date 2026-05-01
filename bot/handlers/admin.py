"""Admin commands: stats, credits, ban/unban, broadcast, redeem-code keys."""
from __future__ import annotations

import asyncio
import logging
import secrets
import string

from telegram import Update
from telegram.ext import ContextTypes

from bot import config
from bot.db import models

log = logging.getLogger(__name__)


def _is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    text = (
        "🛠 *لوحة الأدمن*\n\n"
        "/stats — إحصائيات\n"
        "/addcredit `<id> <amount>` — إضافة رصيد\n"
        "/ban `<id>` /unban `<id>` /blacklist\n"
        "/broadcast `<msg>` — بث للجميع\n"
        "/genkey `<code> <credits> [max_uses=1] [days=0]` — كود\n"
        "/listkeys — عرض الأكواد\n"
    )
    await update.effective_message.reply_markdown(text)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    s = await models.stats()
    await update.effective_message.reply_text(
        f"👥 المستخدمون: {s['users']}\n"
        f"🚫 محظور: {s['banned']}\n"
        f"🔁 العمليات: {s['rotations']}\n"
        f"✅ الناجحة: {s['successful']}\n"
    )


async def cmd_addcredit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if len(args) < 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
        await update.effective_message.reply_text("الاستخدام: /addcredit <id> <amount>"); return
    new_total = await models.add_credits(int(args[0]), int(args[1]))
    await update.effective_message.reply_text(f"✅ الرصيد الجديد: {new_total}")


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text("الاستخدام: /ban <id>"); return
    await models.set_banned(int(args[0]), True)
    await update.effective_message.reply_text("✅ تم الحظر")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text("الاستخدام: /unban <id>"); return
    await models.set_banned(int(args[0]), False)
    await update.effective_message.reply_text("✅ تم رفع الحظر")


async def cmd_blacklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    rows = await models.list_banned()
    if not rows:
        await update.effective_message.reply_text("لا يوجد محظورون."); return
    lines = [f"{r['user_id']}  @{r.get('username') or '-'}" for r in rows]
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    msg_text = update.effective_message.text.partition(" ")[2].strip()
    if not msg_text:
        await update.effective_message.reply_text("الاستخدام: /broadcast <نص>"); return
    ids = await models.all_user_ids()
    sent = 0
    for uid in ids:
        try:
            await ctx.bot.send_message(uid, msg_text)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await update.effective_message.reply_text(f"✅ أُرسِلت إلى {sent}/{len(ids)}")


async def cmd_genkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "الاستخدام: /genkey <code> <credits> [max_uses=1] [days=0]"
        ); return
    code = args[0]
    if code.lower() == "auto":
        code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    try:
        credits = int(args[1])
        max_uses = int(args[2]) if len(args) > 2 else 1
        days = int(args[3]) if len(args) > 3 else 0
    except ValueError:
        await update.effective_message.reply_text("الأرقام غير صحيحة."); return
    ok = await models.create_card_key(code, credits, max_uses, days, update.effective_user.id)
    if ok:
        await update.effective_message.reply_text(f"✅ كود: `{code}`", parse_mode="Markdown")
    else:
        await update.effective_message.reply_text("❌ الكود موجود مسبقاً.")


async def cmd_listkeys(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    rows = await models.list_card_keys()
    if not rows:
        await update.effective_message.reply_text("لا توجد أكواد."); return
    lines = [
        f"`{r['key_code']}` — {r['credits']} pts — {r['current_uses']}/{r['max_uses']}"
        for r in rows
    ]
    await update.effective_message.reply_markdown("\n".join(lines))


async def on_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Hook for admin-specific text states. Returns True if consumed."""
    return False
