import os
import sqlite3
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ChatMemberHandler, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.environ["BOT_TOKEN"]
CHANNEL_ID   = os.environ.get("CHANNEL_ID", "@vipcare_io")
DB_PATH      = os.environ.get("DB_PATH", "users.db")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0"))
PARTNER_CODE = os.environ.get("TIER_PARTNER", "VIPCARE-G4L8V")

RATE_LIMIT  = 5
RATE_WINDOW = 3600

# (name, days_required, env_var, emoji)
TIERS = [
    ("Bronze",   3,   "TIER_BRONZE",   "🥉"),
    ("Silver",   14,  "TIER_SILVER",   "🥈"),
    ("Gold",     30,  "TIER_GOLD",     "🥇"),
    ("Platinum", 60,  "TIER_PLATINUM", "💎"),
    ("Black",    90,  "TIER_BLACK",    "🖤"),
    ("Supreme",  180, "TIER_SUPREME",  "👑"),
]
TIER_NAMES = [t[0] for t in TIERS]


def get_code(env_var: str) -> str:
    return os.environ.get(env_var, "VIPCARE-XXXXX")


def get_current_tier(seconds: int):
    """Highest tier reached. Returns (name, days, env_var, emoji) or None if Entry."""
    result = None
    for tier in TIERS:
        if seconds >= tier[1] * 86400:
            result = tier
    return result


def get_next_tier(seconds: int):
    """Next tier to reach. Returns None if Supreme already reached."""
    for tier in TIERS:
        if seconds < tier[1] * 86400:
            return tier
    return None


def tier_index(name: str) -> int:
    try:
        return TIER_NAMES.index(name)
    except ValueError:
        return -1


