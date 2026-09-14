import logging
import os
import sqlite3
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import BOT_TOKEN, ADMIN_ID, SESSION_HOURS, DELETE_AFTER_HOURS, DB_PATH

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                started_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.commit()


def get_session(user_id):
    with db() as conn:
        return conn.execute(
            """SELECT user_id, username, started_at, expires_at, active
               FROM sessions WHERE user_id = ?""",
            (user_id,),
        ).fetchone()


def create_session(user_id, username):
    started = now_ts()
    expires = started + SESSION_HOURS * 3600

    with db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (user_id, username, started_at, expires_at, active)
               VALUES (?, ?, ?, ?, 1)""",
            (user_id, username or "", started, expires),
        )
        conn.commit()


def end_session(user_id):
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET active = 0 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()


def save_admin_message(message_id):
    with db() as conn:
        conn.execute(
            "INSERT INTO admin_messages (telegram_message_id, created_at) VALUES (?, ?)",
            (message_id, now_ts()),
        )
        conn.commit()


def get_old_admin_messages(cutoff):
    with db() as conn:
        return conn.execute(
            """SELECT id, telegram_message_id
               FROM admin_messages
               WHERE created_at <= ?""",
            (cutoff,),
        ).fetchall()


def delete_admin_message_record(record_id):
    with db() as conn:
        conn.execute(
            "DELETE FROM admin_messages WHERE id = ?",
            (record_id,),
        )
        conn.commit()


async def cleanup_admin_messages(context: ContextTypes.DEFAULT_TYPE):
    cutoff = now_ts() - DELETE_AFTER_HOURS * 3600
    rows = get_old_admin_messages(cutoff)

    for record_id, message_id in rows:
        try:
            await context.bot.delete_message(
                chat_id=ADMIN_ID,
                message_id=message_id,
            )
        except Exception as exc:
            logger.info(
                "Could not delete admin message %s: %s",
                message_id,
                exc,
            )

        delete_admin_message_record(record_id)


async def expire_sessions(context: ContextTypes.DEFAULT_TYPE):
    current = now_ts()

    with db() as conn:
        rows = conn.execute(
            """SELECT user_id
               FROM sessions
               WHERE active = 1 AND expires_at <= ?""",
            (current,),
        ).fetchall()

        for (user_id,) in rows:
            conn.execute(
                "UPDATE sessions SET active = 0 WHERE user_id = ?",
                (user_id,),
            )

        conn.commit()

    for (user_id,) in rows:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "⏰ Your chat session has ended.\n\n"
                    "If you want to contact the admin again, send /start."
                ),
            )
        except Exception as exc:
            logger.info(
                "Could not notify expired user %s: %s",
                user_id,
                exc,
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user or not update.effective_message:
        return

    session = get_session(user.id)
    current = now_ts()

    if session and session[4] == 1 and session[3] > current:
        await update.effective_message.reply_text(
            "⚠️ You are already in an active chat.\n\n"
            "Please send your message. The admin will contact you as soon as possible."
        )
        return

    create_session(user.id, user.username)

    await update.effective_message.reply_text(
        "🔐 Anonymous chat started.\n\n"
        "Please send your message. The admin will contact you as soon as possible."
    )


def get_user_info(user):
    username = f"@{user.username}" if user.username else "No username"

    return (
        "📩 New Anonymous Message\n\n"
        f"👤 User ID: {user.id}\n"
        f"🔹 Username: {username}\n\n"
    )


async def forward_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    # Messages sent by the admin are handled separately.
    if user.id == ADMIN_ID:
        await handle_admin_reply(update, context)
        return

    session = get_session(user.id)
    current = now_ts()

    if not session or session[4] != 1 or session[3] <= current:
        if session and session[4] == 1:
            end_session(user.id)

        await message.reply_text(
            "⏰ Your chat session has ended.\n\n"
            "If you want to contact the admin again, send /start."
        )
        return

    # First send a compact identification message.
    info_message = await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=get_user_info(user),
    )
    save_admin_message(info_message.message_id)

    # Then copy the actual Telegram message. This supports essentially all
    # normal Telegram message types without downloading/re-uploading media.
    try:
        copied_message = await message.copy(chat_id=ADMIN_ID)
        save_admin_message(copied_message.message_id)
    except Exception as exc:
        logger.exception("Failed to copy user message: %s", exc)

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Could not copy message from User ID {user.id}.",
        )

    # No admin notification is sent when the user only opens /start.
    # The user gets a small confirmation after an actual message.
    try:
        await message.reply_text("✅ Your message has been sent to the admin.")
    except Exception:
        pass


def extract_user_id(text):
    if not text:
        return None

    for line in text.splitlines():
        if line.startswith("👤 User ID:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                return None

    return None


async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message:
        return

    # Admin should use Telegram's normal Reply feature.
    if not message.reply_to_message:
        await message.reply_text(
            "↩️ Reply to the user's notification or message to send a response."
        )
        return

    replied = message.reply_to_message
    user_id = extract_user_id(replied.text)

    # If admin replies directly to the copied media/message, Telegram's
    # reply may be to the media itself. The media does not contain the ID.
    # In that case, ask admin to reply to the identification message.
    if not user_id:
        await message.reply_text(
            "⚠️ Please use Reply on the user's information message "
            "(the message containing User ID)."
        )
        return

    session = get_session(user_id)
    current = now_ts()

    if not session or session[4] != 1 or session[3] <= current:
        await message.reply_text(
            "⚠️ This user's chat session has expired."
        )
        return

    try:
        # Copy the admin's reply to the user. This supports text, photos,
        # videos, documents, stickers, voice, etc.
        await message.copy(chat_id=user_id)
    except Exception as exc:
        logger.exception(
            "Failed to send admin reply to user %s: %s",
            user_id,
            exc,
        )
        await message.reply_text(
            "❌ I couldn't send your message to this user."
        )


async def post_init(application: Application):
    init_db()

    if application.job_queue:
        application.job_queue.run_repeating(
            expire_sessions,
            interval=60,
            first=10,
        )

        application.job_queue.run_repeating(
            cleanup_admin_messages,
            interval=300,
            first=30,
        )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            forward_user_message,
        )
    )

    logger.info("WALAWWA Anonymous Admin Bot starting...")
    logger.info("Admin ID: %s", ADMIN_ID)
    logger.info("Session: %s hours", SESSION_HOURS)
    logger.info("Admin message cleanup: %s hours", DELETE_AFTER_HOURS)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
