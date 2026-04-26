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

REQUIRED_SECONDS   = DAYS_REQUIRED * 86400
RATE_LIMIT_HOUR    = 5       # max checks per hour
RATE_WINDOW        = 3600    # 1 hour in seconds

# Content list — (title, days_required)
CONTENT = [
    ("VIP Deposit Retention Framework", 3),
    ("VIP Segmentation Playbook",       999),
    ("Reactivation Flow Templates",     999),
    ("VIP Manager KPI Framework",       999),
    ("VIP Acquisition Quality Audit",   999),
    ("VIP Tech Stack Guide",            999),
]


# -- DB ----------------------------------------------------------------------

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id           INTEGER PRIMARY KEY,
                first_seen        TEXT NOT NULL,
                check_count       INTEGER DEFAULT 0,
                check_window_start TEXT,
                code_given        INTEGER DEFAULT 0
            )
        """)


def get_user(user_id: int):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            "SELECT first_seen, check_count, check_window_start, code_given FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()


def upsert_first_seen(user_id: int):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
            (user_id, now)
        )


def record_check(user_id: int, count: int, window_start: str):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE users SET check_count = ?, check_window_start = ? WHERE user_id = ?",
            (count, window_start, user_id)
        )


def mark_code_given(user_id: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("UPDATE users SET code_given = 1 WHERE user_id = ?", (user_id,))


# -- HELPERS -----------------------------------------------------------------

async def is_channel_member(bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status not in ("left", "kicked", "banned")
    except Exception:
        return False


def seconds_since(iso_date: str) -> int:
    return int((datetime.utcnow() - datetime.fromisoformat(iso_date)).total_seconds())


def format_time(seconds: int) -> str:
    if seconds <= 0:
        return "0h 0m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m" if h > 0 else f"{m}m"


def check_rate_limit(check_count: int, window_start: str | None) -> tuple[bool, int, str]:
    """Returns (is_limited, new_count, new_window_start)."""
    now = datetime.utcnow()
    now_iso = now.isoformat()

    if not window_start:
        return False, 1, now_iso

    elapsed = seconds_since(window_start)
    if elapsed >= RATE_WINDOW:
        # New window
        return False, 1, now_iso

    if check_count >= RATE_LIMIT_HOUR:
        return True, check_count, window_start

    return False, check_count + 1, window_start


def build_content_status(elapsed_seconds: int) -> str:
    lines = []
    for title, days_req in CONTENT:
        req_sec = days_req * 86400
        if days_req >= 999:
            lines.append(f"🔒 {title} - Coming Soon")
        elif elapsed_seconds >= req_sec:
            lines.append(f"✅ {title} - Available")
        else:
            left = req_sec - elapsed_seconds
            lines.append(f"⏳ {title} - {format_time(left)} left")
    return "\n".join(lines)


# -- HANDLERS ----------------------------------------------------------------

WELCOME = (
    "👋 Welcome to VIPcare!\n\n"
    "If you're here - you're serious about VIP retention in iGaming & Gaming.\n\n"
    "This bot gives you access to the *VIPcare Library* - frameworks, models and "
    "playbooks built from 6+ years running VIP programs at top operators.\n\n"
    "*Commands:*\n"
    "/start - check your subscription status\n"
    "/code - see what content is available to you\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    await update.message.reply_text(WELCOME, parse_mode="Markdown")

    if not await is_channel_member(context.bot, user.id):
        await update.message.reply_text(
            "To unlock the library, subscribe to the channel first:\n"
            "👉 t.me/vipcare_io\n\n"
            f"After {DAYS_REQUIRED} days - send /code to get access."
        )
        return

    upsert_first_seen(user.id)
    row = get_user(user.id)
    elapsed = seconds_since(row[0])
    seconds_left = max(0, REQUIRED_SECONDS - elapsed)

    if seconds_left == 0:
        await _send_code(update, user.id, elapsed)
    else:
        await update.message.reply_text(
            f"✅ You're in the channel - great!\n\n"
            f"Your access code will be ready in *{format_time(seconds_left)}*.\n"
            f"Send /code to check your content status.",
            parse_mode="Markdown"
        )


async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not await is_channel_member(context.bot, user.id):
        await update.message.reply_text(
            "❌ You're not subscribed to the channel.\n\n"
            f"Subscribe: t.me/vipcare_io\n"
            f"Come back in {DAYS_REQUIRED} days and send /code."
        )
        return

    upsert_first_seen(user.id)
    row = get_user(user.id)
    first_seen, check_count, window_start, code_given = row

    # Rate limit
    limited, new_count, new_window = check_rate_limit(check_count, window_start)
    if limited:
        await update.message.reply_text(
            f"⏳ You've used all {RATE_LIMIT_HOUR} checks for this hour.\n"
            f"Try again in a bit."
        )
        return

    record_check(user.id, new_count, new_window)

    elapsed = seconds_since(first_seen)
    days_in = elapsed // 86400
    hours_in = (elapsed % 86400) // 3600
    content_status = build_content_status(elapsed)
    checks_left = RATE_LIMIT_HOUR - new_count

    await update.message.reply_text(
        f"📊 *Your status:*\n"
        f"Time in channel: *{days_in}d {hours_in}h*\n\n"
        f"*Content access:*\n{content_status}\n\n"
        f"_Checks remaining this hour: {checks_left}/{RATE_LIMIT_HOUR}_",
        parse_mode="Markdown"
    )

    if elapsed >= REQUIRED_SECONDS and not code_given:
        await _send_code(update, user.id, elapsed)


async def _send_code(update: Update, user_id: int, elapsed: int):
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
        logger.info(f"User {user_id} joined channel - recorded.")


# -- MAIN --------------------------------------------------------------------

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
