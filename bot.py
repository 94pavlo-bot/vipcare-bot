import os
import logging
import psycopg2
from contextlib import contextmanager
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ChatMemberHandler,
    MessageHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


BOT_TOKEN    = os.environ["BOT_TOKEN"]
CHANNEL_ID   = os.environ.get("CHANNEL_ID", "@vipcare_io")
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0"))
PARTNER_CODE = os.environ.get("TIER_PARTNER", "VIPCARE-G4L8V")

# (name, days_required, env_var, emoji)
TIERS = [
    ("Bronze",   3,   "TIER_BRONZE",   "🥉"),
    ("Silver",   14,  "TIER_SILVER",   "🥈"),
    ("Gold",     30,  "TIER_GOLD",     "🥇"),
    ("Platinum", 60,  "TIER_PLATINUM", "🪩"),
    ("Diamond",  90,  "TIER_BLACK",    "💎"),
    ("Supreme",  180, "TIER_SUPREME",  "👑"),
]
TIER_NAMES = [t[0] for t in TIERS]

TIER_UPGRADE_MESSAGES = {
    "Bronze": (
        "🥉 You've reached <b>Bronze</b> status — welcome to the inner circle!\n\n"
        "Your first article from the VIPCare Library is now unlocked. "
        "This is just the beginning. Keep it up 🚀"
    ),
    "Silver": (
        "🥈 <b>Silver</b> status unlocked — you're ahead of the crowd.\n\n"
        "Two weeks in, and the library keeps opening up for you. "
        "The insights at this level are the real deal. Stay consistent."
    ),
    "Gold": (
        "🥇 <b>Gold</b> — you've genuinely earned this.\n\n"
        "30 days of commitment. Three premium frameworks are now yours. "
        "This is where serious VIP operators operate. You belong here."
    ),
    "Platinum": (
        "🪩 <b>Platinum</b> status achieved — that's two months strong.\n\n"
        "You're in a rare group of operators who actually invest in their craft. "
        "Four exclusive pieces of content are now unlocked. "
        "The gap between you and the average operator just got wider."
    ),
    "Diamond": (
        "💎 <b>Diamond</b>. Seriously impressive.\n\n"
        "Three months of consistent engagement puts you in the top 1% of our community. "
        "Five library articles — all yours. "
        "This is what commitment looks like."
    ),
    "Supreme": (
        "👑 <b>Supreme</b>. The highest level — and you made it.\n\n"
        "Six months. That's not luck, that's dedication. "
        "You've unlocked the entire VIPCare Library — every framework, every playbook, everything we've built.\n\n"
        "Welcome to the top. You belong here. 🏆"
    ),
}

WELCOME = (
    "👋 Добро пожаловать в VIPCare.io\n\n"
    "Этот бот даёт доступ к <b>VIPCare Library</b> — статьи, фреймворки и модели "
    "от лидеров индустрии iGaming.\n\n"
    "<b>Команды:</b>\n"
    "/start — проверить статус + получить код\n"
    "/partner — хочешь стать автором новой статьи"
)

PARTNER_MSG = (
    "✍️ <b>Стать автором VIPCare Library</b>\n\n"
    "Спасибо за интерес! Все принятые материалы публикуются с:\n"
    "✅ Указанием автора и гиперссылкой на ваш LinkedIn\n"
    "✅ Отдельным постом в нашем TG-канале\n\n"
    "Единственное условие — материал должен быть реально полезным для iGaming-ниши.\n\n"
    "📎 <b>Отправьте вашу статью (PDF или DOCX):</b>"
)

ACCEPTED_MIME = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def get_code(env_var: str) -> str:
    return os.environ.get(env_var, "VIPCARE-XXXXX")


def get_current_tier(seconds: int):
    result = None
    for tier in TIERS:
        if seconds >= tier[1] * 86400:
            result = tier
    return result


def get_next_tier(seconds: int):
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

@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'users'
            """)
            cols = [row[0] for row in cur.fetchall()]

            if cols and "active_seconds" not in cols:
                cur.execute("DROP TABLE users")
                logger.info("Old DB schema dropped — migrating.")
                cols = []

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id            BIGINT PRIMARY KEY,
                    active_seconds     INTEGER DEFAULT 0,
                    last_sub_start     TEXT,
                    is_subscribed      INTEGER DEFAULT 0,
                    partner            INTEGER DEFAULT 0,
                    highest_sent       TEXT DEFAULT '',
                    check_count        INTEGER DEFAULT 0,
                    check_window_start TEXT,
                    awaiting_article   INTEGER DEFAULT 0
                )
            """)

            if cols and "awaiting_article" not in cols:
                try:
                    cur.execute("ALTER TABLE users ADD COLUMN awaiting_article INTEGER DEFAULT 0")
                    logger.info("Migrated: added awaiting_article column.")
                except Exception:
                    pass


