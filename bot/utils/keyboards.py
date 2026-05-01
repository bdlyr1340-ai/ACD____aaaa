"""Inline keyboards — Pro Edition."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔐 تغيير حساب واحد", callback_data="rotate_one"),
        ],
        [
            InlineKeyboardButton("📋 قائمة حسابات (Bulk)", callback_data="rotate_bulk"),
        ],
        [
            InlineKeyboardButton("📜 تنسيق الإدخال", callback_data="format_help"),
            InlineKeyboardButton("📂 رفع ملف TXT", callback_data="upload_hint"),
        ],
        [
            InlineKeyboardButton("👤 حسابي", callback_data="me"),
            InlineKeyboardButton("📊 إحصائياتي", callback_data="my_stats"),
        ],
        [
            InlineKeyboardButton("📅 حضور يومي", callback_data="checkin"),
            InlineKeyboardButton("🔑 كود تفعيل", callback_data="usekey"),
        ],
        [
            InlineKeyboardButton("🎁 ادعُ صديقاً", callback_data="ref"),
            InlineKeyboardButton("📖 المساعدة", callback_data="help"),
        ],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="back")]
    ])


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✖️ إلغاء العملية", callback_data="cancel")]
    ])
