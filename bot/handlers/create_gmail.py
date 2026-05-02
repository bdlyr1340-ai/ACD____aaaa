"""Create Gmail handler — Pro v13.

Two modes:
  • ✍️ Manual  — user provides:  First | Last | YYYY-MM-DD | m|f
  • 🤖 Auto    — bot asks "how many?" then generates everything randomly
                 and creates accounts back-to-back (bulk).

Both modes use the real `bot.services.gmail_creator.create_gmail_account`.

Output (per successful account, exactly as requested):
    email | password
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
from bot.utils.keyboards import (back_menu, cancel_menu, create_gmail_menu,
                                 main_menu)

log = logging.getLogger(__name__)

try:
    from bot.services.gmail_creator import create_gmail_account
    HAS_CREATOR = True
except Exception as _e:  # pragma: no cover
    log.warning("gmail_creator import failed: %s", _e)
    HAS_CREATOR = False
    create_gmail_account = None  # type: ignore


FIRST_NAMES = [
    "Adam", "Sara", "Omar", "Lina", "Yousef", "Nour", "Ali", "Maya",
    "Khaled", "Rana", "Tariq", "Dina", "Hadi", "Zaid", "Layla",
    "Karim", "Salma", "Hani", "Mira", "Bilal", "Amir", "Reem",
    "Faris", "Hala", "Sami", "Noura", "Rami", "Yara", "Fadi", "Aya",
]
LAST_NAMES = [
    "Hassan", "Saleh", "Karim", "Naser", "Farid", "Sami", "Talal",
    "Rashid", "Murad", "Awad", "Salim", "Najjar", "Halabi", "Sayegh",
    "Khoury", "Antoun", "Jaber", "Ayoub", "Daher", "Haddad", "Mansour",
]

# Maximum auto-batch size to protect users / quota.
MAX_AUTO_BATCH = 20


# ════════════════════════════════════════════════════════════════════
# Generators
# ════════════════════════════════════════════════════════════════════

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


def _rand_identity() -> dict:
    return {
        "first": random.choice(FIRST_NAMES),
        "last":  random.choice(LAST_NAMES),
        "bday":  f"{random.randint(1985, 2002)}-"
                 f"{random.randint(1, 12):02d}-"
                 f"{random.randint(1, 28):02d}",
        "gender": random.choice(["m", "f"]),
        "password": _rand_password(14),
    }


# ════════════════════════════════════════════════════════════════════
# Entry — submenu
# ════════════════════════════════════════════════════════════════════

async def start_flow(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the Manual / Auto sub-menu."""
    ctx.user_data.pop("cg_flow", None)
    await query.message.reply_markdown(
        "🆕 *إنشاء حساب Gmail جديد*\n\n"
        "اختر طريقة الإنشاء:\n"
        "• ✍️ *يدوي* — تُدخل أنت: الاسم، تاريخ الميلاد، الجنس.\n"
        "• 🤖 *تلقائي* — البوت يولّد كل شيء عشوائياً، فقط أخبره بالعدد.\n\n"
        f"💎 التكلفة: *{config.ROTATE_COST}* نقطة لكل حساب ناجح "
        "(يُسترد تلقائياً عند الفشل).",
        reply_markup=create_gmail_menu(),
    )


