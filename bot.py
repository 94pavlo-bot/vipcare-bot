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

BOT_TOKEN     = os.environ["BOT_TOKEN"]
CHANNEL_ID    = os.environ.get("CHANNEL_ID", "@vipcare_io")
ACCESS_CODE   = os.environ.get("ACCESS_CODE", "vipcare777")
DAYS_REQUIRED = int(os.environ.get("DAYS_REQUIRED", "3"))
DB_PATH       = os.environ.get("DB_PATH", "users.db")

REQUIRED_SECONDS = DAYS_REQUIRED * 86400
RATE_LIMIT_SECONDS = 3600  # 1 hour between /code checks


# ── DB ──────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                first_seen   TEXT NOT NULL,
                last_check   TEXT,
                code_given   INTEGER DEFAULT 0
            )
        """)


def get_user(user_id: int):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            "SELECT first_seen, last_check, code_given FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()


def upsert_first_seen(user_id: int):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
            (user_id, now)
        )


def update_last_check(user_id: int):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE users SET last_check = ? WHERE user_id = ?",
            (now, user_id)
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


def seconds_since(iso_date: str) -> int:
    return int((datetime.utcnow() - datetime.fromisoformat(iso_date)).total_seconds())


def format_remaining(seconds_left: int) -> str:
    if seconds_left <= 0:
        return "0h 0m"
    h = seconds_left // 3600
    m = (seconds_left % 3600) // 60
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def rate_limited(last_check: str | None) -> tuple[bool, str]:
    """Returns (is_limited, wait_message)."""
    if not last_check:
        return False, ""
    elapsed = seconds_since(last_check)
    if elapsed < RATE_LIMIT_SECONDS:
        wait = RATE_LIMIT_SECONDS - elapsed
        return True, format_remaining(wait)
    return False, ""


# ── HANDLERS ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not await is_channel_member(context.bot, user.id):
        await update.message.reply_text(
            "👋 Hey!\n\n"
            "To get the VIPcare Library access code, subscribe to the channel first:\n"
            "👉 t.me/vipcare_io\n\n"
            f"After {DAYS_REQUIRED} days in the channel — send /code and I'll give you access."
        )
        return

    upsert_first_seen(user.id)
    row = get_user(user.id)
    elapsed = seconds_since(row[0])
    seconds_left = max(0, REQUIRED_SECONDS - elapsed)

    if seconds_left == 0:
        await _send_code(update, user.id)
    else:
        remaining = format_remaining(seconds_left)
        await update.message.reply_text(
            f"✅ You're in the channel — great!\n\n"
            f"Your access code will be ready in *{remaining}*.\n"
            f"Send /code to check your status anytime.",
            parse_mode="Markdown"
        )


async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not await is_channel_member(context.bot, user.id):
        await update.message.reply_text(
            "❌ You're not subscribed to the channel.\n\n"
            f"Subscribe here: t.me/vipcare_io\n"
            f"Come back in {DAYS_REQUIRED} days and send /code."
        )
        return

    upsert_first_seen(user.id)
    row = get_user(user.id)
    first_seen, last_check, code_given = row

    # Rate limit check
    limited, wait_time = rate_limited(last_check)
    if limited:
        await update.message.reply_text(
            f"⏳ You can check again in *{wait_time}*.\n"
            f"One status check per hour.",
            parse_mode="Markdown"
        )
        return

    update_last_check(user.id)

    elapsed = seconds_since(first_seen)
    seconds_left = max(0, REQUIRED_SECONDS - elapsed)

    if seconds_left == 0:
        await _send_code(update, user.id)
    else:
        remaining = format_remaining(seconds_left)
        await update.message.reply_text(
            f"⏳ *{remaining}* left until your access code is ready.\n\n"
            f"Stay subscribed and check back later — /code",
            parse_mode="Markdown"
        )


async def _send_code(update: Update, user_id: int):
    mark_code_given(user_id)
    await update.message.reply_text(
        "🔓 *Your VIPcare Library access code:*\n\n"
        f"`{ACCESS_CODE}`\n\n"
        "Enter it at [library.vipcare.io](https://library.vipcare.io)",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def track_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return
    if result.new_chat_member.status == "member":
        user_id = result.new_chat_member.user.id
        upsert_first_seen(user_id)
        logger.info(f"User {user_id} joined channel — recorded.")


# ── MAIN ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("code",  cmd_code))
    app.add_handler(ChatMemberHandler(track_join, ChatMemberHandler.CHAT_MEMBER))

    logger.info("VIPcare bot started.")
    app.run_polling(allowed_updates=["message", "chat_member"])


if __name__ == "__main__":
    main()
