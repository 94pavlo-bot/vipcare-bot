import os
import sqlite3
import logging
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ChatMemberHandler, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.environ["BOT_TOKEN"]
CHANNEL_ID     = os.environ.get("CHANNEL_ID", "@vipcare_io")
ACCESS_CODE    = os.environ.get("ACCESS_CODE", "vipcare777")
DAYS_REQUIRED  = int(os.environ.get("DAYS_REQUIRED", "3"))
DB_PATH        = os.environ.get("DB_PATH", "users.db")


# ── DB ──────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                first_seen      TEXT NOT NULL,
                code_given      INTEGER DEFAULT 0
            )
        """)


def get_user(user_id: int):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            "SELECT first_seen, code_given FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()


def upsert_first_seen(user_id: int):
    """Insert if new, ignore if already exists (preserves original date)."""
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
            (user_id, now)
        )


def mark_code_given(user_id: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE users SET code_given = 1 WHERE user_id = ?",
            (user_id,)
        )


# ── HELPERS ─────────────────────────────────────────────────────────────────

async def is_channel_member(bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status not in ("left", "kicked", "banned")
    except Exception:
        return False


def days_since(iso_date: str) -> int:
    return (datetime.utcnow() - datetime.fromisoformat(iso_date)).days


# ── HANDLERS ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not await is_channel_member(context.bot, user.id):
        await update.message.reply_text(
            "👋 Привет!\n\n"
            "Чтобы получить код доступа к *VIPcare Library*, подпишись на канал:\n"
            "👉 t.me/vipcare_io\n\n"
            "После 3 дней в канале напиши /code — и я выдам тебе код.",
            parse_mode="Markdown"
        )
        return

    upsert_first_seen(user.id)
    row = get_user(user.id)
    days = days_since(row[0])
    days_left = max(0, DAYS_REQUIRED - days)

    if days_left == 0:
        await _send_code(update, user.id)
    else:
        await update.message.reply_text(
            f"✅ Ты в канале — отлично!\n\n"
            f"Код доступа к библиотеке будет готов через *{days_left} дн.*\n"
            f"Напиши /code чтобы проверить статус.",
            parse_mode="Markdown"
        )


async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not await is_channel_member(context.bot, user.id):
        await update.message.reply_text(
            "❌ Ты не подписан на канал.\n\n"
            "Подпишись: t.me/vipcare_io\n"
            "Вернись через 3 дня и напиши /code."
        )
        return

    upsert_first_seen(user.id)
    row = get_user(user.id)
    days = days_since(row[0])
    days_left = max(0, DAYS_REQUIRED - days)

    if days_left == 0:
        await _send_code(update, user.id)
    else:
        await update.message.reply_text(
            f"⏳ Осталось *{days_left} дн.* до кода.\n\n"
            f"Возвращайся позже — напиши /code.",
            parse_mode="Markdown"
        )


async def _send_code(update: Update, user_id: int):
    mark_code_given(user_id)
    await update.message.reply_text(
        "🔓 *Код доступа к VIPcare Library:*\n\n"
        f"`{ACCESS_CODE}`\n\n"
        "Вводи на [library.vipcare.io](https://library.vipcare.io)",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def track_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Записываем дату когда юзер вступил в канал."""
    result = update.chat_member
    if not result:
        return
    new_status = result.new_chat_member.status
    user_id    = result.new_chat_member.user.id
    if new_status == "member":
        upsert_first_seen(user_id)
        logger.info(f"User {user_id} joined channel — recorded.")


# ── MAIN ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("code",  cmd_code))
    app.add_handler(ChatMemberHandler(track_join, ChatMemberHandler.CHAT_MEMBER))

    logger.info("Bot started.")
    app.run_polling(allowed_updates=["message", "chat_member"])


if __name__ == "__main__":
    main()
