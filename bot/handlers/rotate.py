"""Rotate handler — Pro Edition.

What changed vs previous version:
  • Auto-detects input even WITHOUT pressing a button:
        – any .txt file uploaded → treated as bulk list
        – any text containing one or more "email | pass | totp" lines
          → single (1 line) or bulk (>1 line) automatically
  • Shows a clear notice when an account didn't have 2FA and the bot
    enabled it for the first time.
  • Reports denied device-tap & timeout cases clearly.
"""
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
ACCOUNT_LINE_RE = re.compile(
    r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+\s*\|\s*\S.*$"
)


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
        "أرسل بيانات الحساب على *سطر واحد*:\n"
        "`email@gmail.com | OldPassword | OLD2FASECRET`\n\n"
        "أو أرسلها على *3 رسائل متتالية* (إيميل، كلمة سر، مفتاح 2FA).\n\n"
        "🔸 إذا لا يوجد 2FA على الحساب: ضع `skip` في النهاية، "
        "والبوت سيقوم بتفعيلها تلقائياً وسيرسل لك المفتاح الجديد.\n"
        "🔸 إذا طُلب تأكيد على الجوال (Tap Yes): يكفي أن توافق من الهاتف "
        "والبوت سينتظر تلقائياً.",
        parse_mode="Markdown",
        reply_markup=cancel_menu(),
    )


