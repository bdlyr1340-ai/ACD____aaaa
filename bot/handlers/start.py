"""Start handler — main menu, info commands, and button routing."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot import config
from bot.db import models
from bot.handlers import rotate as h_rotate
from bot.utils.keyboards import back_menu, main_menu

log = logging.getLogger(__name__)

WELCOME_TEXT = (
    "👋 *أهلاً بيك في بوت تبديل حسابات Google*\n\n"
    "هذا البوت يقوم تلقائياً بـ:\n"
    "• تسجيل الدخول لحساب Gmail.\n"
    "• تخطي المصادقة الثنائية باستخدام مفتاح TOTP الذي ترسله.\n"
    "• تغيير كلمة السر.\n"
    "• إعادة إعداد المصادقة الثنائية.\n"
    "• إرسال البيانات الجديدة إليك.\n\n"
    "💎 *رصيدك الحالي:* `{credits}`\n"
)

HELP_TEXT = (
    "📖 *دليل الاستخدام*\n\n"
    "1) اضغط *🔐 تغيير حساب واحد* وأرسل بيانات الحساب.\n"
    "2) أو *📋 قائمة حسابات* وأرسل ملفاً/رسالة فيها سطر لكل حساب.\n\n"
    "*صيغة كل سطر:*\n"
    "`email@gmail.com | OldPassword | OLD2FASECRET`\n\n"
    "*ملاحظات:*\n"
    "• مفتاح 2FA يكون نصاً Base32 (وليس الكود المؤقت).\n"
    "• إن لم يوجد 2FA: ضع `skip` في النهاية.\n"
    "• كل عملية تبديل ناجحة تكلّف 1 رصيد، والفاشلة تُسترد تلقائياً.\n\n"
    "*الأوامر:*\n"
    "/start — القائمة الرئيسية\n"
    "/me — حسابي ورصيدي\n"
    "/ref — رابط الدعوة\n"
    "/qd — تسجيل حضور يومي\n"
    "/use `<كود>` — استخدام كود تفعيل\n"
    "/help — المساعدة\n"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    args = ctx.args or []
    referred_by = None
    if args and args[0].startswith("ref_") and args[0][4:].isdigit():
        referred_by = int(args[0][4:])

    row = await models.upsert_user(user.id, user.username, user.first_name, referred_by)
    if row.get("is_banned"):
        await update.effective_message.reply_text("🚫 حسابك محظور.")
        return
    await update.effective_message.reply_markdown(
        WELCOME_TEXT.format(credits=row["credits"]),
        reply_markup=main_menu(),
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("✅ البوت شغّال.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = HELP_TEXT
    if update.effective_user.id in config.ADMIN_IDS:
        text += (
            "\n*أوامر الأدمن:*\n"
            "/admin /stats /addcredit /ban /unban /blacklist\n"
            "/broadcast /genkey /listkeys\n"
        )
    await update.effective_message.reply_markdown(text, reply_markup=back_menu())


async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = await models.get_user(user.id) or await models.upsert_user(user.id, user.username, user.first_name)
    text = (
        "👤 *حسابي*\n\n"
        f"• المعرّف: `{row['user_id']}`\n"
        f"• الرصيد: *{row['credits']}*\n"
        f"• إجمالي العمليات: {row.get('total_rotations', 0)}\n"
        f"• الناجحة: {row.get('successful_rotations', 0)}\n"
    )
    await update.effective_message.reply_markdown(text, reply_markup=back_menu())


async def cmd_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    me = await ctx.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user.id}"
    await update.effective_message.reply_markdown(
        f"🎁 شارك الرابط واحصل على *+{config.REFERRAL_BONUS} رصيد* لكل صديق:\n\n`{link}`",
        reply_markup=back_menu(),
    )


async def cmd_qd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not await models.get_user(user.id):
        await update.effective_message.reply_text("اضغط /start أولاً.")
        return
    if await models.checkin(user.id):
        row = await models.get_user(user.id)
        await update.effective_message.reply_text(
            f"✅ +{config.CHECKIN_REWARD} رصيد. رصيدك: {row['credits']}"
        )
    else:
        await update.effective_message.reply_text("❌ سجّلت الحضور اليوم بالفعل.")


async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text("الاستخدام: /use <كود>")
        return
    res = await models.use_card_key(args[0].strip(), user.id)
    if res is None:   await update.effective_message.reply_text("❌ كود غير موجود.")
    elif res == -1:   await update.effective_message.reply_text("❌ الكود مستنفد.")
    elif res == -2:   await update.effective_message.reply_text("❌ الكود منتهي الصلاحية.")
    elif res == -3:   await update.effective_message.reply_text("❌ استخدمت الكود مسبقاً.")
    else:
        row = await models.get_user(user.id)
        await update.effective_message.reply_text(f"✅ +{res} رصيد. رصيدك: {row['credits']}")


# ════════════════════════════════════════════════
# Button routing
# ════════════════════════════════════════════════

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""

    if data == "rotate_one":
        await h_rotate.start_single_flow(q, ctx); return
    if data == "rotate_bulk":
        await h_rotate.start_bulk_flow(q, ctx); return
    if data == "cancel":
        ctx.user_data.clear()
        await q.message.reply_text("تم الإلغاء.", reply_markup=main_menu()); return
    if data == "back":
        await q.message.reply_text("القائمة الرئيسية:", reply_markup=main_menu()); return
    if data == "me":
        await cmd_me(update, ctx); return
    if data == "ref":
        await cmd_ref(update, ctx); return
    if data == "checkin":
        await cmd_qd(update, ctx); return
    if data == "usekey":
        await q.message.reply_text("استخدم: /use <الكود>"); return
    if data == "help":
        await cmd_help(update, ctx); return
