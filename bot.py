import logging
import sqlite3
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    BOT_TOKEN,
    ADMIN_ID,
    SESSION_HOURS,
    DELETE_AFTER_HOURS,
    DB_PATH,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# TIME
# ============================================================

def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


# ============================================================
# DATABASE
# ============================================================

def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as conn:

        # ----------------------------------------------------
        # User sessions
        # ----------------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                started_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)

        # ----------------------------------------------------
        # Admin bot-generated messages
        # ----------------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_id INTEGER NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            )
        """)

        # ----------------------------------------------------
        # Map every admin message to its user
        #
        # This allows the admin to reply to:
        # - User information message
        # - Copied text
        # - Copied photo
        # - Copied video
        # - Document
        # - Voice
        # - Sticker
        # - etc.
        # ----------------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_users (
                telegram_message_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        # ----------------------------------------------------
        # Blocked users
        # ----------------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked_at INTEGER NOT NULL
            )
        """)

        conn.commit()


# ============================================================
# SESSION FUNCTIONS
# ============================================================

def get_session(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT user_id, username, started_at, expires_at, active
            FROM sessions
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()


def create_session(user_id, username):
    started = now_ts()
    expires = started + SESSION_HOURS * 3600

    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
            (user_id, username, started_at, expires_at, active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (user_id, username or "", started, expires),
        )
        conn.commit()


def end_session(user_id):
    with db() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET active = 0
            WHERE user_id = ?
            """,
            (user_id,),
        )
        conn.commit()


# ============================================================
# BLOCK FUNCTIONS
# ============================================================

def is_blocked(user_id):
    with db() as conn:
        row = conn.execute(
            """
            SELECT user_id
            FROM blocked_users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    return row is not None


def block_user(user_id):
    with db() as conn:

        # Add to blocked list
        conn.execute(
            """
            INSERT OR REPLACE INTO blocked_users
            (user_id, blocked_at)
            VALUES (?, ?)
            """,
            (user_id, now_ts()),
        )

        # Immediately end active session
        conn.execute(
            """
            UPDATE sessions
            SET active = 0
            WHERE user_id = ?
            """,
            (user_id,),
        )

        conn.commit()


def unblock_user(user_id):
    with db() as conn:
        conn.execute(
            """
            DELETE FROM blocked_users
            WHERE user_id = ?
            """,
            (user_id,),
        )
        conn.commit()


# ============================================================
# ADMIN MESSAGE FUNCTIONS
# ============================================================

def save_admin_message(message_id, user_id=None):
    current = now_ts()

    with db() as conn:

        conn.execute(
            """
            INSERT OR IGNORE INTO admin_messages
            (telegram_message_id, created_at)
            VALUES (?, ?)
            """,
            (message_id, current),
        )

        if user_id is not None:
            conn.execute(
                """
                INSERT OR REPLACE INTO message_users
                (telegram_message_id, user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (message_id, user_id, current),
            )

        conn.commit()


def get_user_from_admin_message(message_id):
    with db() as conn:
        row = conn.execute(
            """
            SELECT user_id
            FROM message_users
            WHERE telegram_message_id = ?
            """,
            (message_id,),
        ).fetchone()

    if row:
        return row[0]

    return None


def get_old_admin_messages(cutoff):
    with db() as conn:
        return conn.execute(
            """
            SELECT id, telegram_message_id
            FROM admin_messages
            WHERE created_at <= ?
            """,
            (cutoff,),
        ).fetchall()


def delete_admin_message_record(record_id):
    with db() as conn:
        conn.execute(
            """
            DELETE FROM admin_messages
            WHERE id = ?
            """,
            (record_id,),
        )
        conn.commit()


# ============================================================
# DELETE OLD ADMIN BOT MESSAGES
# ============================================================

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

        # Also remove mapping
        with db() as conn:
            conn.execute(
                """
                DELETE FROM message_users
                WHERE telegram_message_id = ?
                """,
                (message_id,),
            )
            conn.commit()


# ============================================================
# DELETE USER'S BOT-GENERATED ADMIN MESSAGES
# ============================================================

def get_admin_messages_for_user(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT am.id, am.telegram_message_id
            FROM admin_messages am
            INNER JOIN message_users mu
                ON am.telegram_message_id = mu.telegram_message_id
            WHERE mu.user_id = ?
            """,
            (user_id,),
        ).fetchall()


async def clear_admin_messages_for_user(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    rows = get_admin_messages_for_user(user_id)

    for record_id, message_id in rows:

        try:
            await context.bot.delete_message(
                chat_id=ADMIN_ID,
                message_id=message_id,
            )

        except Exception as exc:
            logger.info(
                "Could not delete admin message %s for user %s: %s",
                message_id,
                user_id,
                exc,
            )

        delete_admin_message_record(record_id)

        with db() as conn:
            conn.execute(
                """
                DELETE FROM message_users
                WHERE telegram_message_id = ?
                """,
                (message_id,),
            )
            conn.commit()


# ============================================================
# EXPIRE SESSIONS
# ============================================================

async def expire_sessions(context: ContextTypes.DEFAULT_TYPE):
    current = now_ts()

    with db() as conn:

        rows = conn.execute(
            """
            SELECT user_id
            FROM sessions
            WHERE active = 1
            AND expires_at <= ?
            """,
            (current,),
        ).fetchall()

        for (user_id,) in rows:

            conn.execute(
                """
                UPDATE sessions
                SET active = 0
                WHERE user_id = ?
                """,
                (user_id,),
            )

        conn.commit()

    # Notify users whose sessions expired
    for (user_id,) in rows:

        # Never notify blocked users
        if is_blocked(user_id):
            continue

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "⏰ Your chat session has ended.\n\n"
                    "If you want to contact the admin again, "
                    "send /start."
                ),
            )

        except Exception as exc:
            logger.info(
                "Could not notify expired user %s: %s",
                user_id,
                exc,
            )


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    # --------------------------------------------------------
    # BLOCKED USER
    #
    # Completely silent.
    # User receives absolutely nothing.
    # --------------------------------------------------------
    if is_blocked(user.id):
        return

    session = get_session(user.id)
    current = now_ts()

    # --------------------------------------------------------
    # Existing active session
    # --------------------------------------------------------
    if (
        session
        and session[4] == 1
        and session[3] > current
    ):
        await message.reply_text(
            "⚠️ You are already in an active chat.\n\n"
            "Please send your message. "
            "The admin will contact you as soon as possible."
        )
        return

    # --------------------------------------------------------
    # Create new session
    # --------------------------------------------------------
    create_session(
        user.id,
        user.username,
    )

    await message.reply_text(
        "🔐 Anonymous chat started.\n\n"
        "Please send your message. "
        "The admin will contact you as soon as possible."
    )