async def start_bulk_flow(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_state(ctx)
    ctx.user_data["rot_bulk_waiting"] = True
    await query.message.reply_text(
        "📋 *قائمة حسابات (Bulk)*\n\n"
        "أرسل ملفاً نصياً (.txt) — أي اسم — أو رسالة فيها سطر لكل حساب:\n"
        "`email@gmail.com | OldPassword | OLD2FASECRET`\n\n"
        f"الحدّ الأقصى: *{config.MAX_BULK_ACCOUNTS}* حساب في الطلب.\n"
        "تكلفة كل حساب ناجح: *1 رصيد* (الفاشل يُسترد تلقائياً).",
        parse_mode="Markdown",
        reply_markup=cancel_menu(),
    )


# ════════════════════════════════════════════════
# Text handler
# ════════════════════════════════════════════════

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    user = update.effective_user

    if await models.is_banned(user.id):
        await msg.reply_text("🚫 حسابك محظور.")
        return

    # admin first
    from bot.handlers.admin import on_admin_text
    if await on_admin_text(update, ctx):
        return

    text = (msg.text or "").strip()

    # Bulk waiting (user pressed the bulk button)
    if ctx.user_data.get("rot_bulk_waiting"):
        ctx.user_data.pop("rot_bulk_waiting", None)
        accounts = _parse_accounts_block(text)
        await _run_accounts(msg, ctx, user, accounts)
        return

    # Single-flow state
    flow = ctx.user_data.get("rot_flow")
    if flow:
        # If they pasted a full "email | pass | totp" line at any point,
        # short-circuit to single run.
        if "|" in text and EMAIL_RE.search(text.split("|")[0]):
            parsed = _parse_account_line(text)
            _clear_state(ctx)
            if not parsed:
                await msg.reply_text(
                    "❌ صيغة غير صحيحة. مثال:\n"
                    "`email@gmail.com | OldPassword | OLD2FASECRET`",
                    parse_mode="Markdown",
                )
                return
            await _run_accounts(msg, ctx, user, [parsed])
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
                "🔐 أرسل *مفتاح 2FA السري* (Base32) أو `skip` إن لم يوجد "
                "(سيقوم البوت بتفعيله تلقائياً):",
                parse_mode="Markdown",
            )
            try: await msg.delete()
            except Exception: pass
            return

        if flow == "totp":
            totp = "" if text.lower() in ("skip", "تخطي", "-") else text
            email = ctx.user_data.get("rot_email", "")
            pwd = ctx.user_data.get("rot_password", "")
            _clear_state(ctx)
            try: await msg.delete()
            except Exception: pass
            await _run_accounts(msg, ctx, user, [(email, pwd, totp)])
            return

    # ── No active flow: AUTO-DETECT account block in any free text ──
    parsed_lines = _parse_accounts_block(text)
    if parsed_lines:
        await _run_accounts(msg, ctx, user, parsed_lines)
        return

    # Default reply
    await msg.reply_text(
        "👇 اختر من القائمة أو أرسل بيانات الحساب مباشرة:\n"
        "`email@gmail.com | OldPassword | OLD2FASECRET`",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ════════════════════════════════════════════════
# Document handler — accepts ANY .txt regardless of name
# ════════════════════════════════════════════════

async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.document:
        return
    if await models.is_banned(user.id):
        await msg.reply_text("🚫 حسابك محظور.")
        return

    doc = msg.document
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()

    # Accept any file that looks textual
    is_text = name.endswith(".txt") or "text" in mime or name.endswith(".csv")
    if not is_text:
        # If user explicitly opened bulk flow, still try; otherwise ignore.
        if not ctx.user_data.get("rot_bulk_waiting"):
            return

    if doc.file_size and doc.file_size > 500_000:
        await msg.reply_text("❌ الملف كبير جداً (الحد 500 KB).")
        return

    file = await doc.get_file()
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    text = buf.getvalue().decode("utf-8", errors="ignore")

    ctx.user_data.pop("rot_bulk_waiting", None)
    accounts = _parse_accounts_block(text)
    if not accounts:
        await msg.reply_text(
            "❌ لم أجد أي حساب صالح في الملف.\n"
            "صيغة كل سطر: `email | password | totp_secret`",
            parse_mode="Markdown",
        )
        return
    await _run_accounts(msg, ctx, user, accounts)


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
    if not password:
        return None
    totp = parts[2] if len(parts) >= 3 else ""
    if totp.lower() in ("skip", "تخطي", "-"):
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
# Unified runner: 1 or N accounts
# ════════════════════════════════════════════════

async def _run_accounts(msg, ctx, user,
                        accounts: List[Tuple[str, str, str]]) -> None:
    if not accounts:
        await msg.reply_text("❌ لم أجد أي حساب صالح.", reply_markup=back_menu())
        return
    if len(accounts) > config.MAX_BULK_ACCOUNTS:
        await msg.reply_text(
            f"❌ الحد الأقصى {config.MAX_BULK_ACCOUNTS} حساب في الطلب.",
        )
        return

    cost = config.ROTATE_COST * len(accounts)
    if not await models.deduct_credit(user.id, cost):
        await msg.reply_text(
            f"⚠️ رصيد غير كافٍ. تحتاج {cost} نقطة لمعالجة "
            f"{len(accounts)} حساب.\nاستخدم /qd أو /use.",
            reply_markup=back_menu(),
        )
        return

    if len(accounts) > 1:
        header = await msg.reply_text(
            f"📋 سيتم معالجة *{len(accounts)}* حساب…\n"
            "يُرسَل تقرير مباشر بعد كل حساب.",
            parse_mode="Markdown",
        )
    else:
        header = None

    successes: List[str] = []
    failures: List[str] = []

    for idx, (email, password, totp) in enumerate(accounts, 1):
        prefix = f"({idx}/{len(accounts)}) " if len(accounts) > 1 else ""
        sub = await msg.reply_text(f"⏳ {prefix}{email}")

        async def _prog(t, s=sub, p=prefix, e=email):
            try:
                await s.edit_text(
                    f"{p}{e}\n\n{t}", parse_mode="Markdown"
                )
            except Exception:
                pass

        rot_id = await models.log_rotation_start(user.id, email)
        try:
            result = await asyncio.wait_for(
                rotate_google_account(
                    _prog, gmail=email, old_password=password,
                    old_totp_secret=totp, user_id=user.id,
                ),
                timeout=config.ROTATE_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            result = {
                "success": False, "step": "timeout",
                "error": "تجاوز الوقت المسموح", "screenshot_path": None,
                "html_path": None, "new_password": None,
                "new_totp_secret": None, "gmail": email,
                "had_2fa_before": None,
            }
        except Exception as exc:
            log.exception("Rotation crashed")
            result = {
                "success": False, "step": "crash", "error": str(exc),
                "screenshot_path": None, "html_path": None,
                "new_password": None, "new_totp_secret": None,
                "gmail": email, "had_2fa_before": None,
            }

        await models.log_rotation_finish(
            rot_id, user.id,
            success=result.get("success", False),
            step=result.get("step"),
            new_password=result.get("new_password"),
            new_totp_secret=result.get("new_totp_secret"),
            error=result.get("error"),
        )

        if result.get("success"):
            had_2fa = result.get("had_2fa_before")
            note = ""
            if had_2fa is False:
                note = "\n🆕 <i>لم تكن المصادقة الثنائية مفعّلة — قمنا بتفعيلها.</i>"
            line = (
                f"{result['gmail']} | {result['new_password']} "
                f"| {result['new_totp_secret']}"
            )
            successes.append(line)
            try:
                await sub.edit_text(
                    f"✅ <b>{email}</b>{note}\n\n"
                    f"🔑 <code>{result['new_password']}</code>\n"
                    f"🔐 <code>{result['new_totp_secret']}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        else:
            failures.append(
                f"{email}  →  {result.get('step')}: {result.get('error')}"
            )
            await models.add_credits(user.id, config.ROTATE_COST)
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

    # Final summary (only when bulk)
    if len(accounts) > 1:
        summary = (
            "📊 *النتيجة النهائية*\n\n"
            f"المجموع: {len(accounts)}\n"
            f"✅ ناجح: {len(successes)}\n"
            f"❌ فاشل: {len(failures)}\n"
        )
        try:
            if header:
                await header.edit_text(summary, parse_mode="Markdown")
            else:
                await msg.reply_text(summary, parse_mode="Markdown")
        except Exception:
            await msg.reply_text(summary, parse_mode="Markdown")

        if successes:
            body = (
                "email | new_password | new_2fa_secret\n"
                + "\n".join(successes)
            )
            buf = io.BytesIO(body.encode("utf-8"))
            buf.name = "results.txt"
            await msg.reply_document(
                document=buf, filename="results.txt",
                caption="✅ ملف النتائج الناجحة",
            )
        if failures:
            body = "\n".join(failures)
            buf = io.BytesIO(body.encode("utf-8"))
            buf.name = "failures.txt"
            await msg.reply_document(
                document=buf, filename="failures.txt",
                caption="❌ الحسابات الفاشلة",
            )