async def start_manual(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.clear()
    ctx.user_data["cg_flow"] = "manual_info"
    await query.message.reply_markdown(
        "✍️ *إنشاء يدوي*\n\n"
        "أرسل البيانات على *سطر واحد* مفصولة بـ `|`:\n"
        "`First | Last | YYYY-MM-DD | m|f`\n\n"
        "*مثال:* `Adam | Hassan | 1995-04-12 | m`\n\n"
        "اكتب `auto` لأي حقل تريد توليده عشوائياً.\n\n"
        "⚠️ إذا طلب Google رقم هاتف أو كود CAPTCHA — البوت سيرسلها لك "
        "*وتجاوب أنت*، ثم يكمل البوت تلقائياً.",
        reply_markup=cancel_menu(),
    )


async def start_auto(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.clear()
    ctx.user_data["cg_flow"] = "auto_count"
    await query.message.reply_markdown(
        "🤖 *إنشاء تلقائي*\n\n"
        f"كم عدد الحسابات التي تريد إنشاءها؟ (1 – {MAX_AUTO_BATCH})\n\n"
        "أرسل رقماً فقط، مثل: `5`",
        reply_markup=cancel_menu(),
    )


# ════════════════════════════════════════════════════════════════════
# Text router (called from rotate.on_text BEFORE rotation flow)
# ════════════════════════════════════════════════════════════════════

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    flow = ctx.user_data.get("cg_flow")
    if flow == "manual_info":
        return await _on_manual_info(update, ctx)
    if flow == "auto_count":
        return await _on_auto_count(update, ctx)
    if flow == "human":
        return await _on_human(update, ctx)
    return False


# ════════════════════════════════════════════════════════════════════
# Manual single-account flow
# ════════════════════════════════════════════════════════════════════

async def _on_manual_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    user = update.effective_user
    text = (msg.text or "").strip()

    if text.lower() == "auto":
        ident = _rand_identity()
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
        if last.lower() == "auto":  last  = random.choice(LAST_NAMES)
        if bday.lower() == "auto":
            bday = (f"{random.randint(1985, 2002)}-"
                    f"{random.randint(1, 12):02d}-"
                    f"{random.randint(1, 28):02d}")
        if gender not in ("m", "f"):
            gender = random.choice(["m", "f"])
        ident = {
            "first": first, "last": last,
            "bday": bday, "gender": gender,
            "password": _rand_password(14),
        }

    ctx.user_data.pop("cg_flow", None)
    await _create_one(msg, ctx, user, ident)
    return True


# ════════════════════════════════════════════════════════════════════
# Auto bulk flow
# ════════════════════════════════════════════════════════════════════

async def _on_auto_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    user = update.effective_user
    text = (msg.text or "").strip()

    if not text.isdigit():
        await msg.reply_text(
            f"❌ أرسل رقماً فقط بين 1 و {MAX_AUTO_BATCH}."
        )
        return True
    n = int(text)
    if n < 1 or n > MAX_AUTO_BATCH:
        await msg.reply_text(
            f"❌ العدد يجب أن يكون بين 1 و {MAX_AUTO_BATCH}."
        )
        return True

    ctx.user_data.pop("cg_flow", None)

    # Pre-flight credit check (best-effort)
    cost_each = config.ROTATE_COST
    row = await models.get_user(user.id)
    if row and row.get("credits", 0) < cost_each * n:
        await msg.reply_text(
            f"⚠️ رصيد غير كافٍ. تحتاج {cost_each * n} نقطة على الأقل "
            f"({cost_each} لكل حساب).",
            reply_markup=back_menu(),
        )
        return True

    await msg.reply_text(
        f"🤖 سيتم إنشاء *{n}* حساب تلقائياً.\nابدأ…",
        parse_mode="Markdown",
    )

    results: list[dict] = []
    for i in range(1, n + 1):
        ident = _rand_identity()
        await msg.reply_text(
            f"━━━━━━━━━━━━━━\n"
            f"🆕 الحساب {i}/{n}: {ident['first']} {ident['last']}"
        )
        res = await _create_one(msg, ctx, user, ident, silent_summary=True)
        results.append(res or {"success": False})
        # Small breather between accounts
        await asyncio.sleep(2)

    # Summary
    ok = [r for r in results if r.get("success")]
    bad = [r for r in results if not r.get("success")]
    summary = (
        f"✅ نجحت: *{len(ok)}*\n"
        f"❌ فشلت: *{len(bad)}*\n\n"
    )
    if ok:
        summary += "*الحسابات الناجحة:*\n" + "\n".join(
            f"`{r['email']} | {r['password']}`" for r in ok
        )
    await msg.reply_markdown(summary, reply_markup=main_menu())
    return True


# ════════════════════════════════════════════════════════════════════
# Single-account creation core (used by both manual & auto)
# ════════════════════════════════════════════════════════════════════

async def _create_one(msg, ctx, user, ident: dict,
                      silent_summary: bool = False) -> dict | None:
    cost = config.ROTATE_COST
    if not await models.deduct_credit(user.id, cost):
        await msg.reply_text(
            f"⚠️ رصيد غير كافٍ. تحتاج {cost} نقطة.\n"
            "استخدم /qd أو /use.",
            reply_markup=back_menu(),
        )
        return {"success": False, "step": "no_credit",
                "error": "no_credit", "email": None, "password": None}

    if not HAS_CREATOR:
        await models.add_credits(user.id, cost)
        await msg.reply_text(
            "❌ خدمة إنشاء Gmail غير مفعّلة على هذا الخادم.\n"
            "تأكد من وجود `bot/services/gmail_creator.py` ومن تثبيت "
            "`playwright` (و `camoufox` إن أمكن).",
            reply_markup=back_menu(),
        )
        return {"success": False, "step": "no_creator",
                "error": "no_creator", "email": None, "password": None}

    sub = await msg.reply_text(
        f"⏳ بدء إنشاء: {ident['first']} {ident['last']}…"
    )

    async def _prog(t: str):
        try:
            await sub.edit_text(
                f"🆕 {ident['first']} {ident['last']}\n\n{t}",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    # ── Human-in-the-loop (CAPTCHA / SMS) ──────────────────────────
    fut_holder: dict = {}

    async def _human(prompt: str) -> str:
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        fut_holder["fut"] = fut
        ctx.user_data["cg_flow"] = "human"
        await msg.reply_markdown(
            f"🧑‍💻 *مطلوب تدخّل بشري:*\n{prompt}\n\nأرسل الجواب الآن.",
            reply_markup=cancel_menu(),
        )
        try:
            ans = await asyncio.wait_for(fut, timeout=300)
        except asyncio.TimeoutError:
            ans = ""
        ctx.user_data.pop("cg_flow", None)
        return ans

    ctx.user_data["cg_human_holder"] = fut_holder

    try:
        result = await asyncio.wait_for(
            create_gmail_account(
                on_progress=_prog,
                human_input_provider=_human,
                first_name=ident["first"],
                last_name=ident["last"],
                birthday=ident["bday"],
                gender=ident["gender"],
                desired_password=ident["password"],
                user_id=user.id,
            ),
            timeout=config.ROTATE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        result = {"success": False, "step": "timeout",
                  "error": "تجاوز الوقت المسموح",
                  "email": None, "password": None}
    except Exception as exc:
        log.exception("create_gmail crashed")
        result = {"success": False, "step": "crash",
                  "error": str(exc), "email": None, "password": None}
    finally:
        ctx.user_data.pop("cg_human_holder", None)
        ctx.user_data.pop("cg_flow", None)

    if result.get("success"):
        try:
            await sub.edit_text(
                f"✅ تم إنشاء الحساب بنجاح:\n\n"
                f"`{result['email']} | {result['password']}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        # Plain copy-friendly line — exactly what the user asked for
        await msg.reply_text(f"{result['email']} | {result['password']}")
    else:
        # Refund
        await models.add_credits(user.id, cost)
        try:
            await sub.edit_text(
                f"❌ فشل إنشاء الحساب\n"
                f"الخطوة: `{result.get('step')}`\n"
                f"السبب: {result.get('error')}\n\n"
                f"تم استرداد {cost} نقطة.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    return result


# ════════════════════════════════════════════════════════════════════
# Human-input router
# ════════════════════════════════════════════════════════════════════

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
