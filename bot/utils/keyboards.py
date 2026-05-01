"""Inline keyboards."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 تغيير حساب واحد", callback_data="rotate_one")],
        [InlineKeyboardButton("📋 قائمة حسابات (Bulk)", callback_data="rotate_bulk")],
        [
            InlineKeyboardButton("👤 حسابي", callback_data="me"),
            InlineKeyboardButton("🎁 دعوة", callback_data="ref"),
        ],
        [
            InlineKeyboardButton("📅 حضور يومي", callback_data="checkin"),
            InlineKeyboardButton("🔑 كود تفعيل", callback_data="usekey"),
        ],
        [InlineKeyboardButton("📖 المساعدة", callback_data="help")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="back")]])


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖️ إلغاء", callback_data="cancel")]])