# ============================================================
# USER INFORMATION
# ============================================================

def get_user_info(user):
    username = (
        f"@{user.username}"
        if user.username
        else "No username"
    )

    return (
        "📩 New Anonymous Message\n\n"
        f"👤 User ID: {user.id}\n"
        f"🔹 Username: {username}\n\n"
        "↩️ Reply to this message or the user's message "
        "to respond."
    )


# ============================================================
# GET USER ID FROM REPLY
# ============================================================

def get_reply_target_user_id(message):
    if not message.reply_to_message:
        return None

    replied = message.reply_to_message

    # --------------------------------------------------------
    # First: database mapping
    #
    # This handles copied:
    # - text
    # - photos
    # - videos
    # - documents
    # - stickers
    # - voice
    # - animations
    # - etc.
    # --------------------------------------------------------
    user_id = get_user_from_admin_message(
        replied.message_id
    )

    if user_id:
        return user_id

    # --------------------------------------------------------
    # Fallback: read User ID from information message
    # --------------------------------------------------------
    text = replied.text or replied.caption or ""

    if text:
        for line in text.splitlines():

            if line.startswith("👤 User ID:"):

                try:
                    return int(
                        line.split(":", 1)[1].strip()
                    )

                except (ValueError, IndexError):
                    return None

    return None


# ============================================================
# /BLOCK
# ============================================================

