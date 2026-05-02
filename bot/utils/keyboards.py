"""Keyboards — Pro v13.

Adds a sub-menu for Gmail creation:
  • 🆕 إنشاء Gmail  →  opens submenu:
        ✍️ إنشاء يدوي    (user provides First|Last|YYYY-MM-DD|m/f)
        🤖 إنشاء تلقائي  (random everything; asks for COUNT)
Keeps every previous button intact.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔐 تغيير حساب واحد", callback_data="rotate_one"),
            InlineKeyboardButton("📋 قائمة حسابات", callback_data="rotate_bulk"),
        ],
        [
            InlineKeyboardButton("🆕 إنشاء Gmail", callback_data="create_gmail"),
            InlineKeyboardButton("🔑 كلمة سر ثابتة", callback_data="custom_pwd"),
        ],
        [
            InlineKeyboardButton("👤 حسابي", callback_data="me"),
            InlineKeyboardButton("🎁 دعوة", callback_data="ref"),
            InlineKeyboardButton("📅 حضور", callback_data="checkin"),
        ],
        [
            InlineKeyboardButton("🎟 كود تفعيل", callback_data="usekey"),
            InlineKeyboardButton("📜 الصيغة", callback_data="format_help"),
            InlineKeyboardButton("📂 رفع ملف", callback_data="upload_hint"),
        ],
        [InlineKeyboardButton("ℹ️ المساعدة", callback_data="help")],
    ]
    return InlineKeyboardMarkup(rows)


def create_gmail_menu() -> InlineKeyboardMarkup:
    """Submenu: choose Manual vs Auto creation."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ إنشاء يدوي", callback_data="cg_manual")],
        [InlineKeyboardButton("🤖 إنشاء تلقائي", callback_data="cg_auto")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="back")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="back")]]
    )


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ إلغاء", callback_data="cancel")]]
    )


def custom_pwd_menu(has_password: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            "✏️ تعيين/تغيير كلمة السر", callback_data="custom_pwd_set"
        )],
    ]
    if has_password:
        rows.append([InlineKeyboardButton(
            "🗑 إلغاء كلمة السر الثابتة", callback_data="custom_pwd_clear"
        )])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="back")])
    return InlineKeyboardMarkup(rows)
