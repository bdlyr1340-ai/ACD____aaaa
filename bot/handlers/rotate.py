"""Rotate handler — collects creds, runs google_account.rotate_google_account,
sends results back to the user."""
from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import List, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from bot import config
from bot.db import models
from bot.services import rotate_google_account
from bot.utils.error_reporter import report_error
from bot.utils.keyboards import back_menu, cancel_menu, main_menu

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[^@\s|]+@[^@\s|]+\.[^@\s|]+")


# ════════════════════════════════════════════════
# State helpers
# ════════════════════════════════════════════════

def _clear_state(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for k in (
        "rot_flow", "rot_email", "rot_password", "rot_totp",
        "rot_bulk_waiting",
    ):
        ctx.user_data.pop(k, None)


# ════════════════════════════════════════════════
# Entry actions (called from start.on_button)
# ════════════════════════════════════════════════

async def start_single_flow(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_state(ctx)
    ctx.user_data["rot_flow"] = "email"
    await query.message.reply_text(
        "🔐 *تغيير حساب واحد*\n\n"
        "أرسل بيانات الحساب على *سطر واحد* بالشكل:\n"
        "`email@gmail.com | OldPassword | OLD2FASECRET`\n\n"
        "أو أرسلها على *3 رسائل متتالية* (إيميل، كلمة سر، مفتاح 2FA).\n\n"
        "إذا لا يوجد 2FA على الحساب أرسل: `skip` في الخطوة الثالثة.",
        parse_mode="Markdown",
        reply_markup=cancel_menu(),
    )


async def start_bulk_flow(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_state(ctx)
    ctx.user_data["rot_bulk_waiting"] = True
    await query.message.reply_text(
        "📋 *قائمة حسابات (Bulk)*\n\n"
        "أرسل ملفاً نصياً (.txt) أو رسالة فيها سطر لكل حساب بالشكل:\n"
        "`email@gmail.com | OldPassword | OLD2FASECRET`\n\n"
        f"الحدّ الأقصى: *{config.MAX_BULK_ACCOUNTS}* حساب في الطلب الواحد.\n"
        "تكلفة كل حساب: *1 رصيد*.\n",
        parse_mode="Markdown",
        reply_markup=cancel_menu(),
    )


# ════════════════════════════════════════════════
# Text handler — drives single-account & bulk flows
# ════════════════════════════════════════════════

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    user = update.effective_user

    if await models.is_banned(user.id):
        await msg.reply_text("🚫 حسابك محظور.")
        return

    # Admin add-card hook from previous bot is removed; admin module owns its own state.
    from bot.handlers.admin import on_admin_text
    if await on_admin_text(update, ctx):
        return

    text = (msg.text or "").strip()

    # Bulk mode → parse and run
    if ctx.user_data.get("rot_bulk_waiting"):
        ctx.user_data.pop("rot_bulk_waiting", None)
        accounts = _parse_accounts_block(text)
        await _run_bulk(msg, ctx, user, accounts)
        return

    # Single mode
    flow = ctx.user_data.get("rot_flow")
    if flow:
        # Single-line shortcut (email | pass | totp)
        if "|" in text and EMAIL_RE.search(text.split("|")[0]):
            parsed = _parse_account_line(text)
            _clear_state(ctx)
            if not parsed:
                await msg.reply_text("❌ صيغة غير صحيحة. مثال:\n`email@gmail.com | OldPassword | OLD2FASECRET`",
                                     parse_mode="Markdown")
                return
            await _run_single(msg, ctx, user, *parsed)
            return

        if flow == "email":
            if not EMAIL_RE.fullmatch(text):
                await msg.reply_text("❌ إيميل غير صالح. أرسل إيميل Gmail صحيح:")
                return
            ctx.user_data["rot_email"] = text
            ctx.user_data["rot_flow"] = "password"
            await msg.reply_text("✅ الإيميل محفوظ.\n\n🔑 أرسل كلمة السر القديمة:")
            try: await msg.delete()
            except Exception: pass
            return

        if flow == "password":
            ctx.user_data["rot_password"] = text
            ctx.user_data["rot_flow"] = "totp"
            await msg.reply_text(
                "✅ كلمة السر محفوظة.\n\n"
                "🔐 أرسل *مفتاح 2FA السري* (Base32) أو `skip` إن لم يوجد:",
                parse_mode="Markdown",
            )
            try: await msg.delete()
            except Exception: pass
            return

        if flow == "totp":
            totp = "" if text.lower() == "skip" else text
            email = ctx.user_data.get("rot_email", "")
            pwd = ctx.user_data.get("rot_password", "")
            _clear_state(ctx)
            try: await msg.delete()
            except Exception: pass
            await _run_single(msg, ctx, user, email, pwd, totp)
            return

    # Default reply
    await msg.reply_text("اختر من القائمة الرئيسية:", reply_markup=main_menu())


# ════════════════════════════════════════════════
# Bulk text-document handler
# ════════════════════════════════════════════════

async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not ctx.user_data.get("rot_bulk_waiting"):
        return
    if not msg.document:
        return
    if msg.document.file_size and msg.document.file_size > 200_000:
        await msg.reply_text("❌ الملف كبير جداً (الحد 200 KB).")
        return
    file = await msg.document.get_file()
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    text = buf.getvalue().decode("utf-8", errors="ignore")
    ctx.user_data.pop("rot_bulk_waiting", None)
    accounts = _parse_accounts_block(text)
    await _run_bulk(msg, ctx, user, accounts)


# ════════════════════════════════════════════════
# Parsing
# ════════════════════════════════════════════════

def _parse_account_line(line: str) -> Tuple[str, str, str] | None:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2:
        return None
    email = parts[0]
    if not EMAIL_RE.fullmatch(email):
        return None
    password = parts[1]
    totp = parts[2] if len(parts) >= 3 else ""
    if totp.lower() == "skip":
        totp = ""
    return email, password, totp


def _parse_accounts_block(text: str) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parsed = _parse_account_line(raw)
        if parsed:
            out.append(parsed)
    return out


# ════════════════════════════════════════════════
# Runners
# ════════════════════════════════════════════════

async def _run_single(msg, ctx, user, email: str, password: str, totp: str) -> None:
    if not await models.deduct_credit(user.id, config.ROTATE_COST):
        await msg.reply_text("⚠️ رصيد غير كافٍ. استخدم /qd أو /use.", reply_markup=back_menu())
        return

    progress = await msg.reply_text("⏳ بدء العملية...")

    async def _on_progress(t: str) -> None:
        try:
            await progress.edit_text(t, parse_mode="Markdown")
        except Exception:
            pass

    rot_id = await models.log_rotation_start(user.id, email)
    try:
        result = await asyncio.wait_for(
            rotate_google_account(
                _on_progress,
                gmail=email,
                old_password=password,
                old_totp_secret=totp,
                user_id=user.id,
            ),
            timeout=config.ROTATE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        result = {"success": False, "step": "timeout", "error": "تجاوز الوقت المسموح",
                  "screenshot_path": None, "html_path": None,
                  "new_password": None, "new_totp_secret": None, "gmail": email}
    except Exception as exc:
        log.exception("Rotation crashed")
        result = {"success": False, "step": "crash", "error": str(exc),
                  "screenshot_path": None, "html_path": None,
                  "new_password": None, "new_totp_secret": None, "gmail": email}

    await models.log_rotation_finish(
        rot_id, user.id,
        success=result.get("success", False),
        step=result.get("step"),
        new_password=result.get("new_password"),
        new_totp_secret=result.get("new_totp_secret"),
        error=result.get("error"),
    )

    if result.get("success"):
        text = (
            "✅ <b>تم تبديل الحساب بنجاح</b>\n\n"
            f"📧 <code>{result['gmail']}</code>\n"
            f"🔑 كلمة السر الجديدة: <code>{result['new_password']}</code>\n"
            f"🔐 مفتاح 2FA الجديد: <code>{result['new_totp_secret']}</code>"
        )
        await progress.edit_text(text, parse_mode="HTML")
        # Notify admins of success summary
        for aid in config.ADMIN_IDS:
            try:
                await ctx.bot.send_message(
                    aid,
                    f"✅ تبديل ناجح\nuser: {user.id}\n{result['gmail']}",
                )
            except Exception:
                pass
    else:
        # Refund the credit
        await models.add_credits(user.id, config.ROTATE_COST)
        try:
            await progress.delete()
        except Exception:
            pass
        await report_error(
            ctx,
            user_id=user.id,
            gmail=email,
            step=result.get("step", "unknown"),
            error_text=result.get("error", "خطأ غير معروف"),
            screenshot_path=result.get("screenshot_path"),
            html_path=result.get("html_path"),
            rot_id=rot_id,
        )
        await msg.reply_text("↩️ تم إرجاع الرصيد.", reply_markup=main_menu())


async def _run_bulk(msg, ctx, user, accounts: List[Tuple[str, str, str]]) -> None:
    if not accounts:
        await msg.reply_text("❌ لم أجد أي حساب صالح في الرسالة.", reply_markup=back_menu())
        return
    if len(accounts) > config.MAX_BULK_ACCOUNTS:
        await msg.reply_text(f"❌ الحد الأقصى {config.MAX_BULK_ACCOUNTS} حساب.")
        return

    cost = config.ROTATE_COST * len(accounts)
    if not await models.deduct_credit(user.id, cost):
        await msg.reply_text(
            f"⚠️ رصيد غير كافٍ. تحتاج {cost} نقطة لمعالجة {len(accounts)} حساب.",
            reply_markup=back_menu(),
        )
        return

    header = await msg.reply_text(
        f"📋 سيتم معالجة {len(accounts)} حساب…\nيُرسَل تقرير عند انتهاء كل حساب."
    )

    successes: List[str] = []
    failures: List[str] = []

    for idx, (email, password, totp) in enumerate(accounts, 1):
        sub = await msg.reply_text(f"⏳ ({idx}/{len(accounts)}) {email}")
        async def _prog(t, s=sub):
            try: await s.edit_text(f"({idx}/{len(accounts)}) {email}\n\n{t}", parse_mode="Markdown")
            except Exception: pass

        rot_id = await models.log_rotation_start(user.id, email)
        try:
            result = await asyncio.wait_for(
                rotate_google_account(
                    _prog, gmail=email, old_password=password,
                    old_totp_secret=totp, user_id=user.id,
                ),
                timeout=config.ROTATE_TIMEOUT_SEC,
            )
        except Exception as exc:
            result = {"success": False, "step": "crash", "error": str(exc),
                      "screenshot_path": None, "html_path": None,
                      "new_password": None, "new_totp_secret": None, "gmail": email}

        await models.log_rotation_finish(
            rot_id, user.id,
            success=result.get("success", False),
            step=result.get("step"), new_password=result.get("new_password"),
            new_totp_secret=result.get("new_totp_secret"), error=result.get("error"),
        )

        if result.get("success"):
            line = f"{result['gmail']} | {result['new_password']} | {result['new_totp_secret']}"
            successes.append(line)
            try: await sub.edit_text(f"✅ {email}\n<code>{line}</code>", parse_mode="HTML")
            except Exception: pass
        else:
            failures.append(f"{email}  →  {result.get('step')}: {result.get('error')}")
            await models.add_credits(user.id, config.ROTATE_COST)  # refund this one
            try: await sub.delete()
            except Exception: pass
            await report_error(
                ctx, user_id=user.id, gmail=email,
                step=result.get("step", "unknown"),
                error_text=result.get("error", "خطأ غير معروف"),
                screenshot_path=result.get("screenshot_path"),
                html_path=result.get("html_path"),
                rot_id=rot_id,
            )

    # Summary
    summary = (
        f"📊 *النتيجة النهائية*\n\n"
        f"المجموع: {len(accounts)}\n"
        f"✅ ناجح: {len(successes)}\n"
        f"❌ فاشل: {len(failures)}\n"
    )
    try: await header.edit_text(summary, parse_mode="Markdown")
    except Exception: await msg.reply_text(summary, parse_mode="Markdown")

    if successes:
        body = "email | new_password | new_2fa_secret\n" + "\n".join(successes)
        buf = io.BytesIO(body.encode("utf-8")); buf.name = "results.txt"
        await msg.reply_document(document=buf, filename="results.txt", caption="ملف النتائج الناجحة")
    if failures:
        body = "\n".join(failures)
        buf = io.BytesIO(body.encode("utf-8")); buf.name = "failures.txt"
        await msg.reply_document(document=buf, filename="failures.txt", caption="الحسابات الفاشلة")