# ── DB ──────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if cols and "active_seconds" not in cols:
            # Migrate from old schema
            c.execute("DROP TABLE users")
            logger.info("Old DB schema dropped — migrating.")

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id            INTEGER PRIMARY KEY,
                active_seconds     INTEGER DEFAULT 0,
                last_sub_start     TEXT,
                is_subscribed      INTEGER DEFAULT 0,
                partner            INTEGER DEFAULT 0,
                highest_sent       TEXT DEFAULT '',
                check_count        INTEGER DEFAULT 0,
                check_window_start TEXT
            )
        """)


def get_user(user_id: int):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            "SELECT active_seconds, last_sub_start, is_subscribed, partner, "
            "highest_sent, check_count, check_window_start FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()


def upsert_user(user_id: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def set_subscribed(user_id: int, subscribed: bool):
    """Resume or freeze the subscription timer."""
    now_iso = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT active_seconds, last_sub_start, is_subscribed FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not row:
            if subscribed:
                c.execute(
                    "INSERT INTO users (user_id, last_sub_start, is_subscribed) VALUES (?, ?, 1)",
                    (user_id, now_iso)
                )
            return

        active_seconds, last_sub_start, is_sub = row

        if subscribed and not is_sub:
            c.execute(
                "UPDATE users SET last_sub_start = ?, is_subscribed = 1 WHERE user_id = ?",
                (now_iso, user_id)
            )
        elif not subscribed and is_sub and last_sub_start:
            elapsed = int((datetime.utcnow() - datetime.fromisoformat(last_sub_start)).total_seconds())
            c.execute(
                "UPDATE users SET active_seconds = ?, last_sub_start = NULL, is_subscribed = 0 WHERE user_id = ?",
                (active_seconds + elapsed, user_id)
            )


def get_effective_seconds(user_id: int) -> tuple[int, bool]:
    """(total subscribed seconds, is currently subscribed)."""
    row = get_user(user_id)
    if not row:
        return 0, False
    active_seconds, last_sub_start, is_subscribed = row[0], row[1], row[2]
    if is_subscribed and last_sub_start:
        session = int((datetime.utcnow() - datetime.fromisoformat(last_sub_start)).total_seconds())
        return active_seconds + session, True
    return active_seconds, bool(is_subscribed)


def mark_highest_sent(user_id: int, tier_name: str):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("UPDATE users SET highest_sent = ? WHERE user_id = ?", (tier_name, user_id))


def set_partner(user_id: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("UPDATE users SET partner = 1 WHERE user_id = ?", (user_id,))


def record_check(user_id: int, count: int, window_start: str):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE users SET check_count = ?, check_window_start = ? WHERE user_id = ?",
            (count, window_start, user_id)
        )


def check_rate_limit(check_count: int, window_start) -> tuple[bool, int, str]:
    now = datetime.utcnow()
    now_iso = now.isoformat()
    if not window_start:
        return False, 1, now_iso
    elapsed = int((now - datetime.fromisoformat(window_start)).total_seconds())
    if elapsed >= RATE_WINDOW:
        return False, 1, now_iso
    if check_count >= RATE_LIMIT:
        return True, check_count, window_start
    return False, check_count + 1, window_start


# ── HELPERS ─────────────────────────────────────────────────────────────────

def fmt(seconds: int) -> str:
    if seconds <= 0:
        return "0ч 0м"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    if d > 0:
        return f"{d}д {h}ч"
    return f"{h}ч {m}м"


async def is_channel_member(bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status not in ("left", "kicked", "banned")
    except Exception:
        return False


async def maybe_notify_new_tier(update: Update, user_id: int, effective_seconds: int, highest_sent: str):
    """Send congratulations if user reached a new tier since last check."""
    current = get_current_tier(effective_seconds)
    if not current:
        return

    name, _, env_var, emoji = current
    if highest_sent and tier_index(name) <= tier_index(highest_sent):
        return  # Already notified

    code = get_code(env_var)
    await update.message.reply_text(
        f"🎉 Поздравляем — ты достиг <b>{emoji} {name}</b>!\n\n"
        f"Твой VIP статус: <b>{name}</b>\n"
        f"Твой код: <code>{code}</code>\n\n"
        f"Он открывает <b>{name}</b> и весь контент ниже по статусу.\n"
        f"Введи на <a href=\"https://library.vipcare.io\">library.vipcare.io</a>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    mark_highest_sent(user_id, name)


# ── HANDLERS ────────────────────────────────────────────────────────────────

WELCOME = (
    "👋 Добро пожаловать в VIPcare!\n\n"
    "Этот бот даёт доступ к <b>VIPcare Library</b> — фреймворки, модели и плейбуки "
    "из 6+ лет управления VIP программами в топовых операторах.\n\n"
    "<b>Команды:</b>\n"
    "/start — проверить статус\n"
    "/code — получить код доступа\n"
    "/partner — активировать Partner доступ"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(WELCOME, parse_mode="HTML")

    member = await is_channel_member(context.bot, user.id)
    upsert_user(user.id)
    set_subscribed(user.id, member)

    if not member:
        await update.message.reply_text(
            "Чтобы получить доступ к библиотеке, подпишись на канал:\n"
            "👉 t.me/vipcare_io\n\n"
            "После 3 дней подписки отправь /code"
        )
        return

    effective_seconds, _ = get_effective_seconds(user.id)
    row = get_user(user.id)
    highest_sent = row[4] if row else ""

    await maybe_notify_new_tier(update, user.id, effective_seconds, highest_sent)

    # Re-fetch after possible update
    row = get_user(user.id)
    highest_sent = row[4] if row else ""

    current = get_current_tier(effective_seconds)
    nxt = get_next_tier(effective_seconds)

    if not current:
        # Entry
        next_name, next_days, _, next_emoji = nxt
        left = next_days * 86400 - effective_seconds
        await update.message.reply_text(
            f"🆕 Твой VIP статус: <b>Entry</b>\n\n"
            f"Через <b>{fmt(left)}</b> ты получишь статус {next_emoji} <b>{next_name}</b> "
            f"и доступ к первой статье VIP библиотеки.",
            parse_mode="HTML"
        )
    elif nxt:
        name, _, _, emoji = current
        next_name, next_days, _, next_emoji = nxt
        left = next_days * 86400 - effective_seconds
        await update.message.reply_text(
            f"{emoji} Твой VIP статус: <b>{name}</b>\n\n"
            f"Следующий: {next_emoji} <b>{next_name}</b> через <b>{fmt(left)}</b>",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "👑 Твой VIP статус: <b>Supreme</b>\n\n"
            "Ты достиг максимального уровня. Уважение!",
            parse_mode="HTML"
        )


async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    member = await is_channel_member(context.bot, user.id)
    upsert_user(user.id)
    set_subscribed(user.id, member)

    row = get_user(user.id)
    _, _, _, partner, highest_sent, check_count, window_start = row

    if partner:
        await update.message.reply_text(
            f"🤝 Твой VIP статус: <b>Partner</b>\n\n"
            f"Твой код открывает весь контент библиотеки.\n\n"
            f"Твой код: <code>{PARTNER_CODE}</code>\n"
            f"Введи на <a href=\"https://library.vipcare.io\">library.vipcare.io</a>",
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        return

    if not member:
        effective_seconds, _ = get_effective_seconds(user.id)
        current = get_current_tier(effective_seconds)
        status_name = current[0] if current else "Entry"
        status_emoji = current[3] if current else "🆕"
        await update.message.reply_text(
            f"⏸ Твой прогресс заморожен на {status_emoji} <b>{status_name}</b>.\n"
            f"Подпишись на @vipcare_io чтобы продолжить.\n"
            f"Твои дни сохранены — ничего не потеряно.",
            parse_mode="HTML"
        )
        return

    # Rate limit
    limited, new_count, new_window = check_rate_limit(check_count, window_start)
    if limited:
        await update.message.reply_text("⏳ Превышен лимит проверок. Попробуй чуть позже.")
        return
    record_check(user.id, new_count, new_window)

    effective_seconds, _ = get_effective_seconds(user.id)
    await maybe_notify_new_tier(update, user.id, effective_seconds, highest_sent)

    # Re-fetch
    row = get_user(user.id)
    highest_sent = row[4]

    current = get_current_tier(effective_seconds)
    nxt = get_next_tier(effective_seconds)

    if not current:
        next_name, next_days, _, next_emoji = nxt
        left = next_days * 86400 - effective_seconds
        await update.message.reply_text(
            f"🆕 Твой VIP статус: <b>Entry</b>\n\n"
            f"Через <b>{fmt(left)}</b> ты получишь статус {next_emoji} <b>{next_name}</b> "
            f"и доступ к первой статье VIP библиотеки.",
            parse_mode="HTML"
        )
        return

    name, _, env_var, emoji = current
    code = get_code(env_var)

    msg = (
        f"{emoji} Твой VIP статус: <b>{name}</b>\n\n"
        f"Твой код: <code>{code}</code>\n"
        f"Он открывает <b>{name}</b> и весь контент ниже по статусу.\n"
    )
    if nxt:
        next_name, next_days, _, next_emoji = nxt
        left = next_days * 86400 - effective_seconds
        msg += f"\nСледующий: {next_emoji} <b>{next_name}</b> через <b>{fmt(left)}</b>"
    else:
        msg += "\n👑 Ты достиг максимального статуса!"

    msg += f"\n\nВведи на <a href=\"https://library.vipcare.io\">library.vipcare.io</a>"

    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)


async def cmd_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Используй: /partner КОД")
        return

    if context.args[0].upper() != PARTNER_CODE.upper():
        await update.message.reply_text("❌ Неверный код.")
        return

    upsert_user(user.id)
    set_partner(user.id)

    await update.message.reply_text(
        f"🤝 Твой VIP статус: <b>Partner</b>\n\n"
        f"Тебе доступны все фреймворки библиотеки.\n\n"
        f"Твой код: <code>{PARTNER_CODE}</code>\n"
        f"Один код открывает весь контент.\n\n"
        f"Введи на <a href=\"https://library.vipcare.io\">library.vipcare.io</a>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT user_id, active_seconds, last_sub_start, is_subscribed, partner FROM users"
        ).fetchall()

    total      = len(rows)
    subscribed = sum(1 for r in rows if r[3])
    partners   = sum(1 for r in rows if r[4])

    counts = {t[0]: 0 for t in TIERS}
    counts["Entry"] = 0

    for uid, active_sec, last_sub_start, is_sub, partner in rows:
        if partner:
            continue
        eff = active_sec
        if is_sub and last_sub_start:
            eff += int((datetime.utcnow() - datetime.fromisoformat(last_sub_start)).total_seconds())
        tier = get_current_tier(eff)
        name = tier[0] if tier else "Entry"
        counts[name] = counts.get(name, 0) + 1

    await update.message.reply_text(
        f"📊 <b>VIPcare Stats</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"📡 Подписаны: <b>{subscribed}</b> · Отписались: <b>{total - subscribed}</b>\n\n"
        f"<b>VIP сегменты:</b>\n"
        f"🆕 Entry: {counts.get('Entry', 0)}\n"
        f"🥉 Bronze: {counts.get('Bronze', 0)}\n"
        f"🥈 Silver: {counts.get('Silver', 0)}\n"
        f"🥇 Gold: {counts.get('Gold', 0)}\n"
        f"💎 Platinum: {counts.get('Platinum', 0)}\n"
        f"🖤 Black: {counts.get('Black', 0)}\n"
        f"👑 Supreme: {counts.get('Supreme', 0)}\n"
        f"🤝 Partner: {partners}",
        parse_mode="HTML"
    )


async def track_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return

    user_id    = result.new_chat_member.user.id
    new_status = result.new_chat_member.status

    upsert_user(user_id)

    if new_status == "member":
        set_subscribed(user_id, True)
        logger.info(f"User {user_id} subscribed — timer started.")
    elif new_status in ("left", "kicked", "banned"):
        set_subscribed(user_id, False)
        logger.info(f"User {user_id} unsubscribed — timer frozen.")


# ── MAIN ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("code",    cmd_code))
    app.add_handler(CommandHandler("partner", cmd_partner))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(ChatMemberHandler(track_member, ChatMemberHandler.CHAT_MEMBER))

    logger.info("VIPcare bot started.")
    app.run_polling(allowed_updates=["message", "chat_member"])


if __name__ == "__main__":
    main()
