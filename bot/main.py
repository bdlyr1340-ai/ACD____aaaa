"""Telegram bot — long polling entry point (Google Account Rotator)."""
from __future__ import annotations

import logging
import traceback

from telegram import Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from bot import config
from bot.db.connection import close_pool, init_pool
from bot.handlers import admin as h_admin
from bot.handlers import rotate as h_rotate
from bot.handlers import start as h_start

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _post_init(app: Application) -> None:
    await init_pool()
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    log.info("Bot connected: @%s", me.username)


async def _post_shutdown(app: Application) -> None:
    await close_pool()


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error: %s", ctx.error)
    log.error("%s", "".join(traceback.format_exception(None, ctx.error, ctx.error.__traceback__)))
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("حدث خطأ مؤقت، جرّب /start مرة ثانية.")
    except Exception:
        pass


def build_app() -> Application:
    config.validate()
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .concurrent_updates(True)
        .build()
    )

    # User commands
    app.add_handler(CommandHandler("start", h_start.cmd_start))
    app.add_handler(CommandHandler("ping",  h_start.cmd_ping))
    app.add_handler(CommandHandler("help",  h_start.cmd_help))
    app.add_handler(CommandHandler("me",    h_start.cmd_me))
    app.add_handler(CommandHandler("ref",   h_start.cmd_ref))
    app.add_handler(CommandHandler("qd",    h_start.cmd_qd))
    app.add_handler(CommandHandler("use",   h_start.cmd_use))

    # Admin commands
    app.add_handler(CommandHandler("admin",     h_admin.cmd_admin))
    app.add_handler(CommandHandler("stats",     h_admin.cmd_stats))
    app.add_handler(CommandHandler("addcredit", h_admin.cmd_addcredit))
    app.add_handler(CommandHandler("ban",       h_admin.cmd_ban))
    app.add_handler(CommandHandler("unban",     h_admin.cmd_unban))
    app.add_handler(CommandHandler("blacklist", h_admin.cmd_blacklist))
    app.add_handler(CommandHandler("broadcast", h_admin.cmd_broadcast))
    app.add_handler(CommandHandler("genkey",    h_admin.cmd_genkey))
    app.add_handler(CommandHandler("listkeys",  h_admin.cmd_listkeys))

    # Buttons & messages
    app.add_handler(CallbackQueryHandler(h_start.on_button))
    # Accept any uploaded document (txt or otherwise) — handler filters by content
    app.add_handler(MessageHandler(filters.Document.ALL, h_rotate.on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h_rotate.on_text))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    _setup_logging()
    app = build_app()
    log.info("Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