async def block_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    # Only admin can use this command
    if not user or user.id != ADMIN_ID:
        return

    if not message:
        return

    target_user_id = None

    # --------------------------------------------------------
    # Preferred:
    # Reply to user's message and send /block
    # --------------------------------------------------------
    if message.reply_to_message:
        target_user_id = get_reply_target_user_id(message)

    # --------------------------------------------------------
    # Also support:
    # /block 123456789
    # --------------------------------------------------------
    if not target_user_id and context.args:

        try:
            target_user_id = int(context.args[0])
        except ValueError:
            target_user_id = None

    if not target_user_id:

        await message.reply_text(
            "⚠️ Reply to a user's message with /block "
            "or use /block USER_ID."
        )
        return

    # Never allow accidentally blocking yourself
    if target_user_id == ADMIN_ID:

        await message.reply_text(
            "❌ You cannot block yourself."
        )
        return

    # --------------------------------------------------------
    # Block silently
    # --------------------------------------------------------
    block_user(target_user_id)

    # Clear bot-generated admin-side conversation messages
    await clear_admin_messages_for_user(
        context,
        target_user_id,
    )

    # --------------------------------------------------------
    # Confirmation only to ADMIN
    # --------------------------------------------------------
    await message.reply_text(
        f"🚫 User {target_user_id} has been blocked."
    )

    logger.info(
        "User %s blocked by admin.",
        target_user_id,
    )


# ============================================================
# /UNBLOCK
# ============================================================

async def unblock_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    # Only admin
    if not user or user.id != ADMIN_ID:
        return

    if not message:
        return

    target_user_id = None

    # --------------------------------------------------------
    # Reply method
    # --------------------------------------------------------
    if message.reply_to_message:
        target_user_id = get_reply_target_user_id(message)

    # --------------------------------------------------------
    # /unblock USER_ID
    #
    # Useful if the original blocked messages were already
    # deleted.
    # --------------------------------------------------------
    if not target_user_id and context.args:

        try:
            target_user_id = int(context.args[0])
        except ValueError:
            target_user_id = None

    if not target_user_id:

        await message.reply_text(
            "⚠️ Reply to a user's message with /unblock "
            "or use /unblock USER_ID."
        )
        return

    # --------------------------------------------------------
    # Remove block
    # --------------------------------------------------------
    unblock_user(target_user_id)

    await message.reply_text(
        f"✅ User {target_user_id} has been unblocked.\n\n"
        "They can use /start again."
    )

    logger.info(
        "User %s unblocked by admin.",
        target_user_id,
    )


# ============================================================
# /ENDCHAT
# ============================================================

async def endchat_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    # Only admin
    if not user or user.id != ADMIN_ID:
        return

    if not message:
        return

    target_user_id = None

    # --------------------------------------------------------
    # Reply method
    # --------------------------------------------------------
    if message.reply_to_message:
        target_user_id = get_reply_target_user_id(message)

    # --------------------------------------------------------
    # /endchat USER_ID
    # --------------------------------------------------------
    if not target_user_id and context.args:

        try:
            target_user_id = int(context.args[0])
        except ValueError:
            target_user_id = None

    if not target_user_id:

        await message.reply_text(
            "⚠️ Reply to a user's message with /endchat "
            "or use /endchat USER_ID."
        )
        return

    # --------------------------------------------------------
    # End session
    # --------------------------------------------------------
    end_session(target_user_id)

    # --------------------------------------------------------
    # Tell user their chat ended
    # --------------------------------------------------------
    if not is_blocked(target_user_id):

        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "⏹️ Your chat session has ended.\n\n"
                    "If you want to contact the admin again, "
                    "send /start."
                ),
            )

        except Exception as exc:
            logger.info(
                "Could not notify user %s about /endchat: %s",
                target_user_id,
                exc,
            )

    # --------------------------------------------------------
    # Clear bot-generated admin-side messages
    # --------------------------------------------------------
    await clear_admin_messages_for_user(
        context,
        target_user_id,
    )

    await message.reply_text(
        f"⏹️ Chat with User {target_user_id} has ended."
    )

    logger.info(
        "Chat with user %s ended by admin.",
        target_user_id,
    )


# ============================================================
# FORWARD USER MESSAGE
# ============================================================

