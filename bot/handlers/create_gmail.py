"""Create Gmail handler — semi-automated.

Flow:
  1. User presses the button.
  2. Bot asks for: First | Last | Birthday(YYYY-MM-DD) | Gender(m/f)
     (any of them can be 'auto' to randomize)
  3. The service tries to create the account; if Google asks for SMS or
     CAPTCHA, the bot relays it to the user (human-in-the-loop) and waits.
  4. On success the bot sends: email | password   ONLY (as requested).
"""
from __future__ import annotations

import asyncio
import logging
import random
import string

from telegram import Update
from telegram.ext import ContextTypes

from bot import config
from bot.db import models
from bot.utils.keyboards import back_menu, cancel_menu, main_menu

log = logging.getLogger(__name__)

# The actual browser automation is expected to live in
# bot.services.gmail_creator.create_gmail_account(...). It must accept:
#   on_progress(msg: str)  — async coroutine
#   human_input_provider(prompt: str) -> awaitable[str]
# and return: {"success": bool, "email": str | None, "password": str | None,
#              "error": str | None, "step": str | None}
try:
    from bot.services.gmail_creator import create_gmail_account
    HAS_CREATOR = True
except Exception:  # pragma: no cover
    HAS_CREATOR = False
    create_gmail_account = None  # type: ignore


FIRST_NAMES = ["Adam", "Sara", "Omar", "Lina", "Yousef", "Nour", "Ali", "Maya",
               "Khaled", "Rana", "Tariq", "Dina", "Hadi", "Zaid", "Layla"]
LAST_NAMES = ["Hassan", "Saleh", "Karim", "Naser", "Farid", "Sami", "Talal",
              "Rashid", "Murad", "Awad", "Salim", "Najjar", "Halabi"]


def _rand_password(n: int = 14) -> str:
    pools = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
        random.choice("!@#$%^&*?"),
    ]
    pools += random.choices(
        string.ascii_letters + string.digits + "!@#$%^&*?", k=n - 4
    )
    random.shuffle(pools)
    return "".join(pools)


async def start_flow(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.clear()
    ctx.user_data["cg_flow"] = "info"
    await query.message.reply_markdown(
        "🆕 *إنشاء حساب Gmail جديد*\n\n"
        "أرسل البيانات على *سطر واحد* مفصولة بـ `|`:\n"
        "`First | Last | YYYY-MM-DD | m|f`\n\n"
        "اكتب `auto` لأي حقل تريد توليده تلقائياً، أو أرسل `auto` فقط "
        "لتعبئة كل شيء عشوائياً.\n\n"
        "*مثال:* `Adam | Hassan | 1995-04-12 | m`\n\n"
        "⚠️ أحياناً يطلب Google تأكيد رقم هاتف أو كود CAPTCHA — "
        "البوت سيرسلها لك *وتجاوب أنت*، ثم يكمل البوت تلقائياً.",
        reply_markup=cancel_menu(),
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if consumed."""
    flow = ctx.user_data.get("cg_flow")
    if flow == "info":
        return await _on_info(update, ctx)
    if flow == "human":
        return await _on_human(update, ctx)
    return False


async def _on_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    user = update.effective_user
    text = (msg.text or "").strip()

    if text.lower() == "auto":
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        bday = f"{random.randint(1985, 2002)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
        gender = random.choice(["m", "f"])
    else:
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 4:
            await msg.reply_text(
                "❌ صيغة غير صحيحة. مثال:\n"
                "`Adam | Hassan | 1995-04-12 | m`\n"
                "أو أرسل `auto`.",
                parse_mode="Markdown",
            )
            return True
        first, last, bday, gender = parts[0], parts[1], parts[2], parts[3].lower()
        if first.lower() == "auto": first = random.choice(FIRST_NAMES)
        if last.lower() == "auto":  last = random.choice(LAST_NAMES)
        if bday.lower() == "auto":
            bday = f"{random.randint(1985, 2002)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
        if gender not in ("m", "f"):
            gender = random.choice(["m", "f"])

    password = _rand_password(14)

    # Cost: same as a rotation
    cost = config.ROTATE_COST
    if not await models.deduct_credit(user.id, cost):
        await msg.reply_text(
            f"⚠️ رصيد غير كافٍ. تحتاج {cost} نقطة.\nاستخدم /qd أو /use.",
            reply_markup=back_menu(),
        )
        ctx.user_data.pop("cg_flow", None)
        return True

    if not HAS_CREATOR:
        await models.add_credits(user.id, cost)
        ctx.user_data.pop("cg_flow", None)
        await msg.reply_text(
            "❌ خدمة إنشاء Gmail غير مفعّلة على هذا الخادم بعد.\n"
            "تأكد من وجود `bot/services/gmail_creator.py`.",
            reply_markup=back_menu(),
        )
        return True

    sub = await msg.reply_text(f"⏳ بدء إنشاء الحساب: {first} {last}…")

    async def _prog(t: str):
        try: await sub.edit_text(f"🆕 {first} {last}\n\n{t}", parse_mode="Markdown")
        except Exception: pass

    # Human-in-the-loop input provider
    fut_holder: dict = {}

    async def _human(prompt: str) -> str:
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        fut_holder["fut"] = fut
        ctx.user_data["cg_flow"] = "human"
        await msg.reply_markdown(
            f"🧑‍💻 *مطلوب تدخل بشري:*\n{prompt}\n\nأرسل الجواب الآن.",
            reply_markup=cancel_menu(),
        )
        try:
            ans = await asyncio.wait_for(fut, timeout=300)
        except asyncio.TimeoutError:
            ans = ""
        ctx.user_data["cg_flow"] = "info"  # back to processing state
        return ans

    ctx.user_data["cg_human_holder"] = fut_holder

    try:
        result = await asyncio.wait_for(
            create_gmail_account(
                on_progress=_prog,
                human_input_provider=_human,
                first_name=first, last_name=last,
                birthday=bday, gender=gender,
                desired_password=password, user_id=user.id,
            ),
            timeout=config.ROTATE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        result = {"success": False, "step": "timeout",
                  "error": "تجاوز الوقت المسموح", "email": None, "password": None}
    except Exception as exc:
        log.exception("create_gmail crashed")
        result = {"success": False, "step": "crash",
                  "error": str(exc), "email": None, "password": None}

    ctx.user_data.pop("cg_flow", None)
    ctx.user_data.pop("cg_human_holder", None)

    if result.get("success"):
        await sub.edit_text(
            f"✅ تم إنشاء الحساب بنجاح:\n\n"
            f"`{result['email']}` | `{result['password']}`",
            parse_mode="Markdown",
        )
        await msg.reply_text(
            f"{result['email']} | {result['password']}"
        )
    else:
        # Refund
        await models.add_credits(user.id, cost)
        await sub.edit_text(
            f"❌ فشل إنشاء الحساب\n"
            f"الخطوة: `{result.get('step')}`\n"
            f"السبب: {result.get('error')}\n\n"
            f"تم استرداد {cost} نقطة.",
            parse_mode="Markdown",
        )
    return True


async def _on_human(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    holder = ctx.user_data.get("cg_human_holder") or {}
    fut = holder.get("fut")
    if fut and not fut.done():
        fut.set_result((msg.text or "").strip())
        try: await msg.delete()
        except Exception: pass
        await msg.chat.send_message("✅ تم استلام الجواب، البوت يكمل الآن…")
        return True
    return False