def get_user(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT active_seconds, last_sub_start, is_subscribed, partner, "
                "highest_sent, check_count, check_window_start, awaiting_article "
                "FROM users WHERE user_id = %s",
                (user_id,)
            )
            return cur.fetchone()


def upsert_user(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
                (user_id,)
            )


def set_subscribed(user_id: int, subscribed: bool):
    now_iso = utcnow().isoformat()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT active_seconds, last_sub_start, is_subscribed FROM users WHERE user_id = %s",
                (user_id,)
            )
            row = cur.fetchone()

            if not row:
                if subscribed:
                    cur.execute(
                        "INSERT INTO users (user_id, last_sub_start, is_subscribed) VALUES (%s, %s, 1)",
                        (user_id, now_iso)
                    )
                return

            active_seconds, last_sub_start, is_sub = row

            if subscribed and not is_sub:
                cur.execute(
                    "UPDATE users SET last_sub_start = %s, is_subscribed = 1 WHERE user_id = %s",
                    (now_iso, user_id)
                )
            elif not subscribed and is_sub and last_sub_start:
                elapsed = int((utcnow() - parse_iso(last_sub_start)).total_seconds())
                cur.execute(
                    "UPDATE users SET active_seconds = %s, last_sub_start = NULL, is_subscribed = 0 "
                    "WHERE user_id = %s",
                    (active_seconds + elapsed, user_id)
                )


def get_effective_seconds(user_id: int) -> tuple[int, bool]:
    row = get_user(user_id)
    if not row:
        return 0, False
    active_seconds, last_sub_start, is_subscribed = row[0], row[1], row[2]
    if is_subscribed and last_sub_start:
        session = int((utcnow() - parse_iso(last_sub_start)).total_seconds())
        return active_seconds + session, True
    return active_seconds, bool(is_subscribed)


def mark_highest_sent(user_id: int, tier_name: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET highest_sent = %s WHERE user_id = %s",
                (tier_name, user_id)
            )


def set_partner(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET partner = 1 WHERE user_id = %s",
                (user_id,)
            )


def set_partner_step(user_id: int, step: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET awaiting_article = %s WHERE user_id = %s",
                (step, user_id)
            )


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


async def maybe_notify_new_tier(context: ContextTypes.DEFAULT_TYPE, user_id: int,
                                 effective_seconds: int, highest_sent: str) -> bool:
    current = get_current_tier(effective_seconds)
    if not current:
        return False

    name, _, env_var, emoji = current
    if highest_sent and tier_index(name) <= tier_index(highest_sent):
        return False

    code = get_code(env_var)
    base_msg = TIER_UPGRADE_MESSAGES.get(name, f"{emoji} You've reached <b>{name}</b>!")
    full_msg = (
        f"{base_msg}\n\n"
        f"Your access code: <code>{code}</code>\n"
        f"→ <a href=\"https://library.vipcare.io\">library.vipcare.io</a>"
    )

    try:
        await context.bot.send_message(
            user_id, full_msg,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.warning(f"Could not notify user {user_id}: {e}")
        return False

    mark_highest_sent(user_id, name)
    return True


def build_status_block(effective_seconds: int, is_member: bool, partner: bool) -> str:
    if partner:
        return (
            f"🤝 Твой VIP статус: <b>Partner</b>\n\n"
            f"Твой код: <code>{PARTNER_CODE}</code>\n"
            f"→ <a href=\"https://library.vipcare.io\">library.vipcare.io</a>\n\n"
            f"Полный доступ к библиотеке — все фреймворки и плейбуки."
        )

    if not is_member:
        current = get_current_tier(effective_seconds)
        status_emoji = current[3] if current else "🆕"
        status_name  = current[0] if current else "Entry"
        return (
            f"⏸ Твой прогресс заморожен на {status_emoji} <b>{status_name}</b>.\n\n"
            f"Подпишись на @vipcare_io чтобы продолжить — твои дни сохранены."
        )

    current = get_current_tier(effective_seconds)
    nxt     = get_next_tier(effective_seconds)

    if not current:
        next_name, next_days, _, next_emoji = nxt
        left = next_days * 86400 - effective_seconds
        return (
            f"🆕 Твой VIP статус: <b>Entry</b>\n\n"
            f"До {next_emoji} <b>{next_name}</b> — <b>{fmt(left)}</b>.\n"
            f"Оставайся подписан, и мы напишем когда статус повысится 🔔"
        )

    name, _, env_var, emoji = current
    code = get_code(env_var)
    msg = (
        f"{emoji} Твой VIP статус: <b>{name}</b>\n\n"
        f"Твой код: <code>{code}</code>\n"
        f"→ <a href=\"https://library.vipcare.io\">library.vipcare.io</a>\n"
    )
    if nxt:
        next_name, next_days, _, next_emoji = nxt
        left = next_days * 86400 - effective_seconds
        msg += f"\nСледующий: {next_emoji} <b>{next_name}</b> через <b>{fmt(left)}</b>"
    else:
        msg += "\n👑 Максимальный статус достигнут. Уважение!"
    return msg


# ── HANDLERS ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id)

    await update.message.reply_text(WELCOME, parse_mode="HTML", disable_web_page_preview=True)

    member = await is_channel_member(context.bot, user.id)
    set_subscribed(user.id, member)

    effective_seconds, _ = get_effective_seconds(user.id)
    row = get_user(user.id)
    _, _, _, partner, highest_sent, _, _, _ = row

    upgraded = False
    if member and not partner:
        upgraded = await maybe_notify_new_tier(context, user.id, effective_seconds, highest_sent or "")

    if not upgraded:
        msg = build_status_block(effective_seconds, member, bool(partner))
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)


