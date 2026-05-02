"""Custom (pinned) password handler.

Lets the user set/clear a fixed password used for ALL rotations.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.utils import custom_password as cp
from bot.utils.keyboards import back_menu, cancel_menu, custom_pwd_menu, main_menu


def _validate(pwd: str) -> str | None:
    """Return error message or None if password is OK."""
    if len(pwd) < 12:
        return "❌ كلمة السر قصيرة (الحد الأدنى 12 حرف)."
    if len(pwd) > 64:
        return "❌ كلمة السر طويلة (الحد الأقصى 64 حرف)."
    classes = sum([
        any(c.islower() for c in pwd),
        any(c.isupper() for c in pwd),
        any(c.isdigit() for c in pwd),
        any(not c.isalnum() for c in pwd),
    ])
    if classes < 3:
        return "❌ يجب أن تحتوي على 3 من 4: أحرف صغيرة + كبيرة + أرقام + رموز."
    return None


async def show_panel(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = query.from_user.id
    current = cp.get(user_id)
    text = "🔑 *كلمة السر الثابتة*\n\n"
    if current:
        masked = current[:2] + "•" * (len(current) - 4) + current[-2:]
        text += (
            f"الحالية: `{masked}`\n\n"
            "ستُستخدم هذه الكلمة في *كل* عمليات تغيير الحسابات بدلاً من "
            "توليد كلمة عشوائية.\n"
        )
    else:
        text += (
            "لا توجد كلمة سر ثابتة. سيتم توليد كلمة عشوائية لكل حساب.\n\n"
            "اضغط الزر أدناه لتعيين كلمة سر تُستخدم لكل العمليات.\n"
        )
    await query.message.reply_markdown(
        text, reply_markup=custom_pwd_menu(bool(current))
    )


async def start_set(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["cpwd_flow"] = "await_pwd"
    await query.message.reply_markdown(
        "✏️ أرسل كلمة السر الثابتة الآن.\n\n"
        "الشروط: 12-64 حرف، تحتوي على 3 من: أحرف صغيرة، كبيرة، أرقام، رموز.\n"
        "*ملاحظة:* سيتم استخدامها كما هي لكل الحسابات.",
        reply_markup=cancel_menu(),
    )


async def do_clear(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cp.clear(query.from_user.id)
    await query.message.reply_text(
        "🗑 تم إلغاء كلمة السر الثابتة. سيُستأنف توليد كلمات عشوائية.",
        reply_markup=main_menu(),
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if it consumed the message."""
    if ctx.user_data.get("cpwd_flow") != "await_pwd":
        return False
    msg = update.effective_message
    pwd = (msg.text or "").strip()
    err = _validate(pwd)
    if err:
        await msg.reply_text(err + "\nأعد المحاولة:")
        return True
    cp.set_password(update.effective_user.id, pwd)
    ctx.user_data.pop("cpwd_flow", None)
    try: await msg.delete()
    except Exception: pass
    await msg.reply_text(
        "✅ تم حفظ كلمة السر الثابتة. ستُستخدم في كل العمليات القادمة.",
        reply_markup=main_menu(),
    )
    return True


# Commands
async def cmd_setpwd(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text(
            "الاستخدام: /setpwd <كلمة_السر>"
        )
        return
    pwd = " ".join(args).strip()
    err = _validate(pwd)
    if err:
        await update.effective_message.reply_text(err); return
    cp.set_password(update.effective_user.id, pwd)
    try: await update.effective_message.delete()
    except Exception: pass
    await update.effective_message.chat.send_message(
        "✅ تم حفظ كلمة السر الثابتة."
    )


async def cmd_clearpwd(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cp.clear(update.effective_user.id)
    await update.effective_message.reply_text(
        "🗑 تم إلغاء كلمة السر الثابتة."
    )