async def forward_user_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------
    if user.id == ADMIN_ID:
        await handle_admin_reply(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # BLOCKED USER
    #
    # Completely silent.
    # No message.
    # No error.
    # No indication they are blocked.
    # --------------------------------------------------------
    if is_blocked(user.id):
        return

    # --------------------------------------------------------
    # Check active session
    # --------------------------------------------------------
    session = get_session(user.id)
    current = now_ts()

    if (
        not session
        or session[4] != 1
        or session[3] <= current
    ):

        if session and session[4] == 1:
            end_session(user.id)

        await message.reply_text(
            "⏰ Your chat session has ended.\n\n"
            "If you want to contact the admin again, "
            "send /start."
        )

        return

    # ========================================================
    # SEND USER INFORMATION TO ADMIN
    # ========================================================

    try:

        info_message = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=get_user_info(user),
        )

        save_admin_message(
            info_message.message_id,
            user.id,
        )

    except Exception as exc:

        logger.exception(
            "Failed to send user information: %s",
            exc,
        )

        return

    # ========================================================
    # COPY ACTUAL USER MESSAGE
    # ========================================================

    try:

        copied_message = await message.copy(
            chat_id=ADMIN_ID
        )

        # Save both message ID and user ID
        save_admin_message(
            copied_message.message_id,
            user.id,
        )

    except Exception as exc:

        logger.exception(
            "Failed to copy user message: %s",
            exc,
        )

        try:

            error_message = await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "⚠️ Could not copy message from "
                    f"User ID {user.id}."
                ),
            )

            save_admin_message(
                error_message.message_id,
                user.id,
            )

        except Exception:
            pass

    # ========================================================
    # USER CONFIRMATION
    # ========================================================

    try:

        await message.reply_text(
            "✅ Your message has been sent to the admin."
        )

    except Exception:
        pass


# ============================================================
# ADMIN NORMAL REPLY
# ============================================================

async def handle_admin_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    # --------------------------------------------------------
    # Commands are handled by CommandHandler.
    # --------------------------------------------------------
    if message.text and message.text.startswith("/"):
        return

    # --------------------------------------------------------
    # Admin must use Telegram Reply.
    # --------------------------------------------------------
    if not message.reply_to_message:

        await message.reply_text(
            "↩️ Reply to the user's notification or "
            "message to send a response."
        )

        return

    # --------------------------------------------------------
    # Find target user
    # --------------------------------------------------------
    user_id = get_reply_target_user_id(message)

    if not user_id:

        await message.reply_text(
            "⚠️ I couldn't identify the user for this "
            "message. Please reply to the user's message."
        )

        return

    # --------------------------------------------------------
    # Don't send anything to blocked users
    # --------------------------------------------------------
    if is_blocked(user_id):

        await message.reply_text(
            "🚫 This user is currently blocked."
        )

        return

    # --------------------------------------------------------
    # Check session
    # --------------------------------------------------------
    session = get_session(user_id)
    current = now_ts()

    if (
        not session
        or session[4] != 1
        or session[3] <= current
    ):

        await message.reply_text(
            "⚠️ This user's chat session has expired."
        )

        return

    # ========================================================
    # COPY ADMIN MESSAGE TO USER
    # ========================================================

    try:

        await message.copy(
            chat_id=user_id
        )

    except Exception as exc:

        logger.exception(
            "Failed to send admin reply to user %s: %s",
            user_id,
            exc,
        )

        await message.reply_text(
            "❌ I couldn't send your message to this user."
        )


# ============================================================
# POST INIT
# ============================================================

async def post_init(application: Application):
    init_db()

    if application.job_queue:

        # Check expired sessions every minute
        application.job_queue.run_repeating(
            expire_sessions,
            interval=60,
            first=10,
        )

        # Clean old admin bot messages every 5 minutes
        application.job_queue.run_repeating(
            cleanup_admin_messages,
            interval=300,
            first=30,
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ========================================================
    # USER COMMAND
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # ========================================================
    # ADMIN COMMANDS
    #
    # These must be registered BEFORE the general message
    # handler.
    # ========================================================

    application.add_handler(
        CommandHandler(
            "block",
            block_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "unblock",
            unblock_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "endchat",
            endchat_command,
        )
    )

    # ========================================================
    # ALL OTHER TELEGRAM MESSAGES
    #
    # This allows:
    # text
    # photos
    # videos
    # documents
    # audio
    # voice
    # stickers
    # GIFs
    # animations
    # contacts
    # locations
    # polls
    # etc.
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            forward_user_message,
        )
    )

    # ========================================================
    # START
    # ========================================================

    logger.info(
        "WALAWWA Anonymous Admin Bot starting..."
    )

    logger.info(
        "Admin ID: %s",
        ADMIN_ID,
    )

    logger.info(
        "Session: %s hours",
        SESSION_HOURS,
    )

    logger.info(
        "Admin message cleanup: %s hours",
        DELETE_AFTER_HOURS,
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