async def cmd_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id)
    set_partner_step(user.id, 1)
    await update.message.reply_text(PARTNER_MSG, parse_mode="HTML", disable_web_page_preview=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_user(user.id)
    if not row:
        return

    step = row[7]
    if step == 0:
        return

    username = f"@{user.username}" if user.username else f"(ID:{user.id})"

    if step == 1:
        if not update.message.document:
            await update.message.reply_text("📎 Пожалуйста, пришлите файл (PDF или DOCX).")
            return

        mime = update.message.document.mime_type or ""
        if mime not in ACCEPTED_MIME:
            await update.message.reply_text(
                "❌ Принимаем только PDF или DOCX.\n\nПожалуйста, пришлите файл в одном из этих форматов."
            )
            return

        try:
            await context.bot.send_document(
                ADMIN_ID,
                update.message.document.file_id,
                caption=f"📨 Новая статья\nОт: {username} (ID: {user.id})"
            )
        except Exception as e:
            logger.warning(f"Could not forward document from {user.id}: {e}")

        set_partner_step(user.id, 2)
        await update.message.reply_text("Спасибо! Теперь пришлите вашу ссылку на LinkedIn:")
        return

    if step == 2:
        if not update.message.text:
            await update.message.reply_text("🔗 Пожалуйста, пришлите ссылку на LinkedIn текстом.")
            return

        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"🔗 LinkedIn от {username} (ID: {user.id}):\n{update.message.text}"
            )
        except Exception as e:
            logger.warning(f"Could not forward LinkedIn from {user.id}: {e}")

        set_partner_step(user.id, 0)
        await update.message.reply_text(
            "🙏 Отлично! Спасибо за ваш вклад.\n\n"
            "Рассмотрим в течение нескольких дней и вернёмся с обратной связью."
        )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, active_seconds, last_sub_start, is_subscribed, partner FROM users"
            )
            rows = cur.fetchall()

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
            eff += int((utcnow() - parse_iso(last_sub_start)).total_seconds())
        tier = get_current_tier(eff)
        name = tier[0] if tier else "Entry"
        counts[name] = counts.get(name, 0) + 1

    await update.message.reply_text(
        f"📊 <b>VIPcare Stats</b>\n\n"
        f"👥 Total: <b>{total}</b> · Subscribed: <b>{subscribed}</b> · Left: <b>{total - subscribed}</b>\n\n"
        f"<b>VIP segments:</b>\n"
        f"🆕 Entry: {counts.get('Entry', 0)}\n"
        f"🥉 Bronze: {counts.get('Bronze', 0)}\n"
        f"🥈 Silver: {counts.get('Silver', 0)}\n"
        f"🥇 Gold: {counts.get('Gold', 0)}\n"
        f"🪩 Platinum: {counts.get('Platinum', 0)}\n"
        f"💎 Diamond: {counts.get('Diamond', 0)}\n"
        f"👑 Supreme: {counts.get('Supreme', 0)}\n"
        f"🤝 Partner: {partners}",
        parse_mode="HTML"
    )


async def cmd_addpartner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /addpartner USER_ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    upsert_user(target_id)
    set_partner(target_id)
    await update.message.reply_text(f"✅ Partner status granted to user {target_id}.")


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


async def job_check_tier_upgrades(context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, active_seconds, last_sub_start, is_subscribed, highest_sent "
                "FROM users WHERE is_subscribed = 1 AND partner = 0"
            )
            rows = cur.fetchall()

    for uid, active_sec, last_sub_start, is_sub, highest_sent in rows:
        eff = active_sec
        if is_sub and last_sub_start:
            eff += int((utcnow() - parse_iso(last_sub_start)).total_seconds())
        await maybe_notify_new_tier(context, uid, eff, highest_sent or "")


# ── MAIN ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("partner",    cmd_partner))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("addpartner", cmd_addpartner))
    app.add_handler(ChatMemberHandler(track_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO | (filters.TEXT & ~filters.COMMAND)),
        handle_message
    ))

    app.job_queue.run_repeating(job_check_tier_upgrades, interval=21600, first=120)

    logger.info("VIPcare bot started.")
    app.run_polling(allowed_updates=["message", "chat_member"])


if __name__ == "__main__":
    main()
